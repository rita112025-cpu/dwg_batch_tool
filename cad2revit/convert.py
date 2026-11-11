"""Convert one DWG to DXF without opening the source for writing.

The source is hashed, copied with ``copy_hashed``, and only the copy is passed
to ``convert_single.convert`` (accoreconsole ``/readonly``). The source hash is
compared again afterwards. A hash change of the temporary copy is not treated
as a change of the source; the source file itself is the only file that must
stay byte-for-byte identical.

Calling a real ``accoreconsole.exe`` is not executed by the tests. That path
stays unverified until it is run on AutoCAD.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from cad2revit.guard import ProtectionError, paths_overlap
from cad2revit.hashing import _convert_single, copy_hashed, file_sha256


def convert_dwg(source, dxf_path, accore, output_dir, *, timeout=300, convert_func=None):
    """Return a conversion record. The source DWG is never the process input.

    The converter's script log stays in the temporary copy folder and is
    removed with it. ``output_dir`` is checked only so that folder cannot
    overlap the publish directory. The DXF is written to ``dxf_path``.
    """
    source = Path(source)
    dxf_path = Path(dxf_path)
    try:
        before = file_sha256(source)
    except OSError as exc:
        return _record("CONVERSION_FAILED", exc.__class__.__name__, "", "", False)
    accore_path = Path(accore) if str(accore or "").strip() else None
    if accore_path is None or not accore_path.is_file():
        after = _hash_again(source, before)
        return _record(
            "CONVERSION_FAILED",
            "找不到 accoreconsole.exe，無法轉換 DWG",
            before,
            after,
            after == before,
        )
    tmp = Path(tempfile.mkdtemp(prefix="cad2revit-"))
    rec = {"status": "CONVERSION_FAILED", "error": ""}
    try:
        if (
            paths_overlap(tmp, source)
            or paths_overlap(tmp, source.parent)
            or paths_overlap(tmp, output_dir)
        ):
            raise ProtectionError("暫存目錄與輸入或輸出重疊")
        copy_path = tmp / ("src" + source.suffix.lower())
        digest = copy_hashed(source, copy_path)
        if digest != before or file_sha256(copy_path) != before:
            rec = {"status": "CONVERSION_FAILED", "error": "複本雜湊與原始檔不一致"}
        else:
            dxf_path.parent.mkdir(parents=True, exist_ok=True)
            func = convert_func or _convert_single().convert
            # The converter writes its script log under output_dir. Keep that
            # inside the temp folder so a log path cannot replace a drawing.
            cfg = {
                "output_dir": str(tmp.resolve()),
                "input_dir": str(tmp.resolve()),
                "timeout_seconds": int(timeout),
                "overwrite": True,
                "accoreconsole": str(accore_path),
            }
            produced = func(
                copy_path,
                dxf_path,
                str(accore_path),
                cfg=cfg,
                timeout=timeout,
                overwrite=True,
            )
            rec = dict(produced or {})
    except ProtectionError:
        raise
    except Exception as exc:
        rec = {"status": "CONVERSION_FAILED", "error": exc.__class__.__name__}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    after = _hash_again(source, before)
    status = rec.get("status") or "CONVERSION_FAILED"
    dxf_ready = dxf_path.is_file() and dxf_path.stat().st_size > 0
    if after != before:
        status = "SOURCE_MODIFIED"
        dxf_path.unlink(missing_ok=True)
    elif status == "DXF_VALID" and dxf_ready:
        status = "DXF_VALID"
    elif status == "SOURCE_MODIFIED" and dxf_ready and _dxf_readable(dxf_path):
        # convert_single hashes the file it opened. That file is the copy.
        status = "DXF_VALID"
    else:
        status = "CONVERSION_FAILED"
        dxf_path.unlink(missing_ok=True)
    error = str(rec.get("error") or "")
    if status == "SOURCE_MODIFIED":
        error = (error + " " if error else "") + "原始 DWG 雜湊已改變"
    return _record(status, error.strip(), before, after, after == before)


def _record(status, error, before, after, unchanged) -> dict:
    return {
        "status": status,
        "error": error,
        "source_sha256": before,
        "source_sha256_after": after,
        "source_unchanged": unchanged,
        "method": "accoreconsole_temp_copy",
    }


def _hash_again(path, fallback) -> str:
    try:
        return file_sha256(path)
    except OSError:
        return fallback


def _dxf_readable(path) -> bool:
    try:
        import ezdxf

        ezdxf.readfile(str(path))
    except Exception:
        return False
    return True

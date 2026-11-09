"""Convert one DWG to DXF with accoreconsole.exe (read-only on the DWG).

Usage: python convert_single.py <input.dwg> <output.dxf> [accoreconsole.exe]

Verified behaviour (AutoCAD 2027 accoreconsole):
  * args: /i <dwg> /readonly /s <script> /l en-US
  * script must be ASCII/ANSI (UTF-8 non-ASCII text is garbled), so the SAVEAS target is an
    ASCII temp name and Python renames it to the final (possibly Chinese) path afterwards.
  * SAVEAS -> DXF -> <Enter> (16 decimals) -> path ; version follows the current format (2018).
"""
import hashlib
import os
import errno
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, resolve, log_error, decode_console

import ezdxf


def sha256(p, n=1 << 20):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        while chunk := f.read(n):
            h.update(chunk)
    return h.hexdigest()


def copy_hashed(src, dest, n=1 << 20):
    """Copy ``src`` to ``dest`` and return the SHA-256 of the bytes read.

    The digest is computed from the same read that writes the copy, so callers
    do not have to scan the source twice before processing starts. A failed
    copy removes the partial destination.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    try:
        with open(src, "rb") as src_fh, open(dest, "wb") as dest_fh:
            while chunk := src_fh.read(n):
                digest.update(chunk)
                dest_fh.write(chunk)
    except OSError:
        dest.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def validate_dxf(path):
    """Return (status, detail). Never trust the AutoCAD return code alone."""
    p = Path(path)
    if not p.exists():
        return "DXF_INVALID", "file missing"
    if p.stat().st_size == 0:
        return "DXF_INVALID", "0 bytes"
    try:
        ezdxf.readfile(str(p))
    except Exception as e:  # noqa: BLE001
        return "DXF_INVALID", f"ezdxf: {type(e).__name__}: {e}"
    return "DXF_VALID", ""


def _short_path(p):
    """8.3 short path (removes spaces / non-ASCII when the volume supports it)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        n = ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024)
        return buf.value if n else str(p)
    except Exception:  # noqa: BLE001
        return str(p)


def _script_safe(p):
    s = str(p)
    return s.isascii() and " " not in s


def safe_tmpdir(logs):
    """A temp folder whose path is ASCII and has no spaces: the SAVEAS filename prompt splits on
    spaces and the script is read with the ANSI code page, so the temp target must be plain."""
    token = hashlib.sha256(str(Path(logs).resolve()).encode()).hexdigest()[:16]
    cands = [logs / "tmp", Path(tempfile.gettempdir()) / "dwg_batch_tool" / token,
             Path(os.environ.get("PUBLIC", r"C:\Users\Public")) / "dwg_batch_tool" / token]
    for c in cands:
        try:
            c.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        for form in (c, Path(_short_path(c))):
            if _script_safe(form) and os.access(form, os.W_OK):
                return form
    raise OSError("no script-safe temp folder (ASCII, no spaces) available")


def publish_dxf(source, destination):
    """Publish on the destination volume, including cross-drive temporary files."""
    try:
        os.replace(source, destination)
    except OSError as exc:
        if exc.errno != errno.EXDEV and getattr(exc, 'winerror', None) != 17:
            raise
        staging = Path(destination).with_name(Path(destination).name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            shutil.copyfile(source, staging)
            os.replace(staging, destination)
            Path(source).unlink()
        finally:
            staging.unlink(missing_ok=True)


def _terminate(proc):
    """Stop only the process we created (never other AutoCAD processes)."""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def run_accore(cmd, timeout, stop_event=None):
    """Run accoreconsole via Popen. Returns (returncode, stdout, stderr, why)
    where why is '', 'timeout' or 'stopped'."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, shell=False)
    deadline = time.time() + timeout
    why = ""
    while True:
        try:
            out, err = proc.communicate(timeout=0.5)
            return proc.returncode, out, err, why
        except subprocess.TimeoutExpired:
            pass
        if stop_event is not None and stop_event.is_set():
            why = "stopped"
        elif time.time() > deadline:
            why = "timeout"
        if why:
            _terminate(proc)
            try:
                out, err = proc.communicate(timeout=10)
            except Exception:  # noqa: BLE001
                out, err = b"", b""
            return -1, out, err, why


def convert(dwg, dxf, accore, cfg=None, timeout=300, overwrite=False, stop_event=None):
    """Returns dict(source, output, status, return_code, file_size, elapsed_seconds, error)."""
    cfg = cfg or load_config()
    dwg, dxf = Path(dwg), Path(dxf)
    logs = resolve(cfg, "output_dir") / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    rec = dict(source=str(dwg), output=str(dxf), status="", return_code=None,
               file_size=0, elapsed_seconds=0.0, error="")
    t0 = time.time()
    try:
        if not Path(accore).exists():
            raise FileNotFoundError(f"accoreconsole not found: {accore}")
        if not dwg.is_file():
            raise FileNotFoundError(f"DWG not found: {dwg}")
        if not os.access(dwg, os.R_OK):
            raise PermissionError(f"DWG not readable: {dwg}")
        if dxf.exists() and not overwrite:
            rec["status"] = "SKIPPED_EXISTS"
            return rec
        dxf.parent.mkdir(parents=True, exist_ok=True)
        if not os.access(dxf.parent, os.W_OK):
            raise PermissionError(f"no write permission: {dxf.parent}")

        before = sha256(dwg)
        tag = uuid.uuid4().hex[:10]
        tmpdir = safe_tmpdir(logs)
        tmp_dxf = tmpdir / f"{tag}.dxf"
        scr = tmpdir / f"{tag}.scr"
        # ASCII-only script; forward slashes avoid backslash escaping issues
        scr_text = "FILEDIA\n0\n_SAVEAS\nDXF\n\n{}\n_QUIT\n".format(str(tmp_dxf).replace("\\", "/"))
        scr.write_bytes(scr_text.encode("mbcs"))  # strict: fails loudly if path not ANSI-encodable

        cmd = [str(accore), "/i", str(dwg), "/readonly", "/s", str(scr), "/l", "en-US"]
        rc, out, err, why = run_accore(cmd, timeout, stop_event)
        if why == "timeout":
            rec["error"] = f"timeout after {timeout}s"
        elif why == "stopped":
            rec["status"] = "STOPPED"
            rec["error"] = "stopped by user"
        rec["return_code"] = rc
        stem = f"{dwg.stem}_{tag}"
        (logs / f"{stem}.stdout.log").write_text(decode_console(out), encoding="utf-8")
        (logs / f"{stem}.stderr.log").write_text(decode_console(err), encoding="utf-8")

        if rec["status"] == "STOPPED":
            tmp_dxf.unlink(missing_ok=True)
            scr.unlink(missing_ok=True)
            rec["elapsed_seconds"] = round(time.time() - t0, 2)
            return rec
        if not rec["error"]:
            if not tmp_dxf.exists():
                rec["error"] = "DXF not produced (see stdout log)"
            elif tmp_dxf.stat().st_size == 0:
                rec["error"] = "DXF is 0 bytes"
        if not rec["error"]:
            publish_dxf(tmp_dxf, dxf)
            status, detail = validate_dxf(dxf)
            rec["status"] = status
            rec["error"] = detail
            rec["file_size"] = dxf.stat().st_size
        else:
            rec["status"] = "DXF_INVALID"
            if tmp_dxf.exists():
                tmp_dxf.unlink()
        if sha256(dwg) != before:
            rec["status"] = "SOURCE_MODIFIED"
            rec["error"] += " source DWG hash changed!"
        scr.unlink(missing_ok=True)
    except Exception as e:  # noqa: BLE001
        rec["status"] = "ERROR"
        rec["error"] = f"{type(e).__name__}: {e}"
    rec["elapsed_seconds"] = round(time.time() - t0, 2)
    if rec["status"] not in ("DXF_VALID", "SKIPPED_EXISTS", "STOPPED"):
        log_error(cfg, f"{rec['source']} -> {rec['status']} rc={rec['return_code']} {rec['error']}")
    return rec


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(2)
    cfg = load_config()
    accore = sys.argv[3] if len(sys.argv) > 3 else cfg["accoreconsole"]
    r = convert(sys.argv[1], sys.argv[2], accore, cfg, cfg.get("timeout_seconds", 300),
                cfg.get("overwrite", False))
    print(r["status"], r)
    sys.exit(0 if r["status"] in ("DXF_VALID", "SKIPPED_EXISTS") else 1)

"""Convert many DWGs inside ONE accoreconsole process (one console window for the whole batch).

Mechanism (all verified on AutoCAD 2027 accoreconsole):
  * one generated script: for each DWG -> _OPEN "<path>" -> marker -> _SAVEAS DXF <Enter> "<tmp.dxf>" -> marker
  * script is read with the ANSI code page; quoted paths with spaces / Chinese work, the output
    target is still an ASCII temp file that Python validates and moves to the final name
  * stdout (UTF-16) is streamed; markers give per-file progress
  * a corrupt DWG makes AutoCAD show a MessageBox and stall -> detected from stdout, that file is
    failed, the process is killed (only our own PID) and restarted for the remaining files
  * a wrong document being saved (OPEN failed silently) is caught by comparing DWGNAME
  * ``workers`` > 1 splits the files over that many accoreconsole processes running at once
"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parallel
from common import resolve, log_error, decode_console
from convert_single import sha256, validate_dxf, safe_tmpdir, _terminate, publish_dxf

OPEN_RE = re.compile(r"DWGBATCH_OPEN_(\d+)\|([^|\r\n]*)\|")
DONE_RE = re.compile(r"DWGBATCH_DONE_(\d+)_")


def _new_rec(dwg, dxf):
    return dict(source=str(dwg), output=str(dxf), status="", return_code=None,
                file_size=0, elapsed_seconds=0.0, error="")


def _ansi_ok(s):
    try:
        return s.encode("mbcs").decode("mbcs") == s and '"' not in s
    except Exception:  # noqa: BLE001
        return False


class _Locked(Exception):
    pass


def _rm(p, tries=20):
    """Delete a temp file that AutoCAD may still hold open for a moment."""
    for _ in range(tries):
        try:
            Path(p).unlink(missing_ok=True)
            return
        except PermissionError:
            time.sleep(0.25)


_MSGBOX = re.compile(r"\*{10,}MessageBox\*{10,}(.*?)\*{20,}", re.S)
_BENIGN = re.compile(r"唯讀|read[- ]only|使用中", re.I)


def _has_blocking_messagebox(text):
    """A MessageBox means a stalled/corrupt DWG, except AutoCAD's own "file is read-only, open read-only?" prompt,
    which accoreconsole answers itself ("Responding: Yes") and then continues normally."""
    for m in _MSGBOX.finditer(text):
        if not _BENIGN.search(m.group(1)):
            return True
    return "MessageBox" in _MSGBOX.sub("", text)


def _decode(buf):
    data = bytes(buf)
    return data[: len(data) // 2 * 2].decode("utf-16-le", errors="replace")


class _Reader(threading.Thread):
    def __init__(self, stream):
        super().__init__(daemon=True)
        self.stream, self.buf = stream, bytearray()

    def run(self):
        try:
            while True:
                b = self.stream.read1(65536)
                if not b:
                    break
                self.buf.extend(b)
        except Exception:  # noqa: BLE001
            pass


def _run_launch(jobs, accore, cfg, timeout, stop_event, on_start, finalize, tmpdir, logs, base):
    """Run one accoreconsole for `jobs`. Returns (consumed, stopped)."""
    tag = uuid.uuid4().hex[:8]
    lines = ["FILEDIA", "0"]
    for i, j in enumerate(jobs):
        lines += ["_OPEN", f'"{j["spath"]}"',
                  f'(princ (strcat "DWGB" "ATCH_OPEN_{i}|" (getvar "DWGNAME") "|"))',
                  "_SAVEAS", "DXF", "", f'"{j["tmp"]}"',
                  f'(princ (strcat "DWGB" "ATCH_DONE_{i}_"))']
    lines.append("_QUIT")
    scr = tmpdir / f"batch_{tag}.scr"
    scr.write_bytes(("\n".join(lines) + "\n").encode("mbcs"))
    proc = subprocess.Popen([str(accore), "/s", str(scr), "/l", "en-US"], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, shell=False)
    rd, re_ = _Reader(proc.stdout), _Reader(proc.stderr)
    rd.start()
    re_.start()
    consumed, opened, opened_at = 0, {}, {}
    last_progress = time.time()
    fail_msg, stopped = "", False
    scan_from = 0

    try:
        while consumed < len(jobs):
            time.sleep(0.2)
            text = _decode(rd.buf)
            for m in OPEN_RE.finditer(text):
                i = int(m.group(1))
                if i not in opened:
                    opened[i] = m.group(2)
                    opened_at[i] = time.time()
                    jobs[i]["sha_before"] = jobs[i].get("sha_before") or sha256(jobs[i]["dwg"])
                    last_progress = time.time()
            for m in DONE_RE.finditer(text):
                i = int(m.group(1))
                if i == consumed:
                    finalize(jobs[i], opened.get(i, ""), time.time() - opened_at.get(i, last_progress))
                    consumed += 1
                    last_progress = time.time()
                    scan_from = m.end()
                    if consumed < len(jobs):
                        on_start(jobs[consumed])
            if consumed >= len(jobs):
                break
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            if _has_blocking_messagebox(text[scan_from:]):
                fail_msg = "AutoCAD reported an error opening this DWG (invalid/corrupt file?)"
                break
            if proc.poll() is not None:
                time.sleep(0.3)
                if len(_decode(rd.buf)) == len(text):
                    fail_msg = f"accoreconsole exited early (rc={proc.returncode})"
                    break
            elif time.time() - last_progress > timeout:
                fail_msg = f"timeout after {timeout}s"
                break

        if consumed >= len(jobs) and not stopped:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                _terminate(proc)
        else:
            _terminate(proc)
        rd.join(timeout=5)
        re_.join(timeout=5)
        (logs / f"batch_{tag}.stdout.log").write_text(decode_console(bytes(rd.buf)), encoding="utf-8")
        (logs / f"batch_{tag}.stderr.log").write_text(decode_console(bytes(re_.buf)), encoding="utf-8")
        scr.unlink(missing_ok=True)
        return consumed, stopped, fail_msg, proc.returncode
    finally:
        if proc.poll() is None:
            _terminate(proc)


def convert_many(items, accore, cfg, timeout=300, overwrite=False, stop_event=None,
                 on_start=None, on_result=None, workers=1):
    """items: list of (dwg, dxf). on_start(index, total, dwg_path); on_result(rec).
    Returns list of records; the last one has status STOPPED if the user stopped.
    With ``workers`` > 1, on_start / on_result are still called one at a time."""
    on_start = on_start or (lambda i, n, p: None)
    on_result = on_result or (lambda r: None)
    logs = resolve(cfg, "output_dir") / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    results = []
    total = len(items)
    jobs = []
    tmpdir = safe_tmpdir(logs)
    copies = []
    emit_lock = threading.Lock()

    def emit(rec):
        with emit_lock:
            _emit(rec)

    def _emit(rec):
        if rec["status"] not in ("DXF_VALID", "SKIPPED_EXISTS", "STOPPED"):
            log_error(cfg, f"{rec['source']} -> {rec['status']} rc={rec['return_code']} {rec['error']}")
        results.append(rec)
        on_result(rec)

    # ---- pre-checks (per file, no AutoCAD) ----
    for idx, (dwg, dxf) in enumerate(items):
        dwg, dxf = Path(dwg), Path(dxf)
        rec = _new_rec(dwg, dxf)
        try:
            if not dwg.is_file():
                raise FileNotFoundError(f"DWG not found: {dwg}")
            if not os.access(dwg, os.R_OK):
                raise PermissionError(f"DWG not readable: {dwg}")
            if dxf.exists() and not overwrite:
                rec["status"] = "SKIPPED_EXISTS"
                jobs.append(dict(rec=rec, skip=True, index=idx))
                continue
            dxf.parent.mkdir(parents=True, exist_ok=True)
            if not os.access(dxf.parent, os.W_OK):
                raise PermissionError(f"no write permission: {dxf.parent}")
            spath = str(dwg).replace("\\", "/")
            if not _ansi_ok(spath):  # not representable in the ANSI script: use a read-only copy
                cp = tmpdir / f"in_{uuid.uuid4().hex[:8]}.dwg"
                shutil.copyfile(dwg, cp)
                copies.append(cp)
                spath = str(cp).replace("\\", "/")
            tmp = tmpdir / f"{uuid.uuid4().hex[:10]}.dxf"
            jobs.append(dict(rec=rec, skip=False, dwg=dwg, dxf=dxf, spath=spath,
                             tmp=str(tmp).replace("\\", "/"), tmp_path=tmp, index=idx))
        except Exception as e:  # noqa: BLE001
            rec["status"], rec["error"] = "ERROR", f"{type(e).__name__}: {e}"
            jobs.append(dict(rec=rec, skip=True, index=idx))

    def finalize(job, opened_name, elapsed, deferred, retries=2):
        try:
            _finalize(job, opened_name, elapsed, retries)
        except _Locked:
            if retries < 40:
                deferred.append((job, opened_name, elapsed))
            else:
                job["rec"].update(status="ERROR", error="cannot move temp DXF (file locked)")
                emit(job["rec"])
        except Exception as e:  # noqa: BLE001  (never let one file crash the batch)
            rec = job["rec"]
            rec.update(status="ERROR", error=f"{type(e).__name__}: {e}")
            emit(rec)

    def _finalize(job, opened_name, elapsed, retries):
        rec = job["rec"]
        rec["elapsed_seconds"] = round(elapsed, 2)
        rec["return_code"] = 0
        tmp = job["tmp_path"]
        if opened_name and opened_name.casefold() != Path(job["spath"]).name.casefold():
            rec["status"], rec["error"] = "DXF_INVALID", f"wrong drawing opened ({opened_name})"
            _rm(tmp)
        elif not tmp.exists() or tmp.stat().st_size == 0:
            rec["status"], rec["error"] = "DXF_INVALID", "DXF not produced or 0 bytes"
            _rm(tmp)
        else:
            for _ in range(retries):
                try:
                    publish_dxf(tmp, job["dxf"])
                    break
                except PermissionError:
                    time.sleep(0.25)
            else:
                # AutoCAD keeps the last saved DXF open until the next document/exit
                raise _Locked()
            rec["status"], rec["error"] = validate_dxf(job["dxf"])
            rec["file_size"] = job["dxf"].stat().st_size
        if job.get("sha_before") and sha256(job["dwg"]) != job["sha_before"]:
            rec["status"], rec["error"] = "SOURCE_MODIFIED", rec["error"] + " source DWG hash changed!"
        emit(rec)

    def _run_group(group):
        """Convert ``group`` in one accoreconsole (relaunched after a stall). Returns the
        job the user stopped at, or None."""
        deferred = []
        pos = 0
        while pos < len(group):
            if stop_event is not None and stop_event.is_set():
                return group[pos]
            chunk = group[pos:]
            on_start(chunk[0]["index"] + 1, total, str(chunk[0]["dwg"]))
            def _on_start(job):
                on_start(job["index"] + 1, total, str(job["dwg"]))
            consumed, stopped, fail_msg, rc = _run_launch(
                chunk, accore, cfg, timeout, stop_event, _on_start,
                lambda job, name, elapsed: finalize(job, name, elapsed, deferred),
                tmpdir, logs, None)
            pos += consumed
            pending_moves = list(deferred)  # process is gone now, temp files are released
            deferred.clear()
            for d in pending_moves:
                finalize(*d, deferred, retries=40)
            if stopped:
                return group[pos] if pos < len(group) else None
            if consumed < len(chunk) and fail_msg:
                bad = chunk[consumed]
                rec = bad["rec"]
                rec.update(status="DXF_INVALID", return_code=rc, error=fail_msg)
                _rm(bad["tmp_path"])
                emit(rec)
                pos += 1
        return None

    try:
        todo = [j for j in jobs if not j["skip"]]
        stopped_at = [job for job in parallel.run_groups(parallel.split(todo, workers), _run_group) if job]
        if stopped_at:
            first = min(stopped_at, key=lambda job: job["index"])
            results.append(dict(_new_rec(first["dwg"], first["dxf"]), status="STOPPED",
                                error="stopped by user"))
            return _finish(results, copies)
        for j in jobs:
            if j["skip"]:
                emit(j["rec"])
    finally:
        _finish(results, copies)
    return results


def _finish(results, copies):
    for c in copies:
        c.unlink(missing_ok=True)
    copies.clear()
    return results

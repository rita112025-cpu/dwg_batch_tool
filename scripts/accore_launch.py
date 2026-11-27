"""Run one accoreconsole process over a chunk of batch jobs and follow its console markers.

Shared by fontfix_batch, layerbatch and textbatch. The script prints
``OPEN_<i>|<name>|`` when file ``i`` opens and ``DONE_<i>_`` when it is saved; the
launcher matches those markers in stdout, in order, and stops on a blocking
MessageBox, an early exit, a stall longer than ``timeout`` or ``stop_event``.
"""
from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fontfix  # noqa: E402
from common import decode_console  # noqa: E402
from convert_batch import (  # noqa: E402
    DONE_RE,
    OPEN_RE,
    _Reader,
    _decode,
    _has_blocking_messagebox,
    blocking_failure_message,
)
from convert_single import _terminate  # noqa: E402


def run_launch(jobs, accore, lisp_path, script_text, timeout, stop_event, on_file_start, logs, prefix):
    """Run one accoreconsole for ``jobs``. Returns ``(consumed, stopped, fail_msg, stdout)``.

    ``prefix`` names the temp script (``<prefix>_<tag>.scr`` next to ``lisp_path``) and the
    console logs (``<prefix>_batch_<tag>.stdout.log`` / ``.stderr.log`` in ``logs``).
    """
    tag = uuid.uuid4().hex[:8]
    scr = lisp_path.parent / f"{prefix}_{tag}.scr"
    scr.write_bytes(fontfix.script_bytes(script_text))
    # A .py path is the unit-test stand-in (tests/fake_accoreconsole.py). The real
    # tool always points this at accoreconsole.exe.
    command = [str(accore), "/s", str(scr), "/l", "en-US"]
    if str(accore).lower().endswith(".py"):
        command.insert(0, sys.executable)
    proc = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        shell=False,
    )
    reader, err_reader = _Reader(proc.stdout), _Reader(proc.stderr)
    reader.start()
    err_reader.start()
    consumed, opened, opened_at = 0, {}, {}
    last_progress = time.time()
    fail_msg, stopped = "", False
    scan_from = 0
    seen_chars = 0
    try:
        while consumed < len(jobs):
            time.sleep(0.2)
            text = _decode(reader.buf)
            if len(text) != seen_chars:
                # A large DWG keeps printing (block-index updates, enablers) for the
                # whole QSAVE, and only then prints DONE. Silence is a stall.
                last_progress = time.time()
                seen_chars = len(text)
            for match in OPEN_RE.finditer(text):
                index = int(match.group(1))
                if index not in opened and index < len(jobs):
                    opened[index] = match.group(2)
                    opened_at[index] = time.time()
                    last_progress = time.time()
                    on_file_start(jobs[index])
            for match in DONE_RE.finditer(text):
                index = int(match.group(1))
                if index == consumed and index < len(jobs):
                    jobs[index]["opened_name"] = opened.get(index, "")
                    jobs[index]["console"] = text[scan_from:match.end()]
                    jobs[index]["elapsed"] = time.time() - opened_at.get(index, last_progress)
                    consumed += 1
                    last_progress = time.time()
                    scan_from = match.end()
            if consumed >= len(jobs):
                break
            if stop_event is not None and stop_event.is_set():
                stopped = True
                break
            if _has_blocking_messagebox(text[scan_from:]):
                fail_msg = blocking_failure_message(text[scan_from:])
                break
            if proc.poll() is not None:
                time.sleep(0.3)
                if len(_decode(reader.buf)) == len(text):
                    fail_msg = f"accoreconsole 提前結束（rc={proc.returncode}）"
                    break
            elif time.time() - last_progress > timeout:
                fail_msg = f"超過 {timeout} 秒沒有進度"
                break
        if consumed >= len(jobs) and not stopped:
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                _terminate(proc)
        else:
            _terminate(proc)
        reader.join(timeout=5)
        err_reader.join(timeout=5)
        stdout = decode_console(bytes(reader.buf))
        (logs / f"{prefix}_batch_{tag}.stdout.log").write_text(stdout, encoding="utf-8")
        (logs / f"{prefix}_batch_{tag}.stderr.log").write_text(
            decode_console(bytes(err_reader.buf)), encoding="utf-8"
        )
        scr.unlink(missing_ok=True)
        return consumed, stopped, fail_msg, stdout
    finally:
        if proc.poll() is None:
            _terminate(proc)

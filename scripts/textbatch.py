"""Batch text extraction and replacement through accoreconsole.

Original drawings are never opened for write. Each file is copied to an ASCII
temp name first, including when AutoCAD already has it open. Extraction only
reads that copy. Replacement QSAVEs the copy and Python moves it to the output
folder.
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dwgtext  # noqa: E402
import fontfix  # noqa: E402
import parallel  # noqa: E402
from common import decode_console, log_error  # noqa: E402
from convert_batch import (  # noqa: E402
    DONE_RE,
    OPEN_RE,
    _Reader,
    _decode,
    _has_blocking_messagebox,
)
from convert_single import _terminate, publish_dxf, safe_tmpdir, sha256  # noqa: E402


def _stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _copy_bytes(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as src_fh, open(dest, "wb") as dest_fh:
        shutil.copyfileobj(src_fh, dest_fh, length=1024 * 1024)


def _log_line(handle, message):
    handle.write(f"{_stamp()}  {message}\n")
    handle.flush()


def _empty_summary():
    return {
        "dwg": 0,
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "warnings": 0,
        "hits": 0,
        "changes": 0,
        "unchanged": 0,
        "stopped": False,
        "fatal": "",
        "failed_files": [],
        "elapsed": 0.0,
        "results": [],
    }


def _run_launch(jobs, accore, lisp_path, script_text, timeout, stop_event, on_file_start, logs, prefix):
    """Run one accoreconsole. Returns ``(consumed, stopped, fail_msg, stdout)``."""
    tag = uuid.uuid4().hex[:8]
    scr = lisp_path.parent / f"{prefix}_{tag}.scr"
    scr.write_bytes(fontfix.script_bytes(script_text))
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
                fail_msg = "AutoCAD 顯示錯誤對話框後停止（圖檔可能損壞或無法開啟）"
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


def _launch_all(jobs, accore, lisp_path, script_of, timeout, stop_event, logs, prefix, on_file_start,
                workers=1):
    """Run ``jobs``, restarting after a stalled file. Returns True when stopped.

    ``workers`` > 1 splits the jobs over that many accoreconsole processes.
    """
    groups = parallel.split(jobs, workers)
    if len(groups) > 1:
        return any(parallel.run_groups(groups, lambda group: _launch_all(
            group, accore, lisp_path, script_of, timeout, stop_event, logs, prefix, on_file_start,
        )))
    pos = 0
    stopped = False
    while pos < len(jobs):
        if stop_event is not None and stop_event.is_set():
            stopped = True
            if pos < len(jobs):
                jobs[pos]["stopped_here"] = True
            break
        chunk = jobs[pos:]
        for offset, job in enumerate(chunk):
            job["chunk_index"] = offset
        consumed, halt, fail_msg, stdout = _run_launch(
            chunk, accore, lisp_path, script_of(chunk), timeout, stop_event, on_file_start, logs, prefix
        )
        for job in chunk[:consumed]:
            job["stdout_ready"] = stdout
        pos += consumed
        if halt:
            stopped = True
            if pos < len(jobs):
                jobs[pos]["stopped_here"] = True
            break
        if consumed < len(chunk) and fail_msg:
            chunk[consumed]["launch_error"] = fail_msg
            pos += 1
    return stopped


def _blank_rec(src, dest):
    return {
        "file": str(src),
        "output": "" if dest is None else str(dest),
        "status": "",
        "hits": 0,
        "changes": 0,
        "error": "",
        "elapsed_seconds": 0.0,
        "hit_rows": [],
        "change_rows": [],
    }


def _prepare(input_dir, output_dir, logs, recursive, overwrite, track_output):
    """Copy every DWG to a temp file. Return ``(summary, jobs, temps)``."""
    summary = _empty_summary()
    problem = fontfix.directory_problem(input_dir, output_dir)
    if problem:
        summary["fatal"] = problem
        return summary, [], []
    files = fontfix.iter_dwgs(input_dir, recursive)
    summary["dwg"] = len(files)
    src_root = Path(input_dir)
    dst_root = Path(output_dir)
    jobs = []
    for src in files:
        dest = fontfix.output_for(src, src_root, dst_root, recursive) if track_output else None
        rec = _blank_rec(src, dest)
        job = {"rec": rec, "src": src, "dest": dest, "skip": False}
        if track_output and dest is not None and dest.exists() and not overwrite:
            rec["status"] = "SKIPPED"
            job["skip"] = True
        else:
            try:
                job["sha_before"] = sha256(src)
            except OSError as exc:
                rec["status"] = "FAILED"
                rec["error"] = f"無法讀取來源（檔案可能被獨占鎖定）：{exc}"
                job["skip"] = True
        jobs.append(job)
    temps = []
    todo = [job for job in jobs if not job["skip"]]
    if todo:
        tmpdir = safe_tmpdir(logs)
        for job in todo:
            job["token"] = uuid.uuid4().hex[:10]
            tmp_in = tmpdir / f"in_{job['token']}.dwg"
            try:
                _copy_bytes(job["src"], tmp_in)
                try:
                    os.chmod(tmp_in, stat.S_IWRITE | stat.S_IREAD)
                except OSError:
                    pass
            except OSError as exc:
                job["rec"]["status"] = "FAILED"
                job["rec"]["error"] = f"無法讀取來源（檔案可能被獨占鎖定）：{exc}"
                job["skip"] = True
                continue
            temps.append(tmp_in)
            job["tmp_in"] = tmp_in
            job["spath"] = str(tmp_in).replace("\\", "/")
            job["produced"] = tmp_in
    return summary, jobs, temps


def _check_source(job):
    rec = job["rec"]
    try:
        if sha256(job["src"]) != job.get("sha_before"):
            rec["status"] = "FAILED"
            rec["error"] = ((rec.get("error") or "") + " 來源 DWG 的內容在處理中被改變").strip()
    except OSError as exc:
        rec["error"] = ((rec.get("error") or "") + f" 無法再讀取來源：{exc}").strip()


def _open_logs(logs, stem, fields):
    logs.mkdir(parents=True, exist_ok=True)
    text_log = (logs / f"{stem}.log").open("w", encoding="utf-8")
    csv_file = (logs / f"{stem}_results.csv").open("w", newline="", encoding="utf-8-sig")
    jsonl_file = (logs / f"{stem}_results.jsonl").open("w", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fields)
    writer.writeheader()
    csv_file.flush()
    return text_log, csv_file, jsonl_file, writer


def _detail_writer(logs, name, fields):
    handle = (logs / name).open("w", newline="", encoding="utf-8-sig")
    writer = csv.DictWriter(handle, fields)
    writer.writeheader()
    handle.flush()
    return handle, writer


def _accept_dump(job, ready):
    rec = job["rec"]
    rec["elapsed_seconds"] = round(job.get("elapsed") or 0.0, 2)
    if job.get("launch_error"):
        rec["status"] = "FAILED"
        rec["error"] = job["launch_error"]
        return False
    if job.get("stopped_here"):
        rec["status"] = "STOPPED"
        rec["error"] = "已停止"
        return False
    if not job.get("console") and not ready:
        rec["status"] = "FAILED"
        rec["error"] = "沒有收到文字清單"
        return False
    opened = job.get("opened_name") or ""
    expected = Path(job["spath"]).name
    if opened and opened.casefold() != expected.casefold():
        rec["status"] = "FAILED"
        rec["error"] = f"開啟的圖面與預期不同（{opened}）"
        return False
    if not ready:
        rec["status"] = "FAILED"
        rec["error"] = "文字 LISP 沒有載入（見 logs 裡的 stdout）"
        return False
    parsed = dwgtext.parse_dump(job.get("console") or "")
    if parsed["summary"] is None:
        rec["status"] = "FAILED"
        rec["error"] = "；".join(parsed["errors"]) or "沒有收到文字清單"
        return False
    if parsed["errors"]:
        rec["error"] = "；".join(parsed["errors"])
    job["rows"] = parsed["rows"]
    return True


def _plan_changes(rows, find, repl, mode, case_sensitive, color):
    changes = []
    notes = []
    for row in rows:
        plain = dwgtext.plain_of(row)
        try:
            new_plain, count = dwgtext.apply_replacement(plain, find, repl, mode, case_sensitive)
        except re.error as exc:
            notes.append(f"{row.get('handle') or '?'}：{exc}")
            continue
        if count <= 0 or new_plain == plain:
            continue
        if row.get("formatted"):
            new_raw, lost = dwgtext.splice_mtext(row.get("raw") or "", new_plain)
            if lost:
                notes.append(f"{row.get('handle') or '?'}：MTEXT 格式碼無法完整保留，已改寫純文字")
        else:
            new_raw = new_plain
        changes.append({
            "layout": row.get("layout", ""),
            "layer": row.get("layer", ""),
            "kind": row.get("kind", ""),
            "block": row.get("block", ""),
            "handle": row.get("handle", ""),
            "old": plain,
            "new": new_plain,
            "new_raw": new_raw,
            "count": count,
            "color": color,
        })
    return changes, notes


def _publish(job):
    produced = job["produced"]
    if not produced.exists() or produced.stat().st_size < 6 or not fontfix.is_dwg_file(produced):
        time.sleep(0.3)
    if not produced.exists() or produced.stat().st_size < 6:
        return False, "沒有產生 DWG"
    if not fontfix.is_dwg_file(produced):
        return False, "輸出不是 DWG（檔頭不是 ACxxxx）"
    job["dest"].parent.mkdir(parents=True, exist_ok=True)
    last = "輸出檔案仍被 AutoCAD 鎖定"
    for _ in range(40):
        try:
            publish_dxf(produced, job["dest"])
            break
        except PermissionError:
            time.sleep(0.25)
    else:
        return False, last
    if not fontfix.is_dwg_file(job["dest"]):
        return False, "搬移後的檔案不是 DWG"
    return True, ""


def _copy_unchanged(job):
    job["dest"].parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(job["tmp_in"], job["dest"])
    if not fontfix.is_dwg_file(job["dest"]):
        return False, "複製後的檔案不是 DWG"
    return True, ""


def run_extract(
    input_dir,
    output_dir,
    accore,
    *,
    pattern,
    mode="contains",
    case_sensitive=False,
    include_blocks=False,
    recursive=True,
    timeout=300,
    workers=1,
    stop_event=None,
    progress=None,
    log=None,
):
    """Read text from every DWG. No drawing is written to ``output_dir``."""
    log = log or (lambda level, message: None)
    progress = progress or (lambda index, total, path: None)
    started = time.time()
    problem = dwgtext.validate_rule(pattern, mode)
    summary = _empty_summary()
    if problem:
        summary["fatal"] = problem
        return summary
    if not str(accore or "").strip() or not Path(accore).is_file():
        summary["fatal"] = "找不到 accoreconsole.exe"
        return summary
    dst_root = Path(output_dir)
    logs = dst_root / "logs"
    try:
        text_log, csv_file, jsonl_file, writer = _open_logs(logs, "textextract", dwgtext.EXTRACT_FIELDS)
        hits_file, hits_writer = _detail_writer(logs, "textextract_hits.csv", dwgtext.HIT_FIELDS)
    except OSError as exc:
        summary["fatal"] = f"輸出資料夾無法寫入：{exc}"
        return summary
    summary, jobs, temps = _prepare(input_dir, output_dir, logs, recursive, False, False)
    if summary["fatal"]:
        _log_line(text_log, f"批次結束（{summary['fatal']}）")
        text_log.close()
        csv_file.close()
        jsonl_file.close()
        hits_file.close()
        return summary
    _log_line(text_log, f"文字擷取開始: {input_dir}，共 {summary['dwg']} 個檔案")
    done = 0

    def emit(job):
        nonlocal done
        rec = job["rec"]
        _check_source(job)
        done += 1
        progress(done, summary["dwg"], rec["file"])
        summary["results"].append(rec)
        summary["hits"] += int(rec.get("hits") or 0)
        writer.writerow(dwgtext.extract_row(rec))
        csv_file.flush()
        jsonl_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
        jsonl_file.flush()
        name = Path(rec["file"]).name
        status = rec["status"]
        if status == "FAILED":
            summary["failed"] += 1
            summary["failed_files"].append(f"{rec['file']}: {rec.get('error', '')}".strip())
            _log_line(text_log, f"失敗 {name}: {rec.get('error', '')}")
            log("ERROR", f"失敗 {name}: {rec.get('error', '')}")
            log_error({"output_dir": str(dst_root)}, f"{rec['file']} -> {rec.get('error', '')}")
        elif status == "STOPPED":
            summary["stopped"] = True
            _log_line(text_log, f"已停止於 {name}")
            log("WARN", f"已停止於 {name}")
        else:
            summary["ok"] += 1
            label = dwgtext.EXTRACT_STATUS.get(status, status)
            _log_line(text_log, f"{label} {name}（{rec.get('hits', 0)} 筆）")
            log("OK", f"{label} {name}（{rec.get('hits', 0)} 筆）")

    try:
        todo = [job for job in jobs if not job["skip"]]
        lisp_path = None
        if todo:
            lisp_path = todo[0]["tmp_in"].parent / "textdump.lsp"
            lisp_path.write_bytes(fontfix.lisp_bytes(dwgtext.build_dump_lisp(include_blocks)))
            stopped = _launch_all(
                todo, accore, lisp_path,
                lambda chunk: dwgtext.build_dump_script(chunk, lisp_path),
                timeout, stop_event, logs, "textextract",
                lambda job: progress(min(summary["dwg"], done + 1), summary["dwg"], str(job["src"])),
                workers=workers,
            )
            ready = any("TEXTDUMP_READY" in (job.get("stdout_ready") or "") for job in todo)
            for job in todo:
                if job["rec"]["status"]:
                    continue
                if job.get("stopped_here"):
                    job["rec"]["status"] = "STOPPED"
                    job["rec"]["error"] = "已停止"
                elif _accept_dump(job, ready or ("TEXTDUMP_READY" in (job.get("stdout_ready") or ""))):
                    hits = []
                    for row in job.get("rows") or []:
                        plain = dwgtext.plain_of(row)
                        for piece in dwgtext.find_matches(plain, pattern, mode, case_sensitive):
                            hits.append({
                                "file": str(job["src"]),
                                "layout": row.get("layout", ""),
                                "layer": row.get("layer", ""),
                                "kind": row.get("kind", ""),
                                "block": row.get("block", ""),
                                "plain": plain,
                                "match": piece,
                                "x": row.get("x", ""),
                                "y": row.get("y", ""),
                                "handle": row.get("handle", ""),
                            })
                    job["rec"]["hits"] = len(hits)
                    job["rec"]["hit_rows"] = hits
                    job["rec"]["status"] = "DONE" if hits else "EMPTY"
                    for hit in hits:
                        hits_writer.writerow(dwgtext.hit_row(hit))
                    hits_file.flush()
            if stopped:
                summary["stopped"] = True
        for job in jobs:
            if job["rec"]["status"] == "SKIPPED":
                continue
            if not job["rec"]["status"]:
                if summary.get("stopped") or job.get("stopped_here"):
                    job["rec"]["status"] = "STOPPED"
                    job["rec"]["error"] = "已停止"
                else:
                    job["rec"]["status"] = "FAILED"
                    job["rec"]["error"] = job["rec"]["error"] or "沒有收到文字清單"
            emit(job)
    except Exception as exc:  # noqa: BLE001
        summary["fatal"] = f"{type(exc).__name__}: {exc}"
        log_error({"output_dir": str(dst_root)}, f"textextract crashed: {summary['fatal']}")
        log("ERROR", summary["fatal"])
    finally:
        end = "批次結束" if not summary["stopped"] else "批次結束（已停止）"
        if summary["fatal"]:
            end = f"批次結束（{summary['fatal']}）"
        try:
            _log_line(text_log, end)
            text_log.close()
            csv_file.close()
            jsonl_file.close()
            hits_file.close()
        except Exception:  # noqa: BLE001
            pass
        for temp in temps:
            try:
                Path(temp).unlink(missing_ok=True)
            except OSError:
                pass
        summary["elapsed"] = round(time.time() - started, 2)
    return summary


def run_replace(
    input_dir,
    output_dir,
    accore,
    *,
    find,
    repl,
    mode="literal",
    case_sensitive=False,
    include_blocks=False,
    color=None,
    dry_run=False,
    copy_unchanged=False,
    recursive=True,
    overwrite=False,
    timeout=300,
    workers=1,
    stop_event=None,
    progress=None,
    log=None,
):
    """Replace text and write DWGs under ``output_dir``. Inputs are not modified.

    ``color`` is an ACI 0–256 applied only to changed entities, or None.
    ``dry_run`` writes the change list and does not write a DWG.
    Files with no match are ``無變更`` and are copied only when ``copy_unchanged``.
    """
    log = log or (lambda level, message: None)
    progress = progress or (lambda index, total, path: None)
    started = time.time()
    problem = dwgtext.validate_rule(find, mode, replacement=repl, for_replace=True)
    summary = _empty_summary()
    if problem:
        summary["fatal"] = problem
        return summary
    if color is not None and (not isinstance(color, int) or color < 0 or color > 256):
        summary["fatal"] = "ACI 色號必須是 0 到 256"
        return summary
    if not str(accore or "").strip() or not Path(accore).is_file():
        summary["fatal"] = "找不到 accoreconsole.exe"
        return summary
    dst_root = Path(output_dir)
    logs = dst_root / "logs"
    try:
        text_log, csv_file, jsonl_file, writer = _open_logs(logs, "textreplace", dwgtext.REPLACE_FIELDS)
        change_file, change_writer = _detail_writer(logs, "textreplace_changes.csv", dwgtext.CHANGE_FIELDS)
    except OSError as exc:
        summary["fatal"] = f"輸出資料夾無法寫入：{exc}"
        return summary
    summary, jobs, temps = _prepare(input_dir, output_dir, logs, recursive, overwrite, True)
    if summary["fatal"]:
        _log_line(text_log, f"批次結束（{summary['fatal']}）")
        text_log.close()
        csv_file.close()
        jsonl_file.close()
        change_file.close()
        return summary
    label = "預覽" if dry_run else "文字取代"
    _log_line(text_log, f"{label}開始: {input_dir} -> {output_dir}，共 {summary['dwg']} 個檔案")
    done = 0

    def emit(job):
        nonlocal done
        rec = job["rec"]
        if job.get("sha_before") is not None:
            _check_source(job)
        done += 1
        progress(done, summary["dwg"], rec["file"])
        summary["results"].append(rec)
        summary["changes"] += int(rec.get("changes") or 0)
        writer.writerow(dwgtext.replace_row(rec))
        csv_file.flush()
        payload = {key: value for key, value in rec.items() if key != "change_rows"}
        payload["change_rows"] = rec.get("change_rows") or []
        jsonl_file.write(json.dumps(payload, ensure_ascii=False) + "\n")
        jsonl_file.flush()
        name = Path(rec["file"]).name
        status = rec["status"]
        if status == "SKIPPED":
            summary["skipped"] += 1
            _log_line(text_log, f"略過 {name}（輸出已存在）")
            log("INFO", f"略過 {name}（輸出已存在）")
        elif status == "FAILED":
            summary["failed"] += 1
            summary["failed_files"].append(f"{rec['file']}: {rec.get('error', '')}".strip())
            _log_line(text_log, f"失敗 {name}: {rec.get('error', '')}")
            log("ERROR", f"失敗 {name}: {rec.get('error', '')}")
            log_error({"output_dir": str(dst_root)}, f"{rec['file']} -> {rec.get('error', '')}")
        elif status == "STOPPED":
            summary["stopped"] = True
            _log_line(text_log, f"已停止於 {name}")
            log("WARN", f"已停止於 {name}")
        else:
            summary["ok"] += 1
            if status == "UNCHANGED":
                summary["unchanged"] += 1
            shown = dwgtext.REPLACE_STATUS.get(status, status)
            note = f"；{rec['error']}" if rec.get("error") and status in {"SAVED", "PREVIEW"} else ""
            _log_line(text_log, f"{shown} {name}（{rec.get('changes', 0)} 筆）{note}")
            log("OK", f"{shown} {name}（{rec.get('changes', 0)} 筆）{note}")

    try:
        todo = [job for job in jobs if not job["skip"]]
        if todo:
            lisp_path = todo[0]["tmp_in"].parent / "textdump.lsp"
            lisp_path.write_bytes(fontfix.lisp_bytes(dwgtext.build_dump_lisp(include_blocks)))
            stopped = _launch_all(
                todo, accore, lisp_path,
                lambda chunk: dwgtext.build_dump_script(chunk, lisp_path),
                timeout, stop_event, logs, "textreplace",
                lambda job: progress(min(summary["dwg"], done + 1), summary["dwg"], str(job["src"])),
                workers=workers,
            )
            ready = any("TEXTDUMP_READY" in (job.get("stdout_ready") or "") for job in todo)
            apply_jobs = []
            for job in todo:
                if job["rec"]["status"] or job.get("stopped_here") or job.get("launch_error"):
                    continue
                if not _accept_dump(job, ready or ("TEXTDUMP_READY" in (job.get("stdout_ready") or ""))):
                    continue
                changes, notes = _plan_changes(job.get("rows") or [], find, repl, mode, case_sensitive, color)
                if notes:
                    job["rec"]["error"] = "；".join(notes)
                job["changes"] = changes
                job["rec"]["changes"] = len(changes)
                job["rec"]["change_rows"] = [
                    {
                        "file": str(job["src"]),
                        "layer": item["layer"],
                        "kind": item["kind"],
                        "handle": item["handle"],
                        "old": item["old"],
                        "new": item["new"],
                        "count": item["count"],
                    }
                    for item in changes
                ]
                for item in job["rec"]["change_rows"]:
                    change_writer.writerow(dwgtext.change_row(item))
                change_file.flush()
                if not changes:
                    job["rec"]["status"] = "UNCHANGED"
                    if copy_unchanged and not dry_run:
                        try:
                            ok, err = _copy_unchanged(job)
                        except OSError as exc:
                            ok, err = False, f"{type(exc).__name__}: {exc}"
                        if not ok:
                            job["rec"]["status"] = "FAILED"
                            job["rec"]["error"] = err
                elif dry_run:
                    job["rec"]["status"] = "PREVIEW"
                else:
                    apply_jobs.append(job)
            apply_stopped = False
            if apply_jobs and not stopped and not (stop_event is not None and stop_event.is_set()):
                apply_lisp = lisp_path.parent / "textapply.lsp"
                apply_lisp.write_bytes(fontfix.lisp_bytes(dwgtext.build_apply_lisp(
                    [job["changes"] for job in apply_jobs]
                )))
                for index, job in enumerate(apply_jobs):
                    job["apply_index"] = index
                    job.pop("console", None)
                    job.pop("opened_name", None)
                    job.pop("launch_error", None)
                    job.pop("stopped_here", None)
                apply_stopped = _launch_all(
                    apply_jobs, accore, apply_lisp,
                    lambda chunk: dwgtext.build_apply_script(chunk, apply_lisp),
                    timeout, stop_event, logs, "textreplace_apply",
                    lambda job: progress(min(summary["dwg"], done + 1), summary["dwg"], str(job["src"])),
                    workers=workers,
                )
                for job in apply_jobs:
                    if job.get("stopped_here") or (
                        apply_stopped and not job.get("console") and not job.get("launch_error")
                    ):
                        job["rec"]["status"] = "STOPPED"
                        job["rec"]["error"] = "已停止"
                        continue
                    if job.get("launch_error"):
                        job["rec"]["status"] = "FAILED"
                        job["rec"]["error"] = job["launch_error"]
                        continue
                    opened = job.get("opened_name") or ""
                    expected = Path(job["spath"]).name
                    if opened and opened.casefold() != expected.casefold():
                        job["rec"]["status"] = "FAILED"
                        job["rec"]["error"] = f"開啟的圖面與預期不同（{opened}）"
                        continue
                    applied = dwgtext.parse_apply(job.get("console") or "")
                    if applied["summary"] is None:
                        job["rec"]["status"] = "FAILED"
                        job["rec"]["error"] = "；".join(applied["errors"]) or "沒有收到寫回結果"
                        continue
                    ok_n, fail_n = applied["summary"]
                    if fail_n:
                        extra = f"寫回成功 {ok_n} 筆，失敗 {fail_n} 筆"
                        job["rec"]["error"] = "；".join(part for part in (job["rec"].get("error"), extra) if part)
                    if ok_n <= 0:
                        job["rec"]["status"] = "FAILED"
                        job["rec"]["error"] = job["rec"].get("error") or "沒有寫回任何文字"
                        continue
                    try:
                        ok, err = _publish(job)
                    except Exception as exc:  # noqa: BLE001
                        ok, err = False, f"{type(exc).__name__}: {exc}"
                    if not ok:
                        job["rec"]["status"] = "FAILED"
                        job["rec"]["error"] = err
                    else:
                        job["rec"]["status"] = "SAVED"
                        job["rec"]["output"] = str(job["dest"])
            if stopped or apply_stopped:
                summary["stopped"] = True
        for job in jobs:
            if not job["rec"]["status"]:
                if summary.get("stopped") or job.get("stopped_here"):
                    job["rec"]["status"] = "STOPPED"
                    job["rec"]["error"] = "已停止"
                else:
                    job["rec"]["status"] = "FAILED"
                    job["rec"]["error"] = job["rec"]["error"] or "沒有收到文字清單"
            emit(job)
    except Exception as exc:  # noqa: BLE001
        summary["fatal"] = f"{type(exc).__name__}: {exc}"
        log_error({"output_dir": str(dst_root)}, f"textreplace crashed: {summary['fatal']}")
        log("ERROR", summary["fatal"])
    finally:
        end = "批次結束" if not summary["stopped"] else "批次結束（已停止）"
        if summary["fatal"]:
            end = f"批次結束（{summary['fatal']}）"
        try:
            _log_line(text_log, end)
            text_log.close()
            csv_file.close()
            jsonl_file.close()
            change_file.close()
        except Exception:  # noqa: BLE001
            pass
        for temp in temps:
            try:
                Path(temp).unlink(missing_ok=True)
            except OSError:
                pass
        summary["elapsed"] = round(time.time() - started, 2)
    return summary

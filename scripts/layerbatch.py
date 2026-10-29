"""Batch layer classification through accoreconsole.

Original drawings are never opened for write. Each file is copied to an ASCII
temp name first. The copy is classified and, unless this is a dry run, QSAVEd
and moved to the output folder.
"""
from __future__ import annotations

import csv
import datetime
import json
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fontfix  # noqa: E402
import layerclass  # noqa: E402
import parallel  # noqa: E402
from common import decode_console, log_error  # noqa: E402
from convert_batch import DONE_RE, OPEN_RE, _Reader, _decode, _has_blocking_messagebox  # noqa: E402
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
        "moved": 0,
        "created": 0,
        "unchanged": 0,
        "stopped": False,
        "fatal": "",
        "failed_files": [],
        "notes": [],
        "elapsed": 0.0,
        "results": [],
    }


def _run_launch(jobs, accore, lisp_path, script_text, timeout, stop_event, on_file_start, logs):
    """Run one accoreconsole. Returns ``(consumed, stopped, fail_msg, stdout)``."""
    tag = uuid.uuid4().hex[:8]
    scr = lisp_path.parent / f"layerclass_{tag}.scr"
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
        (logs / f"layerclass_batch_{tag}.stdout.log").write_text(stdout, encoding="utf-8")
        (logs / f"layerclass_batch_{tag}.stderr.log").write_text(
            decode_console(bytes(err_reader.buf)), encoding="utf-8"
        )
        scr.unlink(missing_ok=True)
        return consumed, stopped, fail_msg, stdout
    finally:
        if proc.poll() is None:
            _terminate(proc)


def _launch_all(jobs, accore, lisp_path, script_text_of, timeout, stop_event, logs, on_file_start,
                workers=1):
    """Run ``jobs``, restarting after a stalled file. Returns True when stopped.

    ``workers`` > 1 splits the jobs over that many accoreconsole processes.
    """
    groups = parallel.split(jobs, workers)
    if len(groups) > 1:
        return any(parallel.run_groups(groups, lambda group: _launch_all(
            group, accore, lisp_path, script_text_of, timeout, stop_event, logs, on_file_start,
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
        consumed, halt, fail_msg, stdout = _run_launch(
            chunk, accore, lisp_path, script_text_of(chunk), timeout, stop_event, on_file_start, logs
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
        "moved": 0,
        "created": 0,
        "locks": "",
        "error": "",
        "elapsed_seconds": 0.0,
        "move_rows": [],
    }


def _prepare(input_dir, output_dir, logs, recursive, overwrite, dry_run):
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
        dest = fontfix.output_for(src, src_root, dst_root, recursive)
        rec = _blank_rec(src, dest)
        job = {"rec": rec, "src": src, "dest": dest, "src_root": src_root, "skip": False}
        if dest.exists() and not overwrite and not dry_run:
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


def _per_file_csv(logs, src, src_root):
    try:
        rel = Path(src).resolve().relative_to(Path(src_root).resolve())
    except ValueError:
        rel = Path(Path(src).name)
    safe = str(rel.with_suffix(".csv")).replace("\\", "__").replace("/", "__")
    return logs / "layerclass" / safe


def _accept(job, ready):
    rec = job["rec"]
    rec["elapsed_seconds"] = round(job.get("elapsed") or 0.0, 2)
    if job.get("launch_error"):
        rec["status"] = "FAILED"
        rec["error"] = job["launch_error"]
        return None
    if job.get("stopped_here"):
        rec["status"] = "STOPPED"
        rec["error"] = "已停止"
        return None
    if not job.get("console") and not ready:
        rec["status"] = "FAILED"
        rec["error"] = "沒有收到分圖層結果"
        return None
    opened = job.get("opened_name") or ""
    expected = Path(job["spath"]).name
    if opened and opened.casefold() != expected.casefold():
        rec["status"] = "FAILED"
        rec["error"] = f"開啟的圖面與預期不同（{opened}）"
        return None
    if not ready:
        rec["status"] = "FAILED"
        rec["error"] = "分圖層 LISP 沒有載入（見 logs 裡的 stdout）"
        return None
    parsed = layerclass.parse_output(job.get("console") or "")
    if parsed["summary"] is None:
        rec["status"] = "FAILED"
        rec["error"] = "；".join(parsed["errors"]) or "沒有收到分圖層結果"
        return None
    if parsed["errors"]:
        rec["status"] = "FAILED"
        rec["error"] = "；".join(parsed["errors"])
        rec["moved"] = parsed["summary"]["moved"]
        rec["created"] = parsed["summary"]["created"]
        rec["locks"] = layerclass.format_locks(parsed["locks"])
        return parsed
    return parsed


def run(
    input_dir,
    output_dir,
    accore,
    *,
    type_map,
    prefix="",
    other="",
    source_mode="layer0",
    source_layers="0",
    include_paper=False,
    include_blocks=False,
    insert_by_name=True,
    unlock_locked=False,
    keep_appearance=True,
    dry_run=False,
    recursive=True,
    overwrite=False,
    timeout=300,
    workers=1,
    stop_event=None,
    progress=None,
    log=None,
):
    """Classify entities and write DWGs under ``output_dir``. Inputs are not modified.

    ``dry_run`` writes the CSV reports and does not write a DWG.
    """
    log = log or (lambda level, message: None)
    progress = progress or (lambda index, total, path: None)
    started = time.time()
    summary = _empty_summary()
    settings, notes, problem = layerclass.compile_rules(
        type_map, prefix, other, source_mode, source_layers, insert_by_name,
    )
    summary["notes"] = notes
    if problem:
        summary["fatal"] = problem
        return summary
    if not str(accore or "").strip() or not Path(accore).is_file():
        summary["fatal"] = "找不到 accoreconsole.exe"
        return summary
    dst_root = Path(output_dir)
    logs = dst_root / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        text_log = (logs / "layerclass.log").open("w", encoding="utf-8")
        csv_file = (logs / "layerclass_results.csv").open("w", newline="", encoding="utf-8-sig")
        jsonl_file = (logs / "layerclass_results.jsonl").open("w", encoding="utf-8")
        writer = csv.DictWriter(csv_file, layerclass.RESULT_FIELDS)
        writer.writeheader()
        csv_file.flush()
        moves_file = (logs / "layerclass_moves.csv").open("w", newline="", encoding="utf-8-sig")
        moves_writer = csv.DictWriter(moves_file, layerclass.MOVE_FIELDS)
        moves_writer.writeheader()
        moves_file.flush()
    except OSError as exc:
        summary["fatal"] = f"輸出資料夾無法寫入：{exc}"
        return summary
    for note in notes:
        _log_line(text_log, note)
    summary, jobs, temps = _prepare(input_dir, output_dir, logs, recursive, overwrite, dry_run)
    summary["notes"] = notes
    if summary["fatal"]:
        _log_line(text_log, f"批次結束（{summary['fatal']}）")
        text_log.close()
        csv_file.close()
        jsonl_file.close()
        moves_file.close()
        return summary
    label = "預覽" if dry_run else "自動分圖層"
    _log_line(text_log, f"{label}開始: {input_dir} -> {output_dir}，共 {summary['dwg']} 個檔案")
    done = 0

    def emit(job):
        nonlocal done
        rec = job["rec"]
        if job.get("sha_before") is not None and rec["status"] != "SKIPPED":
            _check_source(job)
        done += 1
        progress(done, summary["dwg"], rec["file"])
        summary["results"].append(rec)
        summary["moved"] += int(rec.get("moved") or 0)
        summary["created"] += int(rec.get("created") or 0)
        writer.writerow(layerclass.result_row(rec))
        csv_file.flush()
        payload = {key: value for key, value in rec.items() if key != "move_rows"}
        payload["move_rows"] = rec.get("move_rows") or []
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
            shown = layerclass.STATUS.get(status, status)
            _log_line(
                text_log,
                f"{shown} {name}（搬移 {rec.get('moved', 0)}，新建圖層 {rec.get('created', 0)}）",
            )
            log("OK", f"{shown} {name}（搬移 {rec.get('moved', 0)}）")

    try:
        todo = [job for job in jobs if not job["skip"]]
        if todo:
            lisp_path = todo[0]["tmp_in"].parent / "layerclass.lsp"
            lisp_path.write_bytes(fontfix.lisp_bytes(layerclass.build_lisp(
                settings,
                include_paper=include_paper,
                include_blocks=include_blocks,
                unlock_locked=unlock_locked,
                keep_appearance=keep_appearance,
                dry_run=dry_run,
            )))
            stopped = _launch_all(
                todo, accore, lisp_path,
                lambda chunk: layerclass.build_script(chunk, lisp_path, dry_run),
                timeout, stop_event, logs,
                lambda job: progress(min(summary["dwg"], done + 1), summary["dwg"], str(job["src"])),
                workers=workers,
            )
            ready = any("LAYERCLASS_READY" in (job.get("stdout_ready") or "") for job in todo)
            for job in todo:
                if job["rec"]["status"]:
                    continue
                if job.get("stopped_here"):
                    job["rec"]["status"] = "STOPPED"
                    job["rec"]["error"] = "已停止"
                    continue
                parsed = _accept(job, ready or ("LAYERCLASS_READY" in (job.get("stdout_ready") or "")))
                if parsed is None or job["rec"]["status"] == "FAILED":
                    if parsed is not None:
                        _write_moves(job, parsed, moves_writer, moves_file, logs)
                    continue
                _write_moves(job, parsed, moves_writer, moves_file, logs)
                job["rec"]["moved"] = parsed["summary"]["moved"]
                job["rec"]["created"] = parsed["summary"]["created"]
                job["rec"]["locks"] = layerclass.format_locks(parsed["locks"])
                note = "；".join(parsed["notes"])
                if note:
                    job["rec"]["error"] = note
                if parsed["summary"]["moved"] <= 0:
                    job["rec"]["status"] = "UNCHANGED"
                elif dry_run:
                    job["rec"]["status"] = "PREVIEW"
                else:
                    try:
                        ok, err = _publish(job)
                    except Exception as exc:  # noqa: BLE001
                        ok, err = False, f"{type(exc).__name__}: {exc}"
                    if not ok:
                        job["rec"]["status"] = "FAILED"
                        job["rec"]["error"] = "；".join(part for part in (job["rec"].get("error"), err) if part)
                    else:
                        job["rec"]["status"] = "SAVED"
                        job["rec"]["output"] = str(job["dest"])
            if stopped:
                summary["stopped"] = True
        for job in jobs:
            if not job["rec"]["status"]:
                if summary.get("stopped") or job.get("stopped_here"):
                    job["rec"]["status"] = "STOPPED"
                    job["rec"]["error"] = "已停止"
                else:
                    job["rec"]["status"] = "FAILED"
                    job["rec"]["error"] = job["rec"]["error"] or "沒有收到分圖層結果"
            emit(job)
        _log_line(text_log, f"合計搬移 {summary['moved']}　新建圖層 {summary['created']}")
    except Exception as exc:  # noqa: BLE001
        summary["fatal"] = f"{type(exc).__name__}: {exc}"
        log_error({"output_dir": str(dst_root)}, f"layerclass crashed: {summary['fatal']}")
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
            moves_file.close()
        except Exception:  # noqa: BLE001
            pass
        for temp in temps:
            try:
                Path(temp).unlink(missing_ok=True)
            except OSError:
                pass
        summary["elapsed"] = round(time.time() - started, 2)
    return summary


def _write_moves(job, parsed, moves_writer, moves_file, logs):
    rows = []
    for item in parsed["rows"]:
        rows.append({
            "file": str(job["src"]),
            "layer": item["layer"],
            "color": item["color"],
            "kind": item["kind"],
            "count": item["count"],
            "source": item["source"],
        })
    job["rec"]["move_rows"] = rows
    for item in rows:
        moves_writer.writerow(layerclass.move_row(item))
    moves_file.flush()
    per_path = _per_file_csv(logs, job["src"], job.get("src_root") or job["src"].parent)
    per_path.parent.mkdir(parents=True, exist_ok=True)
    with per_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, layerclass.MOVE_FIELDS)
        writer.writeheader()
        for item in rows:
            writer.writerow(layerclass.move_row(item))


"""Batch title-block attribute export and write-back through accoreconsole.

Same safety model as ``textbatch``: every DWG is copied to an ASCII temp name
first, the original is never opened for write, and write-back QSAVEs the copy
and moves it to the output folder. Write-back re-reads each drawing first, so
only values that differ from the drawing are written.
"""
from __future__ import annotations

import csv
import json
import os
import stat
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fontfix  # noqa: E402
import textbatch  # noqa: E402
import titleblock  # noqa: E402
from common import log_error  # noqa: E402
from convert_single import safe_tmpdir, sha256  # noqa: E402

_log_line = textbatch._log_line


def _summary():
    summary = textbatch._empty_summary()
    summary.update(blocks=0, csv="")
    return summary


def _file_key(src, input_dir):
    try:
        return Path(src).resolve().relative_to(Path(input_dir).resolve()).as_posix()
    except ValueError:
        return str(src)


def _prepare(sources, logs, dests=None, overwrite=False):
    """Copy each source DWG to a temp file. Returns ``(jobs, temps)``."""
    jobs = []
    for index, src in enumerate(sources):
        dest = dests[index] if dests else None
        rec = {"file": str(src), "output": "" if dest is None else str(dest), "status": "",
               "blocks": 0, "changes": 0, "error": "", "elapsed_seconds": 0.0}
        job = {"rec": rec, "src": src, "dest": dest, "skip": False}
        if dest is not None and dest.exists() and not overwrite:
            rec["status"] = "SKIPPED"
            rec["error"] = "輸出已存在"
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
            tmp_in = tmpdir / f"in_{uuid.uuid4().hex[:10]}.dwg"
            try:
                textbatch._copy_bytes(job["src"], tmp_in)
                os.chmod(tmp_in, stat.S_IWRITE | stat.S_IREAD)
            except OSError as exc:
                job["rec"]["status"] = "FAILED"
                job["rec"]["error"] = f"無法讀取來源（檔案可能被獨占鎖定）：{exc}"
                job["skip"] = True
                continue
            temps.append(tmp_in)
            job["tmp_in"] = tmp_in
            job["spath"] = str(tmp_in).replace("\\", "/")
            job["produced"] = tmp_in
    return jobs, temps


def _accept(job, ready):
    """Validate one drawing's dump. Sets ``job["inserts"]`` and returns True on success."""
    rec = job["rec"]
    rec["elapsed_seconds"] = round(job.get("elapsed") or 0.0, 2)
    if job.get("launch_error"):
        rec["status"], rec["error"] = "FAILED", job["launch_error"]
        return False
    if job.get("stopped_here") or not job.get("console"):
        rec["status"], rec["error"] = "STOPPED", "已停止"
        return False
    opened = job.get("opened_name") or ""
    if opened and opened.casefold() != Path(job["spath"]).name.casefold():
        rec["status"], rec["error"] = "FAILED", f"開啟的圖面與預期不同（{opened}）"
        return False
    if not ready:
        rec["status"], rec["error"] = "FAILED", "圖框 LISP 沒有載入（見 logs 裡的 stdout）"
        return False
    parsed = titleblock.parse_dump(job["console"])
    if parsed["summary"] is None:
        rec["status"], rec["error"] = "FAILED", "；".join(parsed["errors"]) or "沒有收到圖框清單"
        return False
    if parsed["errors"]:
        rec["error"] = "；".join(parsed["errors"])
    job["inserts"] = parsed["inserts"]
    return True


def _dump(todo, accore, timeout, stop_event, logs, prefix, on_start):
    """Run the dump LISP over ``todo``. Returns ``(stopped, ready)``."""
    lisp_path = todo[0]["tmp_in"].parent / f"{prefix}_dump.lsp"
    lisp_path.write_bytes(fontfix.lisp_bytes(titleblock.build_dump_lisp()))
    stopped = textbatch._launch_all(
        todo, accore, lisp_path,
        lambda chunk: titleblock.build_dump_script(chunk, lisp_path),
        timeout, stop_event, logs, prefix, on_start,
    )
    ready = any("TBDUMP_READY" in (job.get("stdout_ready") or "") for job in todo)
    return stopped, ready


def _cleanup(temps):
    for temp in temps:
        try:
            Path(temp).unlink(missing_ok=True)
        except OSError:
            pass


def _check_accore(accore):
    if not str(accore or "").strip() or not Path(accore).is_file():
        return "找不到 accoreconsole.exe"
    return None


def run_read(
    input_dir,
    output_dir,
    accore,
    *,
    patterns,
    recursive=True,
    timeout=300,
    stop_event=None,
    progress=None,
    log=None,
):
    """Export attributes of matching title blocks to ``output_dir/titleblock_attrs.csv``."""
    log = log or (lambda level, message: None)
    progress = progress or (lambda index, total, path: None)
    started = time.time()
    summary = _summary()
    patterns = titleblock.parse_patterns(patterns)
    summary["fatal"] = (titleblock.validate_patterns(patterns) or _check_accore(accore)
                        or fontfix.directory_problem(input_dir, output_dir) or "")
    if summary["fatal"]:
        return summary
    dst_root = Path(output_dir)
    logs = dst_root / "logs"
    csv_path = dst_root / titleblock.ATTRS_CSV
    try:
        text_log, res_file, jsonl_file, writer = textbatch._open_logs(logs, "titleblock_read", titleblock.READ_FIELDS)
    except OSError as exc:
        summary["fatal"] = f"輸出資料夾無法寫入：{exc}"
        return summary
    files = fontfix.iter_dwgs(input_dir, recursive)
    summary["dwg"] = len(files)
    _log_line(text_log, f"圖框屬性匯出開始: {input_dir}，共 {len(files)} 個檔案，Block：{', '.join(patterns)}")
    temps = []
    all_rows, columns = [], []
    try:
        jobs, temps = _prepare(files, logs)
        todo = [job for job in jobs if not job["skip"]]
        done = 0
        if todo:
            stopped, ready = _dump(
                todo, accore, timeout, stop_event, logs, "titleblock_read",
                lambda job: progress(min(len(files), done + 1), len(files), str(job["src"])),
            )
            summary["stopped"] = stopped
            for job in todo:
                if _accept(job, ready):
                    selected = titleblock.select_inserts(job["inserts"], patterns)
                    rows, found = titleblock.attr_rows(_file_key(job["src"], input_dir), selected)
                    all_rows.extend(rows)
                    titleblock.merge_columns(columns, found)
                    job["rec"]["blocks"] = len(rows)
                    job["rec"]["status"] = "DONE" if rows else "EMPTY"
        for job in jobs:
            rec = job["rec"]
            textbatch._check_source(job)
            done += 1
            progress(done, len(files), rec["file"])
            summary["results"].append(rec)
            summary["blocks"] += rec["blocks"]
            writer.writerow(titleblock.read_row(rec))
            jsonl_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
            name = Path(rec["file"]).name
            if rec["status"] == "FAILED":
                summary["failed"] += 1
                summary["failed_files"].append(f"{rec['file']}: {rec['error']}")
                _log_line(text_log, f"失敗 {name}: {rec['error']}")
                log("ERROR", f"失敗 {name}: {rec['error']}")
                log_error({"output_dir": str(dst_root)}, f"{rec['file']} -> {rec['error']}")
            elif rec["status"] == "STOPPED":
                summary["stopped"] = True
                _log_line(text_log, f"已停止於 {name}")
            else:
                summary["ok"] += 1
                label = titleblock.READ_STATUS[rec["status"]]
                _log_line(text_log, f"{label} {name}（{rec['blocks']} 個圖框）")
                log("OK", f"{label} {name}（{rec['blocks']} 個圖框）")
        with csv_path.open("w", newline="", encoding="utf-8-sig") as handle:
            out = csv.DictWriter(handle, titleblock.BASE_COLUMNS + columns, restval="")
            out.writeheader()
            out.writerows(all_rows)
        summary["csv"] = str(csv_path)
    except Exception as exc:  # noqa: BLE001
        summary["fatal"] = f"{type(exc).__name__}: {exc}"
        log_error({"output_dir": str(dst_root)}, f"titleblock read crashed: {summary['fatal']}")
        log("ERROR", summary["fatal"])
    finally:
        end = f"批次結束（{summary['fatal']}）" if summary["fatal"] else (
            "批次結束（已停止）" if summary["stopped"] else "批次結束")
        _log_line(text_log, end)
        text_log.close()
        res_file.close()
        jsonl_file.close()
        _cleanup(temps)
        summary["elapsed"] = round(time.time() - started, 2)
    return summary


def load_csv(csv_path):
    """Return ``(rows, tag_columns)`` from an exported (and edited) CSV."""
    return titleblock.read_attrs_csv(titleblock.decode_csv_bytes(Path(csv_path).read_bytes()))


def _group_rows(rows, input_dir):
    """Map each CSV ``file`` to its source DWG. Returns ``(groups, missing)``."""
    groups = {}
    missing = {}
    root = Path(input_dir)
    for row in rows:
        key = (row.get("file") or "").strip()
        src = Path(key) if Path(key).is_absolute() else root / key
        bucket = groups if key and src.is_file() else missing
        bucket.setdefault(key, {"src": src, "rows": []})["rows"].append(row)
    return groups, missing


def run_write(
    input_dir,
    output_dir,
    accore,
    *,
    csv_path,
    dry_run=False,
    overwrite=False,
    timeout=300,
    stop_event=None,
    progress=None,
    log=None,
):
    """Write changed CSV values back. Changed drawings go to ``output_dir``; inputs are not modified."""
    log = log or (lambda level, message: None)
    progress = progress or (lambda index, total, path: None)
    started = time.time()
    summary = _summary()
    summary["fatal"] = _check_accore(accore) or fontfix.directory_problem(input_dir, output_dir) or ""
    if summary["fatal"]:
        return summary
    try:
        rows, tags = load_csv(csv_path)
    except (OSError, ValueError, csv.Error) as exc:
        summary["fatal"] = f"無法讀取 CSV：{exc}"
        return summary
    dst_root = Path(output_dir)
    logs = dst_root / "logs"
    try:
        text_log, res_file, jsonl_file, writer = textbatch._open_logs(logs, "titleblock", titleblock.RESULT_FIELDS)
    except OSError as exc:
        summary["fatal"] = f"輸出資料夾無法寫入：{exc}"
        return summary
    label = "預覽" if dry_run else "圖框屬性寫回"
    _log_line(text_log, f"{label}開始: {csv_path} -> {output_dir}")
    temps = []

    def emit(file_text, output, records):
        for record in records:
            record.update(file=file_text, output=output)
            record.pop("attr_handle", None)
            writer.writerow(titleblock.result_row(record))
            jsonl_file.write(json.dumps(record, ensure_ascii=False) + "\n")
            summary["results"].append(record)
        res_file.flush()
        jsonl_file.flush()

    try:
        groups, missing = _group_rows(rows, input_dir)
        keys = list(groups)
        summary["dwg"] = len(keys) + len(missing)
        _log_line(text_log, f"共 {summary['dwg']} 個檔案")
        for key, group in missing.items():
            summary["failed"] += 1
            error = "找不到來源 DWG" if key else "CSV 的 file 欄是空的"
            summary["failed_files"].append(f"{key}: {error}")
            _log_line(text_log, f"失敗 {key}: {error}")
            log("ERROR", f"失敗 {key}: {error}")
            emit(str(group["src"]), "", [{"status": "FAILED", "error": error}])
        sources = [groups[key]["src"] for key in keys]
        dests = [fontfix.output_for(src, input_dir, output_dir, True)
                 if not Path(key).is_absolute() else dst_root / Path(key).name
                 for key, src in zip(keys, sources)]
        jobs, temps = _prepare(sources, logs, dests, overwrite or dry_run)
        for key, job in zip(keys, jobs):
            job["key"] = key
        todo = [job for job in jobs if not job["skip"]]
        done = 0

        def on_start(job):
            progress(min(summary["dwg"], done + 1), summary["dwg"], str(job["src"]))

        stopped = False
        apply_jobs = []
        if todo:
            stopped, ready = _dump(todo, accore, timeout, stop_event, logs, "titleblock_write", on_start)
            for job in todo:
                if not _accept(job, ready):
                    continue
                edits, records = titleblock.plan_edits(groups[job["key"]]["rows"], job["inserts"], tags)
                job["edits"], job["records"] = edits, records
                job["rec"]["changes"] = len(edits)
                if not edits:
                    job["rec"]["status"] = "UNCHANGED"
                elif dry_run:
                    job["rec"]["status"] = "PREVIEW"
                    for record in records:
                        record["status"] = record["status"] or "PREVIEW"
                else:
                    apply_jobs.append(job)
        if apply_jobs and not stopped and not (stop_event is not None and stop_event.is_set()):
            apply_lisp = apply_jobs[0]["tmp_in"].parent / "titleblock_apply.lsp"
            apply_lisp.write_bytes(fontfix.lisp_bytes(titleblock.build_apply_lisp(
                [job["edits"] for job in apply_jobs])))
            for index, job in enumerate(apply_jobs):
                job["apply_index"] = index
                for field in ("console", "opened_name", "launch_error", "stopped_here"):
                    job.pop(field, None)
            stopped = textbatch._launch_all(
                apply_jobs, accore, apply_lisp,
                lambda chunk: titleblock.build_apply_script(chunk, apply_lisp),
                timeout, stop_event, logs, "titleblock_apply", on_start,
            ) or stopped
            for job in apply_jobs:
                _finish_apply(job)
        for job in jobs:
            rec = job["rec"]
            if job.get("sha_before") is not None:
                textbatch._check_source(job)
            if not rec["status"]:
                rec["status"], rec["error"] = "STOPPED", "已停止"
            records = job.get("records") or []
            if rec["status"] in {"FAILED", "STOPPED"}:
                for record in records:
                    if record["status"] in {"", "WRITTEN"}:
                        record.update(status=rec["status"], error=rec["error"])
            if not records:
                records = [{"status": rec["status"], "error": rec["error"]}]
            done += 1
            progress(done, summary["dwg"], rec["file"])
            emit(rec["file"], rec["output"] if rec["status"] == "WRITTEN" else "", records)
            summary["changes"] += sum(1 for record in records if record.get("status") in {"WRITTEN", "PREVIEW"})
            name = Path(rec["file"]).name
            status = rec["status"]
            shown = titleblock.WRITE_STATUS.get(status, status)
            if status == "FAILED":
                summary["failed"] += 1
                summary["failed_files"].append(f"{rec['file']}: {rec['error']}")
                _log_line(text_log, f"失敗 {name}: {rec['error']}")
                log("ERROR", f"失敗 {name}: {rec['error']}")
                log_error({"output_dir": str(dst_root)}, f"{rec['file']} -> {rec['error']}")
            elif status == "SKIPPED":
                summary["skipped"] += 1
                _log_line(text_log, f"略過 {name}（{rec['error']}）")
                log("INFO", f"略過 {name}（{rec['error']}）")
            elif status == "STOPPED":
                summary["stopped"] = True
                _log_line(text_log, f"已停止於 {name}")
                log("WARN", f"已停止於 {name}")
            else:
                summary["ok"] += 1
                summary["unchanged"] += status == "UNCHANGED"
                _log_line(text_log, f"{shown} {name}（{rec['changes']} 筆）")
                log("OK", f"{shown} {name}（{rec['changes']} 筆）")
    except Exception as exc:  # noqa: BLE001
        summary["fatal"] = f"{type(exc).__name__}: {exc}"
        log_error({"output_dir": str(dst_root)}, f"titleblock write crashed: {summary['fatal']}")
        log("ERROR", summary["fatal"])
    finally:
        end = f"批次結束（{summary['fatal']}）" if summary["fatal"] else (
            "批次結束（已停止）" if summary["stopped"] else "批次結束")
        _log_line(text_log, end)
        text_log.close()
        res_file.close()
        jsonl_file.close()
        _cleanup(temps)
        summary["elapsed"] = round(time.time() - started, 2)
    return summary


def _finish_apply(job):
    """Read the apply output for one drawing, mark each record, and publish the DWG."""
    rec = job["rec"]
    if job.get("launch_error"):
        rec["status"], rec["error"] = "FAILED", job["launch_error"]
        return
    if job.get("stopped_here") or not job.get("console"):
        rec["status"], rec["error"] = "STOPPED", "已停止"
        return
    opened = job.get("opened_name") or ""
    if opened and opened.casefold() != Path(job["spath"]).name.casefold():
        rec["status"], rec["error"] = "FAILED", f"開啟的圖面與預期不同（{opened}）"
        return
    applied = titleblock.parse_apply(job["console"])
    if applied["summary"] is None:
        rec["status"], rec["error"] = "FAILED", "；".join(applied["errors"]) or "沒有收到寫回結果"
        return
    ok_n, fail_n = applied["summary"]
    for record in job["records"]:
        if record["status"]:
            continue
        result = applied["results"].get(record["attr_handle"].upper(), "沒有回報")
        if result == "ok":
            record["status"] = "WRITTEN"
        else:
            record.update(status="FAILED", error=f"寫回失敗：{result}")
    if fail_n:
        rec["error"] = f"寫回成功 {ok_n} 筆，失敗 {fail_n} 筆"
    if ok_n <= 0:
        rec["status"], rec["error"] = "FAILED", rec["error"] or "沒有寫回任何屬性"
        return
    try:
        ok, err = textbatch._publish(job)
    except Exception as exc:  # noqa: BLE001
        ok, err = False, f"{type(exc).__name__}: {exc}"
    if ok:
        rec["status"] = "WRITTEN"
        rec["output"] = str(job["dest"])
    else:
        rec["status"], rec["error"] = "FAILED", err

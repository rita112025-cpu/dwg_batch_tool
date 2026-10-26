"""Copy every DWG to the output folder with missing text-style fonts replaced.

Originals are never opened for write. Each source is copied to an ASCII temp file
first, including when AutoCAD already has the drawing open (it shares the file for
read). accoreconsole then opens that copy, loads the generated UTF-8 LISP, and
QSAVE (current format) or SAVEAS (an explicit version keyword). Python moves the
temp DWG to the final path, which may contain Chinese characters or spaces.

One accoreconsole process handles the batch. A stalled MessageBox fails that file,
kills only that process, and the rest of the batch continues in a new process.
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
from common import decode_console, log_error  # noqa: E402
from convert_batch import (  # noqa: E402
    DONE_RE,
    OPEN_RE,
    _Reader,
    _decode,
    _has_blocking_messagebox,
)
from convert_single import _terminate, publish_dxf, safe_tmpdir, sha256  # noqa: E402

VERSION_LABELS = dict(fontfix.DWG_SAVE_VERSIONS)


def _stamp():
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _copy_bytes(src, dest):
    """Read the source and write a new file. Does not open the source for write."""
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
        "styles_replaced": 0,
        "stopped": False,
        "fatal": "",
        "failed_files": [],
        "still_files": [],
        "elapsed": 0.0,
        "results": [],
    }


def _run_launch(jobs, accore, lisp_path, timeout, stop_event, on_file_start, logs):
    """Run one accoreconsole for ``jobs``. Returns (consumed, stopped, fail_msg, stdout)."""
    tag = uuid.uuid4().hex[:8]
    scr = lisp_path.parent / f"fontfix_{tag}.scr"
    scr.write_bytes(fontfix.script_bytes(fontfix.build_script(jobs, lisp_path)))
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
        (logs / f"fontfix_batch_{tag}.stdout.log").write_text(stdout, encoding="utf-8")
        (logs / f"fontfix_batch_{tag}.stderr.log").write_text(
            decode_console(bytes(err_reader.buf)), encoding="utf-8"
        )
        scr.unlink(missing_ok=True)
        return consumed, stopped, fail_msg, stdout
    finally:
        if proc.poll() is None:
            _terminate(proc)


def _publish_job(job):
    """Move the saved temp DWG to the output path. The source drawing is not this file."""
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


def _finish_job(job, lisp_ready):
    """Fill one result after accoreconsole has released the temp DWG."""
    rec = job["rec"]
    rec["elapsed_seconds"] = round(job.get("elapsed") or 0.0, 2)
    opened = job.get("opened_name") or ""
    expected = Path(job["spath"]).name
    parsed = fontfix.parse_fontfix_output(job.get("console") or "")
    publish = False
    if opened and opened.casefold() != expected.casefold():
        rec["status"] = "FAILED"
        rec["error"] = f"開啟的圖面與預期不同（{opened}）"
    elif not lisp_ready:
        rec["status"] = "FAILED"
        rec["error"] = "字體修復 LISP 沒有載入（見 logs 裡的 stdout）"
    elif parsed["errors"] and not parsed["replacements"]:
        rec["status"] = "FAILED"
        rec["error"] = "；".join(parsed["errors"])
    elif parsed["summary"] is None and not parsed["replacements"]:
        rec["status"] = "FAILED"
        rec["error"] = "；".join(parsed["errors"]) or "沒有收到修復結果"
    else:
        publish = True
    if publish:
        try:
            ok, err = _publish_job(job)
        except Exception as exc:  # noqa: BLE001
            ok, err = False, f"{type(exc).__name__}: {exc}"
        if not ok:
            rec["status"] = "FAILED"
            rec["error"] = err
        else:
            rec["status"] = "SAVED"
            rec["styles_replaced"] = len(parsed["replacements"])
            rec["replacement_rows"] = parsed["replacements"]
            rec["replacements"] = fontfix.replacement_text(parsed["replacements"])
            rec["still_rows"] = parsed["still_missing"]
            rec["still_missing"] = fontfix.still_text(parsed["still_missing"])
            if parsed["errors"]:
                rec["error"] = "；".join(parsed["errors"])
    try:
        if sha256(job["src"]) != job["sha_before"]:
            rec["status"] = "FAILED"
            rec["error"] = ((rec.get("error") or "") + " 來源 DWG 的內容在處理中被改變").strip()
    except OSError as exc:
        rec["error"] = ((rec.get("error") or "") + f" 無法再讀取來源：{exc}").strip()
    return rec


def run(
    input_dir,
    output_dir,
    accore,
    *,
    recursive=True,
    primary=fontfix.DEFAULT_PRIMARY,
    big=fontfix.DEFAULT_BIG,
    mapping=None,
    version="",
    overwrite=False,
    timeout=300,
    stop_event=None,
    progress=None,
    log=None,
):
    """Fix every DWG under ``input_dir`` into ``output_dir``.

    ``progress(index, total, path)`` is 1-based and fires when a file starts and
    when its result is known. ``log(level, message)`` receives short Traditional
    Chinese lines. The summary dict matches the GUI outcome classifier.
    """
    log = log or (lambda level, message: None)
    progress = progress or (lambda index, total, path: None)
    started = time.time()
    summary = _empty_summary()
    problem = fontfix.directory_problem(input_dir, output_dir)
    if problem:
        summary["fatal"] = problem
        return summary
    if not str(accore or "").strip() or not Path(accore).is_file():
        summary["fatal"] = "找不到 accoreconsole.exe"
        return summary
    version = version or ""
    if version not in fontfix.DWG_SAVE_CODES:
        summary["fatal"] = f"不支援的 DWG 版本：{version}"
        return summary
    primary = (primary or "").strip() or fontfix.DEFAULT_PRIMARY
    big = (big or "").strip() or fontfix.DEFAULT_BIG
    mapping = dict(mapping or {})

    src_root = Path(input_dir)
    dst_root = Path(output_dir)
    logs = dst_root / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        probe = logs / ".write_test"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        summary["fatal"] = f"輸出資料夾無法寫入：{exc}"
        return summary

    files = fontfix.iter_dwgs(src_root, recursive)
    summary["dwg"] = len(files)
    version_label = VERSION_LABELS.get(version, version)
    text_log = (logs / "fontfix.log").open("w", encoding="utf-8")
    csv_file = (logs / "fontfix_results.csv").open("w", newline="", encoding="utf-8-sig")
    jsonl_file = (logs / "fontfix_results.jsonl").open("w", encoding="utf-8")
    writer = csv.DictWriter(csv_file, fontfix.CSV_FIELDS)
    writer.writeheader()
    csv_file.flush()
    _log_line(
        text_log,
        f"批次開始: {src_root} -> {dst_root} ({version_label})，共 {len(files)} 個檔案",
    )
    done_count = 0
    temps = []

    def emit(rec):
        nonlocal done_count
        done_count += 1
        progress(done_count, len(files), rec["file"])
        summary["results"].append(rec)
        summary["styles_replaced"] += int(rec.get("styles_replaced") or 0)
        writer.writerow(fontfix.csv_row(rec))
        csv_file.flush()
        jsonl_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
        jsonl_file.flush()
        name = Path(rec["file"]).name
        status = rec["status"]
        if status == "SKIPPED":
            summary["skipped"] += 1
            _log_line(text_log, f"略過 {name}（輸出已存在）")
            log("INFO", f"略過 {name}（輸出已存在）")
        elif status == "SAVED":
            summary["ok"] += 1
            for row in rec.get("replacement_rows") or []:
                _log_line(text_log, "替換 " + name + " | " + fontfix.format_change(row["style"], row))
            note = f"已輸出 {name}（替換 {rec['styles_replaced']} 個樣式）"
            warned = False
            if rec.get("still_missing"):
                summary["warnings"] += 1
                warned = True
                summary["still_files"].append(f"{name}: {rec['still_missing']}")
                note += "；存檔後仍有缺字"
                _log_line(text_log, f"仍缺字 {name} | {rec['still_missing']}")
            if rec.get("error"):
                note += f"；{rec['error']}"
                if not warned:
                    summary["warnings"] += 1
            if warned:
                log("WARN", note)
            else:
                log("OK", note)
            _log_line(text_log, note)
        elif status == "STOPPED":
            summary["stopped"] = True
            _log_line(text_log, f"已停止於 {name}")
            log("WARN", f"已停止於 {name}")
        else:
            summary["failed"] += 1
            summary["failed_files"].append(f"{rec['file']}: FAILED {rec.get('error', '')}".strip())
            _log_line(text_log, f"失敗 {name}: {rec.get('error', '')}")
            log("ERROR", f"失敗 {name}: {rec.get('error', '')}")
            log_error({"output_dir": str(dst_root)}, f"{rec['file']} -> {rec.get('error', '')}")

    try:
        if stop_event is not None and stop_event.is_set():
            summary["stopped"] = True
            return summary

        jobs = []
        for src in files:
            dest = fontfix.output_for(src, src_root, dst_root, recursive)
            rec = {
                "file": str(src),
                "output": str(dest),
                "status": "",
                "styles_replaced": 0,
                "replacements": "",
                "replacement_rows": [],
                "still_missing": "",
                "still_rows": [],
                "error": "",
                "elapsed_seconds": 0.0,
            }
            job = {"rec": rec, "skip": False, "src": src, "dest": dest}
            if dest.exists() and not overwrite:
                rec["status"] = "SKIPPED"
                job["skip"] = True
            else:
                try:
                    job["sha_before"] = sha256(src)
                    job["token"] = uuid.uuid4().hex[:10]
                except OSError as exc:
                    rec["status"] = "FAILED"
                    rec["error"] = f"無法讀取來源（檔案可能被獨占鎖定）：{exc}"
                    job["skip"] = True
            jobs.append(job)

        todo = []
        pending_skips = []
        if any(not job["skip"] for job in jobs):
            tmpdir = safe_tmpdir(logs)
            lisp_path = tmpdir / "fontfix_run.lsp"
            lisp_path.write_bytes(fontfix.lisp_bytes(fontfix.build_lisp(
                primary, big, mapping, fontfix.read_installed_typefaces(),
                fontfix.extra_font_dirs(accore),
            )))
            for job in jobs:
                if job["skip"]:
                    pending_skips.append(job)
                    continue
                tmp_in = tmpdir / f"in_{job['token']}.dwg"
                tmp_out = tmpdir / f"out_{job['token']}.dwg"
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
                    pending_skips.append(job)
                    continue
                temps.append(tmp_in)
                job["tmp_in"] = tmp_in
                job["spath"] = str(tmp_in).replace("\\", "/")
                if version:
                    job["save_version"] = version
                    job["save_path"] = str(tmp_out).replace("\\", "/")
                    job["produced"] = tmp_out
                    temps.append(tmp_out)
                else:
                    job["save_version"] = ""
                    job["produced"] = tmp_in
                todo.append(job)
        else:
            pending_skips = [job for job in jobs if job["skip"]]

        pos = 0
        while pos < len(todo):
            if stop_event is not None and stop_event.is_set():
                summary["stopped"] = True
                stopped_job = todo[pos]
                stopped_job["rec"]["status"] = "STOPPED"
                stopped_job["rec"]["error"] = "已停止"
                emit(stopped_job["rec"])
                break
            chunk = todo[pos:]

            def _on_start(job):
                progress(min(len(files), done_count + 1), len(files), str(job["src"]))

            consumed, stopped, fail_msg, stdout = _run_launch(
                chunk, accore, lisp_path, timeout, stop_event, _on_start, logs
            )
            ready = fontfix.parse_fontfix_output(stdout).get("ready", False)
            for offset in range(consumed):
                emit(_finish_job(chunk[offset], ready))
            pos += consumed
            if stopped:
                summary["stopped"] = True
                if pos < len(todo):
                    stopped_job = todo[pos]
                    stopped_job["rec"]["status"] = "STOPPED"
                    stopped_job["rec"]["error"] = "已停止"
                    emit(stopped_job["rec"])
                break
            if consumed < len(chunk) and fail_msg:
                bad = chunk[consumed]
                bad["rec"]["status"] = "FAILED"
                bad["rec"]["error"] = fail_msg
                emit(bad["rec"])
                pos += 1
        if not summary["stopped"]:
            for job in pending_skips:
                emit(job["rec"])
    except Exception as exc:  # noqa: BLE001
        summary["fatal"] = f"{type(exc).__name__}: {exc}"
        log_error({"output_dir": str(dst_root)}, f"fontfix crashed: {summary['fatal']}")
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
        except Exception:  # noqa: BLE001
            pass
        for temp in temps:
            try:
                Path(temp).unlink(missing_ok=True)
            except OSError:
                pass
        summary["elapsed"] = round(time.time() - started, 2)
    return summary


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="批次替換 DWG 中找不到的文字樣式字體")
    parser.add_argument("input_dir")
    parser.add_argument("output_dir")
    parser.add_argument("--accore", default="")
    parser.add_argument("--primary", default=fontfix.DEFAULT_PRIMARY)
    parser.add_argument("--big", default=fontfix.DEFAULT_BIG)
    parser.add_argument("--map", default="", help="字體對照檔，每行 old=new")
    parser.add_argument("--version", default="", help="SAVEAS 格式關鍵字，空白表示維持目前格式")
    parser.add_argument("--no-recursive", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args(argv)
    from common import load_config
    cfg = load_config()
    accore = args.accore or cfg.get("accoreconsole", "")
    mapping = fontfix.parse_mapping(Path(args.map).read_text(encoding="utf-8") if args.map else "")
    summary = run(
        args.input_dir, args.output_dir, accore,
        recursive=not args.no_recursive, primary=args.primary, big=args.big,
        mapping=mapping, version=args.version, overwrite=args.overwrite,
        timeout=int(cfg.get("timeout_seconds", 300)),
        log=lambda level, message: print(f"[{level}] {message}"),
    )
    print(
        f"檔案 {summary['dwg']}  成功 {summary['ok']}  失敗 {summary['failed']}"
        f"  略過 {summary['skipped']}  仍缺字 {summary['warnings']}"
    )
    return 0 if not summary["fatal"] and not summary["failed"] and not summary["stopped"] else 1


if __name__ == "__main__":
    sys.exit(main())

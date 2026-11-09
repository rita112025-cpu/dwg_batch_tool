"""Batch-enable PROXYGRAPHICS on copied DWGs through accoreconsole."""
from __future__ import annotations

import json
import time
from pathlib import Path

import fontfix
import proxygraphics
import textbatch
from common import log_error


def _empty_summary() -> dict:
    return {
        "dwg": 0, "ok": 0, "failed": 0, "skipped": 0, "warnings": 0,
        "stopped": False, "fatal": "", "failed_files": [], "elapsed": 0.0,
        "results": [],
    }


def run(
    input_dir,
    output_dir,
    accore,
    *,
    recursive=True,
    overwrite=False,
    timeout=300,
    stop_event=None,
    progress=None,
    log=None,
) -> dict:
    """Write verified ``PROXYGRAPHICS=1`` copies under ``output_dir``."""
    progress = progress or (lambda index, total, path: None)
    log = log or (lambda level, message: None)
    started = time.time()
    summary = _empty_summary()
    if not str(accore or "").strip() or not Path(accore).is_file():
        summary["fatal"] = "找不到 accoreconsole.exe"
        return summary

    dst_root = Path(output_dir)
    logs = dst_root / "logs"
    try:
        text_log, csv_file, jsonl_file, writer = textbatch._open_logs(
            logs, "proxygraphics", proxygraphics.CSV_FIELDS
        )
    except OSError as exc:
        summary["fatal"] = f"輸出資料夾無法寫入：{exc}"
        return summary

    prepared, jobs, temps = textbatch._prepare(
        input_dir, output_dir, logs, recursive, overwrite, True
    )
    summary.update({key: value for key, value in prepared.items() if key != "results"})
    summary["results"] = []
    if summary["fatal"]:
        textbatch._log_line(text_log, f"批次結束（{summary['fatal']}）")
        text_log.close()
        csv_file.close()
        jsonl_file.close()
        return summary

    textbatch._log_line(
        text_log, f"PROXYGRAPHICS 批次開始: {input_dir} -> {output_dir}，共 {summary['dwg']} 個檔案"
    )
    done = 0
    lisp_path = None

    def emit(job: dict) -> None:
        nonlocal done
        rec = job["rec"]
        if job.get("sha_before") is not None:
            textbatch._check_source(job)
        done += 1
        progress(done, summary["dwg"], rec["file"])
        summary["results"].append(rec)
        writer.writerow(proxygraphics.csv_row(rec))
        csv_file.flush()
        jsonl_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
        jsonl_file.flush()
        name = Path(rec["file"]).name
        if rec["status"] == "SAVED":
            summary["ok"] += 1
            note = f"已輸出 {name}（PROXYGRAPHICS: {rec.get('before')} -> 1）"
            textbatch._log_line(text_log, note)
            log("OK", note)
        elif rec["status"] == "SKIPPED":
            summary["skipped"] += 1
            note = f"略過 {name}（輸出已存在）"
            textbatch._log_line(text_log, note)
            log("INFO", note)
        elif rec["status"] == "STOPPED":
            summary["stopped"] = True
            textbatch._log_line(text_log, f"已停止於 {name}")
            log("WARN", f"已停止於 {name}")
        else:
            summary["failed"] += 1
            detail = f"{rec['file']}: {rec.get('error', '')}".strip()
            summary["failed_files"].append(detail)
            textbatch._log_line(text_log, f"失敗 {name}: {rec.get('error', '')}")
            log("ERROR", f"失敗 {name}: {rec.get('error', '')}")
            log_error({"output_dir": str(dst_root)}, detail)

    try:
        todo = [job for job in jobs if not job["skip"]]
        if todo:
            lisp_path = todo[0]["tmp_in"].parent / "proxygraphics.lsp"
            lisp_path.write_bytes(fontfix.lisp_bytes(proxygraphics.build_lisp()))
            stopped = textbatch._launch_all(
                todo,
                accore,
                lisp_path,
                lambda chunk: proxygraphics.build_script(chunk, lisp_path),
                timeout,
                stop_event,
                logs,
                "proxygraphics",
                lambda job: progress(min(summary["dwg"], done + 1), summary["dwg"], str(job["src"])),
            )
            summary["stopped"] = stopped
            for job in todo:
                rec = job["rec"]
                rec.update(before="", after="", elapsed_seconds=round(job.get("elapsed") or 0.0, 2))
                if job.get("stopped_here") or (stopped and not job.get("console") and not job.get("launch_error")):
                    rec.update(status="STOPPED", error="已停止")
                    continue
                if job.get("launch_error"):
                    rec.update(status="FAILED", error=job["launch_error"])
                    continue
                opened = job.get("opened_name") or ""
                expected = Path(job["spath"]).name
                if opened and opened.casefold() != expected.casefold():
                    rec.update(status="FAILED", error=f"開啟的圖面與預期不同（{opened}）")
                    continue
                parsed = proxygraphics.parse_output(job.get("console") or "")
                rec["before"], rec["after"] = parsed["before"], parsed["after"]
                if not parsed["ready"]:
                    rec.update(status="FAILED", error="PROXYGRAPHICS LISP 沒有載入")
                elif parsed["errors"]:
                    rec.update(status="FAILED", error="；".join(parsed["errors"]))
                elif parsed["after"] != 1:
                    rec.update(status="FAILED", error="沒有確認 PROXYGRAPHICS 已設為 1")
                else:
                    try:
                        ok, error = textbatch._publish(job)
                    except Exception as exc:  # noqa: BLE001
                        ok, error = False, f"{type(exc).__name__}: {exc}"
                    if ok:
                        rec.update(status="SAVED", output=str(job["dest"]), error="")
                    else:
                        rec.update(status="FAILED", error=error)

        for job in jobs:
            if not job["rec"]["status"]:
                job["rec"].update(status="STOPPED" if summary["stopped"] else "FAILED")
                job["rec"]["error"] = "已停止" if summary["stopped"] else "沒有收到處理結果"
            emit(job)
    except Exception as exc:  # noqa: BLE001
        summary["fatal"] = f"{type(exc).__name__}: {exc}"
        log_error({"output_dir": str(dst_root)}, f"proxygraphics crashed: {summary['fatal']}")
        log("ERROR", summary["fatal"])
    finally:
        end = "批次結束" if not summary["stopped"] else "批次結束（已停止）"
        if summary["fatal"]:
            end = f"批次結束（{summary['fatal']}）"
        try:
            textbatch._log_line(text_log, end)
            text_log.close()
            csv_file.close()
            jsonl_file.close()
        except Exception:  # noqa: BLE001
            pass
        for temp in [*temps, lisp_path]:
            if temp is None:
                continue
            try:
                Path(temp).unlink(missing_ok=True)
            except OSError:
                pass
        summary["elapsed"] = round(time.time() - started, 2)
    return summary

"""Batch-enable PROXYGRAPHICS on copied DWGs through accoreconsole."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import fontfix
import proxygraphics
import textbatch
from common import decode_console, log_error
from convert_single import safe_tmpdir, sha256


def _empty_summary() -> dict:
    return {
        "dwg": 0, "ok": 0, "failed": 0, "skipped": 0, "warnings": 0,
        "stopped": False, "fatal": "", "failed_files": [], "elapsed": 0.0,
        "results": [],
    }


def _backup_output(job: dict, backup_root: Path) -> str:
    """Copy an existing output aside before it is replaced. Returns an error or ``""``."""
    dest = job["dest"]
    if not dest.exists():
        job["dest_existed"] = False
        return ""
    if not dest.is_file():
        return "輸出位置不是檔案，無法備份"
    try:
        digest = sha256(dest)
        backup = backup_root / f"{uuid.uuid4().hex}_{dest.name}"
        shutil.copy2(dest, backup)
        if sha256(backup) != digest:
            return "備份既有輸出後雜湊不一致"
    except OSError as exc:
        return f"無法備份既有輸出：{exc}"
    job.update(dest_existed=True, dest_sha_before=digest, backup=backup)
    return ""


def _drop_backup(job: dict, log=None) -> None:
    backup = job.get("backup")
    if not backup:
        return
    try:
        Path(backup).unlink(missing_ok=True)
        job.pop("backup", None)
    except OSError as exc:
        if log:
            log("WARN", f"備份檔刪除失敗，保留：{backup}（{exc}）")


def _restore_backup(job: dict) -> tuple[bool, str]:
    """Copy the backup back over the output. On failure the backup is kept."""
    backup = job.get("backup")
    dest = job["dest"]
    if not backup or not Path(backup).is_file():
        return False, "找不到備份檔"
    try:
        shutil.copy2(backup, dest)
        if sha256(dest) != job.get("dest_sha_before"):
            return False, "還原後雜湊不一致"
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"
    try:
        Path(backup).unlink(missing_ok=True)
        job.pop("backup", None)
    except OSError:
        pass
    return True, ""


def _rollback_publish(job: dict) -> str:
    """Best-effort restore after publish failed; returns a suffix for the error message."""
    dest = job["dest"]
    backup = job.get("backup")
    if job.get("dest_existed"):
        try:
            intact = dest.is_file() and sha256(dest) == job.get("dest_sha_before")
        except OSError:
            intact = False
        if intact:
            _drop_backup(job)
            return ""
        ok, error = _restore_backup(job)
        if ok:
            return "；已還原原輸出"
        return f"；還原失敗，備份保留於 {backup}：{error}"
    try:
        if dest.exists():
            dest.unlink()
            return "；已刪除未完成輸出"
    except OSError as exc:
        return f"；無法刪除未完成輸出：{exc}"
    return ""


def _verify_published(dest: Path, verify_root: Path, accore: str, timeout: int) -> dict:
    """Reopen a copy of the published DWG and read PROXYGRAPHICS back."""
    token = uuid.uuid4().hex[:10]
    copy_path = verify_root / f"out_{token}.dwg"
    script = verify_root / f"verify_{token}.scr"
    try:
        before = sha256(dest)
        shutil.copy2(dest, copy_path)
        script.write_bytes(proxygraphics.build_verify_script(copy_path).encode("ascii"))
        command = [str(accore), "/s", str(script), "/l", "en-US"]
        if str(accore).lower().endswith(".py"):
            command.insert(0, sys.executable)
        try:
            proc = subprocess.run(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "value": None, "error": f"最終輸出驗證逾時（{timeout} 秒）"}
        text = decode_console(proc.stdout) + decode_console(proc.stderr)
        parsed = proxygraphics.parse_verify_output(text)
        if proc.returncode != 0:
            return {"ok": False, "value": parsed["value"],
                    "error": f"驗證 accoreconsole 結束碼 {proc.returncode}"}
        if sha256(dest) != before:
            return {"ok": False, "value": parsed["value"], "error": "驗證期間最終輸出被修改"}
        if parsed["errors"]:
            return {"ok": False, "value": parsed["value"], "error": "；".join(parsed["errors"])}
        if parsed["value"] != 1:
            return {"ok": False, "value": parsed["value"],
                    "error": f"最終輸出 PROXYGRAPHICS={parsed['value']}，不是 1"}
        return {"ok": True, "value": 1, "error": ""}
    except OSError as exc:
        return {"ok": False, "value": None, "error": f"無法驗證最終輸出：{exc}"}
    finally:
        for path in (copy_path, script):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


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
    """Write verified ``PROXYGRAPHICS=1`` copies under ``output_dir``.

    Success requires the published file to be reopened with accoreconsole and read
    back as 1. Existing outputs are backed up before replacement and restored when
    that verification fails.
    """
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
    for job in jobs:
        job["rec"].update(save="NOT_RUN", verify="NOT_RUN", output_value="")
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
    backup_root = None
    verify_root = None

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
            note = (f"已輸出 {name}（PROXYGRAPHICS: {rec.get('before')} -> 1；"
                    f"輸出驗證={rec.get('output_value')}）")
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
            try:
                backup_root = Path(tempfile.mkdtemp(
                    prefix="proxygraphics_backup_", dir=str(safe_tmpdir(logs))))
                verify_root = Path(tempfile.mkdtemp(
                    prefix="proxygraphics_verify_", dir=str(safe_tmpdir(logs))))
            except OSError as exc:
                raise RuntimeError(f"無法建立代理圖形驗證暫存區：{exc}") from exc
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
                    continue
                if parsed["errors"]:
                    rec.update(status="FAILED", error="；".join(parsed["errors"]))
                    continue
                if parsed["before"] is None:
                    rec.update(status="FAILED", error="沒有取得原始 PROXYGRAPHICS 值")
                    continue
                if parsed["after"] != 1:
                    rec.update(status="FAILED", error="沒有確認暫存副本的 PROXYGRAPHICS 已設為 1")
                    continue

                save_required = parsed["before"] != 1
                if not save_required:
                    rec["save"] = "NOT_REQUIRED"
                elif parsed.get("save") == "OK":
                    rec["save"] = "OK"
                else:
                    note = parsed.get("save_note") or "QSAVE 失敗或沒有收到存檔結果"
                    rec.update(status="FAILED", save="FAILED", error=note)
                    continue

                if save_required:
                    try:
                        changed = sha256(job["tmp_in"]) != job.get("sha_before")
                    except OSError:
                        changed = False
                    if not changed:
                        # Auxiliary evidence only, never the sole reason to fail:
                        # the final output verification decides success.
                        textbatch._log_line(
                            text_log,
                            f"輔助檢查：{Path(rec['file']).name} 存檔標記為 OK，但暫存檔雜湊未變；"
                            "仍以最終輸出重新開啟結果為準",
                        )

                backup_error = _backup_output(job, backup_root)
                if backup_error:
                    rec.update(status="FAILED", error=backup_error)
                    continue
                try:
                    ok, error = textbatch._publish(job)
                except Exception as exc:  # noqa: BLE001
                    ok, error = False, f"{type(exc).__name__}: {exc}"
                if not ok:
                    rec.update(status="FAILED", error=f"{error}{_rollback_publish(job)}")
                    continue

                verified = _verify_published(job["dest"], verify_root, accore, timeout)
                rec["verify"] = "PASS" if verified["ok"] else "FAIL"
                rec["output_value"] = verified["value"] if verified["value"] is not None else ""
                if verified["ok"]:
                    rec.update(status="SAVED", error="")
                    _drop_backup(job, log)
                    continue
                detail = verified["error"]
                if job.get("dest_existed"):
                    restored, restore_error = _restore_backup(job)
                    if restored:
                        detail += "；已還原原輸出"
                    else:
                        detail += f"；還原失敗，備份保留於 {job.get('backup')}：{restore_error}"
                else:
                    try:
                        if job["dest"].exists():
                            job["dest"].unlink()
                            detail += "；已刪除未驗證輸出"
                        else:
                            detail += "；未驗證輸出不存在"
                    except OSError as exc:
                        detail += f"；無法刪除未驗證輸出：{exc}"
                rec.update(status="FAILED", error=detail)

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
        keep_backups = [
            job for job in jobs
            if job.get("backup") and Path(job["backup"]).exists()
        ]
        if keep_backups:
            log("WARN", "保留輸出備份：" + "；".join(str(job["backup"]) for job in keep_backups))
        elif backup_root is not None:
            shutil.rmtree(backup_root, ignore_errors=True)
        if verify_root is not None:
            shutil.rmtree(verify_root, ignore_errors=True)
        for temp in [*temps, lisp_path]:
            if temp is None:
                continue
            try:
                Path(temp).unlink(missing_ok=True)
            except OSError:
                pass
        summary["elapsed"] = round(time.time() - started, 2)
    return summary

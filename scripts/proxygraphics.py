"""Generate and parse AutoCAD scripts that enable saved proxy graphics."""
from __future__ import annotations

from pathlib import Path

import fontfix

MARKER_SEP = "|=|"


def build_lisp() -> str:
    """Return AutoLISP that sets and verifies ``PROXYGRAPHICS`` for one drawing."""
    return f'''(defun pg-emit (s)
  (princ (strcat "\\n" s "\\n"))
  (princ))

(defun pg-run (/ olderr)
  (setq olderr *error*)
  (defun *error* (msg)
    (pg-emit (strcat "PROXYGRAPHICS_ERROR{MARKER_SEP}" msg))
    (setq *error* olderr)
    (princ))
  (setq *pg-before* (getvar "PROXYGRAPHICS"))
  ;; Setting the same value can still mark the drawing dirty.
  (if (/= *pg-before* 1)
    (setvar "PROXYGRAPHICS" 1))
  (setq *pg-after* (getvar "PROXYGRAPHICS"))
  (pg-emit (strcat "PROXYGRAPHICS_RESULT{MARKER_SEP}" (itoa *pg-before*)
                   "{MARKER_SEP}" (itoa *pg-after*)))
  (setq *error* olderr)
  (princ))

(princ "\\nPROXYGRAPHICS_READY\\n")
(princ)
'''


def build_script(jobs: list[dict], lisp_path: str | Path) -> str:
    """Build one ASCII script that opens, updates, and saves every temp DWG."""
    lisp = str(lisp_path).replace("\\", "/")
    lines = [
        "FILEDIA", "0", "CMDDIA", "0", "EXPERT", "5", "SECURELOAD", "0",
    ]
    for index, job in enumerate(jobs):
        lines.extend([
            "_.OPEN",
            f'"{job["spath"]}"',
            f'(princ (strcat "\\nDWGB" "ATCH_OPEN_{index}|" (getvar "DWGNAME") "|\\n"))',
            f'(load "{lisp}")',
            "(if (and (boundp 'pg-run) pg-run) (pg-run) "
            f'(princ (strcat "\\nPROXYGRAPHICS_" "ERROR{MARKER_SEP}lisp not loaded\\n")))',
            '(if (and (boundp \'*pg-before*) (/= *pg-before* 1)) (command "_.QSAVE") '
            '(if (> (getvar "DBMOD") 0) (command "_.CLOSE" "N")))',
            f'(princ (strcat "\\nDWGB" "ATCH_DONE_{index}_\\n"))',
        ])
    lines.append("_.QUIT")
    text = "\n".join(lines) + "\n"
    fontfix.script_bytes(text)
    return text


def parse_output(text: str) -> dict:
    """Parse the result markers for a single drawing."""
    ready = False
    before = None
    after = None
    errors: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line == "PROXYGRAPHICS_READY":
            ready = True
        elif line.startswith("PROXYGRAPHICS_RESULT" + MARKER_SEP):
            fields = line.split(MARKER_SEP)
            if len(fields) >= 3:
                try:
                    before, after = int(fields[1]), int(fields[2])
                except ValueError:
                    errors.append("PROXYGRAPHICS 回傳值不是整數")
        elif line.startswith("PROXYGRAPHICS_ERROR" + MARKER_SEP):
            message = line.split(MARKER_SEP, 1)[1].strip()
            if message:
                errors.append(message)
    return {"ready": ready, "before": before, "after": after, "errors": errors}


CSV_FIELDS = ["檔案", "輸出", "狀態", "原設定", "新設定", "錯誤", "秒數"]
STATUS_TEXT = {"SAVED": "已輸出", "FAILED": "失敗", "SKIPPED": "略過", "STOPPED": "已停止"}


def csv_row(rec: dict) -> dict:
    return {
        "檔案": rec.get("file", ""),
        "輸出": rec.get("output", ""),
        "狀態": STATUS_TEXT.get(rec.get("status", ""), rec.get("status", "")),
        "原設定": rec.get("before", ""),
        "新設定": rec.get("after", ""),
        "錯誤": rec.get("error", ""),
        "秒數": rec.get("elapsed_seconds", 0),
    }

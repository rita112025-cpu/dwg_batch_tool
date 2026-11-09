"""Generate and parse AutoCAD scripts that enable saved proxy graphics."""
from __future__ import annotations

from pathlib import Path

import fontfix

MARKER_SEP = "|=|"
SAVE_MARKER = "PROXYGRAPHICS_SAVE"
VERIFY_MARKER = "PROXYGRAPHICS_VERIFY"


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


def build_save_commands() -> str:
    """Return the AutoLISP expression that QSAVEs the temp copy and reports the result.

    Kept as one line so AutoCAD's script reader executes it as one expression. When
    ``PROXYGRAPHICS`` was already 1 no save is needed; when QSAVE leaves ``DBMOD``
    nonzero the unsaved copy is closed without saving and the caller treats it as a
    failed save.
    """
    return (
        "(if (not (boundp '*pg-before*)) "
        '(princ "\\n' + SAVE_MARKER + MARKER_SEP + 'FAILED' + MARKER_SEP + 'no pg-run result\\n") '
        "(if (/= *pg-before* 1) "
        '(progn (command "_.QSAVE") '
        '(if (= (getvar "DBMOD") 0) '
        '(princ "\\n' + SAVE_MARKER + MARKER_SEP + 'OK\\n") '
        '(princ "\\n' + SAVE_MARKER + MARKER_SEP + 'FAILED\\n"))) '
        '(princ "\\n' + SAVE_MARKER + MARKER_SEP + 'NOT_REQUIRED\\n")))'
    )


def build_script(jobs: list[dict], lisp_path: str | Path) -> str:
    """Build one ASCII script that opens, updates, saves and closes every temp DWG."""
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
            build_save_commands(),
            '(if (> (getvar "DBMOD") 0) (command "_.CLOSE" "N"))',
            f'(princ (strcat "\\nDWGB" "ATCH_DONE_{index}_\\n"))',
        ])
    lines.append("_.QUIT")
    text = "\n".join(lines) + "\n"
    fontfix.script_bytes(text)
    return text


def build_verify_script(drawing_path: str | Path) -> str:
    """Return a read-only script that opens a copy and prints its PROXYGRAPHICS value."""
    path = str(drawing_path).replace("\\", "/")
    return f'''FILEDIA
0
CMDDIA
0
EXPERT
5
_.OPEN
"{path}"
(princ (strcat "\\n{VERIFY_MARKER}{MARKER_SEP}" (itoa (getvar "PROXYGRAPHICS")) "\\n"))
_.QUIT
'''


def parse_output(text: str) -> dict:
    """Parse the result markers for a single drawing."""
    ready = False
    before = None
    after = None
    save = None
    save_note = ""
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
        elif line.startswith(SAVE_MARKER + MARKER_SEP):
            fields = line.split(MARKER_SEP)
            if len(fields) >= 2:
                save = fields[1].strip()
            if len(fields) >= 3:
                save_note = MARKER_SEP.join(fields[2:]).strip()
        elif line.startswith("PROXYGRAPHICS_ERROR" + MARKER_SEP):
            message = line.split(MARKER_SEP, 1)[1].strip()
            if message:
                errors.append(message)
    return {"ready": ready, "before": before, "after": after,
            "save": save, "save_note": save_note, "errors": errors}


def parse_verify_output(text: str) -> dict:
    """Parse the PROXYGRAPHICS value printed after reopening a published copy."""
    value = None
    errors: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line.startswith(VERIFY_MARKER + MARKER_SEP):
            fields = line.split(MARKER_SEP)
            if len(fields) >= 2:
                try:
                    value = int(fields[1])
                except ValueError:
                    errors.append("最終輸出驗證值不是整數")
    if value is None:
        errors.append("沒有收到最終輸出驗證結果")
    return {"value": value, "errors": errors}


CSV_FIELDS = ["檔案", "輸出", "狀態", "原設定", "新設定", "存檔", "輸出驗證", "輸出值", "錯誤", "秒數"]
STATUS_TEXT = {"SAVED": "已輸出", "FAILED": "失敗", "SKIPPED": "略過", "STOPPED": "已停止"}
SAVE_TEXT = {"OK": "已存檔", "FAILED": "失敗", "NOT_REQUIRED": "不需要", "NOT_RUN": "未執行"}
VERIFY_TEXT = {"PASS": "通過", "FAIL": "失敗", "NOT_RUN": "未執行"}


def csv_row(rec: dict) -> dict:
    return {
        "檔案": rec.get("file", ""),
        "輸出": rec.get("output", ""),
        "狀態": STATUS_TEXT.get(rec.get("status", ""), rec.get("status", "")),
        "原設定": rec.get("before", ""),
        "新設定": rec.get("after", ""),
        "存檔": SAVE_TEXT.get(rec.get("save", ""), rec.get("save", "")),
        "輸出驗證": VERIFY_TEXT.get(rec.get("verify", ""), rec.get("verify", "")),
        "輸出值": rec.get("output_value", ""),
        "錯誤": rec.get("error", ""),
        "秒數": rec.get("elapsed_seconds", 0),
    }

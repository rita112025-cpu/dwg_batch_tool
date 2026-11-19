"""GUI-independent model: task list, outcome vocabulary and result classification.

No tkinter import, so it can be imported and tested on any platform. app.py re-exports every name."""
import re
from pathlib import Path

import layerstd


# ---------- one outcome vocabulary: 成功 / 略過 / 警告 / 失敗 (+ running / idle) ----------
# Every state has a text label AND a symbol, so meaning never depends on colour alone.
# Colours: dark text on a light tint (contrast well above 4.5:1); bar colours are mid-tone fills.
STATE = {
    "success": dict(label="成功", symbol="✓", bg="#E2F3E5", fg="#14532D", bar="#2E7D32"),
    "skipped": dict(label="略過", symbol="–", bg="#E9EAEC", fg="#3B3F45", bar="#7A8088"),
    "warning": dict(label="警告", symbol="⚠", bg="#FFF0C7", fg="#5F4100", bar="#C77C00"),
    "failed": dict(label="失敗", symbol="✗", bg="#FADADA", fg="#7A1212", bar="#C62828"),
    "running": dict(label="執行中", symbol="…", bg="#E1ECF8", fg="#123A66", bar="#2F6FB3"),
    "idle": dict(label="尚未執行", symbol="–", bg="#EEEFF1", fg="#3B3F45", bar="#7A8088"),
}
LOG_STATE = {"OK": "success", "INFO": "idle", "WARN": "warning", "ERROR": "failed"}   # backend log level -> state
LOG_TEXT = {"OK": "✓ 成功", "INFO": "· 資訊", "WARN": "⚠ 警告", "ERROR": "✗ 失敗"}
LOG_FG = {"success": "#1B6E2C", "warning": "#8A5A00", "failed": "#B3261E"}                # on white, contrast >= 5.5:1

PRIMARY_RESULTS = {
    "version": ("csv/dwg_versions.csv",),
    "fontfix": ("logs/fontfix_results.csv",),
    "proxygraphics": ("logs/proxygraphics_results.csv",),
    "textextract": ("logs/textextract_hits.csv",),
    "textreplace": ("logs/textreplace_changes.csv",),
    "layerclass": ("logs/layerclass_moves.csv",),
    "dwg": ("csv/scada_hits.csv", "csv/file_index.csv"),
    "analysis": ("cross_reference/cross_reference.csv", "database/project.db"),
    "diff": ("dwgdiff/diff_summary.csv",),
    "layerstd": ("layer_check/" + layerstd.HTML_NAME,),
    "tb_read": ("titleblock_attrs.csv",),
    "tb_write": ("logs/titleblock_results.csv",),
}


# ---------- selectable jobs ----------
# One entry per task. The order here is the order the checkboxes appear in and the order a batch runs.
TASKS = (
    ("scan", "掃描資料夾"),
    ("version", "DWG / DWT 版本表"),
    ("fontfix", "缺字體修復"),
    ("proxygraphics", "Revit 代理圖形"),
    ("layerclass", "自動分圖層"),
    ("textextract", "文字擷取"),
    ("textreplace", "文字取代"),
    ("dwg", "DWG 轉 DXF + 解析"),
    ("analysis", "工程資料分析"),
    ("diff", "差異比對"),
    ("layerstd", "圖層標準檢查"),
    ("tb_read", "圖框屬性匯出"),
    ("tb_write", "圖框屬性寫回"),
)
TASK_LABELS = dict(TASKS)
NUMBERS = "①②③④⑤⑥⑦⑧⑨"
# Jobs that always need accoreconsole; the shared start button stays usable without AutoCAD for the others.
# diff and layerstd read DXF without AutoCAD and only report DWG files that cannot be converted.
NEEDS_ACCORE = frozenset({"fontfix", "proxygraphics", "layerclass", "textextract", "textreplace", "dwg",
                          "tb_read", "tb_write"})
SEVERITY = {"success": 0, "skipped": 1, "warning": 2, "failed": 3}


class TaskError(Exception):
    """A task cannot start: title + the Traditional Chinese reason shown to the user."""

    def __init__(self, title, message):
        super().__init__(message)
        self.title = title
        self.message = message


def job_number(index):
    """①..⑨ for batch rows; plain digits if a batch ever grows past nine tasks."""
    return NUMBERS[index - 1] if 0 < index <= len(NUMBERS) else str(index)


def get_primary_result(mode, output_dir):
    """Return the first existing primary result for *mode*, or None."""
    out = Path(output_dir)
    for rel in PRIMARY_RESULTS.get(mode, ()):
        candidate = out / rel
        if candidate.is_file():
            return candidate
    return None


def classify_outcome(s):
    """Run summary -> (state, one-line verdict). Partial failure is a warning, not a failure and not a success."""
    failed = int(s.get("failed", 0) or 0) + int(s.get("analyze_failed", 0) or 0)
    ok = int(s.get("ok", 0) or 0)
    if s.get("fatal"):
        return "failed", "處理中止（嚴重錯誤）"
    if s.get("stopped"):
        return "warning", "已停止（結果不完整）"
    if failed or s.get("failed_files"):
        if ok > 0 or int(s.get("skipped", 0) or 0) > 0:
            return "warning", "部分完成（Completed with warnings）：其餘檔案已完成，失敗的檔案見下方"
        return "failed", "全部失敗：沒有任何檔案處理成功"
    if int(s.get("warnings", 0) or 0):
        return "warning", "已完成，但有檔案需要檢視（缺少選用套件或需 OCR）"
    if ok == 0 and int(s.get("skipped", 0) or 0):
        return "skipped", "全部略過（輸出已存在）"
    return "success", "全部完成"


_CODES = (
    ("DXF_INVALID", "corrupt", "此 DWG 無法開啟或已損毀；請在 AutoCAD 開啟後另存，再重跑"),
    ("DXF_INVALID", "timeout", "轉檔逾時；圖面可能過大或 AutoCAD 沒有回應"),
    ("DXF_INVALID", "", "轉出的 DXF 無效（詳見 errors.log）"),
    ("SKIPPED_DEPENDENCY", "", "缺少選用套件，已略過（安裝方式見 README）"),
    ("UNSUPPORTED_FORMAT", "", "不支援的檔案格式"),
    ("SOURCE_MODIFIED", "", "來源檔案在處理中被改變，結果不可信"),
    ("FAILED", "", "處理失敗（詳見 errors.log）"),
    ("ERROR", "", "處理失敗（詳見 errors.log）"),
)
_CODE_RE = re.compile(r"\b(DXF_INVALID|SKIPPED_DEPENDENCY|UNSUPPORTED_FORMAT|SOURCE_MODIFIED|FAILED|ERROR)\b")


def friendly_failure(item):
    """'C:\\x\\a.dwg: DXF_INVALID ...' -> 'a.dwg — 此 DWG 無法開啟…'. Raw text stays in errors.log."""
    text = str(item)
    m = _CODE_RE.search(text)
    if not m:
        return text
    name = text[:m.start()].rstrip(": ").strip()
    prefix = ""
    for tag, label in (("(analyze)", "分析階段"), ("(search)", "搜尋階段")):
        if name.startswith(tag):
            prefix, name = label + "：", name[len(tag):].strip()
    name = re.split(r"[\\/]", name)[-1] or name
    lowered = text.lower()
    for code, needle, message in _CODES:
        if code == m.group(1) and (not needle or needle in lowered):
            return f"{prefix}{name} — {message}"
    return f"{prefix}{name} — {text[m.start():]}"

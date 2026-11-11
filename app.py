"""SCADA DWG Analyzer - tkinter GUI. Heavy work runs in a worker thread; the UI only reads a Queue."""
import json
import os
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

ROOT = Path(__file__).resolve().parent
# ezdxf's compiled .pyd accelerators are unsigned and Windows App Control blocks them; use pure Python.
os.environ.setdefault("EZDXF_DISABLE_C_EXT", "1")
sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402
import convert_single  # noqa: E402
import env_check  # noqa: E402
import pipeline  # noqa: E402
import dwg_version  # noqa: E402
import fontfix  # noqa: E402
import fontfix_batch  # noqa: E402
import proxybatch  # noqa: E402
import dwgtext  # noqa: E402
import textbatch  # noqa: E402
import layerclass  # noqa: E402
import layerbatch  # noqa: E402
import dwgdiff  # noqa: E402
import layerstd  # noqa: E402
import parallel  # noqa: E402
import titleblock  # noqa: E402
import titleblock_batch  # noqa: E402

try:  # crisp text on high-DPI Windows displays
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:  # noqa: BLE001
    pass

try:  # own taskbar identity, so Windows shows our icon instead of pythonw's
    ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("SCADA.DWGAnalyzer")
except Exception:  # noqa: BLE001
    pass

ICON = ROOT / "dwg_analysis.ico"


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


class ProgressBar(tk.Canvas):
    """Progress bar whose fill colour follows the run outcome (ttk's native bar cannot be recoloured)."""

    def __init__(self, master, variable):
        super().__init__(master, height=18, highlightthickness=1, highlightbackground="#B8BCC2", bg="#F1F2F4")
        self._var, self._color = variable, STATE["running"]["bar"]
        variable.trace_add("write", lambda *a: self._draw())
        self.bind("<Configure>", lambda e: self._draw())

    def set_color(self, color):
        self._color = color
        self._draw()

    def _draw(self):
        self.delete("all")
        try:
            pct = max(0.0, min(100.0, float(self._var.get() or 0)))
        except (tk.TclError, ValueError):
            pct = 0.0
        if pct > 0:
            self.create_rectangle(0, 0, self.winfo_width() * pct / 100.0, self.winfo_height(), fill=self._color, width=0)


class App:
    def __init__(self, root):
        self.root = root
        root.title("SCADA DWG Analyzer")
        root.geometry("1000x760")
        root.minsize(900, 650)
        self.cfg = common.load_config()
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._can_stop = False
        self._closing = False
        self._close_polling = False
        self._close_started = 0.0
        self._children_released = False
        self.env_ok = False
        self.summary = None
        self.analysis_inputs = []
        # Multi-select: one BooleanVar per task. v_mode stays as the "most recently checked task" and
        # keeps single-selection callers (and tests) working when they set it programmatically.
        self.v_modes = {key: tk.BooleanVar(value=key == "version") for key, _label in TASKS}
        self.v_mode = tk.StringVar(value="version")
        self._mode_synced = "version"
        self._batch = None
        self._batch_out = ""
        self._preparers = {
            "scan": self._prepare_scan, "version": self._prepare_versions,
            "fontfix": self._prepare_fontfix, "proxygraphics": self._prepare_proxygraphics,
            "layerclass": self._prepare_layerclass, "textextract": self._prepare_extract,
            "textreplace": self._prepare_replace, "dwg": self._prepare_dwg,
            "analysis": self._prepare_analysis, "diff": self._prepare_diff,
            "layerstd": self._prepare_layerstd, "tb_read": self._prepare_tb_read,
            "tb_write": self._prepare_tb_write,
        }
        self._runners = {
            "scan": self._run_scan, "version": self._run_versions,
            "fontfix": self._run_fontfix, "proxygraphics": self._run_proxygraphics,
            "layerclass": self._run_layerclass, "textextract": self._run_extract,
            "textreplace": self._run_replace, "dwg": self._run_dwg,
            "analysis": self._run_analysis, "diff": self._run_diff,
            "layerstd": self._run_layerstd, "tb_read": self._run_tb_read,
            "tb_write": self._run_tb_write,
        }
        self._renderers = {
            "scan": self._render_scan, "version": self._render_versions,
            "fontfix": self._render_fontfix, "proxygraphics": self._render_proxygraphics,
            "layerclass": self._render_layerclass, "textextract": self._render_extract,
            "textreplace": self._render_replace, "dwg": self._render_dwg,
            "analysis": self._render_analysis, "diff": self._render_diff,
            "layerstd": self._render_layerstd, "tb_read": self._render_tb_read,
            "tb_write": self._render_tb_write,
        }

        self.v_in = tk.StringVar(value=str(common.resolve(self.cfg, "input_dir")))
        self.v_out = tk.StringVar(value=str(common.resolve(self.cfg, "output_dir")))
        self.opts = {k: tk.BooleanVar(value=v) for k, v in dict(
            recursive=self.cfg.get("recursive", True), convert=True, analyze=True, scada=True,
            csv=True, json=True, overwrite=self.cfg.get("overwrite", False)).items()}
        self.v_workers = tk.StringVar(value=str(parallel.clamp_workers(self.cfg.get("workers", 1))))
        self.v_progress = tk.DoubleVar(value=0)
        self.v_ptext = tk.StringVar(value="")
        self.v_current = tk.StringVar(value="")
        self.v_scan = tk.StringVar(value="")
        self.v_env = {k: tk.StringVar(value=f"{k}: ...") for k in
                      ("Python", "ezdxf", "AutoCAD", "accoreconsole")}
        self._build()
        self.root.after(100, self._poll)
        self._check_env()

    # ---------- layout ----------
    # Five numbered sections in one scrollable column. Only the selected job's settings are visible.
    def _build(self):
        pad = dict(padx=8, pady=4)
        shell = ttk.Frame(self.root)
        shell.pack(fill="both", expand=True)
        self.form_canvas = tk.Canvas(shell, highlightthickness=0)
        scrollbar = ttk.Scrollbar(shell, orient='vertical', command=self.form_canvas.yview)
        scrollbar.pack(side='right', fill='y')
        self.form_canvas.pack(side='left', fill='both', expand=True)
        self.form_canvas.configure(yscrollcommand=scrollbar.set)
        main = ttk.Frame(self.form_canvas, padding=8)
        self.form_body = main
        window = self.form_canvas.create_window((0, 0), window=main, anchor='nw')
        main.bind('<Configure>', lambda event: self.form_canvas.configure(scrollregion=self.form_canvas.bbox('all')))
        self.form_canvas.bind('<Configure>', lambda event: self.form_canvas.itemconfigure(window, width=event.width))

        def scroll(event):
            if not isinstance(event.widget, (tk.Text, tk.Listbox)):
                self.form_canvas.yview_scroll(-int(event.delta / 120), 'units')
        self.root.bind('<MouseWheel>', scroll)
        self.root.bind_all('<FocusIn>', self._on_focus_in, add='+')   # Tab to an off-screen control scrolls to it

        # ---- 1 基本設定 ----
        src = ttk.LabelFrame(main, text="① 基本設定")
        src.pack(fill="x", **pad)
        env = ttk.Frame(src)
        env.pack(fill="x", padx=6, pady=(4, 0))
        self.v_env_summary = tk.StringVar(value="環境：Python …　AutoCAD …　accoreconsole …")
        ttk.Label(env, textvariable=self.v_env_summary, justify="left").pack(side="left", fill="x", expand=True)
        self.btn_env_details = ttk.Button(env, text="環境詳細資訊 ▸", command=self._toggle_env_details)
        self.btn_env_details.pack(side="left", padx=4)
        self.btn_recheck = ttk.Button(env, text="重新檢查", command=self._check_env)
        self.btn_recheck.pack(side="left", padx=(4, 0))

        self.env_details = ttk.Frame(src)
        self.env_labels = {}
        for k in self.v_env:
            lb = ttk.Label(self.env_details, textvariable=self.v_env[k], wraplength=800, justify="left")
            lb.pack(fill="x", padx=6, pady=1)
            self.env_labels[k] = lb
        self.v_accore = tk.StringVar(value="")
        self.lbl_accore = ttk.Label(self.env_details, textvariable=self.v_accore, wraplength=800, justify="left")
        self.lbl_accore.pack(fill="x", padx=6, pady=(1, 4))
        self._wrap_to_width(self.lbl_accore, self.env_details)

        paths = ttk.Frame(src)
        paths.pack(fill="x", padx=6, pady=2)
        self.browse_buttons = []
        for r, (label, var) in enumerate((("輸入資料夾", self.v_in), ("輸出資料夾", self.v_out))):
            ttk.Label(paths, text=label, width=12).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(paths, textvariable=var).grid(row=r, column=1, sticky="ew", pady=3, padx=4)
            button = ttk.Button(paths, text="瀏覽", command=lambda v=var: self._browse(v))
            button.grid(row=r, column=2, padx=4)
            self.browse_buttons.append(button)
        paths.columnconfigure(1, weight=1)
        workers_row = ttk.Frame(src)
        workers_row.pack(fill="x", padx=6, pady=(0, 4))
        ttk.Label(workers_row, text="同時處理數", width=12).pack(side="left")
        self.workers_spin = ttk.Spinbox(workers_row, from_=1, to=parallel.MAX_WORKERS, width=5,
                                        textvariable=self.v_workers)
        self.workers_spin.pack(side="left", padx=4)
        ttk.Label(workers_row, text="同時開啟的 accoreconsole 數（1–8，預設 1；按「儲存設定」才寫入 config.json）",
                  justify="left").pack(side="left")

        # ---- 2 我要做什麼（可多選） ----
        selector = ttk.LabelFrame(main, text="② 我要做什麼？（可多選）")
        selector.pack(fill="x", **pad)
        mode_row = ttk.Frame(selector)
        mode_row.pack(fill="x", padx=6, pady=6)
        self.mode_buttons = []
        for i, (value, text) in enumerate(TASKS):
            button = ttk.Checkbutton(mode_row, text=text, variable=self.v_modes[value],
                                     command=lambda key=value: self._on_mode_toggle(key))
            button.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=2)
            self.mode_buttons.append(button)
        quick_row = ttk.Frame(selector)
        quick_row.pack(fill="x", padx=6, pady=(0, 2))
        self.btn_select_all = ttk.Button(quick_row, text="全選", width=8, command=self._select_all_modes)
        self.btn_select_all.pack(side="left")
        self.btn_clear_all = ttk.Button(quick_row, text="全不選", width=8, command=self._clear_all_modes)
        self.btn_clear_all.pack(side="left", padx=(6, 0))
        run_row = ttk.Frame(selector)
        run_row.pack(fill="x", padx=6, pady=(0, 6))
        self.btn_run_selected = ttk.Button(run_row, text="開始所選任務", command=self._start_selected)
        self.btn_run_selected.pack(side="left")
        self.lbl_selection = ttk.Label(run_row, text="", justify="left", wraplength=700)
        self.lbl_selection.pack(side="left", padx=(8, 0))
        self._wrap_to_width(self.lbl_selection, run_row, margin=220)

        # ---- 3 任務設定（勾選幾項就顯示幾項） ----
        mode_box = ttk.LabelFrame(main, text="③ 任務設定（顯示所有已勾選的任務）")
        mode_box.pack(fill="x", **pad)
        self.mode_frame = ttk.Frame(mode_box)
        self.mode_frame.pack(fill="x", padx=6, pady=6)
        self.mode_frames = {}
        self.mode_empty = ttk.Frame(self.mode_frame)
        ttk.Label(self.mode_empty, text="請在②勾選至少一項任務。",
                  justify="left").pack(anchor="w")

        scan = ttk.Frame(self.mode_frame)
        self.mode_frames["scan"] = scan
        ttk.Label(scan, text="掃描資料夾", font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11)).pack(anchor="w")
        ttk.Label(scan, text="掃描輸入資料夾中的工程檔案，不修改任何來源檔案。", justify="left").pack(anchor="w", pady=(2, 6))
        ttk.Checkbutton(scan, text="遞迴搜尋子資料夾", variable=self.opts["recursive"]).pack(anchor="w")
        self.btn_scan = ttk.Button(scan, text="開始掃描", command=self._scan)
        self.btn_scan.pack(anchor="w", pady=(8, 0))

        version = ttk.Frame(self.mode_frame)
        self.mode_frames["version"] = version
        ttk.Label(version, text="DWG / DWT 版本表", font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11)).pack(anchor="w")
        ttk.Label(version, text="直接讀取 DWG / DWT 檔頭版本碼。\n不需要 AutoCAD、不修改圖檔。",
                  justify="left").pack(anchor="w", pady=(2, 6))
        ttk.Checkbutton(version, text="遞迴搜尋子資料夾", variable=self.opts["recursive"]).pack(anchor="w")
        self.btn_versions = ttk.Button(version, text="產生版本表", command=self._scan_versions)
        self.btn_versions.pack(anchor="w", pady=(8, 0))

        dwg = ttk.Frame(self.mode_frame)
        self.mode_frames["dwg"] = dwg
        ttk.Label(dwg, text="DWG 轉 DXF + 解析", font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11)).pack(anchor="w")
        ttk.Label(dwg, text="使用 accoreconsole 將 DWG 轉 DXF，再執行圖面解析及 SCADA 搜尋。",
                  justify="left").pack(anchor="w", pady=(2, 6))
        grid = ttk.Frame(dwg)
        grid.pack(fill="x")
        items = [("recursive", "遞迴搜尋子資料夾"), ("convert", "DWG → DXF"),
                 ("analyze", "DXF 內容解析"), ("scada", "SCADA 關鍵字搜尋"),
                 ("csv", "產生 CSV"), ("json", "產生 JSON"), ("overwrite", "覆寫既有 DXF")]
        for i, (k, t) in enumerate(items):
            ttk.Checkbutton(grid, text=t, variable=self.opts[k]).grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 24), pady=2)

        self.kw_toggle = ttk.Button(dwg, text="進階設定：SCADA 關鍵字 ▸", command=self._toggle_keywords)
        self.kw_toggle.pack(anchor="w", pady=(8, 2))
        self.kw_frame = ttk.Frame(dwg)
        self.kw_text = tk.Text(self.kw_frame, height=4, width=30, font=("Consolas", 10))
        self.kw_text.pack(side="left", fill="both", expand=True, pady=4)
        self.kw_text.insert("1.0", "\n".join(self.cfg.get("keywords", [])))
        self.kw_text.bind("<Tab>", lambda e: (e.widget.tk_focusNext().focus_set(), "break")[1])
        self.kw_text.bind("<Shift-Tab>", lambda e: (e.widget.tk_focusPrev().focus_set(), "break")[1])
        self.btn_save_config = ttk.Button(self.kw_frame, text="儲存設定", command=self._save_config)
        self.btn_save_config.pack(side="right", padx=8)
        self.btn_start = ttk.Button(dwg, text="開始處理 DWG", command=self._start)
        self.btn_start.pack(anchor="w", pady=(8, 0))

        analysis = ttk.Frame(self.mode_frame)
        self.mode_frames["analysis"] = analysis
        ttk.Label(analysis, text="工程資料分析", font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11)).pack(anchor="w")
        ttk.Label(analysis, text="分析 DXF、IFC、Excel/CSV、PDF、DOCX、Navisworks。\n遇到 DWG 時才可能需要 AutoCAD。",
                  justify="left").pack(anchor="w", pady=(2, 6))
        self.v_analysis = tk.StringVar(value="使用上方輸入資料夾，或選擇多個檔案")
        ttk.Label(analysis, text="目前來源：").pack(anchor="w")
        la = ttk.Label(analysis, textvariable=self.v_analysis, wraplength=800, justify="left")
        la.pack(fill="x", pady=2)
        self._wrap_to_width(la, analysis)
        brow = ttk.Frame(analysis)
        brow.pack(fill="x", pady=2)
        self.btn_files = ttk.Button(brow, text="選擇檔案", command=self._select_analysis_files)
        self.btn_files.pack(side="left")
        self.btn_folder = ttk.Button(brow, text="使用輸入資料夾", command=self._analysis_folder)
        self.btn_folder.pack(side="left", padx=6)
        self.btn_analysis = ttk.Button(analysis, text="開始資料分析", command=self._start_analysis)
        self.btn_analysis.pack(anchor="w", pady=(6, 0))

        fontfix_box = ttk.Frame(self.mode_frame)
        self.mode_frames["fontfix"] = fontfix_box
        ttk.Label(fontfix_box, text="缺字體修復",
                  font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11)).pack(anchor="w")
        ttk.Label(
            fontfix_box,
            text=("使用上方的輸入資料夾與輸出資料夾。每個 DWG 與 DWT 會複製到輸出資料夾，"
                  "只替換找不到的文字樣式字體；已安裝的字體與 @ 開頭的直排字體保持原樣。\n"
                  "不會修改原始圖檔，修復後的樣板仍是 .dwt。正在 AutoCAD 中開啟的圖也會先複製再處理。"),
            justify="left", wraplength=760,
        ).pack(anchor="w", pady=(2, 6))
        ttk.Checkbutton(fontfix_box, text="包含子資料夾（輸出保留相同的資料夾結構）",
                        variable=self.opts["recursive"]).pack(anchor="w")
        self.v_ff_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(fontfix_box, text="覆寫既有的輸出 DWG / DWT",
                        variable=self.v_ff_overwrite).pack(anchor="w")
        form = ttk.Frame(fontfix_box)
        form.pack(fill="x", pady=(6, 0))
        self.v_ff_primary = tk.StringVar(value=fontfix.DEFAULT_PRIMARY)
        self.v_ff_big = tk.StringVar(value=fontfix.DEFAULT_BIG)
        ttk.Label(form, text="預設主字體", width=12).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(form, textvariable=self.v_ff_primary, width=24).grid(row=0, column=1, sticky="w", pady=2)
        ttk.Label(form, text="預設大字體", width=12).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(form, textvariable=self.v_ff_big, width=24).grid(row=1, column=1, sticky="w", pady=2)
        ttk.Label(form, text="另存版本", width=12).grid(row=2, column=0, sticky="w", pady=2)
        self.ff_version_box = ttk.Combobox(
            form, state="readonly", width=22,
            values=[label for _code, label in fontfix.DWG_SAVE_VERSIONS])
        self.ff_version_box.grid(row=2, column=1, sticky="w", pady=2)
        self.ff_version_box.current(0)
        form.columnconfigure(1, weight=1)
        ttk.Label(fontfix_box, text="字體對照（選填，每行 舊字體=新字體）").pack(anchor="w", pady=(8, 2))
        self.ff_map_text = tk.Text(fontfix_box, height=3, width=40, font=("Consolas", 10))
        self.ff_map_text.pack(fill="x", pady=(0, 4))
        self.ff_map_text.bind("<Tab>", lambda e: (e.widget.tk_focusNext().focus_set(), "break")[1])
        self.ff_map_text.bind("<Shift-Tab>", lambda e: (e.widget.tk_focusPrev().focus_set(), "break")[1])
        self.btn_fontfix = ttk.Button(fontfix_box, text="開始修復字體", command=self._start_fontfix)
        self.btn_fontfix.pack(anchor="w", pady=(4, 0))

        proxy_box = ttk.Frame(self.mode_frame)
        self.mode_frames["proxygraphics"] = proxy_box
        ttk.Label(
            proxy_box,
            text="Revit 代理圖形（PROXYGRAPHICS=1）",
            font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11),
        ).pack(anchor="w")
        ttk.Label(
            proxy_box,
            text=("每個 DWG 都先複製到暫存檔，再以 AutoCAD 設定 PROXYGRAPHICS=1 並存檔。\n"
                  "完成檔寫入輸出資料夾，原始 DWG 不會被修改；不會 EXPLODE 物件。"),
            justify="left",
            wraplength=760,
        ).pack(anchor="w", pady=(2, 6))
        ttk.Checkbutton(
            proxy_box, text="包含子資料夾", variable=self.opts["recursive"]
        ).pack(anchor="w")
        self.v_pg_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            proxy_box, text="覆寫既有的輸出 DWG", variable=self.v_pg_overwrite
        ).pack(anchor="w")
        self.btn_proxygraphics = ttk.Button(
            proxy_box, text="開始設定代理圖形", command=self._start_proxygraphics
        )
        self.btn_proxygraphics.pack(anchor="w", pady=(8, 0))

        extract = ttk.Frame(self.mode_frame)
        self.mode_frames["textextract"] = extract
        ttk.Label(extract, text="文字擷取",
                  font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11)).pack(anchor="w")
        ttk.Label(
            extract,
            text=("使用上方的輸入／輸出資料夾。只讀取 TEXT、MTEXT、ATTRIB、ATTDEF，不輸出 DWG、不修改原始圖檔。\n"
                  "AutoLISP 沒有正規表示式，所以先把文字清單印出來，再由 Python 做包含、萬用字元或正規表示式比對。"),
            justify="left", wraplength=760,
        ).pack(anchor="w", pady=(2, 6))
        ex_form = ttk.Frame(extract)
        ex_form.pack(fill="x")
        self.v_tx_pattern = tk.StringVar()
        ttk.Label(ex_form, text="搜尋文字", width=12).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(ex_form, textvariable=self.v_tx_pattern).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(ex_form, text="比對方式", width=12).grid(row=1, column=0, sticky="w", pady=2)
        self.tx_match_box = ttk.Combobox(
            ex_form, state="readonly", width=24,
            values=[label for _code, label in dwgtext.MATCH_MODES])
        self.tx_match_box.grid(row=1, column=1, sticky="w", pady=2)
        self.tx_match_box.current(0)
        ex_form.columnconfigure(1, weight=1)
        self.v_tx_case = tk.BooleanVar(value=False)
        self.v_tx_blocks = tk.BooleanVar(value=False)
        ttk.Checkbutton(extract, text="區分大小寫", variable=self.v_tx_case).pack(anchor="w")
        ttk.Checkbutton(extract, text="包含圖塊定義裡的文字（座標是圖塊內部座標）",
                        variable=self.v_tx_blocks).pack(anchor="w")
        ttk.Checkbutton(extract, text="包含子資料夾", variable=self.opts["recursive"]).pack(anchor="w")
        self.btn_extract = ttk.Button(extract, text="開始擷取文字", command=self._start_extract)
        self.btn_extract.pack(anchor="w", pady=(8, 0))

        replace = ttk.Frame(self.mode_frame)
        self.mode_frames["textreplace"] = replace
        ttk.Label(replace, text="文字取代",
                  font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11)).pack(anchor="w")
        ttk.Label(
            replace,
            text=("每個 DWG 先複製到暫存檔再修改，原始圖檔不會被改寫。變更後的 DWG 寫到輸出資料夾。\n"
                  "正規表示式的取代內容可用 \\1、\\2。範例：^(W\\d+)\\s.*$ 取代成 \\1，會留下假梁編號、去掉後面的尺寸。\n"
                  "MTEXT 會盡量保留格式碼；保留不了時改寫成純文字，並記在結果的錯誤欄。"),
            justify="left", wraplength=760,
        ).pack(anchor="w", pady=(2, 6))
        rp_form = ttk.Frame(replace)
        rp_form.pack(fill="x")
        self.v_rp_find = tk.StringVar()
        self.v_rp_repl = tk.StringVar()
        ttk.Label(rp_form, text="尋找", width=12).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(rp_form, textvariable=self.v_rp_find).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(rp_form, text="取代成", width=12).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(rp_form, textvariable=self.v_rp_repl).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(rp_form, text="比對方式", width=12).grid(row=2, column=0, sticky="w", pady=2)
        self.rp_mode_box = ttk.Combobox(
            rp_form, state="readonly", width=24,
            values=[label for _code, label in dwgtext.REPLACE_MODES])
        self.rp_mode_box.grid(row=2, column=1, sticky="w", pady=2)
        self.rp_mode_box.current(0)
        self.v_rp_color = tk.StringVar(value="1")
        ttk.Label(rp_form, text="ACI 色號", width=12).grid(row=3, column=0, sticky="w", pady=2)
        ttk.Entry(rp_form, textvariable=self.v_rp_color, width=8).grid(row=3, column=1, sticky="w", pady=2)
        rp_form.columnconfigure(1, weight=1)
        self.v_rp_case = tk.BooleanVar(value=False)
        self.v_rp_blocks = tk.BooleanVar(value=False)
        self.v_rp_recolor = tk.BooleanVar(value=False)
        self.v_rp_dry = tk.BooleanVar(value=False)
        self.v_rp_copy = tk.BooleanVar(value=False)
        self.v_rp_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(replace, text="區分大小寫", variable=self.v_rp_case).pack(anchor="w")
        ttk.Checkbutton(replace, text="包含圖塊定義裡的文字", variable=self.v_rp_blocks).pack(anchor="w")
        ttk.Checkbutton(replace, text="把變更過的文字改成上面的 ACI 色號（0=BYBLOCK，256=BYLAYER）",
                        variable=self.v_rp_recolor).pack(anchor="w")
        ttk.Checkbutton(replace, text="只產生變更清單，不輸出 DWG", variable=self.v_rp_dry).pack(anchor="w")
        ttk.Checkbutton(replace, text="沒有變更的檔案也複製到輸出資料夾", variable=self.v_rp_copy).pack(anchor="w")
        ttk.Checkbutton(replace, text="包含子資料夾（輸出保留相同的資料夾結構）",
                        variable=self.opts["recursive"]).pack(anchor="w")
        ttk.Checkbutton(replace, text="覆寫既有的輸出 DWG", variable=self.v_rp_overwrite).pack(anchor="w")
        self.btn_replace = ttk.Button(replace, text="開始取代文字", command=self._start_replace)
        self.btn_replace.pack(anchor="w", pady=(8, 0))

        layer_box = ttk.Frame(self.mode_frame)
        self.mode_frames["layerclass"] = layer_box
        ttk.Label(layer_box, text="自動分圖層",
                  font=(tkfont.nametofont("TkDefaultFont").actual("family"), 11)).pack(anchor="w")
        ttk.Label(
            layer_box,
            text=("圖面常常全部堆在圖層 0。這個模式依物件類型把實體搬到對應圖層，"
                  "新圖層顏色依 ACI 1 到 9 循環；已經存在的圖層不改顏色。\n"
                  "每個 DWG 先複製到暫存檔再處理，原始圖檔不會被改寫。"
                  "圖塊定義裡的物件預設不改。"),
            justify="left", wraplength=760,
        ).pack(anchor="w", pady=(2, 6))
        self.v_ly_scope = tk.StringVar(value="layer0")
        scope = ttk.Frame(layer_box)
        scope.pack(fill="x")
        for code, label in layerclass.SOURCE_MODES:
            ttk.Radiobutton(scope, text=label, value=code, variable=self.v_ly_scope).pack(side="left", padx=(0, 12))
        ly_form = ttk.Frame(layer_box)
        ly_form.pack(fill="x", pady=(4, 0))
        self.v_ly_sources = tk.StringVar(value="0")
        self.v_ly_prefix = tk.StringVar(value="")
        self.v_ly_other = tk.StringVar(value="")
        ttk.Label(ly_form, text="來源圖層", width=14).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(ly_form, textvariable=self.v_ly_sources).grid(row=0, column=1, sticky="ew", pady=2)
        ttk.Label(ly_form, text="圖層前綴", width=14).grid(row=1, column=0, sticky="w", pady=2)
        ttk.Entry(ly_form, textvariable=self.v_ly_prefix).grid(row=1, column=1, sticky="ew", pady=2)
        ttk.Label(ly_form, text="其他物件圖層", width=14).grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(ly_form, textvariable=self.v_ly_other).grid(row=2, column=1, sticky="ew", pady=2)
        ly_form.columnconfigure(1, weight=1)
        ttk.Label(layer_box, text="來源圖層只在「指定來源圖層」時使用，可用逗號分隔。其他物件圖層空白表示不搬。").pack(anchor="w")
        self.v_ly_paper = tk.BooleanVar(value=False)
        self.v_ly_blocks = tk.BooleanVar(value=False)
        self.v_ly_insert = tk.BooleanVar(value=True)
        self.v_ly_unlock = tk.BooleanVar(value=False)
        self.v_ly_keep = tk.BooleanVar(value=True)
        self.v_ly_dry = tk.BooleanVar(value=False)
        self.v_ly_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(layer_box, text="包含配置（紙空間）", variable=self.v_ly_paper).pack(anchor="w")
        ttk.Checkbutton(layer_box, text="也修改圖塊定義裡的物件（每個插入都會跟著變）",
                        variable=self.v_ly_blocks).pack(anchor="w")
        ttk.Checkbutton(layer_box, text="圖塊（INSERT）放到與圖塊有效名稱相同的圖層",
                        variable=self.v_ly_insert).pack(anchor="w")
        ttk.Checkbutton(layer_box, text="暫時解鎖鎖定圖層，搬完再鎖回（沒勾就略過，並在結果列出圖層）",
                        variable=self.v_ly_unlock).pack(anchor="w")
        ttk.Checkbutton(
            layer_box,
            text="保持原外觀：BYLAYER 的顏色與線型改成原圖層的明確值（取消則改跟著新圖層的顏色）",
            variable=self.v_ly_keep,
        ).pack(anchor="w")
        ttk.Checkbutton(layer_box, text="只產生清單，不輸出 DWG", variable=self.v_ly_dry).pack(anchor="w")
        ttk.Checkbutton(layer_box, text="包含子資料夾（輸出保留相同的資料夾結構）",
                        variable=self.opts["recursive"]).pack(anchor="w")
        ttk.Checkbutton(layer_box, text="覆寫既有的輸出 DWG", variable=self.v_ly_overwrite).pack(anchor="w")
        ttk.Label(layer_box, text="物件類型=圖層（空白圖層＝不搬；INSERT 不用寫在這裡）").pack(anchor="w", pady=(6, 2))
        self.layer_map_text = tk.Text(layer_box, height=6, width=40, font=("Consolas", 10))
        self.layer_map_text.pack(fill="x")
        self.layer_map_text.insert("1.0", layerclass.DEFAULT_MAP_TEXT)
        self.layer_map_text.bind("<Tab>", lambda e: (e.widget.tk_focusNext().focus_set(), "break")[1])
        self.layer_map_text.bind("<Shift-Tab>", lambda e: (e.widget.tk_focusPrev().focus_set(), "break")[1])
        self.btn_layer = ttk.Button(layer_box, text="開始分圖層", command=self._start_layerclass)
        self.btn_layer.pack(anchor="w", pady=(6, 0))

        title_font = (tkfont.nametofont("TkDefaultFont").actual("family"), 11)
        diff_box = ttk.Frame(self.mode_frame)
        self.mode_frames["diff"] = diff_box
        ttk.Label(diff_box, text="差異比對", font=title_font).pack(anchor="w")
        ttk.Label(
            diff_box,
            text=("比對兩版圖面：可選兩個檔案，或兩個資料夾（依相對路徑配對，副檔名不計）。\n"
                  "DWG 會先用 accoreconsole 轉成 DXF；DXF 直接讀取。原始圖檔不會被修改。\n"
                  "結果寫到輸出資料夾的 dwgdiff\\（diff_summary.csv、diff_details.csv、diff_report.json）。"),
            justify="left", wraplength=760,
        ).pack(anchor="w", pady=(2, 6))
        df_form = ttk.Frame(diff_box)
        df_form.pack(fill="x")
        self.v_df_old = tk.StringVar(value="")
        self.v_df_new = tk.StringVar(value="")
        self.v_df_tol = tk.StringVar(value="0.001")
        self.diff_browse_buttons = []
        for row, (label, var) in enumerate((("舊版（檔案或資料夾）", self.v_df_old),
                                            ("新版（檔案或資料夾）", self.v_df_new))):
            ttk.Label(df_form, text=label, width=20).grid(row=row, column=0, sticky="w", pady=2)
            ttk.Entry(df_form, textvariable=var).grid(row=row, column=1, sticky="ew", pady=2, padx=4)
            folder_btn = ttk.Button(df_form, text="資料夾", command=lambda v=var: self._browse(v))
            folder_btn.grid(row=row, column=2, padx=2)
            file_btn = ttk.Button(df_form, text="檔案", command=lambda v=var: self._browse_drawing(v))
            file_btn.grid(row=row, column=3, padx=2)
            self.diff_browse_buttons += [folder_btn, file_btn]
        ttk.Label(df_form, text="座標容差", width=20).grid(row=2, column=0, sticky="w", pady=2)
        ttk.Entry(df_form, textvariable=self.v_df_tol, width=12).grid(row=2, column=1, sticky="w", pady=2, padx=4)
        df_form.columnconfigure(1, weight=1)
        self.v_df_handles = tk.BooleanVar(value=True)
        self.v_df_paper = tk.BooleanVar(value=True)
        ttk.Checkbutton(diff_box, text="優先以 handle 配對物件（取消則只用類型＋圖層＋幾何）",
                        variable=self.v_df_handles).pack(anchor="w")
        ttk.Checkbutton(diff_box, text="包含配置（紙空間）", variable=self.v_df_paper).pack(anchor="w")
        ttk.Checkbutton(diff_box, text="包含子資料夾", variable=self.opts["recursive"]).pack(anchor="w")
        self.btn_diff = ttk.Button(diff_box, text="開始比對", command=self._start_diff)
        self.btn_diff.pack(anchor="w", pady=(8, 0))

        std_box = ttk.Frame(self.mode_frame)
        self.mode_frames["layerstd"] = std_box
        ttk.Label(std_box, text="圖層標準檢查", font=title_font).pack(anchor="w")
        ttk.Label(
            std_box,
            text=("依圖層標準檔（CSV 或 JSON）檢查每張圖的圖層名稱、顏色、線型、線寬、列印設定、"
                  "圖層 0 上的圖元、空圖層與必要圖層。\n"
                  "DXF 直接讀取；DWG 先用 accoreconsole 轉成 DXF。原始圖檔不會被改寫。"
                  "範例標準檔：samples\\layer_standard.csv"),
            justify="left", wraplength=760,
        ).pack(anchor="w", pady=(2, 6))
        std_form = ttk.Frame(std_box)
        std_form.pack(fill="x")
        self.v_ls_standard = tk.StringVar(value=str(ROOT / "samples" / "layer_standard.csv"))
        ttk.Label(std_form, text="圖層標準檔", width=14).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(std_form, textvariable=self.v_ls_standard).grid(row=0, column=1, sticky="ew", pady=2)
        self.btn_ls_browse = ttk.Button(std_form, text="瀏覽", command=self._browse_layer_standard)
        self.btn_ls_browse.grid(row=0, column=2, padx=4)
        std_form.columnconfigure(1, weight=1)
        self.v_ls_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(std_box, text="包含子資料夾（輸出保留相同的資料夾結構）",
                        variable=self.opts["recursive"]).pack(anchor="w")
        ttk.Checkbutton(std_box, text="重新轉換已存在的 DXF（layer_check\\dxf）",
                        variable=self.v_ls_overwrite).pack(anchor="w")
        self.btn_layerstd = ttk.Button(std_box, text="開始檢查圖層", command=self._start_layerstd)
        self.btn_layerstd.pack(anchor="w", pady=(6, 0))

        tb_read_box = ttk.Frame(self.mode_frame)
        self.mode_frames["tb_read"] = tb_read_box
        ttk.Label(tb_read_box, text="圖框屬性匯出", font=title_font).pack(anchor="w")
        ttk.Label(
            tb_read_box,
            text=("把符合名稱的圖框 Block 屬性寫成輸出資料夾裡的 titleblock_attrs.csv"
                  "（每個圖框一列，每個 tag 一欄），可用 Excel 編輯。不輸出 DWG、不修改原始圖檔。"),
            justify="left", wraplength=760,
        ).pack(anchor="w", pady=(2, 6))
        tb_form = ttk.Frame(tb_read_box)
        tb_form.pack(fill="x")
        self.v_tb_blocks = tk.StringVar(value="TITLE*, 圖框*")
        ttk.Label(tb_form, text="圖框 Block 名稱", width=14).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(tb_form, textvariable=self.v_tb_blocks).grid(row=0, column=1, sticky="ew", pady=2)
        tb_form.columnconfigure(1, weight=1)
        ttk.Label(tb_read_box, text="名稱可用 * 與 ?，多個以逗號分隔；動態圖塊以有效名稱比對。").pack(anchor="w")
        ttk.Checkbutton(tb_read_box, text="包含子資料夾", variable=self.opts["recursive"]).pack(anchor="w")
        self.btn_tb_read = ttk.Button(tb_read_box, text="開始匯出圖框屬性", command=self._start_tb_read)
        self.btn_tb_read.pack(anchor="w", pady=(6, 0))

        tb_write_box = ttk.Frame(self.mode_frame)
        self.mode_frames["tb_write"] = tb_write_box
        ttk.Label(tb_write_box, text="圖框屬性寫回", font=title_font).pack(anchor="w")
        ttk.Label(
            tb_write_box,
            text=("讀回編輯過的 titleblock_attrs.csv，只把和圖面不同的值寫回。每個 DWG 先複製到暫存檔處理，"
                  "原始圖檔不會被改寫，變更後的 DWG 寫到輸出資料夾。"),
            justify="left", wraplength=760,
        ).pack(anchor="w", pady=(2, 6))
        wb_form = ttk.Frame(tb_write_box)
        wb_form.pack(fill="x")
        self.v_tb_csv = tk.StringVar(value="")
        ttk.Label(wb_form, text="寫回用 CSV", width=14).grid(row=0, column=0, sticky="w", pady=2)
        ttk.Entry(wb_form, textvariable=self.v_tb_csv).grid(row=0, column=1, sticky="ew", pady=2)
        self.btn_tb_csv = ttk.Button(wb_form, text="選擇…", command=self._browse_tb_csv)
        self.btn_tb_csv.grid(row=0, column=2, padx=(6, 0), pady=2)
        wb_form.columnconfigure(1, weight=1)
        self.v_tb_dry = tk.BooleanVar(value=False)
        self.v_tb_overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(tb_write_box, text="只預覽變更，不輸出 DWG", variable=self.v_tb_dry).pack(anchor="w")
        ttk.Checkbutton(tb_write_box, text="覆寫既有的輸出 DWG", variable=self.v_tb_overwrite).pack(anchor="w")
        self.btn_tb_write = ttk.Button(tb_write_box, text="開始寫回圖框屬性", command=self._start_tb_write)
        self.btn_tb_write.pack(anchor="w", pady=(6, 0))

        # ---- 4 執行狀態 ----
        st = ttk.LabelFrame(main, text="④ 執行狀態")
        st.pack(fill="both", expand=True, **pad)
        pf = ttk.Frame(st)
        pf.pack(fill="x", padx=6, pady=(4, 0))
        self.progress_bar = ProgressBar(pf, self.v_progress)
        self.progress_bar.pack(side="left", fill="x", expand=True)
        ttk.Label(pf, textvariable=self.v_ptext, width=28).pack(side="left", padx=(8, 0))
        cur = ttk.Label(st, textvariable=self.v_current, anchor="w", wraplength=800, justify="left")
        cur.pack(fill="x", padx=8, pady=(3, 0))
        self._wrap_to_width(cur, st)
        self.btn_stop = ttk.Button(st, text="停止", command=self._stop, state="disabled")
        self.btn_stop.pack(anchor="w", padx=8, pady=(4, 2))
        self.log_toggle = ttk.Button(st, text="執行記錄 ▾", command=self._toggle_log)
        self.log_toggle.pack(anchor="w", padx=8, pady=(2, 0))
        self.log_frame = ttk.Frame(st)
        self.log_frame.pack(fill="both", expand=True, padx=6, pady=6)
        self.log_box = scrolledtext.ScrolledText(self.log_frame, height=7, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True)
        for state, color in LOG_FG.items():
            self.log_box.tag_configure(state, foreground=color)

        # ---- 5 結果 ----
        rf = ttk.LabelFrame(main, text="⑤ 結果")
        rf.pack(fill="x", **pad)
        self.result_frame = rf
        # outcome banner: symbol + word + colour. Regular weight: bold falls back to a synthetic face for some CJK glyphs.
        self.lbl_status = tk.Label(rf, anchor="w", justify="left", padx=10, pady=6, wraplength=700,
                                   font=(tkfont.nametofont("TkDefaultFont").actual("family"), 10))
        self.lbl_status.pack(fill="x", padx=8, pady=(4, 2))
        self._wrap_to_width(self.lbl_status, rf, margin=50)
        self._set_status("idle", "尚未執行")
        self.v_result = tk.StringVar(value="尚未執行")       # full summary text (first line = the verdict)
        self.v_result_body = tk.StringVar(value="尚未執行")  # what is shown under the banner: without the repeated verdict
        self.v_result.trace_add("write", lambda *a: self.v_result_body.set(self._result_body(self.v_result.get())))
        lr = ttk.Label(rf, textvariable=self.v_result_body, justify="left", wraplength=760)
        lr.pack(fill="x", padx=8, pady=2)
        self._wrap_to_width(lr, rf)
        quick = ttk.Frame(rf)
        quick.pack(fill="x", padx=8, pady=(4, 2))
        self.btn_output = ttk.Button(quick, text="開啟輸出資料夾", command=lambda: self._open(""), state="disabled")
        self.btn_output.pack(side="left")
        self.btn_primary = ttk.Button(quick, text="開啟主要結果", command=self._open_primary, state="disabled")
        self.btn_primary.pack(side="left", padx=6)
        self.btn_errors = ttk.Button(quick, text="開啟 errors.log", command=lambda: self._open("logs/errors.log"), state="disabled")
        self.btn_errors.pack(side="left")

        self.more_toggle = ttk.Button(rf, text="更多結果 ▸", command=self._toggle_more_results)
        self.more_toggle.pack(anchor="w", padx=8, pady=(4, 2))
        self.more_results = ttk.Frame(rf)
        groups = (
            ("共用", (("開啟 errors.log", "logs/errors.log"), ("開啟 dwg_versions.csv", "csv/dwg_versions.csv"))),
            ("缺字體修復", (("開啟 fontfix_results.csv", "logs/fontfix_results.csv"),
                       ("開啟 fontfix.log", "logs/fontfix.log"))),
            ("文字擷取", (("開啟 textextract_hits.csv", "logs/textextract_hits.csv"),
                      ("開啟 textextract_results.csv", "logs/textextract_results.csv"),
                      ("開啟 textextract.log", "logs/textextract.log"))),
            ("文字取代", (("開啟 textreplace_changes.csv", "logs/textreplace_changes.csv"),
                      ("開啟 textreplace_results.csv", "logs/textreplace_results.csv"),
                      ("開啟 textreplace.log", "logs/textreplace.log"))),
            ("自動分圖層", (("開啟 layerclass_moves.csv", "logs/layerclass_moves.csv"),
                       ("開啟 layerclass_results.csv", "logs/layerclass_results.csv"),
                       ("開啟 layerclass.log", "logs/layerclass.log"))),
            ("DWG 結果", (("開啟 scada_hits.csv", "csv/scada_hits.csv"), ("開啟 object_hits.csv", "csv/object_hits.csv"),
                         ("開啟 file_index.csv", "csv/file_index.csv"))),
            ("差異比對", (("開啟 diff_summary.csv", "dwgdiff/diff_summary.csv"),
                      ("開啟 diff_details.csv", "dwgdiff/diff_details.csv"),
                      ("開啟 diff_report.json", "dwgdiff/diff_report.json"))),
            ("圖層標準檢查", (("開啟 layer_check_report.html", "layer_check/" + layerstd.HTML_NAME),
                        ("開啟 layer_check_summary.csv", "layer_check/" + layerstd.SUMMARY_NAME),
                        ("開啟 layer_check_details.csv", "layer_check/layer_check_details.csv"))),
            ("圖框屬性", (("開啟 titleblock_attrs.csv", "titleblock_attrs.csv"),
                      ("開啟 titleblock_results.csv", "logs/titleblock_results.csv"),
                      ("開啟 titleblock.log", "logs/titleblock.log"))),
            ("工程分析結果", (("開啟 project.db", "database/project.db"),
                         ("開啟 cross_reference.csv", "cross_reference/cross_reference.csv"),
                         ("開啟 requirements.csv", "docx/requirements.csv"), ("開啟 boq_items.csv", "excel/boq_items.csv"),
                         ("開啟 ifc_objects.csv", "ifc/ifc_objects.csv"),
                         ("開啟 navis_clashes.csv", "navisworks/navis_clashes.csv"))),
        )
        self.output_buttons = {}
        self.analysis_outputs = {}
        self.result_buttons = [("", self.btn_output), ("logs/errors.log", self.btn_errors)]
        for title, files in groups:
            box = ttk.Frame(self.more_results)
            box.pack(fill="x", padx=8, pady=(2, 4))
            ttk.Label(box, text=title).grid(row=0, column=0, columnspan=3, sticky="w")
            for i, (text, rel) in enumerate(files):
                b = ttk.Button(box, text=text, command=lambda r=rel: self._open(r), state="disabled")
                b.grid(row=1 + i // 3, column=i % 3, sticky="ew", padx=(0, 6), pady=2)
                self.output_buttons[rel] = b
                self.result_buttons.append((rel, b))
                if title == "工程分析結果":
                    self.analysis_outputs[rel] = b
            for col in range(3):
                box.columnconfigure(col, weight=1, uniform=title)
        self.v_out.trace_add('write', lambda *args: self._refresh_analysis_outputs())
        self._refresh_mode_ui()
        self._refresh_analysis_outputs()

    def _wrap_to_width(self, label, container, margin=40):
        """Keep a wrapping label as wide as its container so text is never cut off."""
        container.bind('<Configure>', lambda e, lb=label, m=margin: lb.configure(wraplength=max(200, e.width - m)), add='+')

    def _on_focus_in(self, event):
        """Scroll the form so the focused control is visible (keyboard Tab must never land off-screen)."""
        w = event.widget
        try:
            if not str(w).startswith(str(self.form_body)):
                return
            canvas = self.form_canvas
            total = max(1, self.form_body.winfo_height())
            top = w.winfo_rooty() - self.form_body.winfo_rooty()
            bottom = top + w.winfo_height()
            view_top = canvas.canvasy(0)
            view_bottom = view_top + canvas.winfo_height()
            if top < view_top:
                canvas.yview_moveto(max(0.0, (top - 8) / total))
            elif bottom > view_bottom:
                canvas.yview_moveto(min(1.0, (bottom + 8 - canvas.winfo_height()) / total))
        except tk.TclError:
            pass

    def _reveal(self, widget):
        """Scroll a whole section (e.g. the results) into view."""
        try:
            self.root.update_idletasks()
            total = max(1, self.form_body.winfo_height())
            top = widget.winfo_rooty() - self.form_body.winfo_rooty()
            self.form_canvas.yview_moveto(max(0.0, min(1.0, (top - 8) / total)))
        except tk.TclError:
            pass

    def _toggle_env_details(self):
        if self.env_details.winfo_manager():
            self.env_details.pack_forget()
            self.btn_env_details.configure(text="環境詳細資訊 ▸")
        else:
            self.env_details.pack(fill="x", padx=6, pady=(2, 4), before=self.env_details.master.winfo_children()[-1])
            self.btn_env_details.configure(text="環境詳細資訊 ▾")

    def _toggle_keywords(self):
        if self.kw_frame.winfo_manager():
            self.kw_frame.pack_forget()
            self.kw_toggle.configure(text="進階設定：SCADA 關鍵字 ▸")
        else:
            self.kw_frame.pack(fill="x", pady=(0, 4), before=self.btn_start)
            self.kw_toggle.configure(text="進階設定：SCADA 關鍵字 ▾")

    def _toggle_log(self):
        if self.log_frame.winfo_manager():
            self.log_frame.pack_forget()
            self.log_toggle.configure(text="執行記錄 ▸")
        else:
            self.log_frame.pack(fill="both", expand=True, padx=6, pady=6)
            self.log_toggle.configure(text="執行記錄 ▾")

    def _toggle_more_results(self):
        if self.more_results.winfo_manager():
            self.more_results.pack_forget()
            self.more_toggle.configure(text="更多結果 ▸")
        else:
            self.more_results.pack(fill="x", after=self.more_toggle)
            self.more_toggle.configure(text="更多結果 ▾")

    def _selected_modes(self):
        """Task keys in UI order (the same order a batch runs them)."""
        return [key for key, _label in TASKS if self.v_modes[key].get()]

    def _on_mode_toggle(self, key):
        """A task checkbox changed: remember the most recently checked task for single-task callers."""
        if self.v_modes[key].get():
            self.v_mode.set(key)
        else:
            selected = self._selected_modes()
            if selected:
                self.v_mode.set(selected[-1])
        self._mode_synced = self.v_mode.get()
        self._refresh_mode_ui()

    def _set_all_modes(self, checked):
        """Tick or untick every task at once; the shared start button and ③ follow immediately."""
        for var in self.v_modes.values():
            var.set(checked)
        selected = self._selected_modes()
        if selected:
            self.v_mode.set(selected[-1])   # keep the single-task compatibility variable on a real task
        self._mode_synced = self.v_mode.get()
        self._refresh_mode_ui()

    def _select_all_modes(self):
        self._set_all_modes(True)

    def _clear_all_modes(self):
        self._set_all_modes(False)

    def _refresh_mode_ui(self):
        """Show the settings of every checked task without changing any saved/user-entered values.

        Setting ``v_mode`` programmatically (older callers) still selects exactly that one task.
        """
        current = self.v_mode.get()
        if current in self.v_modes and current != self._mode_synced:
            for key, var in self.v_modes.items():
                var.set(key == current)
            self._mode_synced = current
        selected = self._selected_modes()
        for frame in self.mode_frames.values():
            frame.pack_forget()
        self.mode_empty.pack_forget()
        if selected:
            for key in selected:
                self.mode_frames[key].pack(fill="x")
        else:
            self.mode_empty.pack(fill="x")
        self._refresh_selection_ui()
        self._set_busy_ui(self._busy())
        self._refresh_analysis_outputs()

    def _refresh_selection_ui(self):
        selected = self._selected_modes()
        count = len(selected)
        self.btn_run_selected.configure(text=f"開始所選任務（{count}）" if count else "開始所選任務")
        if not count:
            self.lbl_selection.configure(text="請至少勾選一項任務。")
        elif count == 1:
            self.lbl_selection.configure(text=f"將執行：{TASK_LABELS[selected[0]]}")
        else:
            self.lbl_selection.configure(
                text=f"已勾選 {count} 項，將依序執行：" + "、".join(TASK_LABELS[k] for k in selected))

    def _set_busy_ui(self, busy):
        """Centralised enable/disable policy for all job controls."""
        state = "disabled" if busy else "normal"
        for widget in (self.mode_buttons + self.browse_buttons + self.diff_browse_buttons +
                       [self.btn_select_all, self.btn_clear_all,
                        self.btn_scan, self.btn_versions, self.btn_analysis, self.btn_fontfix,
                        self.btn_proxygraphics,
                        self.btn_extract, self.btn_replace, self.btn_layer,
                        self.btn_diff, self.btn_layerstd, self.btn_ls_browse, self.btn_tb_csv,
                        self.btn_files, self.btn_folder, self.btn_recheck, self.btn_save_config,
                        self.workers_spin]):
            widget.configure(state=state)
        self.btn_start.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_fontfix.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_proxygraphics.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_extract.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_replace.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_layer.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_tb_read.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_tb_write.configure(state="normal" if not busy and self.env_ok else "disabled")
        selected = self._selected_modes()
        accore_needed = any(key in NEEDS_ACCORE for key in selected)
        self.btn_run_selected.configure(
            state="normal" if selected and not busy and (self.env_ok or not accore_needed) else "disabled")
        cancellable = busy and self._can_stop
        self.btn_stop.configure(state="normal" if cancellable else "disabled")
        self._refresh_analysis_outputs()

    def _get_primary_result(self):
        """Newest checked task first: one button still works when several tasks share an output folder."""
        out = self.v_out.get().strip()
        for key in reversed(self._selected_modes()):
            target = get_primary_result(key, out)
            if target is not None:
                return target
        return None

    def _open_primary(self):
        target = self._get_primary_result()
        if target is not None:
            os.startfile(str(target))  # noqa: S606 (Windows only)

    # ---------- helpers ----------
    def _select_analysis_files(self):
        paths = filedialog.askopenfilenames(title="選擇工程資料", filetypes=[("工程資料", "*.dwg *.dxf *.ifc *.xlsx *.xlsm *.csv *.pdf *.docx *.xml *.html"), ("所有檔案", "*.*")])
        if paths:
            self.analysis_inputs = list(paths)
            self.v_analysis.set(f"已選擇 {len(paths)} 個檔案")

    def _analysis_folder(self):
        self.analysis_inputs = []
        self.v_analysis.set("使用上方輸入資料夾")

    def _refresh_analysis_outputs(self):
        """One entry per output file: enabled only when it exists (folder button: when the folder exists) and no run is active."""
        out = Path(self.v_out.get().strip() or ".")
        for rel, button in self.result_buttons:
            target = out / rel if rel else out
            exists = target.is_dir() if not rel else target.is_file()
            button.configure(state="normal" if exists and not self._busy() else "disabled")
        primary = self._get_primary_result()
        self.btn_primary.configure(state="normal" if primary is not None and not self._busy() else "disabled")

    def _start_analysis(self):
        self._start_tasks(["analysis"])

    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or str(ROOT))
        if d:
            var.set(os.path.normpath(d))

    def _browse_drawing(self, var):
        path = filedialog.askopenfilename(title="選擇圖檔", filetypes=[("圖檔", "*.dwg *.dxf"), ("所有檔案", "*.*")])
        if path:
            var.set(os.path.normpath(path))

    def _browse_layer_standard(self):
        path = filedialog.askopenfilename(title="選擇圖層標準檔",
                                          filetypes=[("圖層標準", "*.csv *.json"), ("所有檔案", "*.*")])
        if path:
            self.v_ls_standard.set(os.path.normpath(path))

    def _browse_tb_csv(self):
        path = filedialog.askopenfilename(title="選擇 titleblock_attrs.csv",
                                          filetypes=[("CSV", "*.csv"), ("所有檔案", "*.*")])
        if path:
            self.v_tb_csv.set(os.path.normpath(path))

    def _keywords(self):
        return [l.strip() for l in self.kw_text.get("1.0", "end").splitlines() if l.strip()]

    def _workers(self):
        """Concurrent accoreconsole count from the ① field; anything invalid falls back to 1."""
        return parallel.clamp_workers(self.v_workers.get())

    def _accore(self):
        """Path for jobs that may use accoreconsole; empty when none was found (per-file failures then)."""
        return self.cfg.get("accoreconsole", "") if self.env_ok else ""

    def _current_cfg(self):
        return dict(self.cfg, input_dir=self.v_in.get().strip(), output_dir=self.v_out.get().strip(),
                    keywords=self._keywords(), workers=self._workers())

    def _save_config(self):
        cfg = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
        cfg.update(input_dir=self.v_in.get().strip(), output_dir=self.v_out.get().strip(),
                   keywords=self._keywords(), recursive=self.opts["recursive"].get(),
                   overwrite=self.opts["overwrite"].get(), workers=self._workers())
        try:
            (ROOT / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                                              encoding="utf-8")
            self.cfg = dict(self.cfg, **cfg)
            self._log("OK", "設定已儲存 config.json")
        except OSError as e:
            messagebox.showerror("儲存失敗", str(e))

    @staticmethod
    def _result_body(text):
        """The banner already states the verdict; show the rest of the summary (counts, failed files) below it."""
        lines = text.split("\n")
        rest = "\n".join(lines[1:]).lstrip("\n")
        return rest if len(lines) > 1 and rest else text

    def _set_status(self, state, text):
        """Outcome banner in the results section: symbol + word + colour (never colour alone)."""
        st = STATE[state]
        self.lbl_status.configure(text=f"{st['symbol']} {st['label']}　{text}" if state != "idle" else f"{st['symbol']} {text}",
                                  bg=st["bg"], fg=st["fg"])
        self.progress_bar.set_color(st["bar"])

    def _log(self, level, msg):
        import datetime
        state = LOG_STATE.get(level, "idle")
        line = f"[{datetime.datetime.now():%H:%M:%S}] {LOG_TEXT.get(level, level):<6} {msg}\n"
        self.log_box.configure(state="normal")
        self.log_box.insert("end", line, state if state in LOG_FG else ())
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _open(self, rel):
        p = Path(self.v_out.get().strip()) / rel if rel else Path(self.v_out.get().strip())
        if not p.exists():
            messagebox.showinfo("找不到檔案", f"尚未產生：\n{p}")
            return
        os.startfile(str(p))  # noqa: S606 (Windows only)

    # ---------- environment ----------
    def _check_env(self):
        for v in self.v_env.values():
            v.set(v.get().split(":")[0] + ": ...")
        self.btn_recheck.configure(state="disabled")
        self.btn_start.configure(state="disabled")

        def work():
            path, cands = env_check.find_accore(self.cfg)
            self.q.put(("env_paths", path, cands))
        threading.Thread(target=work, daemon=True).start()

    def _on_env_paths(self, path, cands):
        if not path and len(cands) > 1:
            path = self._choose_accore(cands)
        if path:
            self.cfg["accoreconsole"] = path

        def work():
            self.q.put(("env", env_check.check(self.cfg)))
        threading.Thread(target=work, daemon=True).start()

    def _choose_accore(self, cands):
        win = tk.Toplevel(self.root)
        win.title("選擇 accoreconsole.exe")
        win.transient(self.root)
        win.grab_set()
        ttk.Label(win, text="找到多個 accoreconsole.exe，請選擇：").pack(padx=10, pady=6)
        lb = tk.Listbox(win, width=80, height=min(8, len(cands)), exportselection=False)
        for c in cands:
            lb.insert("end", c)
        lb.selection_set(0)
        lb.pack(padx=10)
        result = {"v": ""}

        def ok():
            sel = lb.curselection()
            result["v"] = cands[sel[0]] if sel else ""
            win.destroy()
        ttk.Button(win, text="確定", command=ok).pack(pady=8)
        self.root.wait_window(win)
        return result["v"]

    def _on_env(self, r):
        def mark(ok):
            return "OK" if ok else "NOT FOUND"
        self.v_env["Python"].set(f"Python: {mark(bool(r['python']))} {r['python']}")
        self.v_env["ezdxf"].set(f"ezdxf: {mark(bool(r['ezdxf']))} {r['ezdxf']}")
        self.v_env["AutoCAD"].set(f"AutoCAD: {mark(r['accore_ok'])} {r['autocad']}")
        self.v_env["accoreconsole"].set(f"accoreconsole: {mark(r['accore_ok'])}")
        self.env_ok = r["ok"]
        self.v_env_summary.set(
            f"環境：Python {'✓' if r['python'] else '✗'}　"
            f"AutoCAD {'✓' if r['accore_ok'] else '✗'}　"
            f"accoreconsole {'✓' if r['accore_ok'] else '✗'}")
        for line in r["lines"]:
            self._log("INFO", line)
        if not r["accore_ok"]:
            self._log("ERROR", "accoreconsole NOT FOUND，無法開始處理")
        self.btn_recheck.configure(state="normal" if not self._busy() else "disabled")
        self._set_busy_ui(self._busy())
        self.v_accore.set(f"accoreconsole 路徑：{r['accore'] or '未設定'}")

    # ---------- task settings: validate -> run -> render ----------
    def _require_accore(self, action):
        if not self.env_ok:
            raise TaskError(f"無法{action}", "找不到可用的 accoreconsole；請先完成環境設定並重新檢查。")

    def _fontfix_version(self):
        label = self.ff_version_box.get()
        for code, text in fontfix.DWG_SAVE_VERSIONS:
            if text == label:
                return code
        return None

    def _combo_code(self, box, pairs):
        label = box.get()
        for code, text in pairs:
            if text == label:
                return code
        return None

    def _text_folders(self):
        src = self.v_in.get().strip()
        dst = self.v_out.get().strip()
        problem = fontfix.directory_problem(src, dst)
        if problem:
            raise TaskError("路徑錯誤", problem)
        return src, dst

    def _replace_color(self):
        if not self.v_rp_recolor.get():
            return None
        raw = self.v_rp_color.get().strip()
        if not raw.isdigit() or int(raw) > 256:
            raise TaskError("色號錯誤", "ACI 色號必須是 0 到 256。")
        return int(raw)

    # ---- prepare: read and validate the current widgets, or raise TaskError ----
    def _prepare_scan(self):
        src = self.v_in.get().strip()
        if not Path(src).is_dir():
            raise TaskError("輸入資料夾不存在", src or "請選擇輸入資料夾")
        return dict(input_dir=src, recursive=self.opts["recursive"].get())

    def _prepare_versions(self):
        src = self.v_in.get().strip()
        dst = self.v_out.get().strip()
        if not Path(src).is_dir():
            raise TaskError("輸入資料夾不存在", src or "請選擇輸入資料夾")
        if not dst:
            raise TaskError("請指定輸出資料夾", "")
        problem = fontfix.overlap_problem(src, dst)
        if problem:
            raise TaskError("路徑錯誤", problem)
        return dict(input_dir=src, output_dir=dst, recursive=self.opts["recursive"].get())

    def _prepare_fontfix(self):
        self._require_accore("修復字體")
        src, dst = self._text_folders()
        version = self._fontfix_version()
        if version is None:
            raise TaskError("版本錯誤", "不支援的 DWG 版本。")
        return dict(
            src=src, dst=dst, accore=self.cfg.get("accoreconsole", ""), version=version,
            primary=self.v_ff_primary.get().strip() or fontfix.DEFAULT_PRIMARY,
            big=self.v_ff_big.get().strip() or fontfix.DEFAULT_BIG,
            mapping=fontfix.parse_mapping(self.ff_map_text.get("1.0", "end")),
            recursive=self.opts["recursive"].get(), overwrite=self.v_ff_overwrite.get(),
            timeout=int(self.cfg.get("timeout_seconds", 300)), workers=self._workers(),
        )

    def _prepare_proxygraphics(self):
        self._require_accore("設定代理圖形")
        src, dst = self._text_folders()
        return dict(
            src=src, dst=dst, accore=self.cfg.get("accoreconsole", ""),
            recursive=self.opts["recursive"].get(), overwrite=self.v_pg_overwrite.get(),
            timeout=int(self.cfg.get("timeout_seconds", 300)),
        )

    def _prepare_extract(self):
        self._require_accore("擷取文字")
        src, dst = self._text_folders()
        mode = self._combo_code(self.tx_match_box, dwgtext.MATCH_MODES)
        problem = dwgtext.validate_rule(self.v_tx_pattern.get(), mode)
        if problem:
            raise TaskError("搜尋文字", problem)
        return dict(
            src=src, dst=dst, accore=self.cfg.get("accoreconsole", ""),
            pattern=self.v_tx_pattern.get(), mode=mode,
            case_sensitive=self.v_tx_case.get(), include_blocks=self.v_tx_blocks.get(),
            recursive=self.opts["recursive"].get(),
            timeout=int(self.cfg.get("timeout_seconds", 300)), workers=self._workers(),
        )

    def _prepare_replace(self):
        self._require_accore("取代文字")
        src, dst = self._text_folders()
        color = self._replace_color()
        mode = self._combo_code(self.rp_mode_box, dwgtext.REPLACE_MODES)
        find = self.v_rp_find.get()
        repl = self.v_rp_repl.get()
        problem = dwgtext.validate_rule(find, mode, replacement=repl, for_replace=True)
        if problem:
            raise TaskError("取代規則", problem)
        return dict(
            src=src, dst=dst, accore=self.cfg.get("accoreconsole", ""),
            find=find, repl=repl, mode=mode, color=color,
            case_sensitive=self.v_rp_case.get(), include_blocks=self.v_rp_blocks.get(),
            dry_run=self.v_rp_dry.get(), copy_unchanged=self.v_rp_copy.get(),
            overwrite=self.v_rp_overwrite.get(), recursive=self.opts["recursive"].get(),
            timeout=int(self.cfg.get("timeout_seconds", 300)), workers=self._workers(),
        )

    def _prepare_layerclass(self):
        self._require_accore("分圖層")
        src, dst = self._text_folders()
        type_map = self.layer_map_text.get("1.0", "end")
        _settings, _notes, rule_error = layerclass.compile_rules(
            type_map, self.v_ly_prefix.get(), self.v_ly_other.get(),
            self.v_ly_scope.get(), self.v_ly_sources.get(), self.v_ly_insert.get(),
        )
        if rule_error:
            raise TaskError("分圖層設定", rule_error)
        return dict(
            src=src, dst=dst, accore=self.cfg.get("accoreconsole", ""), type_map=type_map,
            prefix=self.v_ly_prefix.get(), other=self.v_ly_other.get(),
            source_mode=self.v_ly_scope.get(), source_layers=self.v_ly_sources.get(),
            include_paper=self.v_ly_paper.get(), include_blocks=self.v_ly_blocks.get(),
            insert_by_name=self.v_ly_insert.get(), unlock_locked=self.v_ly_unlock.get(),
            keep_appearance=self.v_ly_keep.get(), dry_run=self.v_ly_dry.get(),
            overwrite=self.v_ly_overwrite.get(), recursive=self.opts["recursive"].get(),
            timeout=int(self.cfg.get("timeout_seconds", 300)), workers=self._workers(),
        )

    def _prepare_dwg(self):
        self._require_accore("處理 DWG")
        cfg = self._current_cfg()
        if not Path(cfg["input_dir"]).is_dir():
            raise TaskError("輸入資料夾不存在", cfg["input_dir"])
        if not cfg["output_dir"]:
            raise TaskError("請指定輸出資料夾", "")
        problem = fontfix.overlap_problem(cfg["input_dir"], cfg["output_dir"])
        if problem:
            raise TaskError("路徑錯誤", problem)
        if not cfg["keywords"] and self.opts["scada"].get():
            raise TaskError("沒有關鍵字", "SCADA 搜尋需要至少一個關鍵字")
        return dict(cfg=cfg, opts={key: var.get() for key, var in self.opts.items()})

    def _prepare_analysis(self):
        cfg = self._current_cfg()
        cfg["recursive"] = self.opts["recursive"].get()
        inputs = self.analysis_inputs or [cfg["input_dir"]]
        if not cfg["output_dir"] or any(not Path(p).exists() for p in inputs):
            raise TaskError("路徑錯誤", "請選擇存在的輸入檔案／資料夾，並指定輸出資料夾。")
        for item in inputs:
            problem = fontfix.overlap_problem(item, cfg["output_dir"])
            if problem:
                raise TaskError("路徑錯誤", problem)
        return dict(cfg=cfg, inputs=inputs)

    def _prepare_diff(self):
        old, new = self.v_df_old.get().strip(), self.v_df_new.get().strip()
        dst = self.v_out.get().strip()
        if not old or not new or not dst:
            raise TaskError("路徑錯誤", "請指定舊版、新版與輸出資料夾。")
        if not Path(old).exists() or not Path(new).exists():
            raise TaskError("路徑錯誤", "舊版或新版的檔案／資料夾不存在。")
        # dwgdiff writes converted DXFs under the output folder; inside an input tree a later
        # run would scan them as inputs.
        for label, path in (("舊版", old), ("新版", new)):
            problem = fontfix.directory_problem(path, dst) if Path(path).is_dir() else fontfix.overlap_problem(path, dst)
            if problem:
                raise TaskError("路徑錯誤", f"{label}：{problem}")
        raw = self.v_df_tol.get().strip()
        try:
            tolerance = float(raw)
        except ValueError:
            raise TaskError("座標容差錯誤", "座標容差必須是數字。") from None
        if tolerance < 0:
            raise TaskError("座標容差錯誤", "座標容差不可為負數。")
        return dict(
            old=old, new=new, dst=dst, accore=self._accore(), tolerance=tolerance,
            recursive=self.opts["recursive"].get(), use_handles=self.v_df_handles.get(),
            include_paper=self.v_df_paper.get(), timeout=int(self.cfg.get("timeout_seconds", 300)),
        )

    def _prepare_layerstd(self):
        src, dst = self._text_folders()
        standard = self.v_ls_standard.get().strip()
        try:
            layerstd.load_standard(standard)
        except (OSError, ValueError, re.error) as exc:
            raise TaskError("圖層標準檔", str(exc)) from None
        return dict(
            src=src, dst=dst, standard=standard, accore=self._accore(),
            recursive=self.opts["recursive"].get(), overwrite=self.v_ls_overwrite.get(),
            timeout=int(self.cfg.get("timeout_seconds", 300)),
        )

    def _prepare_tb_read(self):
        self._require_accore("匯出圖框屬性")
        src, dst = self._text_folders()
        patterns = self.v_tb_blocks.get()
        problem = titleblock.validate_patterns(patterns)
        if problem:
            raise TaskError("圖框 Block 名稱", problem)
        return dict(
            src=src, dst=dst, accore=self.cfg.get("accoreconsole", ""), patterns=patterns,
            recursive=self.opts["recursive"].get(), timeout=int(self.cfg.get("timeout_seconds", 300)),
        )

    def _prepare_tb_write(self):
        self._require_accore("寫回圖框屬性")
        src, dst = self._text_folders()
        csv_path = self.v_tb_csv.get().strip()
        if not csv_path:
            raise TaskError("寫回圖框屬性", "請選擇編輯過的 titleblock_attrs.csv（CSV 檔）")
        return dict(
            src=src, dst=dst, accore=self.cfg.get("accoreconsole", ""), csv_path=csv_path,
            dry_run=self.v_tb_dry.get(), overwrite=self.v_tb_overwrite.get(),
            timeout=int(self.cfg.get("timeout_seconds", 300)),
        )

    # ---- run: called on the worker thread; never touches tkinter ----
    def _run_scan(self, params, log, progress):
        return pipeline.scan(params["input_dir"], params["recursive"])

    def _run_versions(self, params, log, progress):
        csv_path = Path(params["output_dir"]) / "csv" / "dwg_versions.csv"
        rows = dwg_version.write_version_csv(
            [Path(params["input_dir"])], csv_path, recursive=params["recursive"])
        ok = sum(row["status"] == "OK" for row in rows)
        unknown = sum(row["status"] == "UNKNOWN_CODE" for row in rows)
        return dict(csv=str(csv_path), total=len(rows), ok=ok, unknown=unknown,
                    invalid=len(rows) - ok - unknown,
                    dwt=sum(row["extension"] == ".dwt" for row in rows))

    def _run_fontfix(self, params, log, progress):
        return fontfix_batch.run(
            params["src"], params["dst"], params["accore"], recursive=params["recursive"],
            primary=params["primary"], big=params["big"], mapping=params["mapping"],
            version=params["version"], overwrite=params["overwrite"], timeout=params["timeout"],
            workers=params["workers"], stop_event=self.stop_event, progress=progress, log=log,
        )

    def _run_proxygraphics(self, params, log, progress):
        return proxybatch.run(
            params["src"], params["dst"], params["accore"], recursive=params["recursive"],
            overwrite=params["overwrite"], timeout=params["timeout"],
            stop_event=self.stop_event, progress=progress, log=log,
        )

    def _run_extract(self, params, log, progress):
        return textbatch.run_extract(
            params["src"], params["dst"], params["accore"], pattern=params["pattern"],
            mode=params["mode"], case_sensitive=params["case_sensitive"],
            include_blocks=params["include_blocks"], recursive=params["recursive"],
            timeout=params["timeout"], workers=params["workers"],
            stop_event=self.stop_event, progress=progress, log=log,
        )

    def _run_replace(self, params, log, progress):
        return textbatch.run_replace(
            params["src"], params["dst"], params["accore"], find=params["find"],
            repl=params["repl"], mode=params["mode"], case_sensitive=params["case_sensitive"],
            include_blocks=params["include_blocks"], color=params["color"],
            dry_run=params["dry_run"], copy_unchanged=params["copy_unchanged"],
            recursive=params["recursive"], overwrite=params["overwrite"],
            timeout=params["timeout"], workers=params["workers"],
            stop_event=self.stop_event, progress=progress, log=log,
        )

    def _run_layerclass(self, params, log, progress):
        return layerbatch.run(
            params["src"], params["dst"], params["accore"], type_map=params["type_map"],
            prefix=params["prefix"], other=params["other"], source_mode=params["source_mode"],
            source_layers=params["source_layers"], include_paper=params["include_paper"],
            include_blocks=params["include_blocks"], insert_by_name=params["insert_by_name"],
            unlock_locked=params["unlock_locked"], keep_appearance=params["keep_appearance"],
            dry_run=params["dry_run"], recursive=params["recursive"],
            overwrite=params["overwrite"], timeout=params["timeout"], workers=params["workers"],
            stop_event=self.stop_event, progress=progress, log=log,
        )

    def _run_dwg(self, params, log, progress):
        return pipeline.run_pipeline(params["cfg"], params["opts"], log=log, progress=progress,
                                     stop_event=self.stop_event)

    def _run_analysis(self, params, log, progress):
        return pipeline.run_pipeline(params["cfg"], dict(multiformat=True, inputs=params["inputs"]),
                                     log=log, progress=progress, stop_event=self.stop_event)

    def _run_diff(self, params, log, progress):
        return dwgdiff.run(
            params["old"], params["new"], params["dst"], params["accore"], tolerance=params["tolerance"],
            recursive=params["recursive"], use_handles=params["use_handles"],
            include_paper=params["include_paper"], timeout=params["timeout"],
            stop_event=self.stop_event, on_progress=progress, log=log,
        )

    def _run_layerstd(self, params, log, progress):
        return layerstd.run(
            params["src"], params["dst"], params["standard"], params["accore"],
            recursive=params["recursive"], overwrite=params["overwrite"], timeout=params["timeout"],
            stop_event=self.stop_event, progress=progress, log=log,
        )

    def _run_tb_read(self, params, log, progress):
        return titleblock_batch.run_read(
            params["src"], params["dst"], params["accore"], patterns=params["patterns"],
            recursive=params["recursive"], timeout=params["timeout"],
            stop_event=self.stop_event, progress=progress, log=log,
        )

    def _run_tb_write(self, params, log, progress):
        return titleblock_batch.run_write(
            params["src"], params["dst"], params["accore"], csv_path=params["csv_path"],
            dry_run=params["dry_run"], overwrite=params["overwrite"], timeout=params["timeout"],
            stop_event=self.stop_event, progress=progress, log=log,
        )

    # ---- render: turn one finished result into (state, verdict, body) ----
    @staticmethod
    def _failed_files_block(items):
        items = list(items or [])
        if not items:
            return ""
        shown = "\n".join(f"  - {friendly_failure(item)}" for item in items[:8])
        if len(items) > 8:
            shown += f"\n  ... 另有 {len(items) - 8} 個（見 errors.log）"
        return f"\n\n失敗檔案：\n{shown}"

    def _render_scan(self, result):
        if result.get("fatal"):
            self.v_scan.set("")
            return "failed", "掃描失敗", f"錯誤：{result['fatal']}"
        self.v_scan.set(
            f"找到 DWG：{result['dwg']}｜DXF：{result['dxf']}｜總容量：{pipeline.fmt_size(result['size'])}｜"
            f"資料夾：{result['folders']}｜中文檔名：{result['chinese']}｜空白路徑：{result['spaces']}｜"
            f"重複檔名：{result['duplicates']}")
        self._log("INFO", self.v_scan.get())
        total = result["dwg"] + result["dxf"]
        return "success", "掃描完成", (
            f"檔案：{total}　DWG：{result['dwg']}　DXF：{result['dxf']}　資料夾：{result['folders']}")

    def _render_versions(self, result):
        if result.get("fatal"):
            self.v_scan.set("")
            return "failed", "版本表失敗", f"錯誤：{result['fatal']}"
        self.v_scan.set(
            f"版本表完成：{result['total']} 檔（DWT {result['dwt']}）｜已辨識：{result['ok']}｜"
            f"未知代碼：{result['unknown']}｜異常：{result['invalid']}")
        self._log("OK", f"DWG/DWT 版本表已輸出：{result['csv']}")
        state = "warning" if result["unknown"] or result["invalid"] else "success"
        return state, "版本表完成", (
            f"檔案：{result['total']}　已辨識：{result['ok']}　未知代碼：{result['unknown']}　"
            f"異常：{result['invalid']}　DWT：{result['dwt']}\n結果：{result['csv']}")

    def _render_fontfix(self, result):
        if result.get("dwg", 0) == 0 and not result.get("fatal") and not result.get("stopped"):
            state, verdict = "warning", "沒有找到 DWG 或 DWT"
        else:
            state, verdict = classify_outcome(result)
        lines = [
            f"DWG/DWT：{result.get('dwg', 0)}　成功：{result.get('ok', 0)}　"
            f"失敗：{result.get('failed', 0)}　略過：{result.get('skipped', 0)}",
            f"替換樣式：{result.get('styles_replaced', 0)}　"
            f"存檔後仍缺字：{result.get('warnings', 0)}",
            "結果：logs/fontfix_results.csv",
        ]
        if result.get("fatal"):
            lines.append(f"錯誤：{result['fatal']}")
        still = result.get("still_files") or []
        if still:
            shown = "\n".join(f"  - {item}" for item in still[:8])
            if len(still) > 8:
                shown += f"\n  ... 另有 {len(still) - 8} 個"
            lines.append(f"仍缺字：\n{shown}")
        return state, verdict, "\n".join(lines) + self._failed_files_block(result.get("failed_files"))

    def _render_proxygraphics(self, result):
        if result.get("dwg", 0) == 0 and not result.get("fatal") and not result.get("stopped"):
            state, verdict = "warning", "沒有找到 DWG"
        else:
            state, verdict = classify_outcome(result)
        lines = [
            f"DWG：{result.get('dwg', 0)}　成功：{result.get('ok', 0)}　"
            f"失敗：{result.get('failed', 0)}　略過：{result.get('skipped', 0)}",
            "結果：logs/proxygraphics_results.csv",
        ]
        if result.get("fatal"):
            lines.append(f"錯誤：{result['fatal']}")
        return state, verdict, "\n".join(lines) + self._failed_files_block(result.get("failed_files"))

    def _render_text_result(self, result, detail_line, result_path):
        if result.get("dwg", 0) == 0 and not result.get("fatal") and not result.get("stopped"):
            state, verdict = "warning", "沒有找到 DWG"
        else:
            state, verdict = classify_outcome(result)
        lines = [
            f"DWG：{result.get('dwg', 0)}　成功：{result.get('ok', 0)}　"
            f"失敗：{result.get('failed', 0)}　略過：{result.get('skipped', 0)}",
            detail_line,
            f"結果：{result_path}",
        ]
        if result.get("fatal"):
            lines.append(f"錯誤：{result['fatal']}")
        return state, verdict, "\n".join(lines) + self._failed_files_block(result.get("failed_files"))

    def _render_extract(self, result):
        return self._render_text_result(result, f"符合筆數：{result.get('hits', 0)}",
                                        "logs/textextract_hits.csv")

    def _render_replace(self, result):
        return self._render_text_result(
            result,
            f"變更筆數：{result.get('changes', 0)}　無變更：{result.get('unchanged', 0)}",
            "logs/textreplace_changes.csv")

    def _render_layerclass(self, result):
        if result.get("dwg", 0) == 0 and not result.get("fatal") and not result.get("stopped"):
            state, verdict = "warning", "沒有找到 DWG"
        else:
            state, verdict = classify_outcome(result)
        lines = [
            f"DWG：{result.get('dwg', 0)}　成功：{result.get('ok', 0)}　"
            f"失敗：{result.get('failed', 0)}　略過：{result.get('skipped', 0)}",
            f"搬移數量：{result.get('moved', 0)}　新建圖層：{result.get('created', 0)}　"
            f"無變更：{result.get('unchanged', 0)}",
            "結果：logs/layerclass_moves.csv",
        ]
        if result.get("fatal"):
            lines.append(f"錯誤：{result['fatal']}")
        return state, verdict, "\n".join(lines) + self._failed_files_block(result.get("failed_files"))

    def _render_dwg(self, result):
        state, verdict = classify_outcome(result)
        lines = [
            f"DWG：{result.get('dwg', 0)}",
            f"成功：{result.get('ok', 0)}",
            f"失敗：{result.get('failed', 0)}",
            f"略過：{result.get('skipped', 0)}",
            f"分析失敗：{result.get('analyze_failed', 0)}",
            f"DXF：{result.get('dxf', 0)}",
            f"Keyword Hits：{result.get('keyword_hits', 0)}",
            f"Unique Matched Objects：{result.get('unique_objects', 0)}",
            f"Suspect text records：{result.get('suspect_texts', 0)}",
            f"耗時：{pipeline.fmt_duration(result.get('elapsed', 0))}",
        ]
        if result.get("fatal"):
            lines.append(f"錯誤：{result['fatal']}")
        return state, verdict, "\n".join(lines) + self._failed_files_block(result.get("failed_files"))

    def _render_analysis(self, result):
        state, verdict = classify_outcome(result)
        lines = [
            f"檔案：{result.get('total', 0)}　成功：{result.get('ok', 0)}　"
            f"失敗：{result.get('failed', 0)}　略過：{result.get('skipped', 0)}　"
            f"警告：{result.get('warnings', 0)}",
        ]
        if result.get("fatal"):
            lines.append(f"錯誤：{result['fatal']}")
        return state, verdict, "\n".join(lines) + self._failed_files_block(result.get("failed_files"))

    def _render_diff(self, result):
        if result.get("pairs", 0) == 0 and not result.get("fatal") and not result.get("stopped"):
            state, verdict = "warning", "沒有找到可比對的 DWG / DXF"
        else:
            state, verdict = classify_outcome(result)
        lines = [
            f"配對：{result.get('pairs', 0)}　有差異：{result.get('changed', 0)}　"
            f"相同：{result.get('identical', 0)}　只在一邊：{result.get('skipped', 0)}　"
            f"失敗：{result.get('failed', 0)}",
            f"新增：{result.get('added', 0)}　刪除：{result.get('removed', 0)}　修改：{result.get('modified', 0)}",
            "結果：dwgdiff/diff_summary.csv、dwgdiff/diff_details.csv",
        ]
        if result.get("fatal"):
            lines.append(f"錯誤：{result['fatal']}")
        return state, verdict, "\n".join(lines) + self._failed_files_block(result.get("failed_files"))

    def _render_layerstd(self, result):
        if result.get("dwg", 0) == 0 and not result.get("fatal") and not result.get("stopped"):
            state, verdict = "warning", "沒有找到 DWG / DXF"
        else:
            state, verdict = classify_outcome(result)
            if state == "success" and result.get("violations"):
                state, verdict = "warning", "檢查完成，有圖層不符合標準"
        lines = [
            f"檔案：{result.get('dwg', 0)}　已檢查：{result.get('ok', 0)}　失敗：{result.get('failed', 0)}",
            f"違規：{result.get('violations', 0)}（錯誤 {result.get('errors', 0)}）",
            "報告：layer_check/layer_check_report.html",
        ]
        if result.get("fatal"):
            lines.append(f"錯誤：{result['fatal']}")
        return state, verdict, "\n".join(lines) + self._failed_files_block(result.get("failed_files"))

    def _render_tb_read(self, result):
        if result.get("csv"):
            self.v_tb_csv.set(result["csv"])   # ready for 寫回 without browsing
        return self._render_text_result(result, f"圖框數：{result.get('blocks', 0)}", "titleblock_attrs.csv")

    def _render_tb_write(self, result):
        return self._render_text_result(
            result,
            f"變更筆數：{result.get('changes', 0)}　無變更：{result.get('unchanged', 0)}",
            "logs/titleblock_results.csv")

    # ---- single-task compatibility entry points ----
    def _scan(self):
        self._start_tasks(["scan"])

    def _scan_versions(self):
        self._start_tasks(["version"])

    def _start_fontfix(self):
        self._start_tasks(["fontfix"])

    def _start_proxygraphics(self):
        self._start_tasks(["proxygraphics"])

    def _start_extract(self):
        self._start_tasks(["textextract"])

    def _start_replace(self):
        self._start_tasks(["textreplace"])

    def _start_layerclass(self):
        self._start_tasks(["layerclass"])

    def _start_diff(self):
        self._start_tasks(["diff"])

    def _start_layerstd(self):
        self._start_tasks(["layerstd"])

    def _start_tb_read(self):
        self._start_tasks(["tb_read"])

    def _start_tb_write(self):
        self._start_tasks(["tb_write"])

    def _on_fontfix_done(self, result):
        self._show_finished("fontfix", result)

    def _on_proxygraphics_done(self, result):
        self._show_finished("proxygraphics", result)

    def _on_extract_done(self, result):
        self._show_finished("textextract", result)

    def _on_replace_done(self, result):
        self._show_finished("textreplace", result)

    def _on_layer_done(self, result):
        self._show_finished("layerclass", result)

    # ---------- direct result rendering ----------
    def _show_finished(self, key, result):
        """Render one finished task outside the worker queue (used by the compatibility handlers)."""
        self.summary = result
        self.worker = None
        self._can_stop = False
        self._set_busy_ui(False)
        state, verdict, body = self._renderers[key](result)
        self._show_result_text(state, verdict, body)
        if not result.get("stopped"):
            self.v_progress.set(100)

    def _show_result_text(self, state, verdict, body):
        self.v_result.set(f"{STATE[state]['symbol']} {STATE[state]['label']}：{verdict}\n{body}")
        self._set_status(state, verdict)
        self._refresh_analysis_outputs()
        self._reveal(self.result_frame)

    def _log_crash(self, key, params, text):
        out = (params.get("dst") or params.get("output_dir")
               or (params.get("cfg") or {}).get("output_dir") or self._batch_out)
        if not out:
            return
        try:
            common.log_error({"output_dir": out}, f"{key} crashed: {text}")
        except OSError:
            pass

    # ---------- batch runner ----------
    def _start_selected(self):
        keys = self._selected_modes()
        if not keys:
            messagebox.showinfo("尚未勾選任務", "請先在「② 我要做什麼？」勾選至少一項任務。")
            return
        self._start_tasks(keys)

    def _start_tasks(self, keys):
        """Validate every requested task first, then run the valid ones in order on one worker thread."""
        if self._busy():
            return
        prepared, errors = [], []
        for key in keys:
            try:
                prepared.append((key, self._preparers[key]()))
            except TaskError as exc:
                errors.append((key, exc.title, exc.message))
            except Exception as exc:  # noqa: BLE001 - a broken setting must not kill the GUI
                errors.append((key, "無法開始任務", f"{type(exc).__name__}: {exc}"))
        if errors:
            for key, _title, message in errors:
                self._log("WARN", f"{TASK_LABELS[key]} 未執行：{message}")
            lines = [f"・{TASK_LABELS[key]}：{message}" for key, _title, message in errors]
            if not prepared:
                messagebox.showerror(errors[0][1] if len(errors) == 1 else "無法開始任務", "\n".join(lines))
                return
            messagebox.showwarning(
                "部分任務未執行",
                "以下任務的設定未完成，已略過：\n" + "\n".join(lines)
                + f"\n\n其餘 {len(prepared)} 項任務會繼續執行。")
        total = len(prepared)
        self.stop_event.clear()
        self._can_stop = any(key not in ("scan", "version") for key, _ in prepared)
        self.summary = None
        self._batch = dict(total=total, results=[])
        self._batch_out = self.v_out.get().strip()   # worker thread reads this plain string, never tkinter
        self.v_progress.set(0)
        self.v_ptext.set("")
        self.v_current.set("")
        self.v_result.set("處理中…")
        names = "、".join(TASK_LABELS[key] for key, _ in prepared)
        if total == 1:
            self._set_status("running", f"{names} 執行中…")
            self._log("INFO", f"開始：{names}")
        else:
            self._set_status("running", f"準備執行 {total} 項任務…")
            self._log("INFO", f"批次開始（{total} 項）：{names}")

        def work():
            for index, (key, params) in enumerate(prepared, 1):
                if self.stop_event.is_set():
                    for rest_key, _rest in prepared[index - 1:]:
                        self.q.put(("task_skipped", rest_key, "已停止"))
                    break
                self.q.put(("task_start", key, index))

                def log(level, message, _key=key):
                    self.q.put(("log", level, message))

                def progress(*args, _key=key):
                    if len(args) == 4:
                        stage, i, n, current = args
                    else:
                        i, n, current = args
                        stage = TASK_LABELS[_key]
                    self.q.put(("task_progress", _key, stage, i, n, current))

                try:
                    result = self._runners[key](params, log, progress)
                except Exception as exc:  # noqa: BLE001
                    result = dict(fatal=f"{type(exc).__name__}: {exc}")
                    self._log_crash(key, params, result["fatal"])
                self.q.put(("task_result", key, result))
            self.q.put(("batch_done",))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_busy_ui(True)

    def _on_task_start(self, key, index):
        batch = self._batch or dict(total=1, results=[])
        self._batch = batch
        total = max(1, batch["total"])
        self.v_progress.set(100.0 * (index - 1) / total)
        mark = f"{job_number(index)} " if total > 1 else ""
        self.v_ptext.set(f"{index}/{total} {TASK_LABELS[key]}（0%）" if total > 1 else TASK_LABELS[key])
        self.v_current.set("")
        self._set_status("running", f"{index}/{total} 執行中：{TASK_LABELS[key]}" if total > 1
                         else f"{TASK_LABELS[key]} 執行中…")
        self._log("INFO", f"▶ {mark}{TASK_LABELS[key]} 開始")

    def _on_task_progress(self, key, stage, i, n, current):
        batch = self._batch or dict(total=1, results=[])
        total = max(1, batch["total"])
        index = min(total, len(batch["results"]) + 1)
        fraction = (i / n) if n else 0.0
        pct = 100.0 * ((index - 1) + max(0.0, min(1.0, fraction))) / total
        self.v_progress.set(pct)
        prefix = f"{index}/{total} " if total > 1 else ""
        self.v_ptext.set(f"{pct:.1f}%　{prefix}{stage} {i} / {n}")
        self.v_current.set(f"目前處理：{current}")

    def _on_task_result(self, key, result):
        self.summary = result
        if not isinstance(result, dict):
            result = {"fatal": f"未預期的結果型別：{type(result).__name__}"}
        try:
            state, verdict, body = self._renderers[key](result)
        except Exception as exc:  # noqa: BLE001 - one malformed result must not stop the queue
            state, verdict, body = "failed", "結果無法顯示", f"錯誤：{type(exc).__name__}: {exc}"
        batch = self._batch
        if batch is None:
            batch = self._batch = dict(total=1, results=[])
        batch["results"].append((key, state, verdict, body))
        index = len(batch["results"])
        mark = f"{job_number(index)} " if batch["total"] > 1 else ""
        text = f"{STATE[state]['symbol']} {STATE[state]['label']}：{verdict}\n{body}"
        if mark:
            text = f"{mark}{TASK_LABELS[key]}　{STATE[state]['symbol']} {STATE[state]['label']}：{verdict}\n{body}"
        self.v_result.set(text)
        self._set_status(state, verdict)
        level = {"success": "OK", "warning": "WARN", "failed": "ERROR"}.get(state, "INFO")
        self._log(level, f"{mark}{TASK_LABELS[key]}：{verdict}")
        self._refresh_analysis_outputs()

    def _on_task_skipped(self, key, reason):
        batch = self._batch
        if batch is None:
            return
        batch["results"].append((key, "skipped", reason, ""))
        index = len(batch["results"])
        mark = f"{job_number(index)} " if batch["total"] > 1 else ""
        self._log("WARN", f"{mark}{TASK_LABELS[key]}：{reason}")

    def _on_batch_done(self):
        self.worker = None
        self._can_stop = False
        self._set_busy_ui(False)
        batch = self._batch or dict(total=0, results=[])
        self._batch = None
        results = batch["results"]
        stopped = self.stop_event.is_set()
        if not results:
            self._set_status("warning", "沒有執行任何任務")
            self.v_result.set("– 略過：沒有執行任何任務")
            return
        if batch.get("total", 1) <= 1:
            key, state, verdict, body = results[0]
            self.v_result.set(f"{STATE[state]['symbol']} {STATE[state]['label']}：{verdict}\n{body}")
            self._set_status(state, verdict)
            if not stopped:
                self.v_progress.set(100)
            self._reveal(self.result_frame)
            return
        worst = max((item[1] for item in results), key=lambda value: SEVERITY.get(value, 0))
        stopped = any(item[1] == "skipped" and item[2] == "已停止" for item in results)
        state = "warning" if stopped else worst
        done = sum(1 for item in results if item[1] != "skipped")
        heads = {
            "failed": f"批次結束，有任務失敗（共 {len(results)} 項）",
            "warning": f"批次完成，部分任務需要檢視（共 {len(results)} 項）",
            "skipped": f"批次完成（結果都已存在，共 {len(results)} 項）",
            "success": f"批次完成（{len(results)} 項任務）",
        }
        head = (f"已停止：完成 {done} / {batch.get('total', len(results))} 項任務"
                if stopped else heads.get(state, f"批次完成（{len(results)} 項任務）"))
        lines = [f"{STATE[state]['symbol']} {STATE[state]['label']}：{head}"]
        for index, (key, task_state, verdict, _body) in enumerate(results, 1):
            lines.append(f"{job_number(index)} {TASK_LABELS[key]}：{STATE[task_state]['symbol']} "
                         f"{STATE[task_state]['label']}　{verdict}")
        lines.append("")
        for index, (key, _task_state, _verdict, body) in enumerate(results, 1):
            if not body:
                continue
            lines.append(f"──── {job_number(index)} {TASK_LABELS[key]} ────")
            lines.append(body)
        self.v_result.set("\n".join(lines))
        self._set_status(state, head)
        self._log("WARN" if state in ("warning", "failed") else "OK", f"批次結束：{head}")
        if not stopped:
            self.v_progress.set(100)
        self._reveal(self.result_frame)

    # ---------- run ----------
    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _start(self):
        self._start_tasks(["dwg"])

    def _stop(self):
        self.stop_event.set()
        self.btn_stop.configure(state="disabled")
        self._log("WARN", "停止中：目前任務結束後停止...")

    def _on_done(self, s):
        """Compatibility entry point: render one finished DWG result without a worker."""
        self._show_finished("dwg", s)

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "log":
                    self._log(m[1], m[2])
                elif k == "task_start":
                    self._on_task_start(m[1], m[2])
                elif k == "task_progress":
                    _, key, stage, i, n, cur = m
                    self._on_task_progress(key, stage, i, n, cur)
                elif k == "task_result":
                    self._on_task_result(m[1], m[2])
                elif k == "task_skipped":
                    self._on_task_skipped(m[1], m[2])
                elif k == "batch_done":
                    self._on_batch_done()
                elif k == "env_paths":
                    self._on_env_paths(m[1], m[2])
                elif k == "env":
                    self._on_env(m[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)

    def close_requested(self):
        """Ask to leave. Returns True only when the window may be destroyed immediately.

        A running worker is told to stop. The Tk thread keeps polling; it does not
        wait on the worker.
        """
        if self._closing:
            return False
        if not self._busy():
            return True
        if not messagebox.askyesno("處理中", "仍在處理，確定要停止並離開？"):
            return False
        self._closing = True
        self._close_started = time.monotonic()
        self._children_released = False
        self.stop_event.set()
        try:
            self._set_status("warning", "正在停止，請稍候…")
            self.btn_stop.configure(state="disabled")
        except tk.TclError:
            pass
        return False

    def poll_close(self):
        """One non-blocking shutdown step. True when the window should close.

        After a short grace period, child consoles are stopped so the worker can
        finish. Temp copies are removed once the worker has left, or when the
        wait has gone on long enough that the window must close anyway.
        """
        alive = self.worker is not None and self.worker.is_alive()
        waited = time.monotonic() - self._close_started
        if alive and not self._children_released and waited >= 3:
            self._children_released = True
            convert_single.release_children()
        if alive and waited < 20:
            return False
        convert_single.cleanup_runtime()
        return True


def main():
    root = tk.Tk()
    if ICON.exists():
        try:  # default=: Toplevel dialogs inherit the icon too
            root.iconbitmap(default=str(ICON))
        except tk.TclError:
            pass
    app = App(root)

    def on_close():
        if app.close_requested():
            convert_single.cleanup_runtime()
            root.destroy()
            return
        if not app._closing or app._close_polling:
            return

        def tick():
            if app.poll_close():
                root.destroy()
            else:
                root.after(100, tick)

        app._close_polling = True
        root.after(100, tick)
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

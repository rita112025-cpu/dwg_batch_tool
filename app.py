"""SCADA DWG Analyzer - tkinter GUI. Heavy work runs in a worker thread; the UI only reads a Queue."""
import json
import os
import queue
import re
import sys
import threading
import tkinter as tk
import tkinter.font as tkfont
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
import common  # noqa: E402
import env_check  # noqa: E402
import pipeline  # noqa: E402
import dwg_version  # noqa: E402
import fontfix  # noqa: E402
import fontfix_batch  # noqa: E402
import dwgtext  # noqa: E402
import textbatch  # noqa: E402
import layerclass  # noqa: E402
import layerbatch  # noqa: E402

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
    "textextract": ("logs/textextract_hits.csv",),
    "textreplace": ("logs/textreplace_changes.csv",),
    "layerclass": ("logs/layerclass_moves.csv",),
    "dwg": ("csv/scada_hits.csv", "csv/file_index.csv"),
    "analysis": ("cross_reference/cross_reference.csv", "database/project.db"),
}


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
        self.env_ok = False
        self.summary = None
        self.analysis_inputs = []
        self.v_mode = tk.StringVar(value="version")

        self.v_in = tk.StringVar(value=str(common.resolve(self.cfg, "input_dir")))
        self.v_out = tk.StringVar(value=str(common.resolve(self.cfg, "output_dir")))
        self.opts = {k: tk.BooleanVar(value=v) for k, v in dict(
            recursive=self.cfg.get("recursive", True), convert=True, analyze=True, scada=True,
            csv=True, json=True, overwrite=self.cfg.get("overwrite", False)).items()}
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

        # ---- 2 我要做什麼 ----
        selector = ttk.LabelFrame(main, text="② 我要做什麼？")
        selector.pack(fill="x", **pad)
        radio_row = ttk.Frame(selector)
        radio_row.pack(fill="x", padx=6, pady=6)
        self.mode_buttons = []
        modes = (("scan", "掃描資料夾"), ("version", "DWG / DWT 版本表"), ("fontfix", "缺字體修復"),
                 ("layerclass", "自動分圖層"),
                 ("textextract", "文字擷取"), ("textreplace", "文字取代"),
                 ("dwg", "DWG 轉 DXF + 解析"), ("analysis", "工程資料分析"))
        for i, (value, text) in enumerate(modes):
            radio = ttk.Radiobutton(radio_row, text=text, value=value, variable=self.v_mode,
                                    command=self._refresh_mode_ui)
            radio.grid(row=i // 3, column=i % 3, sticky="w", padx=(0, 18), pady=2)
            self.mode_buttons.append(radio)

        # ---- 3 本工作設定 ----
        mode_box = ttk.LabelFrame(main, text="③ 本工作設定")
        mode_box.pack(fill="x", **pad)
        self.mode_frame = ttk.Frame(mode_box)
        self.mode_frame.pack(fill="x", padx=6, pady=6)
        self.mode_frames = {}

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

    def _refresh_mode_ui(self):
        """Show only the selected job settings without changing any saved/user-entered values."""
        for frame in self.mode_frames.values():
            frame.pack_forget()
        self.mode_frames[self.v_mode.get()].pack(fill="x")
        self._set_busy_ui(self._busy())
        self._refresh_analysis_outputs()

    def _set_busy_ui(self, busy):
        """Centralised enable/disable policy for all job controls."""
        state = "disabled" if busy else "normal"
        for widget in (self.mode_buttons + self.browse_buttons +
                       [self.btn_scan, self.btn_versions, self.btn_analysis, self.btn_fontfix,
                        self.btn_extract, self.btn_replace, self.btn_layer,
                        self.btn_files, self.btn_folder, self.btn_recheck, self.btn_save_config]):
            widget.configure(state=state)
        self.btn_start.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_fontfix.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_extract.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_replace.configure(state="normal" if not busy and self.env_ok else "disabled")
        self.btn_layer.configure(state="normal" if not busy and self.env_ok else "disabled")
        cancellable = busy and self._can_stop
        self.btn_stop.configure(state="normal" if cancellable else "disabled")
        self._refresh_analysis_outputs()

    def _get_primary_result(self):
        return get_primary_result(self.v_mode.get(), self.v_out.get().strip())

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
        if self._busy(): return
        cfg = self._current_cfg()
        cfg['recursive'] = self.opts['recursive'].get()
        inputs = self.analysis_inputs or [cfg['input_dir']]
        if not cfg['output_dir'] or any(not Path(p).exists() for p in inputs):
            messagebox.showerror("路徑錯誤", "請選擇存在的輸入檔案／資料夾，並指定輸出資料夾。")
            return
        self.stop_event.clear()
        self._can_stop = True
        self.summary = None
        self.v_result.set("分析中...")
        self._set_status("running", "分析工程資料中…")
        self.v_progress.set(0)
        self.v_ptext.set("")
        self.v_current.set("")
        def work():
            try:
                result = pipeline.run_pipeline(cfg, dict(multiformat=True, inputs=inputs),
                    log=lambda lv, msg: self.q.put(('log', lv, msg)),
                    progress=lambda *args: self.q.put(('progress', *args)), stop_event=self.stop_event)
                self.q.put(('analysis_done', result))
            except Exception as exc:
                common.log_error(cfg, f'multiformat: {type(exc).__name__}: {exc}')
                self.q.put(('analysis_done', dict(fatal=str(exc), total=0, ok=0, failed=0, warnings=0, stopped=False)))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_busy_ui(True)

    def _analysis_done(self, result):
        self.summary = result
        self.worker = None
        self._can_stop = False
        self._set_busy_ui(False)
        state, verdict = classify_outcome(result)
        st = STATE[state]
        failed_files = result.get('failed_files', [])
        shown = "\n".join(f"  - {friendly_failure(x)}" for x in failed_files[:8])
        if len(failed_files) > 8:
            shown += f"\n  ... 另有 {len(failed_files) - 8} 個（見 errors.log）"
        self.v_result.set(f"{st['symbol']} {st['label']}：{verdict}\n"
                          f"檔案：{result['total']}　成功：{result['ok']}　失敗：{result['failed']}　"
                          f"略過：{result.get('skipped', 0)}　警告：{result['warnings']}\n" + result['fatal']
                          + (f"\n\n失敗檔案：\n{shown}" if failed_files else ""))
        self._set_status(state, verdict)
        self.v_progress.set(100 if not result['stopped'] else self.v_progress.get())
        self._reveal(self.result_frame)

    def _browse(self, var):
        d = filedialog.askdirectory(initialdir=var.get() or str(ROOT))
        if d:
            var.set(os.path.normpath(d))

    def _keywords(self):
        return [l.strip() for l in self.kw_text.get("1.0", "end").splitlines() if l.strip()]

    def _current_cfg(self):
        return dict(self.cfg, input_dir=self.v_in.get().strip(), output_dir=self.v_out.get().strip(),
                    keywords=self._keywords())

    def _save_config(self):
        cfg = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
        cfg.update(input_dir=self.v_in.get().strip(), output_dir=self.v_out.get().strip(),
                   keywords=self._keywords(), recursive=self.opts["recursive"].get(),
                   overwrite=self.opts["overwrite"].get())
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

    # ---------- scan ----------
    def _scan_versions(self):
        """Read ACxxxx headers from DWG/DWT and export a version inventory CSV."""
        if self._busy():
            return
        input_dir = Path(self.v_in.get().strip())
        output_dir = Path(self.v_out.get().strip())
        if not input_dir.is_dir():
            messagebox.showerror("輸入資料夾不存在", str(input_dir))
            return
        if not str(output_dir).strip():
            messagebox.showerror("請指定輸出資料夾", "")
            return

        csv_path = output_dir / "csv" / "dwg_versions.csv"
        recursive = self.opts["recursive"].get()
        self._can_stop = False
        self.v_scan.set("讀取 DWG/DWT 版本中...")
        self.v_result.set("處理中...")
        self._set_status("running", "讀取 DWG / DWT 版本中…")
        self.v_progress.set(0)
        self.v_ptext.set("")
        self.v_current.set("")

        def work():
            try:
                rows = dwg_version.write_version_csv([input_dir], csv_path, recursive=recursive)
                ok = sum(r["status"] == "OK" for r in rows)
                unknown = sum(r["status"] == "UNKNOWN_CODE" for r in rows)
                invalid = len(rows) - ok - unknown
                dwt = sum(r["extension"] == ".dwt" for r in rows)
                self.q.put(("versions_done", str(csv_path), len(rows), ok, unknown, invalid, dwt))
            except Exception as exc:  # noqa: BLE001
                self.q.put(("versions_err", str(exc)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_busy_ui(True)

    def _scan(self):
        if self._busy():
            return
        d = self.v_in.get().strip()
        rec = self.opts["recursive"].get()
        self._can_stop = False
        if not Path(d).is_dir():
            messagebox.showerror("輸入資料夾不存在", d)
            return

        def work():
            try:
                self.q.put(("scan", pipeline.scan(d, rec)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("scan_err", str(e)))
        self.v_scan.set("掃描中...")
        self.v_result.set("處理中...")
        self._set_status("running", "掃描資料夾中…")
        self.v_progress.set(0)
        self.v_ptext.set("")
        self.v_current.set("")
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_busy_ui(True)

    def _fontfix_version(self):
        label = self.ff_version_box.get()
        for code, text in fontfix.DWG_SAVE_VERSIONS:
            if text == label:
                return code
        return None

    def _start_fontfix(self):
        if self._busy():
            return
        if not self.env_ok:
            messagebox.showerror("無法修復字體", "找不到可用的 accoreconsole；請先完成環境設定並重新檢查。")
            return
        src = self.v_in.get().strip()
        dst = self.v_out.get().strip()
        problem = fontfix.directory_problem(src, dst)
        if problem:
            messagebox.showerror("路徑錯誤", problem)
            return
        version = self._fontfix_version()
        if version is None:
            messagebox.showerror("版本錯誤", "不支援的 DWG 版本。")
            return
        primary = self.v_ff_primary.get().strip() or fontfix.DEFAULT_PRIMARY
        big = self.v_ff_big.get().strip() or fontfix.DEFAULT_BIG
        mapping = fontfix.parse_mapping(self.ff_map_text.get("1.0", "end"))
        recursive = self.opts["recursive"].get()
        overwrite = self.v_ff_overwrite.get()
        timeout = int(self.cfg.get("timeout_seconds", 300))
        accore = self.cfg.get("accoreconsole", "")
        self.stop_event.clear()
        self._can_stop = True
        self.summary = None
        self.v_progress.set(0)
        self.v_ptext.set("")
        self.v_current.set("")
        self.v_result.set("修復字體中...")
        self._set_status("running", "缺字體修復中…")

        def work():
            try:
                result = fontfix_batch.run(
                    src, dst, accore, recursive=recursive, primary=primary, big=big,
                    mapping=mapping, version=version, overwrite=overwrite, timeout=timeout,
                    stop_event=self.stop_event,
                    progress=lambda i, n, cur: self.q.put(("progress", "缺字體修復", i, n, cur)),
                    log=lambda level, message: self.q.put(("log", level, message)),
                )
            except Exception as exc:  # noqa: BLE001
                result = dict(dwg=0, ok=0, failed=0, skipped=0, warnings=0, styles_replaced=0,
                              stopped=False, fatal=f"{type(exc).__name__}: {exc}",
                              failed_files=[], still_files=[], elapsed=0)
                common.log_error({"output_dir": dst}, f"fontfix crashed: {result['fatal']}")
            self.q.put(("fontfix_done", result))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_busy_ui(True)

    def _on_fontfix_done(self, result):
        self.summary = result
        self.worker = None
        self._can_stop = False
        self._set_busy_ui(False)
        if result.get("dwg", 0) == 0 and not result.get("fatal") and not result.get("stopped"):
            state, verdict = "warning", "沒有找到 DWG 或 DWT"
        else:
            state, verdict = classify_outcome(result)
        failed_files = result.get("failed_files", [])
        shown = "\n".join(f"  - {friendly_failure(x)}" for x in failed_files[:8])
        if len(failed_files) > 8:
            shown += f"\n  ... 另有 {len(failed_files) - 8} 個（見 errors.log）"
        still = result.get("still_files") or []
        still_shown = "\n".join(f"  - {item}" for item in still[:8])
        if len(still) > 8:
            still_shown += f"\n  ... 另有 {len(still) - 8} 個"
        st = STATE[state]
        self.v_result.set(
            f"{st['symbol']} {st['label']}：{verdict}\n"
            f"DWG/DWT：{result.get('dwg', 0)}　成功：{result.get('ok', 0)}　"
            f"失敗：{result.get('failed', 0)}　略過：{result.get('skipped', 0)}\n"
            f"替換樣式：{result.get('styles_replaced', 0)}　"
            f"存檔後仍缺字：{result.get('warnings', 0)}\n"
            f"結果：logs/fontfix_results.csv"
            + (f"\n錯誤：{result['fatal']}" if result.get("fatal") else "")
            + (f"\n\n失敗檔案：\n{shown}" if failed_files else "")
            + (f"\n\n仍缺字：\n{still_shown}" if still else "")
        )
        self._set_status(state, verdict)
        if not result.get("fatal") and not result.get("stopped"):
            self.v_progress.set(100)
        self._refresh_analysis_outputs()
        self._reveal(self.result_frame)

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
            messagebox.showerror("路徑錯誤", problem)
            return None
        return src, dst

    def _replace_color(self):
        if not self.v_rp_recolor.get():
            return None
        raw = self.v_rp_color.get().strip()
        if not raw.isdigit() or int(raw) > 256:
            messagebox.showerror("色號錯誤", "ACI 色號必須是 0 到 256。")
            return "bad"
        return int(raw)

    def _start_extract(self):
        if self._busy():
            return
        if not self.env_ok:
            messagebox.showerror("無法擷取文字", "找不到可用的 accoreconsole；請先完成環境設定並重新檢查。")
            return
        folders = self._text_folders()
        if folders is None:
            return
        src, dst = folders
        mode = self._combo_code(self.tx_match_box, dwgtext.MATCH_MODES)
        problem = dwgtext.validate_rule(self.v_tx_pattern.get(), mode)
        if problem:
            messagebox.showerror("搜尋文字", problem)
            return
        pattern = self.v_tx_pattern.get()
        case_sensitive = self.v_tx_case.get()
        include_blocks = self.v_tx_blocks.get()
        recursive = self.opts["recursive"].get()
        timeout = int(self.cfg.get("timeout_seconds", 300))
        accore = self.cfg.get("accoreconsole", "")
        self.stop_event.clear()
        self._can_stop = True
        self.summary = None
        self.v_progress.set(0)
        self.v_ptext.set("")
        self.v_current.set("")
        self.v_result.set("擷取文字中...")
        self._set_status("running", "文字擷取中…")

        def work():
            try:
                result = textbatch.run_extract(
                    src, dst, accore, pattern=pattern, mode=mode,
                    case_sensitive=case_sensitive, include_blocks=include_blocks,
                    recursive=recursive, timeout=timeout, stop_event=self.stop_event,
                    progress=lambda i, n, cur: self.q.put(("progress", "文字擷取", i, n, cur)),
                    log=lambda level, message: self.q.put(("log", level, message)),
                )
            except Exception as exc:  # noqa: BLE001
                result = dict(dwg=0, ok=0, failed=0, skipped=0, warnings=0, hits=0,
                              stopped=False, fatal=f"{type(exc).__name__}: {exc}",
                              failed_files=[], elapsed=0)
                common.log_error({"output_dir": dst}, f"textextract crashed: {result['fatal']}")
            self.q.put(("extract_done", result))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_busy_ui(True)

    def _on_text_done(self, result, title, detail_line, result_path):
        self.summary = result
        self.worker = None
        self._can_stop = False
        self._set_busy_ui(False)
        if result.get("dwg", 0) == 0 and not result.get("fatal") and not result.get("stopped"):
            state, verdict = "warning", "沒有找到 DWG"
        else:
            state, verdict = classify_outcome(result)
        failed_files = result.get("failed_files", [])
        shown = "\n".join(f"  - {friendly_failure(x)}" for x in failed_files[:8])
        if len(failed_files) > 8:
            shown += f"\n  ... 另有 {len(failed_files) - 8} 個（見 errors.log）"
        st = STATE[state]
        self.v_result.set(
            f"{st['symbol']} {st['label']}：{verdict}\n"
            f"DWG：{result.get('dwg', 0)}　成功：{result.get('ok', 0)}　"
            f"失敗：{result.get('failed', 0)}　略過：{result.get('skipped', 0)}\n"
            f"{detail_line}\n"
            f"結果：{result_path}"
            + (f"\n錯誤：{result['fatal']}" if result.get("fatal") else "")
            + (f"\n\n失敗檔案：\n{shown}" if failed_files else "")
        )
        self._set_status(state, verdict)
        if not result.get("fatal") and not result.get("stopped"):
            self.v_progress.set(100)
        self._refresh_analysis_outputs()
        self._reveal(self.result_frame)

    def _on_extract_done(self, result):
        self._on_text_done(
            result, "文字擷取",
            f"符合筆數：{result.get('hits', 0)}",
            "logs/textextract_hits.csv",
        )

    def _start_replace(self):
        if self._busy():
            return
        if not self.env_ok:
            messagebox.showerror("無法取代文字", "找不到可用的 accoreconsole；請先完成環境設定並重新檢查。")
            return
        folders = self._text_folders()
        if folders is None:
            return
        color = self._replace_color()
        if color == "bad":
            return
        mode = self._combo_code(self.rp_mode_box, dwgtext.REPLACE_MODES)
        find = self.v_rp_find.get()
        repl = self.v_rp_repl.get()
        problem = dwgtext.validate_rule(find, mode, replacement=repl, for_replace=True)
        if problem:
            messagebox.showerror("取代規則", problem)
            return
        case_sensitive = self.v_rp_case.get()
        include_blocks = self.v_rp_blocks.get()
        dry_run = self.v_rp_dry.get()
        copy_unchanged = self.v_rp_copy.get()
        overwrite = self.v_rp_overwrite.get()
        recursive = self.opts["recursive"].get()
        timeout = int(self.cfg.get("timeout_seconds", 300))
        accore = self.cfg.get("accoreconsole", "")
        src, dst = folders
        self.stop_event.clear()
        self._can_stop = True
        self.summary = None
        self.v_progress.set(0)
        self.v_ptext.set("")
        self.v_current.set("")
        self.v_result.set("預覽文字取代中..." if dry_run else "取代文字中...")
        self._set_status("running", "文字取代預覽中…" if dry_run else "文字取代中…")

        def work():
            try:
                result = textbatch.run_replace(
                    src, dst, accore, find=find, repl=repl, mode=mode,
                    case_sensitive=case_sensitive, include_blocks=include_blocks,
                    color=color, dry_run=dry_run, copy_unchanged=copy_unchanged,
                    recursive=recursive, overwrite=overwrite, timeout=timeout,
                    stop_event=self.stop_event,
                    progress=lambda i, n, cur: self.q.put(("progress", "文字取代", i, n, cur)),
                    log=lambda level, message: self.q.put(("log", level, message)),
                )
            except Exception as exc:  # noqa: BLE001
                result = dict(dwg=0, ok=0, failed=0, skipped=0, warnings=0, changes=0,
                              unchanged=0, stopped=False, fatal=f"{type(exc).__name__}: {exc}",
                              failed_files=[], elapsed=0)
                common.log_error({"output_dir": dst}, f"textreplace crashed: {result['fatal']}")
            self.q.put(("replace_done", result))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_busy_ui(True)

    def _on_replace_done(self, result):
        self._on_text_done(
            result, "文字取代",
            f"變更筆數：{result.get('changes', 0)}　無變更：{result.get('unchanged', 0)}",
            "logs/textreplace_changes.csv",
        )

    def _start_layerclass(self):
        if self._busy():
            return
        if not self.env_ok:
            messagebox.showerror("無法分圖層", "找不到可用的 accoreconsole；請先完成環境設定並重新檢查。")
            return
        src = self.v_in.get().strip()
        dst = self.v_out.get().strip()
        problem = fontfix.directory_problem(src, dst)
        if problem:
            messagebox.showerror("路徑錯誤", problem)
            return
        type_map = self.layer_map_text.get("1.0", "end")
        prefix = self.v_ly_prefix.get()
        other = self.v_ly_other.get()
        source_mode = self.v_ly_scope.get()
        source_layers = self.v_ly_sources.get()
        _settings, _notes, rule_error = layerclass.compile_rules(
            type_map, prefix, other, source_mode, source_layers, self.v_ly_insert.get(),
        )
        if rule_error:
            messagebox.showerror("分圖層設定", rule_error)
            return
        include_paper = self.v_ly_paper.get()
        include_blocks = self.v_ly_blocks.get()
        insert_by_name = self.v_ly_insert.get()
        unlock_locked = self.v_ly_unlock.get()
        keep_appearance = self.v_ly_keep.get()
        dry_run = self.v_ly_dry.get()
        overwrite = self.v_ly_overwrite.get()
        recursive = self.opts["recursive"].get()
        timeout = int(self.cfg.get("timeout_seconds", 300))
        accore = self.cfg.get("accoreconsole", "")
        self.stop_event.clear()
        self._can_stop = True
        self.summary = None
        self.v_progress.set(0)
        self.v_ptext.set("")
        self.v_current.set("")
        self.v_result.set("預覽分圖層中..." if dry_run else "分圖層中...")
        self._set_status("running", "自動分圖層預覽中…" if dry_run else "自動分圖層中…")

        def work():
            try:
                result = layerbatch.run(
                    src, dst, accore, type_map=type_map, prefix=prefix, other=other,
                    source_mode=source_mode, source_layers=source_layers,
                    include_paper=include_paper, include_blocks=include_blocks,
                    insert_by_name=insert_by_name, unlock_locked=unlock_locked,
                    keep_appearance=keep_appearance, dry_run=dry_run,
                    recursive=recursive, overwrite=overwrite, timeout=timeout,
                    stop_event=self.stop_event,
                    progress=lambda i, n, cur: self.q.put(("progress", "自動分圖層", i, n, cur)),
                    log=lambda level, message: self.q.put(("log", level, message)),
                )
            except Exception as exc:  # noqa: BLE001
                result = dict(dwg=0, ok=0, failed=0, skipped=0, warnings=0, moved=0, created=0,
                              unchanged=0, stopped=False, fatal=f"{type(exc).__name__}: {exc}",
                              failed_files=[], notes=[], elapsed=0)
                common.log_error({"output_dir": dst}, f"layerclass crashed: {result['fatal']}")
            self.q.put(("layer_done", result))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_busy_ui(True)

    def _on_layer_done(self, result):
        self.summary = result
        self.worker = None
        self._can_stop = False
        self._set_busy_ui(False)
        if result.get("dwg", 0) == 0 and not result.get("fatal") and not result.get("stopped"):
            state, verdict = "warning", "沒有找到 DWG"
        else:
            state, verdict = classify_outcome(result)
        failed_files = result.get("failed_files", [])
        shown = "\n".join(f"  - {friendly_failure(x)}" for x in failed_files[:8])
        if len(failed_files) > 8:
            shown += f"\n  ... 另有 {len(failed_files) - 8} 個（見 errors.log）"
        st = STATE[state]
        self.v_result.set(
            f"{st['symbol']} {st['label']}：{verdict}\n"
            f"DWG：{result.get('dwg', 0)}　成功：{result.get('ok', 0)}　"
            f"失敗：{result.get('failed', 0)}　略過：{result.get('skipped', 0)}\n"
            f"搬移數量：{result.get('moved', 0)}　新建圖層：{result.get('created', 0)}　"
            f"無變更：{result.get('unchanged', 0)}\n"
            f"結果：logs/layerclass_moves.csv"
            + (f"\n錯誤：{result['fatal']}" if result.get("fatal") else "")
            + (f"\n\n失敗檔案：\n{shown}" if failed_files else "")
        )
        self._set_status(state, verdict)
        if not result.get("fatal") and not result.get("stopped"):
            self.v_progress.set(100)
        self._refresh_analysis_outputs()
        self._reveal(self.result_frame)

    # ---------- run ----------
    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _start(self):
        if self._busy():
            return
        if not self.env_ok:
            messagebox.showerror("無法處理 DWG", "找不到可用的 accoreconsole；請先完成環境設定並重新檢查。")
            return
        cfg = self._current_cfg()
        if not Path(cfg["input_dir"]).is_dir():
            messagebox.showerror("輸入資料夾不存在", cfg["input_dir"])
            return
        if not cfg["output_dir"]:
            messagebox.showerror("請指定輸出資料夾", "")
            return
        if not cfg["keywords"] and self.opts["scada"].get():
            messagebox.showwarning("沒有關鍵字", "SCADA 搜尋需要至少一個關鍵字")
            return
        opts = {k: v.get() for k, v in self.opts.items()}
        self.stop_event.clear()
        self._can_stop = True
        self.v_progress.set(0)
        self.v_ptext.set("")
        self.v_result.set("處理中...")
        self._set_status("running", "DWG 轉 DXF＋解析中…")
        self.root.after(50, self._refresh_analysis_outputs)   # output buttons stay disabled while a run is active

        def work():
            try:
                s = pipeline.run_pipeline(
                    cfg, opts,
                    log=lambda lv, m: self.q.put(("log", lv, m)),
                    progress=lambda st, i, n, cur: self.q.put(("progress", st, i, n, cur)),
                    stop_event=self.stop_event)
            except Exception as e:  # noqa: BLE001
                s = dict(dwg=0, ok=0, failed=0, dxf=0, keyword_hits=0, unique_objects=0, suspect_texts=0, elapsed=0, stopped=False,
                         fatal=f"{type(e).__name__}: {e}")
                common.log_error(cfg, f"pipeline crashed: {s['fatal']}")
            self.q.put(("done", s))
        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()
        self._set_busy_ui(True)

    def _stop(self):
        self.stop_event.set()
        self.btn_stop.configure(state="disabled")
        self._log("WARN", "停止中：等待目前的 accoreconsole 結束...")

    def _on_done(self, s):
        self.summary = s
        self.worker = None      # the worker has finished; lets the output buttons enable
        self._can_stop = False
        self._set_busy_ui(False)
        state, verdict = classify_outcome(s)
        head = f"{STATE[state]['symbol']} {STATE[state]['label']}：{verdict}"
        if not s["fatal"] and not s["stopped"]:
            self.v_progress.set(100)
        failed_files = s.get("failed_files", [])
        shown = "\n".join(f"  - {friendly_failure(x)}" for x in failed_files[:8])
        if len(failed_files) > 8:
            shown += f"\n  ... 另有 {len(failed_files) - 8} 個（見 errors.log）"
        self.v_result.set(
            f"{head}\n\nDWG：{s['dwg']}\n成功：{s['ok']}\n失敗：{s['failed']}\n略過：{s.get('skipped', 0)}\n"
            f"分析失敗：{s.get('analyze_failed', 0)}\nDXF：{s['dxf']}\n"
            f"Keyword Hits：{s['keyword_hits']}\nUnique Matched Objects：{s['unique_objects']}\n"
            f"Suspect text records：{s['suspect_texts']}\n耗時：{pipeline.fmt_duration(s['elapsed'])}"
            + (f"\n錯誤：{s['fatal']}" if s["fatal"] else "")
            + (f"\n\n失敗檔案：\n{shown}" if failed_files else ""))
        self._set_status(state, verdict)
        self._reveal(self.result_frame)

    def _poll(self):
        try:
            while True:
                m = self.q.get_nowait()
                k = m[0]
                if k == "log":
                    self._log(m[1], m[2])
                elif k == "progress":
                    _, stage, i, n, cur = m
                    pct = 100.0 * i / n if n else 0
                    self.v_progress.set(pct)
                    self.v_ptext.set(f"{pct:.1f}%　{stage} {i} / {n}")
                    self.v_current.set(f"目前處理：{cur}")
                elif k == "done":
                    self._on_done(m[1])
                elif k == 'analysis_done':
                    self._analysis_done(m[1])
                elif k == "fontfix_done":
                    self._on_fontfix_done(m[1])
                elif k == "extract_done":
                    self._on_extract_done(m[1])
                elif k == "replace_done":
                    self._on_replace_done(m[1])
                elif k == "layer_done":
                    self._on_layer_done(m[1])
                elif k == "env_paths":
                    self._on_env_paths(m[1], m[2])
                elif k == "env":
                    self._on_env(m[1])
                elif k == "scan":
                    r = m[1]
                    self.worker = None
                    self._can_stop = False
                    self._set_busy_ui(False)
                    self.v_scan.set(
                        f"找到 DWG：{r['dwg']}｜DXF：{r['dxf']}｜總容量：{pipeline.fmt_size(r['size'])}｜"
                        f"資料夾：{r['folders']}｜中文檔名：{r['chinese']}｜空白路徑：{r['spaces']}｜"
                        f"重複檔名：{r['duplicates']}")
                    total = r['dwg'] + r['dxf']
                    self.v_result.set(f"✓ 成功：掃描完成\n檔案：{total}　DWG：{r['dwg']}　DXF：{r['dxf']}　資料夾：{r['folders']}")
                    self._set_status("success", "掃描完成")
                    self.v_progress.set(100)
                    self._log("INFO", self.v_scan.get())
                    self._reveal(self.result_frame)
                elif k == "scan_err":
                    self.worker = None
                    self._can_stop = False
                    self._set_busy_ui(False)
                    self.v_scan.set("")
                    self.v_result.set(f"✗ 失敗：掃描失敗\n錯誤：{m[1]}")
                    self._set_status("failed", "掃描失敗")
                    messagebox.showerror("掃描失敗", m[1])
                elif k == "versions_done":
                    _, csv_path, total, ok, unknown, invalid, dwt = m
                    self.worker = None
                    self._can_stop = False
                    self._set_busy_ui(False)
                    self.v_scan.set(
                        f"版本表完成：{total} 檔（DWT {dwt}）｜已辨識：{ok}｜未知代碼：{unknown}｜異常：{invalid}")
                    state = "warning" if unknown or invalid else "success"
                    self.v_result.set(
                        f"{STATE[state]['symbol']} {STATE[state]['label']}：版本表完成\n"
                        f"檔案：{total}　已辨識：{ok}　未知代碼：{unknown}　異常：{invalid}　DWT：{dwt}")
                    self._set_status(state, "版本表完成")
                    self.v_progress.set(100)
                    self._log("OK", f"DWG/DWT 版本表已輸出：{csv_path}")
                    self._refresh_analysis_outputs()
                    self._reveal(self.result_frame)
                elif k == "versions_err":
                    self.worker = None
                    self._can_stop = False
                    self._set_busy_ui(False)
                    self.v_scan.set("")
                    self.v_result.set(f"✗ 失敗：版本表失敗\n錯誤：{m[1]}")
                    self._set_status("failed", "版本表失敗")
                    self._log("ERROR", f"DWG/DWT 版本表失敗：{m[1]}")
                    messagebox.showerror("版本表失敗", m[1])
        except queue.Empty:
            pass
        self.root.after(100, self._poll)


def main():
    root = tk.Tk()
    if ICON.exists():
        try:  # default=: Toplevel dialogs inherit the icon too
            root.iconbitmap(default=str(ICON))
        except tk.TclError:
            pass
    app = App(root)

    def on_close():
        if app._busy():
            if not messagebox.askyesno("處理中", "仍在處理，確定要停止並離開？"):
                return
            app.stop_event.set()
            app.worker.join(timeout=20)  # lets the running accoreconsole be terminated cleanly
        root.destroy()
    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()


if __name__ == "__main__":
    main()

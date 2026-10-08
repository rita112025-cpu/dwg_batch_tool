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
        root.geometry("900x740")
        root.minsize(760, 620)
        self.cfg = common.load_config()
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self.env_ok = False
        self.summary = None
        self.analysis_inputs = []

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
    # Four numbered sections in one scrollable column:  1 source | 2 what to run (A: DWG->DXF, B: analysis)
    # | 3 progress + log | 4 results.  Every control lives inside the scroll area, so nothing is unreachable.
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

        # ---- 1 來源 ----
        src = ttk.LabelFrame(main, text="① 來源　Source")
        src.pack(fill="x", **pad)
        env = ttk.Frame(src)
        env.pack(fill="x", padx=6, pady=(4, 0))
        self.env_labels = {}
        for i, k in enumerate(self.v_env):
            lb = ttk.Label(env, textvariable=self.v_env[k], wraplength=380, justify="left")
            lb.grid(row=i // 2, column=i % 2, sticky="w", padx=(0, 16), pady=1)
            self.env_labels[k] = lb
        env.columnconfigure(0, weight=1)
        env.columnconfigure(1, weight=1)
        ttk.Button(env, text="重新檢查", command=self._check_env).grid(row=0, column=2, rowspan=2, padx=4)
        self.v_accore = tk.StringVar(value="")
        self.lbl_accore = ttk.Label(src, textvariable=self.v_accore, wraplength=700, justify="left")
        self.lbl_accore.pack(fill="x", padx=6, pady=(2, 0))
        self._wrap_to_width(self.lbl_accore, src)
        note = ttk.Label(src, text="AutoCAD／accoreconsole 只在處理 DWG（流程 A）時需要；DXF、IFC、Excel、PDF、DOCX、"
                                   "Navisworks 的分析（流程 B）不依賴 AutoCAD。", wraplength=700, justify="left")
        note.pack(fill="x", padx=6, pady=(2, 4))
        self._wrap_to_width(note, src)

        paths = ttk.Frame(src)
        paths.pack(fill="x", padx=6, pady=2)
        for r, (label, var) in enumerate((("輸入資料夾 Input", self.v_in), ("輸出資料夾 Output", self.v_out))):
            ttk.Label(paths, text=label, width=16).grid(row=r, column=0, sticky="w", pady=3)
            ttk.Entry(paths, textvariable=var).grid(row=r, column=1, sticky="ew", pady=3, padx=4)
            ttk.Button(paths, text="Browse", command=lambda v=var: self._browse(v)).grid(row=r, column=2, padx=4)
        paths.columnconfigure(1, weight=1)
        scanrow = ttk.Frame(src)
        scanrow.pack(fill="x", padx=6, pady=(2, 6))
        self.btn_scan = ttk.Button(scanrow, text="掃描輸入資料夾 Scan", command=self._scan)
        self.btn_scan.pack(side="left")
        self.btn_versions = ttk.Button(scanrow, text="產生 DWG/DWT 版本表", command=self._scan_versions)
        self.btn_versions.pack(side="left", padx=(6, 0))
        lbl_scan = ttk.Label(scanrow, textvariable=self.v_scan, wraplength=430, justify="left")
        lbl_scan.pack(side="left", padx=10, fill="x", expand=True)
        self._wrap_to_width(lbl_scan, scanrow, margin=300)

        # ---- 2 執行內容 ----
        run = ttk.LabelFrame(main, text="② 執行內容　What to run（兩條流程各自獨立）")
        run.pack(fill="x", **pad)
        cols = ttk.Frame(run)
        cols.pack(fill="x", padx=6, pady=4)
        cols.columnconfigure(0, weight=1, uniform="flow")
        cols.columnconfigure(1, weight=1, uniform="flow")

        fa = ttk.LabelFrame(cols, text="A．DWG 批次轉 DXF（需要 AutoCAD）")
        fa.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        na = ttk.Label(fa, text="讀取輸入資料夾內的 .dwg，用 accoreconsole 轉成 DXF，再解析並搜尋 SCADA 關鍵字。",
                       wraplength=340, justify="left")
        na.pack(fill="x", padx=6, pady=(4, 2))
        self._wrap_to_width(na, fa)
        grid = ttk.Frame(fa)
        grid.pack(fill="x", padx=6)
        items = [("recursive", "遞迴搜尋子資料夾"), ("convert", "DWG → DXF"),
                 ("analyze", "DXF 內容解析"), ("scada", "SCADA 關鍵字搜尋"),
                 ("csv", "產生 CSV"), ("json", "產生 JSON"), ("overwrite", "覆寫既有 DXF")]
        for i, (k, t) in enumerate(items):
            ttk.Checkbutton(grid, text=t, variable=self.opts[k]).grid(row=i // 2, column=i % 2, sticky="w", padx=4, pady=1)
        self.btn_start = ttk.Button(fa, text="開始 DWG 轉 DXF＋解析", command=self._start)
        self.btn_start.pack(anchor="w", padx=6, pady=6)

        fb = ttk.LabelFrame(cols, text="B．工程資料分析（不需要 AutoCAD）")
        fb.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        nb = ttk.Label(fb, text="直接解析 DXF、IFC、Excel/CSV、PDF、DOCX、Navisworks；選到 .dwg 時才會用到 AutoCAD。"
                                "「遞迴搜尋」沿用左側設定。", wraplength=340, justify="left")
        nb.pack(fill="x", padx=6, pady=(4, 2))
        self._wrap_to_width(nb, fb)
        self.v_analysis = tk.StringVar(value="使用上方輸入資料夾，或選擇多個檔案")
        la = ttk.Label(fb, textvariable=self.v_analysis, wraplength=340, justify="left")
        la.pack(fill="x", padx=6, pady=2)
        self._wrap_to_width(la, fb)
        brow = ttk.Frame(fb)
        brow.pack(fill="x", padx=6, pady=2)
        self.btn_files = ttk.Button(brow, text="選擇檔案", command=self._select_analysis_files)
        self.btn_files.pack(side="left")
        self.btn_folder = ttk.Button(brow, text="使用輸入資料夾", command=self._analysis_folder)
        self.btn_folder.pack(side="left", padx=6)
        self.btn_analysis = ttk.Button(fb, text="開始分析工程資料", command=self._start_analysis)
        self.btn_analysis.pack(anchor="w", padx=6, pady=6)

        kw = ttk.LabelFrame(run, text="SCADA 關鍵字 Keywords（兩條流程共用，一行一個）")
        kw.pack(fill="x", padx=6, pady=(2, 6))
        self.kw_text = tk.Text(kw, height=3, width=30, font=("Consolas", 10))
        self.kw_text.pack(side="left", fill="both", expand=True, padx=6, pady=4)
        self.kw_text.insert("1.0", "\n".join(self.cfg.get("keywords", [])))
        self.kw_text.bind("<Tab>", lambda e: (e.widget.tk_focusNext().focus_set(), "break")[1])
        self.kw_text.bind("<Shift-Tab>", lambda e: (e.widget.tk_focusPrev().focus_set(), "break")[1])
        ttk.Button(kw, text="儲存設定", command=self._save_config).pack(side="right", padx=8)

        # ---- 3 執行狀態 ----
        st = ttk.LabelFrame(main, text="③ 執行狀態　Progress")
        st.pack(fill="both", expand=True, **pad)
        pf = ttk.Frame(st)
        pf.pack(fill="x", padx=6, pady=(4, 0))
        self.btn_stop = ttk.Button(pf, text="停止 Stop", command=self._stop, state="disabled")
        self.btn_stop.pack(side="left")
        self.progress_bar = ProgressBar(pf, self.v_progress)
        self.progress_bar.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(pf, textvariable=self.v_ptext, width=34).pack(side="left")
        cur = ttk.Label(st, textvariable=self.v_current, anchor="w", wraplength=760, justify="left")
        cur.pack(fill="x", padx=8)
        self._wrap_to_width(cur, st)
        self.log_box = scrolledtext.ScrolledText(st, height=6, state="disabled", font=("Consolas", 9))
        self.log_box.pack(fill="both", expand=True, padx=6, pady=6)
        for state, color in LOG_FG.items():
            self.log_box.tag_configure(state, foreground=color)

        # ---- 4 結果 ----
        rf = ttk.LabelFrame(main, text="④ 結果　Results")
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
        # one entry per output file; enabled only once the file exists
        groups = (
            ("共用", (("開啟輸出資料夾", ""), ("開啟 errors.log", "logs/errors.log"),
                    ("開啟 dwg_versions.csv", "csv/dwg_versions.csv"))),
            ("流程 A 結果", (("開啟 scada_hits.csv", "csv/scada_hits.csv"), ("開啟 object_hits.csv", "csv/object_hits.csv"),
                         ("開啟 file_index.csv", "csv/file_index.csv"))),
            ("流程 B 結果", (("開啟 project.db", "database/project.db"),
                         ("開啟 cross_reference.csv", "cross_reference/cross_reference.csv"),
                         ("開啟 requirements.csv", "docx/requirements.csv"), ("開啟 boq_items.csv", "excel/boq_items.csv"),
                         ("開啟 ifc_objects.csv", "ifc/ifc_objects.csv"),
                         ("開啟 navis_clashes.csv", "navisworks/navis_clashes.csv"))),
        )
        self.output_buttons = {}
        self.analysis_outputs = {}
        for title, files in groups:
            box = ttk.Frame(rf)
            box.pack(fill="x", padx=8, pady=(2, 4))
            ttk.Label(box, text=title).grid(row=0, column=0, columnspan=3, sticky="w")
            for i, (text, rel) in enumerate(files):
                b = ttk.Button(box, text=text, command=lambda r=rel: self._open(r), state="disabled")
                b.grid(row=1 + i // 3, column=i % 3, sticky="ew", padx=(0, 6), pady=2)
                self.output_buttons[rel] = b
                if title == "流程 B 結果":
                    self.analysis_outputs[rel] = b
            for col in range(3):
                box.columnconfigure(col, weight=1, uniform=title)
        self.v_out.trace_add('write', lambda *args: self._refresh_analysis_outputs())
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
        for rel, button in self.output_buttons.items():
            target = out / rel if rel else out
            exists = target.is_dir() if not rel else target.is_file()
            button.configure(state="normal" if exists and not self._busy() else "disabled")

    def _start_analysis(self):
        if self._busy(): return
        cfg = self._current_cfg()
        cfg['recursive'] = self.opts['recursive'].get()
        inputs = self.analysis_inputs or [cfg['input_dir']]
        if not cfg['output_dir'] or any(not Path(p).exists() for p in inputs):
            messagebox.showerror("路徑錯誤", "請選擇存在的輸入檔案／資料夾，並指定輸出資料夾。")
            return
        self.stop_event.clear()
        self.summary = None
        self.v_result.set("分析中...")
        self._set_status("running", "分析工程資料中…")
        self.v_progress.set(0)
        for b in (self.btn_start, self.btn_scan, self.btn_versions, self.btn_analysis, self.btn_files, self.btn_folder): b.configure(state="disabled")
        self.btn_stop.configure(state="normal")
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
        self._refresh_analysis_outputs()

    def _analysis_done(self, result):
        self.summary = result
        self.worker = None
        for b in (self.btn_scan, self.btn_versions, self.btn_analysis, self.btn_files, self.btn_folder): b.configure(state="normal")
        self.btn_start.configure(state="normal" if self.env_ok else "disabled")
        self.btn_stop.configure(state="disabled")
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
        self._refresh_analysis_outputs()
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
        for line in r["lines"]:
            self._log("INFO", line)
        if not r["accore_ok"]:
            self._log("ERROR", "accoreconsole NOT FOUND，無法開始處理")
        self.btn_start.configure(state="normal" if self.env_ok and not self._busy() else "disabled")
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
        self.btn_versions.configure(state="disabled")
        self.v_scan.set("讀取 DWG/DWT 版本中...")

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

        threading.Thread(target=work, daemon=True).start()

    def _scan(self):
        d = self.v_in.get().strip()
        rec = self.opts["recursive"].get()

        def work():
            try:
                self.q.put(("scan", pipeline.scan(d, rec)))
            except Exception as e:  # noqa: BLE001
                self.q.put(("scan_err", str(e)))
        self.v_scan.set("掃描中...")
        threading.Thread(target=work, daemon=True).start()

    # ---------- run ----------
    def _busy(self):
        return self.worker is not None and self.worker.is_alive()

    def _start(self):
        if self._busy():
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
        self.btn_start.configure(state="disabled")
        self.btn_scan.configure(state="disabled")
        self.btn_versions.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.btn_analysis.configure(state="disabled")
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

    def _stop(self):
        self.stop_event.set()
        self.btn_stop.configure(state="disabled")
        self._log("WARN", "停止中：等待目前的 accoreconsole 結束...")

    def _on_done(self, s):
        self.summary = s
        self.worker = None      # the worker has finished; lets the output buttons enable
        self.btn_scan.configure(state="normal")
        self.btn_versions.configure(state="normal")
        self.btn_stop.configure(state="disabled")
        self.btn_analysis.configure(state="normal")
        self.btn_start.configure(state="normal" if self.env_ok else "disabled")
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
        self._refresh_analysis_outputs()
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
                    self.v_ptext.set(f"[{stage}] 目前檔案 {i} / {n}    進度 {pct:.1f}%")
                    self.v_current.set(f"目前處理：{cur}")
                elif k == "done":
                    self._on_done(m[1])
                elif k == 'analysis_done':
                    self._analysis_done(m[1])
                elif k == "env_paths":
                    self._on_env_paths(m[1], m[2])
                elif k == "env":
                    self._on_env(m[1])
                elif k == "scan":
                    r = m[1]
                    self.v_scan.set(
                        f"找到 DWG：{r['dwg']}｜DXF：{r['dxf']}｜總容量：{pipeline.fmt_size(r['size'])}｜"
                        f"資料夾：{r['folders']}｜中文檔名：{r['chinese']}｜空白路徑：{r['spaces']}｜"
                        f"重複檔名：{r['duplicates']}")
                    self._log("INFO", self.v_scan.get())
                elif k == "scan_err":
                    self.v_scan.set("")
                    messagebox.showerror("掃描失敗", m[1])
                elif k == "versions_done":
                    _, csv_path, total, ok, unknown, invalid, dwt = m
                    self.btn_versions.configure(state="normal")
                    self.v_scan.set(
                        f"版本表完成：{total} 檔（DWT {dwt}）｜已辨識：{ok}｜未知代碼：{unknown}｜異常：{invalid}")
                    self._log("OK", f"DWG/DWT 版本表已輸出：{csv_path}")
                    self._refresh_analysis_outputs()
                elif k == "versions_err":
                    self.btn_versions.configure(state="normal")
                    self.v_scan.set("")
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

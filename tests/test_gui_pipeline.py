"""Tests for the GUI-facing pipeline and the tkinter app (smoke)."""
import csv
import hashlib
import shutil
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
from common import load_config  # noqa: E402
import pipeline  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample.dwg"
CFG = load_config()


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def _fresh(root):
    for w in root.winfo_children():
        w.destroy()


def test_mode_defaults_and_environment_gating(monkeypatch, tk_root):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)
    assert a.v_mode.get() == "version"
    assert str(a.btn_versions["state"]) == "normal"       # version headers do not need AutoCAD
    assert not a.env_ok and str(a.btn_start["state"]) == "disabled"
    assert not a.env_details.winfo_manager()
    assert not a.kw_frame.winfo_manager()
    assert not a.more_results.winfo_manager()
    assert not a.log_frame.winfo_manager()
    a._toggle_env_details()
    assert a.env_details.winfo_manager()
    a._toggle_env_details()
    assert not a.env_details.winfo_manager()
    a._toggle_log()
    assert a.log_frame.winfo_manager()
    a._toggle_log()
    assert not a.log_frame.winfo_manager()
    original_convert = a.opts["convert"].get()
    original_keywords = a._keywords()
    for mode in ("scan", "analysis", "version", "fontfix", "dwg"):
        a.v_mode.set(mode)
        a._refresh_mode_ui()
        assert a.mode_frames[mode].winfo_manager()
        assert sum(bool(frame.winfo_manager()) for frame in a.mode_frames.values()) == 1
    assert a.opts["convert"].get() == original_convert
    assert a._keywords() == original_keywords
    a.v_mode.set("dwg")
    a._refresh_mode_ui()
    assert str(a.btn_start["state"]) == "disabled"        # DWG conversion does need accoreconsole
    a._toggle_keywords()
    assert a.kw_frame.winfo_manager()
    a._toggle_keywords()
    assert not a.kw_frame.winfo_manager()
    a._toggle_more_results()
    assert a.more_results.winfo_manager()
    a._toggle_more_results()
    assert not a.more_results.winfo_manager()
    a.v_mode.set("analysis")
    a._refresh_mode_ui()
    assert str(a.btn_analysis["state"]) == "normal"       # non-DWG analysis stays available


def test_shared_run_shortcut_dispatches_selected_mode(monkeypatch, tk_root):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)
    called = []
    actions = {
        "scan": "_scan",
        "version": "_scan_versions",
        "fontfix": "_start_fontfix",
        "dwg": "_start",
        "analysis": "_start_analysis",
    }
    for mode, method in actions.items():
        monkeypatch.setattr(a, method, lambda selected=mode: called.append(selected))
        a.v_mode.set(mode)
        assert a._run_selected() == "break"
    assert called == list(actions)


def test_primary_result_selection(tmp_path):
    import app as appmod
    out = tmp_path / "out"
    (out / "csv").mkdir(parents=True)
    (out / "cross_reference").mkdir()
    (out / "database").mkdir()

    versions = out / "csv" / "dwg_versions.csv"
    versions.touch()
    assert appmod.get_primary_result("version", out) == versions

    file_index = out / "csv" / "file_index.csv"
    file_index.touch()
    assert appmod.get_primary_result("dwg", out) == file_index
    scada = out / "csv" / "scada_hits.csv"
    scada.touch()
    assert appmod.get_primary_result("dwg", out) == scada

    fontfix_csv = out / "logs" / "fontfix_results.csv"
    fontfix_csv.parent.mkdir(parents=True, exist_ok=True)
    fontfix_csv.touch()
    assert appmod.get_primary_result("fontfix", out) == fontfix_csv

    project_db = out / "database" / "project.db"
    project_db.touch()
    assert appmod.get_primary_result("analysis", out) == project_db
    cross_reference = out / "cross_reference" / "cross_reference.csv"
    cross_reference.touch()
    assert appmod.get_primary_result("analysis", out) == cross_reference
    assert appmod.get_primary_result("scan", out) is None


@pytest.fixture
def env(tmp_path):
    inp = tmp_path / "測試 資料"          # Chinese + space, as required
    (inp / "sub").mkdir(parents=True)
    shutil.copy(FIXTURE, inp / "圖面 01.dwg")
    shutil.copy(FIXTURE, inp / "sub" / "圖面 01.dwg")   # duplicate name
    (inp / "bad.dwg").write_text("not a dwg")
    cfg = dict(CFG, input_dir=str(inp), output_dir=str(tmp_path / "輸出 out"))
    return inp, cfg


def test_scan_readonly_counts(env):
    inp, _ = env
    before = sorted((p.name, p.stat().st_mtime) for p in inp.rglob("*"))
    r = pipeline.scan(inp)
    assert r["dwg"] == 3 and r["folders"] == 1
    assert r["chinese"] == 2 and r["spaces"] == 3 and r["duplicates"] == 1
    assert before == sorted((p.name, p.stat().st_mtime) for p in inp.rglob("*"))


@pytest.mark.accore
def test_full_pipeline_chinese_paths_and_error_isolation(env):
    inp, cfg = env
    hashes = {p: sha(p) for p in inp.rglob("*.dwg")}
    logs = []
    prog = []
    s = pipeline.run_pipeline(cfg, {}, log=lambda l, m: logs.append((l, m)),
                              progress=lambda *a: prog.append(a))
    assert not s["fatal"] and not s["stopped"]
    assert (s["dwg"], s["ok"], s["failed"]) == (3, 2, 1)
    assert s["dxf"] == 2 and s["keyword_hits"] > 0 and 0 < s["unique_objects"] <= s["keyword_hits"]
    out = Path(cfg["output_dir"])
    assert (out / "dxf" / "圖面 01.dxf").exists() and (out / "dxf" / "sub" / "圖面 01.dxf").exists()
    assert (out / "logs" / "gui.log").exists() and (out / "logs" / "errors.log").exists()
    assert any(l == "ERROR" and "bad.dwg" in m for l, m in logs)
    assert any(l == "INFO" and "continue" in m for l, m in logs)   # batch went on after failure
    assert prog and prog[-1][0] == "SCADA"
    assert all(sha(p) == h for p, h in hashes.items())              # originals untouched
    with open(out / "csv" / "scada_hits.csv", encoding="utf-8-sig") as f:
        assert len(list(csv.DictReader(f))) == s["keyword_hits"]


@pytest.mark.accore
def test_option_toggles(env):
    _, cfg = env
    s = pipeline.run_pipeline(cfg, dict(scada=False, csv=False, json=False, analyze=True))
    out = Path(cfg["output_dir"])
    assert not (out / "csv" / "file_index.csv").exists()
    assert not (out / "json").exists() or not list((out / "json").rglob("*.json"))
    assert not (out / "csv" / "scada_hits.csv").exists()
    assert s["ok"] == 2


@pytest.mark.accore
def test_stop_before_start(env):
    _, cfg = env
    ev = threading.Event()
    ev.set()
    s = pipeline.run_pipeline(cfg, {}, stop_event=ev)
    assert s["stopped"] and s["ok"] == 0
    assert not list((Path(cfg["output_dir"]) / "dxf").rglob("*.dxf"))


@pytest.mark.accore
def test_stop_during_accoreconsole(env):
    _, cfg = env
    ev = threading.Event()
    seen = []

    def progress(stage, i, n, cur):
        if stage == "DWG→DXF" and i == 2 and not seen:
            seen.append(1)
            threading.Timer(0.3, ev.set).start()   # fires while file 2 is inside accoreconsole
    t0 = time.time()
    s = pipeline.run_pipeline(cfg, {}, progress=progress, stop_event=ev)
    assert s["stopped"]
    assert time.time() - t0 < 60
    out = Path(cfg["output_dir"]) / "dxf"
    assert not list(out.rglob("*.dxf")) or len(list(out.rglob("*.dxf"))) <= 1
    import convert_single
    tmp = convert_single.safe_tmpdir(Path(cfg["output_dir"]) / "logs")
    assert " " not in str(tmp) and str(tmp).isascii()
    assert not list(tmp.glob("*.dxf"))


def test_fatal_when_accore_missing(env):
    _, cfg = env
    s = pipeline.run_pipeline(dict(cfg, accoreconsole=r"C:\nope\accoreconsole.exe"), {})
    assert s["fatal"] and s["ok"] == 0


@pytest.mark.accore
def test_gui_smoke(env, tk_root):
    import app as appmod
    inp, cfg = env
    root = tk_root
    _fresh(root)
    if True:
        a = appmod.App(root)
        a.v_in.set(str(inp))
        a.v_out.set(cfg["output_dir"])
        deadline = time.time() + 60
        while not a.env_ok and time.time() < deadline:      # env check finished via queue
            root.update()
            time.sleep(0.05)
        assert a.env_ok and str(a.btn_start["state"]) == "normal"
        a._scan()
        while "找到 DWG：3" not in a.v_scan.get() and time.time() < deadline:
            root.update()
            time.sleep(0.05)
        assert "找到 DWG：3" in a.v_scan.get()
        a._start()
        assert str(a.btn_stop["state"]) == "normal"
        while a.summary is None and time.time() < deadline + 120:
            root.update()
            time.sleep(0.05)
        assert a.summary and a.summary["ok"] == 2 and a.summary["failed"] == 1
        assert "Completed with warnings" in a.v_result.get()   # 2 ok + 1 failed is not "failed"
        assert "bad.dwg" in a.v_result.get()
        assert str(a.btn_start["state"]) == "normal"


def test_gui_partial_failure_shows_warnings_not_total_failure(tk_root):
    import app as appmod
    root = tk_root
    _fresh(root)
    if True:
        a = appmod.App(root)
        a._on_done(dict(dwg=100, ok=99, failed=1, skipped=0, failed_files=["bad.dwg: DXF_INVALID x"],
                        analyze_failed=0, dxf=99, keyword_hits=5, unique_objects=3, suspect_texts=0,
                        elapsed=3.0, stopped=False, fatal=""))
        text = a.v_result.get()
        assert "Completed with warnings" in text and "中止" not in text
        assert "成功：99" in text and "失敗：1" in text and "略過：0" in text   # vocabulary unified: 成功 / 略過 / 警告 / 失敗
        assert "bad.dwg" in text
        a._on_done(dict(dwg=3, ok=3, failed=0, skipped=0, failed_files=[], analyze_failed=0, dxf=3,
                        keyword_hits=0, unique_objects=0, suspect_texts=0, elapsed=1.0, stopped=False,
                        fatal=""))
        assert "Completed with warnings" not in a.v_result.get()


# ---- outcome vocabulary (成功 / 略過 / 警告 / 失敗): text + symbol + colour, never colour alone ----
def test_classify_outcome_states():
    import app as appmod
    c = appmod.classify_outcome
    base = dict(fatal="", stopped=False, failed=0, ok=3, skipped=0, warnings=0, failed_files=[])
    assert c(base)[0] == "success"
    assert c(dict(base, failed=1, failed_files=["x.dwg: DXF_INVALID"]))[0] == "warning"          # partial failure
    assert c(dict(base, ok=0, failed=2, failed_files=["a", "b"]))[0] == "failed"                # nothing succeeded
    assert c(dict(base, fatal="boom"))[0] == "failed"
    assert c(dict(base, stopped=True))[0] == "warning"
    assert c(dict(base, warnings=2))[0] == "warning"
    assert c(dict(base, ok=0, skipped=3))[0] == "skipped"
    assert "Completed with warnings" in c(dict(base, failed=1, failed_files=["x"]))[1]


def test_every_state_has_label_symbol_and_readable_contrast():
    import app as appmod

    def lum(h):
        r, g, b = (int(h[i:i + 2], 16) / 255 for i in (1, 3, 5))
        f = lambda v: v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
        return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)

    def ratio(a, b):
        la, lb = sorted((lum(a), lum(b)), reverse=True)
        return (la + 0.05) / (lb + 0.05)
    assert len({(v["label"], v["symbol"]) for v in appmod.STATE.values()}) >= 5
    for name, v in appmod.STATE.items():
        assert v["label"] and v["symbol"], name
        assert ratio(v["fg"], v["bg"]) >= 7.0, (name, ratio(v["fg"], v["bg"]))          # banner text
    for name, fg in appmod.LOG_FG.items():
        assert ratio(fg, "#FFFFFF") >= 4.5, (name, ratio(fg, "#FFFFFF"))               # coloured log text on white


@pytest.mark.parametrize("raw,expect", [
    ("C:/in/bad.dwg: DXF_INVALID AutoCAD reported an error opening this DWG (invalid/corrupt file?)", "bad.dwg — 此 DWG 無法開啟或已損毀"),
    ("big.dwg: DXF_INVALID timeout after 300s", "big.dwg — 轉檔逾時"),
    ("C:/in/spec.pdf: SKIPPED_DEPENDENCY: ImportError: pdfplumber", "spec.pdf — 缺少選用套件"),
    ("(analyze) x.dxf: ERROR: KeyError: 'a'", "分析階段：x.dxf — 處理失敗"),
    ("plain text without a code", "plain text without a code"),
])
def test_friendly_failure(raw, expect):
    import app as appmod
    assert expect in appmod.friendly_failure(raw)


def test_gui_banner_progress_and_log_colours(tk_root):
    import app as appmod
    root = tk_root
    _fresh(root)
    a = appmod.App(root)
    ok = dict(dwg=3, ok=2, failed=1, skipped=0, failed_files=["C:/x/bad.dwg: DXF_INVALID corrupt file"], analyze_failed=0,
              dxf=2, keyword_hits=0, unique_objects=0, suspect_texts=0, elapsed=1.0, stopped=False, fatal="")
    a._on_done(ok)
    st = appmod.STATE["warning"]
    assert a.lbl_status.cget("bg") == st["bg"] and a.lbl_status.cget("fg") == st["fg"]
    assert st["symbol"] in a.lbl_status.cget("text") and st["label"] in a.lbl_status.cget("text")
    assert a.progress_bar._color == st["bar"]
    assert "bad.dwg — 此 DWG 無法開啟或已損毀" in a.v_result.get()
    a._on_done(dict(ok, failed=0, failed_files=[]))
    assert appmod.STATE["success"]["label"] in a.lbl_status.cget("text")
    assert a.progress_bar._color == appmod.STATE["success"]["bar"]
    a._log("WARN", "w")
    a._log("ERROR", "e")
    a._log("OK", "o")
    text = a.log_box.get("1.0", "end")
    assert "⚠ 警告" in text and "✗ 失敗" in text and "✓ 成功" in text
    assert a.log_box.tag_ranges("warning") and a.log_box.tag_ranges("failed") and a.log_box.tag_ranges("success")

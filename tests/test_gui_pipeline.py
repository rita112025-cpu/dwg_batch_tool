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
    assert a.log_frame.winfo_manager()
    a._toggle_env_details()
    assert a.env_details.winfo_manager()
    a._toggle_env_details()
    assert not a.env_details.winfo_manager()
    a._toggle_log()
    assert not a.log_frame.winfo_manager()
    a._toggle_log()
    assert a.log_frame.winfo_manager()
    original_convert = a.opts["convert"].get()
    original_keywords = a._keywords()
    for mode in ("scan", "analysis", "version", "fontfix", "proxygraphics", "layerclass", "textextract", "textreplace", "dwg"):
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


def test_mode_picker_uses_checkbuttons_and_shows_every_checked_task(monkeypatch, tk_root):
    """② 我要做什麼？ is multi-select: every checked task keeps its own settings visible."""
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)
    assert a.mode_buttons and all(button.winfo_class() == "TCheckbutton" for button in a.mode_buttons)
    assert a._selected_modes() == ["version"]                  # same safe default as before
    assert str(a.btn_run_selected["state"]) == "normal"       # version headers do not need AutoCAD

    for key in ("scan", "version", "analysis"):
        a.v_modes[key].set(True)
        a._on_mode_toggle(key)
    assert a._selected_modes() == ["scan", "version", "analysis"]
    assert sum(bool(frame.winfo_manager()) for frame in a.mode_frames.values()) == 3
    assert a.v_mode.get() == "analysis"                        # most recently checked task
    assert "（3）" in a.btn_run_selected["text"]
    assert str(a.btn_run_selected["state"]) == "normal"

    a.v_modes["fontfix"].set(True)
    a._on_mode_toggle("fontfix")
    assert str(a.btn_run_selected["state"]) == "disabled"     # fontfix needs accoreconsole
    a.v_modes["fontfix"].set(False)
    a._on_mode_toggle("fontfix")
    assert str(a.btn_run_selected["state"]) == "normal"

    for key in a._selected_modes():
        a.v_modes[key].set(False)
        a._on_mode_toggle(key)
    assert a._selected_modes() == []
    assert a.mode_empty.winfo_manager()
    assert str(a.btn_run_selected["state"]) == "disabled"


def test_select_all_and_clear_all_buttons(monkeypatch, tk_root):
    """全選 / 全不選 refresh the checkboxes, ③, the start-button count and the order hint immediately."""
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)
    a.env_ok = True
    a._set_busy_ui(False)
    assert str(a.btn_select_all["state"]) == "normal"

    a.btn_select_all.invoke()
    assert a._selected_modes() == [key for key, _label in appmod.TASKS]
    assert all(var.get() for var in a.v_modes.values())
    assert sum(bool(frame.winfo_manager()) for frame in a.mode_frames.values()) == len(appmod.TASKS)
    assert a.btn_run_selected["text"] == f"開始所選任務（{len(appmod.TASKS)}）"
    assert all(label in a.lbl_selection["text"] for _key, label in appmod.TASKS)
    assert str(a.btn_run_selected["state"]) == "normal"

    a._set_busy_ui(True)                                     # both buttons lock during a run
    assert str(a.btn_select_all["state"]) == "disabled"
    assert str(a.btn_clear_all["state"]) == "disabled"
    a._set_busy_ui(False)

    a.btn_clear_all.invoke()
    assert a._selected_modes() == []
    assert not any(var.get() for var in a.v_modes.values())
    assert not any(frame.winfo_manager() for frame in a.mode_frames.values())
    assert a.mode_empty.winfo_manager()
    assert "請至少勾選一項任務" in a.lbl_selection["text"]
    assert str(a.btn_run_selected["state"]) == "disabled"
    assert a.btn_run_selected["text"] == "開始所選任務"

    a.v_modes["textextract"].set(True)                       # single selection still works after 全不選
    a._on_mode_toggle("textextract")
    assert a._selected_modes() == ["textextract"]
    assert a.mode_frames["textextract"].winfo_manager()
    assert a.btn_run_selected["text"] == "開始所選任務（1）"


def test_selected_tasks_run_in_order_and_share_one_summary(monkeypatch, tk_root, tmp_path):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)
    a.v_in.set(str(tmp_path))
    a.v_out.set(str(tmp_path / "out"))
    calls = []

    def fake_scan(path, recursive=True):
        calls.append(("scan", recursive))
        return dict(dwg=3, dxf=1, size=1024, folders=2, chinese=1, spaces=0, duplicates=0)

    def fake_versions(inputs, csv_path, recursive=True):
        calls.append(("version", recursive))
        return [dict(status="OK", extension=".dwg"), dict(status="OK", extension=".dwt")]

    monkeypatch.setattr(appmod.pipeline, "scan", fake_scan)
    monkeypatch.setattr(appmod.dwg_version, "write_version_csv", fake_versions)
    for key in ("version", "scan"):
        a.v_modes[key].set(True)
        a._on_mode_toggle(key)
    assert a._selected_modes() == ["scan", "version"]          # UI order, not click order
    a._start_selected()
    deadline = time.time() + 15
    while a._batch is not None and time.time() < deadline:
        tk_root.update()
        time.sleep(0.01)
    assert calls == [("scan", True), ("version", True)]
    text = a.v_result.get()
    assert "批次完成（2 項任務）" in text
    assert "① 掃描資料夾" in text and "② DWG / DWT 版本表" in text
    assert "檔案：4" in text and "DWT：1" in text
    assert a.v_progress.get() == 100
    assert str(a.btn_run_selected["state"]) == "normal"


def test_batch_skips_only_the_invalid_tasks(monkeypatch, tk_root, tmp_path):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)
    a.v_in.set(str(tmp_path))
    a.v_out.set(str(tmp_path / "out"))
    warnings = []
    monkeypatch.setattr(appmod.messagebox, "showwarning", lambda title, msg: warnings.append((title, msg)))
    monkeypatch.setattr(appmod.messagebox, "showerror",
                        lambda title, msg: (_ for _ in ()).throw(AssertionError(f"unexpected error: {title}: {msg}")))
    calls = []

    def fake_versions(inputs, csv_path, recursive=True):
        calls.append(recursive)
        return [dict(status="OK", extension=".dwg")]

    monkeypatch.setattr(appmod.dwg_version, "write_version_csv", fake_versions)
    for key in ("version", "fontfix"):
        a.v_modes[key].set(True)
        a._on_mode_toggle(key)
    assert not a.env_ok                                     # fontfix cannot be prepared
    a._start_selected()
    deadline = time.time() + 15
    while a._batch is not None and time.time() < deadline:
        tk_root.update()
        time.sleep(0.01)
    assert warnings and "缺字體修復" in warnings[0][1] and "accoreconsole" in warnings[0][1]
    assert calls == [True]                                  # only the valid task ran
    assert "版本表完成" in a.v_result.get()


def test_stopping_a_batch_skips_the_tasks_that_did_not_start(monkeypatch, tk_root, tmp_path):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)
    a.env_ok = True
    folder = tmp_path / "in"
    folder.mkdir()
    a.v_in.set(str(folder))
    a.v_out.set(str(tmp_path / "out"))
    a._set_busy_ui(False)
    calls = []

    def fake_pipeline(cfg, opts, **kwargs):
        calls.append(dict(opts))
        kwargs["stop_event"].set()                          # user pressed stop during the first task
        return dict(dwg=1, ok=1, failed=0, skipped=0, failed_files=[], analyze_failed=0,
                    dxf=1, keyword_hits=0, unique_objects=0, suspect_texts=0, elapsed=0.1,
                    stopped=True, fatal="")

    monkeypatch.setattr(appmod.pipeline, "run_pipeline", fake_pipeline)
    a.v_modes["version"].set(False)
    a._on_mode_toggle("version")                            # drop the default task for this test
    for key in ("dwg", "analysis"):
        a.v_modes[key].set(True)
        a._on_mode_toggle(key)
    a._start_selected()
    deadline = time.time() + 15
    while a._batch is not None and time.time() < deadline:
        tk_root.update()
        time.sleep(0.01)
    text = a.v_result.get()
    assert "已停止：完成 1 / 2 項任務" in text
    assert "② 工程資料分析：– 略過　已停止" in text
    assert len(calls) == 1 and not calls[0].get("multiformat")   # the queued analysis never started



def test_all_tasks_run_in_one_batch(monkeypatch, tk_root, tmp_path):
    """Every task keeps its own validation and result rendering when they share one batch."""
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)
    a.env_ok = True
    inp = tmp_path / "in"
    inp.mkdir()
    a.v_in.set(str(inp))
    a.v_out.set(str(tmp_path / "out"))
    a.v_tx_pattern.set("W12")
    a.v_rp_find.set("W12")
    a.v_rp_repl.set("W13")
    a._set_busy_ui(False)
    calls, captured = [], {}

    monkeypatch.setattr(appmod.pipeline, "scan",
                        lambda path, recursive=True: (calls.append("scan") or dict(
                            dwg=1, dxf=0, size=10, folders=1, chinese=0, spaces=0, duplicates=0)))
    monkeypatch.setattr(appmod.dwg_version, "write_version_csv",
                        lambda *args, **kwargs: (calls.append("version") or [dict(status="OK", extension=".dwg")]))

    def fake_fontfix(*args, **kwargs):
        calls.append("fontfix")
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, styles_replaced=0,
                    stopped=False, fatal="", failed_files=[], still_files=[], elapsed=0.1)

    def fake_proxy(*args, **kwargs):
        calls.append("proxygraphics")
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0,
                    stopped=False, fatal="", failed_files=[], elapsed=0.1)

    def fake_extract(*args, **kwargs):
        calls.append("textextract")
        captured["extract"] = kwargs
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, hits=2,
                    stopped=False, fatal="", failed_files=[], elapsed=0.1)

    def fake_replace(*args, **kwargs):
        calls.append("textreplace")
        captured["replace"] = kwargs
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, changes=1, unchanged=0,
                    stopped=False, fatal="", failed_files=[], elapsed=0.1)

    def fake_layer(*args, **kwargs):
        calls.append("layerclass")
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, moved=1, created=1,
                    unchanged=0, stopped=False, fatal="", failed_files=[], notes=[], elapsed=0.1)

    def fake_pipeline(cfg, opts, **kwargs):
        if opts.get("multiformat"):
            calls.append("analysis")
            return dict(total=1, ok=1, failed=0, skipped=0, warnings=0, fatal="",
                        stopped=False, failed_files=[])
        calls.append("dwg")
        return dict(dwg=1, ok=1, failed=0, skipped=0, failed_files=[], analyze_failed=0,
                    dxf=1, keyword_hits=0, unique_objects=0, suspect_texts=0, elapsed=0.1,
                    stopped=False, fatal="")

    monkeypatch.setattr(appmod.fontfix_batch, "run", fake_fontfix)
    monkeypatch.setattr(appmod.proxybatch, "run", fake_proxy)
    monkeypatch.setattr(appmod.textbatch, "run_extract", fake_extract)
    monkeypatch.setattr(appmod.textbatch, "run_replace", fake_replace)
    monkeypatch.setattr(appmod.layerbatch, "run", fake_layer)
    monkeypatch.setattr(appmod.pipeline, "run_pipeline", fake_pipeline)

    def fake_diff(*args, **kwargs):
        calls.append("diff")
        return dict(pairs=1, changed=0, identical=1, skipped=0, failed=0, added=0, removed=0,
                    modified=0, stopped=False, fatal="", failed_files=[], elapsed=0.1)

    def fake_layerstd(*args, **kwargs):
        calls.append("layerstd")
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, violations=0, errors=0,
                    stopped=False, fatal="", failed_files=[], elapsed=0.1)

    def fake_tb_read(*args, **kwargs):
        calls.append("tb_read")
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, blocks=1, csv="",
                    stopped=False, fatal="", failed_files=[], elapsed=0.1)

    def fake_tb_write(*args, **kwargs):
        calls.append("tb_write")
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, changes=1, unchanged=0,
                    stopped=False, fatal="", failed_files=[], elapsed=0.1)

    monkeypatch.setattr(appmod.dwgdiff, "run", fake_diff)
    monkeypatch.setattr(appmod.layerstd, "run", fake_layerstd)
    monkeypatch.setattr(appmod.titleblock_batch, "run_read", fake_tb_read)
    monkeypatch.setattr(appmod.titleblock_batch, "run_write", fake_tb_write)
    a.v_df_old.set(str(inp))
    a.v_df_new.set(str(inp))
    a.v_tb_csv.set(str(tmp_path / "titleblock_attrs.csv"))

    for key, _label in appmod.TASKS:
        a.v_modes[key].set(True)
        a._on_mode_toggle(key)
    assert len(a._selected_modes()) == len(appmod.TASKS)
    a._start_selected()
    deadline = time.time() + 20
    while a._batch is not None and time.time() < deadline:
        tk_root.update()
        time.sleep(0.01)
    assert calls == [key for key, _label in appmod.TASKS]
    assert captured["extract"]["pattern"] == "W12"
    assert captured["replace"]["find"] == "W12" and captured["replace"]["repl"] == "W13"
    text = a.v_result.get()
    assert f"批次完成（{len(appmod.TASKS)} 項任務）" in text
    for _key, label in appmod.TASKS:
        assert label in text
    assert a.v_progress.get() == 100


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

    proxy_csv = out / "logs" / "proxygraphics_results.csv"
    proxy_csv.touch()
    assert appmod.get_primary_result("proxygraphics", out) == proxy_csv

    hits = out / "logs" / "textextract_hits.csv"
    hits.touch()
    assert appmod.get_primary_result("textextract", out) == hits
    changes = out / "logs" / "textreplace_changes.csv"
    changes.touch()
    assert appmod.get_primary_result("textreplace", out) == changes
    moves = out / "logs" / "layerclass_moves.csv"
    moves.touch()
    assert appmod.get_primary_result("layerclass", out) == moves

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
        while ("找到 DWG：3" not in a.v_scan.get() or a._busy() or a._batch is not None) and time.time() < deadline:
            root.update()
            time.sleep(0.05)
        assert "找到 DWG：3" in a.v_scan.get()
        a._start()
        assert str(a.btn_stop["state"]) == "normal"
        while (a.summary is None or a._busy() or a._batch is not None) and time.time() < deadline + 120:
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


def test_every_workers_runner_forwards_the_worker_count(monkeypatch, tk_root):
    """Each runner of a batch that takes workers must pass it on, not only store it in params."""
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)

    class Params(dict):
        def __missing__(self, key):
            return key

    seen = {}
    for runner, module, func in (
        ("_run_fontfix", appmod.fontfix_batch, "run"),
        ("_run_extract", appmod.textbatch, "run_extract"),
        ("_run_replace", appmod.textbatch, "run_replace"),
        ("_run_layerclass", appmod.layerbatch, "run"),
    ):
        monkeypatch.setattr(module, func, lambda *args, _name=runner, **kwargs: seen.setdefault(_name, kwargs) and {})
        getattr(a, runner)(Params(workers=5), lambda *args: None, lambda *args: None)
        assert seen[runner]["workers"] == 5, runner


def test_workers_field_reaches_batches_and_is_clamped(monkeypatch, tk_root, tmp_path):
    """The ① 同時處理數 value feeds every batch that takes workers; bad input falls back to a safe count."""
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    _fresh(tk_root)
    a = appmod.App(tk_root)
    a.env_ok = True
    inp = tmp_path / "in"
    inp.mkdir()
    a.v_in.set(str(inp))
    a.v_out.set(str(tmp_path / "out"))
    a.v_tx_pattern.set("W12")
    for raw, expect in (("3", 3), ("99", appmod.parallel.MAX_WORKERS), ("abc", 1), ("0", 1)):
        a.v_workers.set(raw)
        assert a._prepare_extract()["workers"] == expect
        assert a._workers() == expect
        assert a._current_cfg()["workers"] == expect

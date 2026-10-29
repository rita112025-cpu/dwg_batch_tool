"""DWG diff mode: marker parsing, matching rules, folder pairing, and the batch with a fake accoreconsole."""
import csv
import hashlib
import os
import stat
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dwgdiff  # noqa: E402
import fontfix  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
OLD_LOG = FIXTURES / "dwgdiff_old.log"
NEW_LOG = FIXTURES / "dwgdiff_new.log"
SPACES_LOG = FIXTURES / "dwgdiff_spaces.log"
HEADER = b"AC1032" + b"\x00" * 32


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _text(path):
    return Path(path).read_text(encoding="utf-8")


def _dump_lines(log_path):
    """The DIFF lines of a fixture log, i.e. what the LISP prints for one drawing."""
    keep = ("DIFFROW", "DIFFATTR", "DIFFDUMP_SUMMARY")
    return [line for line in _text(log_path).splitlines() if line.startswith(keep)]


def _dwg(path, lines=None, extra=b""):
    """A fake DWG: a real AC1032 header, then the dump the fake accoreconsole will print."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = HEADER + extra
    if lines is not None:
        body += b"\nDIFFDUMP_BEGIN\n" + ("\n".join(lines) + "\n").encode("utf-8")
    path.write_bytes(body)


def _compare_fixtures(**kwargs):
    old = dwgdiff.parse_dump(_text(OLD_LOG))["rows"]
    new = dwgdiff.parse_dump(_text(NEW_LOG))["rows"]
    return dwgdiff.compare_rows(old, new, **kwargs)


def _props(result, handle):
    return {
        item["property"]: (item["old"], item["new"], item["match"])
        for item in result["changes"]
        if item["change"] == "MODIFIED" and item["new_handle"] == handle
    }


def _read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def test_parse_fixture_rows_attributes_and_escapes():
    parsed = dwgdiff.parse_dump(_text(OLD_LOG))
    assert parsed["ready"] is True
    assert parsed["summary"] == 11
    assert parsed["errors"] == []
    rows = {row["handle"]: row for row in parsed["rows"]}
    assert len(rows) == 11
    insert = rows["1A3"]
    assert insert["kind"] == "INSERT" and insert["block"] == "VALVE"
    assert insert["attrs"] == {"TAG": "V-101", "SIZE": "50"}
    assert insert["geom"]["sx"] == "1.00000000"
    mtext = rows["1A5"]
    assert mtext["layout"] == "Layout1"
    assert mtext["text"] == "{\\H2.5;A|=|B}\\P第二行"   # wire escapes and the |=| separator restored
    assert rows["1A6"]["geom"]["verts"].split()[-1] == "10.00000000,10.00000000"
    assert rows["1A7"]["geom"] == {} and rows["1A7"]["text"] == ""


def test_space_expanded_line_is_still_parsed():
    parsed = dwgdiff.parse_dump(_text(SPACES_LOG))
    assert parsed["summary"] == 1
    assert parsed["rows"][0]["handle"] == "1A0"
    assert parsed["rows"][0]["layer"] == "WALL"
    assert parsed["rows"][0]["geom"] == {"len": "100"}


def test_errors_and_duplicate_attribute_tags():
    text = "\n".join([
        "DIFFROW|=|AA|=|INSERT|=|Model|=|0|=|0|=|0|=|0|=||=||=|B",
        "DIFFATTR|=|aa|=|NO|=|1",
        "DIFFATTR|=|AA|=|NO|=|2",
        "DIFF_ERROR|=|bad thing",
        "DIFFDUMP_SUMMARY|=|0",
    ])
    parsed = dwgdiff.parse_dump(text)
    assert parsed["rows"][0]["attrs"] == {"NO": "1", "NO#2": "2"}
    assert parsed["errors"] == ["bad thing"]
    assert parsed["summary"] == 0
    assert parsed["ready"] is False


# ---------------------------------------------------------------------------
# Matching and classification
# ---------------------------------------------------------------------------

def test_fixture_classification_counts():
    result = _compare_fixtures()
    assert (result["added"], result["deleted"], result["modified"], result["unchanged"]) == (2, 2, 5, 4)
    assert result["by_handle"] == 7
    assert result["by_position"] == 2
    added = {item["new_handle"] for item in result["changes"] if item["change"] == "ADDED"}
    deleted = {item["old_handle"] for item in result["changes"] if item["change"] == "DELETED"}
    assert added == {"1B0", "1A7"}       # 1A7 is a TEXT now: same handle, different type
    assert deleted == {"1A4", "1A7"}     # the old 1A7 POINT


def test_modified_entities_list_each_changed_property():
    result = _compare_fixtures()
    assert _props(result, "1A1") == {"半徑": ("5", "7.5", "handle")}
    assert _props(result, "1A2") == {
        "圖層": ("NOTE", "NOTE-NEW", "handle"),
        "文字內容": ("PUMP-01", "PUMP-02", "handle"),
    }
    insert = _props(result, "1A3")
    assert insert["位置"] == ("200, 100, 0", "205, 100, 0", "handle")
    assert insert["屬性 SIZE"] == ("50", "80", "handle")
    assert set(insert) == {"位置", "屬性 SIZE"}
    assert set(_props(result, "1A6")) == {"頂點座標"}
    # different handle, same type and point: paired by position
    assert _props(result, "3C1") == {"半徑": ("3", "4", "position")}
    # 0 vs 0.00000000 and an omitted default scale are not changes
    assert _props(result, "1A0") == {} and _props(result, "1A8") == {}
    added_text = [i for i in result["changes"] if i["change"] == "ADDED" and i["new_handle"] == "1A7"][0]
    assert added_text["new"] == "TEXT「NEW」" and added_text["property"] == ""


def test_without_handles_only_position_pairs():
    result = _compare_fixtures(use_handle=False)
    assert result["by_handle"] == 0
    # the moved INSERT can no longer be paired; it becomes deleted + added
    assert (result["added"], result["deleted"]) == (3, 3)
    assert result["modified"] == 4
    assert {i["old_handle"] for i in result["changes"] if i["change"] == "DELETED"} == {"1A3", "1A4", "1A7"}


def test_tolerance_controls_numeric_changes():
    def row(handle, x, r):
        return {"handle": handle, "kind": "CIRCLE", "layout": "Model", "layer": "0", "x": x, "y": "0",
                "z": "0", "geom": {"r": r}, "text": "", "block": "", "attrs": {}}

    old = [row("A", "1.0", "5.0")]
    new = [row("A", "1.0000004", "5.0000004")]
    assert dwgdiff.compare_rows(old, new, tolerance=1e-6)["unchanged"] == 1
    tight = dwgdiff.compare_rows(old, new, tolerance=1e-9)
    assert tight["modified"] == 1
    assert {i["property"] for i in tight["changes"]} == {"位置", "半徑"}
    # no handle, near the same point: paired within tolerance, not added/deleted
    loose = dwgdiff.compare_rows([row("A", "1.0", "5")], [row("B", "1.0004", "5")], tolerance=1e-3)
    assert (loose["by_position"], loose["unchanged"], loose["added"]) == (1, 1, 0)


def test_position_match_prefers_the_closest_candidate():
    def text(handle, body):
        return {"handle": handle, "kind": "TEXT", "layout": "Model", "layer": "0", "x": "0", "y": "0",
                "z": "0", "geom": {}, "text": body, "block": "", "attrs": {}}

    result = dwgdiff.compare_rows([text("A", "B")], [text("X", "A"), text("Y", "B")])
    assert result["unchanged"] == 1 and result["added"] == 1
    assert [i["new_handle"] for i in result["changes"] if i["change"] == "ADDED"] == ["X"]


# ---------------------------------------------------------------------------
# LISP and script
# ---------------------------------------------------------------------------

def test_lisp_is_read_only_and_script_is_ascii(tmp_path):
    src = dwgdiff.build_dump_lisp()
    assert fontfix._lisp_parens_balanced(src)
    assert fontfix.MARKER_SEP in src
    assert "(defun dd-dump-current" in src and "DIFFDUMP_READY" in src
    code = "\n".join(line.split(";;;", 1)[0] for line in src.splitlines())
    lowered = code.lower()
    for forbidden in ("entmod", "entmake", "entdel", "qsave", "saveas", "vla-"):
        assert forbidden not in lowered
    assert fontfix.lisp_bytes(src).startswith(b"\xef\xbb\xbf")
    jobs = [{"spath": "C:/Temp/old_a.dwg"}, {"spath": "C:/Temp/new_a.dwg"}]
    script = dwgdiff.build_dump_script(jobs, tmp_path / "dwgdiff_dump.lsp")
    script.encode("ascii")
    assert script.count("_.OPEN") == 2
    assert "QSAVE" not in script and "SAVEAS" not in script
    assert 'DIFF_" "ERROR' in script            # an echo never starts with the full marker
    assert script.rstrip().endswith("_.QUIT")


# ---------------------------------------------------------------------------
# Folders
# ---------------------------------------------------------------------------

def test_folder_rules(tmp_path):
    old, new, out = tmp_path / "舊版", tmp_path / "新版", tmp_path / "out"
    old.mkdir()
    new.mkdir()
    assert dwgdiff.folder_problem("", new, out) == "請選擇舊版資料夾"
    assert dwgdiff.folder_problem(old, "", out) == "請選擇新版資料夾"
    assert dwgdiff.folder_problem(old, new, "") == "請指定輸出資料夾"
    assert dwgdiff.folder_problem(tmp_path / "x", new, out) == "舊版資料夾不存在"
    assert dwgdiff.folder_problem(old, old, out) == "舊版與新版資料夾不可相同"
    assert dwgdiff.folder_problem(old, new, new) == "輸出資料夾不可與新版資料夾相同"
    assert dwgdiff.folder_problem(old, new, old / "logs") == "輸出資料夾不可放在舊版資料夾裡面"
    assert dwgdiff.folder_problem(old, new, out) is None


def test_pairing_by_relative_path_ignores_case(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _dwg(old / "A.dwg")
    _dwg(old / "sub" / "B.DWG")
    _dwg(old / "only_old.dwg")
    _dwg(new / "a.dwg")
    _dwg(new / "sub" / "b.dwg")
    _dwg(new / "B.dwg")            # same name, different folder: not the pair of sub/B
    _dwg(new / "only_new.dwg")
    (new / "notes.txt").write_text("x", encoding="utf-8")
    pairs, only_old, only_new = dwgdiff.pair_files(old, new, recursive=True)
    assert [(name, Path(a).name, Path(b).name) for name, a, b in pairs] == [
        ("A.dwg", "A.dwg", "a.dwg"), ("sub/B.DWG", "B.DWG", "b.dwg")]
    assert [name for name, _p in only_old] == ["only_old.dwg"]
    assert [name for name, _p in only_new] == ["B.dwg", "only_new.dwg"]
    flat_pairs, flat_old, _flat_new = dwgdiff.pair_files(old, new, recursive=False)
    assert [name for name, _a, _b in flat_pairs] == ["A.dwg"]
    assert [name for name, _p in flat_old] == ["only_old.dwg"]


# ---------------------------------------------------------------------------
# Batch with the fake accoreconsole
# ---------------------------------------------------------------------------

def _run(old, new, out, **kwargs):
    return dwgdiff.run(old, new, out, FAKE, timeout=30, **kwargs)


def test_batch_chinese_paths_writes_csv_html_and_leaves_sources(tmp_path):
    old = tmp_path / "舊版 圖面"
    new = tmp_path / "新版 圖面"
    out = tmp_path / "比對 結果"
    _dwg(old / "平面 圖.dwg", _dump_lines(OLD_LOG))
    _dwg(new / "平面 圖.dwg", _dump_lines(NEW_LOG))
    _dwg(old / "sub" / "same.dwg", _dump_lines(OLD_LOG))
    _dwg(new / "sub" / "same.dwg", _dump_lines(OLD_LOG))
    _dwg(old / "removed.dwg", [])
    _dwg(new / "added.dwg", [])
    sources = sorted(p for p in tmp_path.rglob("*.dwg"))
    before = {p: _sha(p) for p in sources}

    summary = _run(old, new, out)

    assert summary["fatal"] == ""
    assert (summary["dwg"], summary["ok"], summary["failed"], summary["skipped"]) == (2, 2, 0, 2)
    assert (summary["added"], summary["deleted"], summary["modified"]) == (2, 2, 5)
    assert summary["changed_files"] == 1
    assert sorted(summary["unpaired"]) == ["added.dwg（只有新版）", "removed.dwg（只有舊版）"]
    assert {p: _sha(p) for p in sources} == before
    assert not list(out.rglob("*.dwg"))            # read-only mode: no drawing written, temp copies removed

    logs = out / "logs"
    changes = _read_csv(logs / "dwgdiff_changes.csv")
    assert list(changes[0].keys()) == dwgdiff.CHANGE_FIELDS
    assert {row["比對檔案"] for row in changes} == {"平面 圖.dwg"}
    kinds = [row["變更類型"] for row in changes]
    assert kinds.count("新增") == 2 and kinds.count("刪除") == 2
    radius = [row for row in changes if row["新 handle"] == "1A1"]
    assert [(r["變更屬性"], r["舊值"], r["新值"], r["比對方式"]) for r in radius] == [("半徑", "5", "7.5", "handle")]
    assert any(r["比對方式"] == "座標+類型" and r["新 handle"] == "3C1" for r in changes)

    results = {row["比對檔案"]: row for row in _read_csv(logs / "dwgdiff_results.csv")}
    assert results["平面 圖.dwg"]["狀態"] == "有差異"
    assert results["sub/same.dwg"]["狀態"] == "無差異"
    assert results["sub/same.dwg"]["未變更"] == "11"
    assert results["removed.dwg"]["狀態"] == "只有舊版"
    assert results["added.dwg"]["狀態"] == "只有新版"

    report = (logs / "dwgdiff_report.html").read_text(encoding="utf-8")
    assert report.startswith("<!doctype html>")
    assert "DWG 圖面差異比對報告" in report and "平面 圖.dwg" in report
    assert "PUMP-02" in report and "屬性 SIZE" in report
    assert "<script" not in report and "http" not in report     # self-contained
    assert (logs / "dwgdiff.log").is_file()
    assert any(logs.glob("dwgdiff_batch_*.stdout.log"))


def test_html_escapes_drawing_text(tmp_path):
    old, new, out = tmp_path / "old", tmp_path / "new", tmp_path / "out"
    _dwg(old / "a.dwg", ["DIFFROW|=|A|=|TEXT|=|Model|=|0|=|0|=|0|=|0|=||=|<b>old</b>|=|", "DIFFDUMP_SUMMARY|=|1"])
    _dwg(new / "a.dwg", ["DIFFROW|=|A|=|TEXT|=|Model|=|0|=|0|=|0|=|0|=||=|<script>x</script>|=|",
                         "DIFFDUMP_SUMMARY|=|1"])
    summary = _run(old, new, out)
    assert summary["modified"] == 1
    report = (out / "logs" / "dwgdiff_report.html").read_text(encoding="utf-8")
    assert "&lt;script&gt;x&lt;/script&gt;" in report and "<script>" not in report
    assert "&lt;b&gt;old&lt;/b&gt;" in report


def test_crashed_drawing_fails_only_its_pair(tmp_path):
    old, new, out = tmp_path / "old", tmp_path / "new", tmp_path / "out"
    _dwg(old / "a_bad.dwg", _dump_lines(OLD_LOG))
    _dwg(new / "a_bad.dwg", _dump_lines(NEW_LOG), extra=b"FONTFIX_TEST_CRASH")
    _dwg(old / "b_ok.dwg", _dump_lines(OLD_LOG))
    _dwg(new / "b_ok.dwg", _dump_lines(NEW_LOG))
    summary = _run(old, new, out)
    assert summary["failed"] == 1 and summary["ok"] == 1
    by_name = {rec["name"]: rec for rec in summary["results"]}
    assert by_name["a_bad.dwg"]["status"] == "FAILED"
    assert "新版" in by_name["a_bad.dwg"]["error"] and "對話框" in by_name["a_bad.dwg"]["error"]
    assert by_name["b_ok.dwg"]["status"] == "DIFF"


def test_incomplete_dump_is_a_failure_not_deletions(tmp_path):
    old, new, out = tmp_path / "old", tmp_path / "new", tmp_path / "out"
    lines = _dump_lines(OLD_LOG)
    _dwg(old / "a.dwg", lines)
    _dwg(new / "a.dwg", [line for line in lines if "1A4" not in line])   # summary still says 11
    summary = _run(old, new, out)
    rec = summary["results"][0]
    assert rec["status"] == "FAILED"
    assert "不完整" in rec["error"] and rec["deleted"] == 0
    assert _read_csv(out / "logs" / "dwgdiff_changes.csv") == []


def test_lisp_error_fails_the_pair(tmp_path):
    old, new, out = tmp_path / "old", tmp_path / "new", tmp_path / "out"
    _dwg(old / "a.dwg", _dump_lines(OLD_LOG))
    _dwg(new / "a.dwg", ["DIFF_ERROR|=|bad DXF group", "DIFFDUMP_SUMMARY|=|0"])
    rec = _run(old, new, out)["results"][0]
    assert rec["status"] == "FAILED" and "bad DXF group" in rec["error"]


def test_readonly_sources_and_fatal_cases(tmp_path):
    old, new, out = tmp_path / "old", tmp_path / "new", tmp_path / "out"
    _dwg(old / "a.dwg", _dump_lines(OLD_LOG))
    _dwg(new / "a.dwg", _dump_lines(OLD_LOG))
    for path in (old / "a.dwg", new / "a.dwg"):
        os.chmod(path, stat.S_IREAD)
    try:
        before = (_sha(old / "a.dwg"), _sha(new / "a.dwg"))
        summary = _run(old, new, out)
        assert summary["results"][0]["status"] == "SAME"
        assert (_sha(old / "a.dwg"), _sha(new / "a.dwg")) == before
    finally:
        for path in (old / "a.dwg", new / "a.dwg"):
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
    assert dwgdiff.run(old, new, out, tmp_path / "missing.exe")["fatal"] == "找不到 accoreconsole.exe"
    assert _run(old, old, out)["fatal"] == "舊版與新版資料夾不可相同"
    assert _run(old, new, out, tolerance="abc")["fatal"] == "座標容許誤差必須是數字"


def test_stop_before_start_marks_pairs_stopped(tmp_path):
    import threading
    old, new, out = tmp_path / "old", tmp_path / "new", tmp_path / "out"
    _dwg(old / "a.dwg", _dump_lines(OLD_LOG))
    _dwg(new / "a.dwg", _dump_lines(NEW_LOG))
    stop = threading.Event()
    stop.set()
    summary = _run(old, new, out, stop_event=stop)
    assert summary["stopped"] is True
    assert summary["results"][0]["status"] == "STOPPED"
    assert not list(out.rglob("*.dwg"))


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def test_dwgdiff_mode_widgets(monkeypatch, tk_root, tmp_path):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    for child in tk_root.winfo_children():
        child.destroy()
    app = appmod.App(tk_root)
    labels = {button.cget("text") for button in app.mode_buttons}
    assert "圖面差異比對" in labels
    assert str(app.btn_diff["state"]) == "disabled"          # needs accoreconsole
    app.env_ok = True
    app._set_busy_ui(False)
    app.v_mode.set("dwgdiff")
    app._refresh_mode_ui()
    assert app.mode_frames["dwgdiff"].winfo_manager()
    assert str(app.btn_diff["state"]) == "normal"
    assert app.v_df_handle.get() is True

    errors = []
    monkeypatch.setattr(appmod.messagebox, "showerror", lambda title, msg: errors.append(msg))
    old, new, out = tmp_path / "old", tmp_path / "new", tmp_path / "out"
    old.mkdir()
    new.mkdir()
    app.v_df_old.set(str(old))
    app.v_df_new.set(str(old))
    app.v_out.set(str(out))
    app._start_dwgdiff()
    assert errors and "相同" in errors[-1]
    app.v_df_new.set(str(new))
    app.v_df_tol.set("abc")
    app._start_dwgdiff()
    assert "容許誤差" in errors[-1]

    calls = {}

    def fake_run(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return dict(dwg=2, ok=2, failed=0, skipped=1, warnings=1, added=3, deleted=1, modified=4,
                    unchanged=9, changed_files=1, unpaired=["x.dwg（只有新版）"], stopped=False,
                    fatal="", failed_files=[], elapsed=0.1, results=[])

    monkeypatch.setattr(appmod.dwgdiff, "run", fake_run)
    app.v_df_tol.set("0.001")
    app.v_df_handle.set(False)
    app._start_dwgdiff()
    deadline = time.time() + 5
    while app.summary is None and time.time() < deadline:
        tk_root.update()
        time.sleep(0.05)
    assert calls["args"][:3] == (str(old), str(new), str(out))
    assert calls["kwargs"]["tolerance"] == 0.001
    assert calls["kwargs"]["use_handle"] is False
    shown = app.v_result.get()
    assert "新增：3" in shown and "刪除：1" in shown and "修改：4" in shown
    assert "x.dwg（只有新版）" in shown
    assert "警告" in shown
    report = out / "logs" / "dwgdiff_report.html"
    report.parent.mkdir(parents=True)
    report.write_text("<html></html>", encoding="utf-8")
    assert appmod.get_primary_result("dwgdiff", out) == report

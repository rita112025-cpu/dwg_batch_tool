"""Title-block attribute export / write-back: parsing, planning, and the fake accoreconsole path."""
import csv
import hashlib
import shutil
import sys
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fontfix  # noqa: E402
import titleblock  # noqa: E402
import titleblock_batch  # noqa: E402
from common import load_config  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"
SAMPLE = Path(__file__).resolve().parent / "fixtures" / "sample.dwg"
HEADER = b"AC1032" + b"\x00" * 32
PATTERNS = "TITLE*, 圖框*"

DUMP = "\n".join([
    "TBINSERT|=|Layout1|=|TITLE-A1|=|TITLE-A1|=|1A0",
    "TBATTR|=|1A0|=|DWG_NO|=|001|=|1A1|=|0",
    "TBATTR|=|1A0|=|NOTE|=|a|\\=|b|=|1A2|=|0",
    "TBATTR|=|1A0|=|NOTE|=|second|=|1A3|=|0",
    "TBINSERT|=|Model|=|*U12|=|圖框A3|=|2B0",
    "TBATTR|=|2B0|=|MEMO|=|多行|=|2B1|=|1",
    "TBINSERT|=|Model|=|DOOR|=||=|",
    "TBATTR|=|FFF|=|ORPHAN|=|x|=|9|=|0",
    "TBATTR|=|2B0|=|NOHANDLE|=|x|=||=|0",
    "TB_ERROR|=|",
    "TB_ERROR|=|bad thing",
    "TBDUMP_SUMMARY|=|x",
    "TBDUMP_SUMMARY|=|2",
    "TBDUMP_READY",
])


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dwg(path, extra=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEADER + extra)


def _rows(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def _write_rows(path, rows, fields, encoding="utf-8-sig"):
    with Path(path).open("w", encoding=encoding, newline="") as fh:
        writer = csv.DictWriter(fh, fields)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_patterns_split_validate_and_match():
    assert titleblock.parse_patterns("TITLE*, 圖框*；A?\n\n") == ["TITLE*", "圖框*", "A?"]
    assert titleblock.parse_patterns([" a ", ""]) == ["a"]
    assert titleblock.validate_patterns(" , ") is not None
    assert titleblock.validate_patterns("TITLE*") is None
    assert titleblock.name_matches(["title-a1"], ["TITLE*"])
    assert titleblock.name_matches(["", "圖框A3"], ["圖框*"])
    assert not titleblock.name_matches(["DOOR"], ["TITLE*"])


def test_parse_dump_groups_attributes_and_numbers_repeated_tags():
    parsed = titleblock.parse_dump(DUMP)
    assert parsed["ready"] is True
    assert parsed["summary"] == 2
    assert parsed["errors"] == ["bad thing"]
    assert [item["handle"] for item in parsed["inserts"]] == ["1A0", "2B0"]
    first, second = parsed["inserts"]
    assert [attr["column"] for attr in first["attrs"]] == ["DWG_NO", "NOTE", "NOTE#2"]
    assert first["attrs"][1]["value"] == "a|=|b"
    assert second["block_name"] == "圖框A3" and second["name"] == "*U12"
    assert second["attrs"][0]["multiline"] is True
    selected = titleblock.select_inserts(parsed["inserts"], ["圖框*"])
    assert [item["handle"] for item in selected] == ["2B0"]


def test_parse_apply():
    parsed = titleblock.parse_apply(
        "TBAPPLY|=|1a1|=|ok\nTBAPPLY|=||=|ok\nTBAPPLY_SUMMARY|=|x|=|y\nTBAPPLY_SUMMARY|=|1|=|0\nTB_ERROR|=|oops\n")
    assert parsed["summary"] == (1, 0)
    assert parsed["results"] == {"1A1": "ok"}
    assert parsed["errors"] == ["oops"]


def test_handles_survive_excel():
    assert titleblock.protect_handle("1E5") == '="1E5"'
    assert titleblock.protect_handle("2B0") == "2B0"
    assert titleblock.protect_handle(None) == ""
    assert titleblock.clean_handle('="1E5"') == "1E5"
    assert titleblock.clean_handle(" '2B0 ") == "2B0"
    assert titleblock.clean_handle(None) == ""


def test_csv_decoding_and_header_check():
    assert titleblock.decode_csv_bytes("圖號".encode("utf-8-sig")) == "圖號"
    assert titleblock.decode_csv_bytes("圖號".encode("cp950")) == "圖號"
    assert titleblock.decode_csv_bytes(b"\x81\xff") != ""
    with pytest.raises(ValueError, match="handle"):
        titleblock.read_attrs_csv("file,layout,block_name\n")
    rows, tags = titleblock.read_attrs_csv(
        " file ,layout,block_name,handle,TITLE\na.dwg,Model,T,=\"1E5\",x\n,,,,y\n")
    assert tags == ["TITLE"]
    assert len(rows) == 1 and rows[0]["handle"] == "1E5"


def test_plan_edits_covers_every_case():
    inserts = titleblock.parse_dump(DUMP)["inserts"]
    rows = [
        {"handle": "1a0", "layout": "Layout1", "block_name": "TITLE-A1",
         "DWG_NO": "1", "NOTE": "new", "NOTE#2": "second", "MEMO": "", "GONE": "x"},
        {"handle": "2B0", "MEMO": "改", "DWG_NO": ""},
        {"handle": "ZZZ"},
    ]
    edits, records = titleblock.plan_edits(rows, inserts, ["DWG_NO", "NOTE", "NOTE#2", "MEMO", "GONE", "ABSENT"])
    assert edits == [{"handle": "1A2", "value": "new"}]
    by_tag = {(r["handle"], r["tag"]): r for r in records}
    assert by_tag[("1a0", "DWG_NO")]["status"] == "SKIPPED"
    assert "Excel" in by_tag[("1a0", "DWG_NO")]["error"]
    assert by_tag[("1a0", "NOTE")]["status"] == "" and by_tag[("1a0", "NOTE")]["attr_handle"] == "1A2"
    assert by_tag[("1a0", "GONE")]["status"] == "FAILED"
    assert by_tag[("2B0", "MEMO")]["status"] == "SKIPPED"
    assert by_tag[("ZZZ", "")]["status"] == "FAILED"
    assert ("1a0", "MEMO") not in by_tag and ("2B0", "DWG_NO") not in by_tag
    row = titleblock.result_row(dict(by_tag[("1a0", "NOTE")], status="WRITTEN", file="a.dwg"))
    assert row["狀態"] == "已寫入" and row["原值"] == "a|=|b" and row["新值"] == "new"
    assert titleblock.read_row({"file": "a", "status": "DONE", "blocks": 2})["狀態"] == "已讀取"


def test_generated_lisp_and_scripts():
    dump = titleblock.build_dump_lisp()
    assert "TBDUMP_READY" in dump and "TB_ERROR|=|" in dump
    assert fontfix.lisp_bytes(dump).startswith(b"\xef\xbb\xbf")
    apply_src = titleblock.build_apply_lisp([[{"handle": "1A1", "value": '圖 "1"\n'}], []])
    assert '(list "1A1" "圖 \\"1\\"\\n")' in apply_src
    assert "(defun tb-apply-1 () (tb-apply-rows nil))" in apply_src
    jobs = [{"spath": "C:/temp/in_a.dwg"}, {"spath": "C:/temp/in_b.dwg"}]
    script = titleblock.build_dump_script(jobs, "C:\\temp\\tb.lsp")
    script.encode("ascii")
    assert "_.QSAVE" not in script and "(tb-dump-current)" in script
    assert '(load "C:/temp/tb.lsp")' in script
    apply_script = titleblock.build_apply_script(jobs, "C:/temp/tb.lsp")
    assert apply_script.count("_.QSAVE") == 2 and "(tb-apply-1)" in apply_script


def test_unbalanced_lisp_is_refused(monkeypatch):
    monkeypatch.setattr(titleblock.fontfix, "_lisp_parens_balanced", lambda src: False)
    with pytest.raises(RuntimeError):
        titleblock.build_dump_lisp()
    calls = iter([True, False])
    monkeypatch.setattr(titleblock.fontfix, "_lisp_parens_balanced", lambda src: next(calls))
    with pytest.raises(RuntimeError):
        titleblock.build_apply_lisp([[]])


# ---------------------------------------------------------------------------
# Batch runner against the fake accoreconsole
# ---------------------------------------------------------------------------

def _read(src, dst, **kwargs):
    kwargs.setdefault("patterns", PATTERNS)
    return titleblock_batch.run_read(src, dst, FAKE, timeout=30, **kwargs)


def _write(src, dst, csv_path, **kwargs):
    return titleblock_batch.run_write(src, dst, FAKE, csv_path=csv_path, timeout=30, **kwargs)


def test_read_exports_matching_blocks_and_leaves_sources(tmp_path):
    src = tmp_path / "輸入 資料"
    first = src / "A01 圖面.dwg"
    second = src / "子資料夾" / "b.dwg"
    _dwg(first)
    _dwg(second)
    digest = _sha(first)
    dst = tmp_path / "輸出"
    seen = []
    summary = _read(src, dst, progress=lambda i, n, path: seen.append((i, n)))
    assert summary["fatal"] == ""
    assert (summary["dwg"], summary["ok"], summary["failed"], summary["blocks"]) == (2, 2, 0, 4)
    assert seen and seen[-1] == (2, 2)
    assert _sha(first) == digest
    assert not list(dst.rglob("*.dwg"))
    csv_path = dst / "titleblock_attrs.csv"
    assert summary["csv"] == str(csv_path)
    assert csv_path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = _rows(csv_path)
    assert list(rows[0]) == ["file", "layout", "block_name", "handle", "DWG_NO", "TITLE", "DATE", "NOTE"]
    assert [row["file"] for row in rows] == ["A01 圖面.dwg", "A01 圖面.dwg", "子資料夾/b.dwg", "子資料夾/b.dwg"]
    assert rows[0]["TITLE"] == "一樓平面圖" and rows[0]["DWG_NO"] == "001"
    assert rows[1]["block_name"] == "圖框A3" and rows[1]["handle"] == '="1E5"' and rows[1]["TITLE"] == ""
    results = (dst / "logs" / "titleblock_read_results.csv").read_text(encoding="utf-8-sig")
    assert "已讀取" in results


def test_read_without_match_and_bad_inputs(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    empty = _read(src, tmp_path / "none", patterns="NOPE*", recursive=False)
    assert empty["ok"] == 1 and empty["blocks"] == 0
    assert list(_rows(tmp_path / "none" / "titleblock_attrs.csv")) == []
    assert "無圖框" in (tmp_path / "none" / "logs" / "titleblock_read_results.csv").read_text(encoding="utf-8-sig")
    assert "圖框" in _read(src, tmp_path / "x", patterns=" ")["fatal"]
    assert "accoreconsole" in titleblock_batch.run_read(src, tmp_path / "x", tmp_path / "no.exe", patterns="T*")["fatal"]
    assert "相同" in _read(src, src)["fatal"]
    blocked = tmp_path / "blocked"
    blocked.write_text("file, not a folder")
    assert "無法寫入" in _read(src, blocked)["fatal"]


def test_read_reports_lisp_not_loaded(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg", b"TB_TEST_NOREADY")
    logs = []
    summary = _read(src, tmp_path / "out", log=lambda level, message: logs.append(level))
    assert summary["failed"] == 1 and "LISP" in summary["failed_files"][0]
    assert "ERROR" in logs


def test_read_stop_before_start(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    stop = threading.Event()
    stop.set()
    summary = _read(src, tmp_path / "out", stop_event=stop)
    assert summary["stopped"] is True and summary["ok"] == 0


def test_read_crash_is_reported_as_fatal(tmp_path, monkeypatch):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    monkeypatch.setattr(titleblock_batch, "_dump", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    summary = _read(src, tmp_path / "out")
    assert "boom" in summary["fatal"]
    assert "boom" in (tmp_path / "out" / "logs" / "titleblock_read.log").read_text(encoding="utf-8")


def _exported(tmp_path):
    src = tmp_path / "in"
    drawing = src / "圖 01.dwg"
    _dwg(drawing)
    staging = tmp_path / "export"
    assert _read(src, staging)["ok"] == 1
    return src, drawing, staging / "titleblock_attrs.csv"


def test_write_back_only_changed_values(tmp_path):
    src, drawing, csv_path = _exported(tmp_path)
    digest = _sha(drawing)
    rows = _rows(csv_path)
    fields = list(rows[0])
    rows[0]["TITLE"] = "二樓平面圖"
    rows[0]["DWG_NO"] = "1"
    rows[1]["NOTE"] = "改"
    _write_rows(csv_path, rows, fields)
    dst = tmp_path / "out"
    summary = _write(src, dst, csv_path)
    assert summary["fatal"] == ""
    assert (summary["dwg"], summary["ok"], summary["failed"], summary["changes"]) == (1, 1, 0, 1)
    assert _sha(drawing) == digest
    out = dst / "圖 01.dwg"
    assert out.is_file() and b"TBAPPLIED" in out.read_bytes()
    results = _rows(dst / "logs" / "titleblock_results.csv")
    by_tag = {(row["handle"], row["tag"]): row for row in results}
    assert by_tag[("1A0", "TITLE")]["狀態"] == "已寫入"
    assert by_tag[("1A0", "TITLE")]["原值"] == "一樓平面圖"
    assert by_tag[("1A0", "TITLE")]["輸出"] == str(out)
    assert by_tag[("1A0", "DWG_NO")]["狀態"] == "略過"
    assert by_tag[("1E5", "NOTE")]["狀態"] == "略過"
    assert len(results) == 3
    jsonl = (dst / "logs" / "titleblock_results.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(jsonl) == 3 and '"before"' in jsonl[0]


def test_write_dry_run_unchanged_and_cp950(tmp_path):
    src, _drawing, csv_path = _exported(tmp_path)
    same = _write(src, tmp_path / "same", csv_path)
    assert same["unchanged"] == 1 and same["changes"] == 0
    assert not list((tmp_path / "same").rglob("*.dwg"))
    assert "無變更" in (tmp_path / "same" / "logs" / "titleblock_results.csv").read_text(encoding="utf-8-sig")
    rows = _rows(csv_path)
    rows[0]["TITLE"] = "三樓"
    _write_rows(csv_path, rows, list(rows[0]), encoding="cp950")
    preview = _write(src, tmp_path / "preview", csv_path, dry_run=True)
    assert preview["ok"] == 1 and preview["changes"] == 1
    assert not list((tmp_path / "preview").rglob("*.dwg"))
    text = (tmp_path / "preview" / "logs" / "titleblock_results.csv").read_text(encoding="utf-8-sig")
    assert "預覽" in text and "三樓" in text


def test_write_reports_missing_files_handles_and_existing_output(tmp_path):
    src, drawing, csv_path = _exported(tmp_path)
    rows = _rows(csv_path)
    fields = list(rows[0])
    rows[0]["TITLE"] = "新"
    rows.append(dict(rows[0], handle="ZZZ"))
    rows.append(dict(rows[0], file="不存在.dwg"))
    rows.append(dict(rows[0], file="", layout="x"))
    rows.append(dict(rows[0], file=str(drawing)))
    _write_rows(csv_path, rows, fields)
    dst = tmp_path / "out"
    summary = _write(src, dst, csv_path)
    assert summary["dwg"] == 4
    assert summary["failed"] == 2
    assert any("找不到來源" in item for item in summary["failed_files"])
    assert any("file 欄是空的" in item for item in summary["failed_files"])
    results = _rows(dst / "logs" / "titleblock_results.csv")
    assert any(row["handle"] == "ZZZ" and row["狀態"] == "失敗" for row in results)
    again = _write(src, dst, csv_path)
    assert again["skipped"] == 2
    assert "略過" in (dst / "logs" / "titleblock_results.csv").read_text(encoding="utf-8-sig")
    forced = _write(src, dst, csv_path, overwrite=True)
    assert forced["skipped"] == 0 and forced["ok"] == 2


def test_write_bad_csv_and_inputs(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    bad = tmp_path / "bad.csv"
    bad.write_text("a,b\n1,2\n", encoding="utf-8")
    assert "CSV" in _write(src, tmp_path / "o", bad)["fatal"]
    assert "CSV" in _write(src, tmp_path / "o", tmp_path / "missing.csv")["fatal"]
    assert "相同" in _write(src, src, bad)["fatal"]
    good = tmp_path / "good.csv"
    good.write_text("file,layout,block_name,handle\n", encoding="utf-8")
    blocked = tmp_path / "blocked"
    blocked.write_text("x")
    assert "無法寫入" in _write(src, blocked, good)["fatal"]
    empty = _write(src, tmp_path / "empty", good)
    assert empty["fatal"] == "" and empty["dwg"] == 0


def test_write_stop_and_crash(tmp_path, monkeypatch):
    src, _drawing, csv_path = _exported(tmp_path)
    rows = _rows(csv_path)
    rows[0]["TITLE"] = "新"
    _write_rows(csv_path, rows, list(rows[0]))
    stop = threading.Event()
    stop.set()
    stopped = _write(src, tmp_path / "stopped", csv_path, stop_event=stop)
    assert stopped["stopped"] is True and stopped["ok"] == 0
    assert "已停止" in (tmp_path / "stopped" / "logs" / "titleblock_results.csv").read_text(encoding="utf-8-sig")

    real = titleblock_batch.textbatch._launch_all
    calls = []

    def stop_on_apply(jobs, *args):
        calls.append(len(jobs))
        if len(calls) == 2:
            for job in jobs:
                job["stopped_here"] = True
            return True
        return real(jobs, *args)

    monkeypatch.setattr(titleblock_batch.textbatch, "_launch_all", stop_on_apply)
    halted = _write(src, tmp_path / "halted", csv_path)
    assert halted["stopped"] is True
    assert "已停止" in (tmp_path / "halted" / "logs" / "titleblock_results.csv").read_text(encoding="utf-8-sig")

    monkeypatch.setattr(titleblock_batch, "_group_rows", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    assert "boom" in _write(src, tmp_path / "crash", csv_path)["fatal"]


def _apply_job(tmp_path, console, **extra):
    produced = tmp_path / "in_x.dwg"
    _dwg(produced)
    job = {
        "rec": {"status": "", "error": "", "output": ""},
        "console": console,
        "produced": produced,
        "dest": tmp_path / "out" / "x.dwg",
        "records": [
            {"status": "", "error": "", "attr_handle": "1a1"},
            {"status": "", "error": "", "attr_handle": "1A2"},
            {"status": "SKIPPED", "error": "多行", "attr_handle": "1A3"},
        ],
    }
    job.update(extra)
    return job


def test_finish_apply_branches(tmp_path, monkeypatch):
    job = _apply_job(tmp_path, "", launch_error="超時")
    titleblock_batch._finish_apply(job)
    assert job["rec"]["status"] == "FAILED" and job["rec"]["error"] == "超時"
    job = _apply_job(tmp_path, "")
    titleblock_batch._finish_apply(job)
    assert job["rec"]["status"] == "STOPPED"
    job = _apply_job(tmp_path, "TB_ERROR|=|bad")
    titleblock_batch._finish_apply(job)
    assert job["rec"]["error"] == "bad"
    job = _apply_job(tmp_path, "TBAPPLY|=|1A2|=|missing\nTBAPPLY_SUMMARY|=|0|=|1")
    titleblock_batch._finish_apply(job)
    assert job["rec"]["status"] == "FAILED" and "失敗 1" in job["rec"]["error"]
    job = _apply_job(tmp_path, "TBAPPLY|=|1A1|=|ok\nTBAPPLY|=|1A2|=|missing\nTBAPPLY_SUMMARY|=|1|=|1")
    titleblock_batch._finish_apply(job)
    assert job["rec"]["status"] == "WRITTEN"
    assert [r["status"] for r in job["records"]] == ["WRITTEN", "FAILED", "SKIPPED"]
    assert "missing" in job["records"][1]["error"]
    assert (tmp_path / "out" / "x.dwg").is_file()
    job = _apply_job(tmp_path, "TBAPPLY|=|1A1|=|ok\nTBAPPLY_SUMMARY|=|1|=|0")
    monkeypatch.setattr(titleblock_batch.textbatch, "_publish", lambda job: (False, "鎖定"))
    titleblock_batch._finish_apply(job)
    assert job["rec"]["status"] == "FAILED" and job["rec"]["error"] == "鎖定"
    job = _apply_job(tmp_path, "TBAPPLY|=|1A1|=|ok\nTBAPPLY_SUMMARY|=|1|=|0")
    monkeypatch.setattr(titleblock_batch.textbatch, "_publish",
                        lambda job: (_ for _ in ()).throw(OSError("disk")))
    titleblock_batch._finish_apply(job)
    assert "disk" in job["rec"]["error"]


def test_prepare_reports_unreadable_source(tmp_path, monkeypatch):
    src = tmp_path / "a.dwg"
    _dwg(src)
    monkeypatch.setattr(titleblock_batch, "sha256", lambda path: (_ for _ in ()).throw(OSError("locked")))
    jobs, temps = titleblock_batch._prepare([src], tmp_path / "logs")
    assert jobs[0]["rec"]["status"] == "FAILED" and temps == []
    monkeypatch.setattr(titleblock_batch, "sha256", lambda path: "x")
    monkeypatch.setattr(titleblock_batch.textbatch, "_copy_bytes",
                        lambda a, b: (_ for _ in ()).throw(OSError("locked")))
    jobs, temps = titleblock_batch._prepare([src], tmp_path / "logs")
    assert "locked" in jobs[0]["rec"]["error"] and temps == []
    assert titleblock_batch._file_key(src, tmp_path / "elsewhere") == str(src)


def test_accept_rejects_wrong_drawing(tmp_path):
    job = {"rec": {"status": "", "error": ""}, "console": "TBDUMP_SUMMARY|=|0",
           "opened_name": "other.dwg", "spath": "C:/t/in_a.dwg"}
    assert titleblock_batch._accept(job, True) is False
    assert "不同" in job["rec"]["error"]


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def test_titleblock_mode_widgets(monkeypatch, tk_root, tmp_path):
    import time
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    for child in tk_root.winfo_children():
        child.destroy()
    app = appmod.App(tk_root)
    assert "圖框屬性" in {button.cget("text") for button in app.mode_buttons}
    app.env_ok = True
    app.v_mode.set("titleblock")
    app._refresh_mode_ui()
    assert app.mode_frames["titleblock"].winfo_manager()
    assert str(app.btn_tb_read["state"]) == "normal" and str(app.btn_tb_write["state"]) == "normal"

    errors = []
    monkeypatch.setattr(appmod.messagebox, "showerror", lambda title, msg: errors.append(msg))
    src = tmp_path / "in"
    src.mkdir()
    out = tmp_path / "out"
    out.mkdir()
    app.v_in.set(str(src))
    app.v_out.set(str(src))
    app._start_tb_read()
    assert errors and "相同" in errors[-1]
    app.v_out.set(str(out))
    app.v_tb_blocks.set(" ")
    app._start_tb_read()
    assert "圖框" in errors[-1]
    app.v_tb_csv.set("")
    app._start_tb_write()
    assert "CSV" in errors[-1]

    calls = {}

    def fake(name, **extra):
        def run(*args, **kwargs):
            kwargs["progress"](1, 1, "a.dwg")
            kwargs["log"]("OK", "done")
            calls[name] = kwargs
            return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, blocks=2, changes=3,
                        unchanged=0, stopped=False, fatal="", failed_files=[], elapsed=0.1, **extra)
        return run

    def wait():
        deadline = time.time() + 5
        while app.summary is None and time.time() < deadline:
            tk_root.update()
            time.sleep(0.05)

    monkeypatch.setattr(appmod.titleblock_batch, "run_read", fake("read", csv=str(out / "titleblock_attrs.csv")))
    monkeypatch.setattr(appmod.titleblock_batch, "run_write", fake("write"))
    app.v_tb_blocks.set("TITLE*")
    app._start_tb_read()
    wait()
    assert calls["read"]["patterns"] == "TITLE*"
    assert "圖框數：2" in app.v_result.get()
    assert app.v_tb_csv.get() == str(out / "titleblock_attrs.csv")
    app.v_tb_dry.set(True)
    app._start_tb_write()
    wait()
    assert calls["write"]["csv_path"] == str(out / "titleblock_attrs.csv")
    assert calls["write"]["dry_run"] is True
    assert "變更筆數：3" in app.v_result.get()

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(appmod.titleblock_batch, "run_read", boom)
    app._start_tb_read()
    wait()
    assert "boom" in app.v_result.get()
    monkeypatch.setattr(appmod.titleblock_batch, "run_write", boom)
    app._start_tb_write()
    wait()
    assert "boom" in app.v_result.get()
    app.env_ok = False
    app._start_tb_read()
    app._start_tb_write()
    assert "accoreconsole" in errors[-1]


# ---------------------------------------------------------------------------
# Real AutoCAD
# ---------------------------------------------------------------------------

@pytest.mark.accore
def test_real_accore_read_runs(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    shutil.copyfile(SAMPLE, src / "sample.dwg")
    summary = titleblock_batch.run_read(src, tmp_path / "out", load_config()["accoreconsole"],
                                        patterns="*", timeout=300)
    assert summary["fatal"] == "" and summary["failed"] == 0
    assert (tmp_path / "out" / "titleblock_attrs.csv").is_file()

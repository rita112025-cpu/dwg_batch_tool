"""Text extract / replace rules, marker parsing, and the fake accoreconsole path."""
import csv
import hashlib
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dwgtext  # noqa: E402
import fontfix  # noqa: E402
import textbatch  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"
SPACES = Path(__file__).resolve().parent / "fixtures" / "textdump_spaces.log"
HEADER = b"AC1032" + b"\x00" * 32


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dwg(path, extra=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEADER + extra)


def _dump_line(*fields):
    return "TEXTDUMP|=|" + "|=|".join(dwgtext.escape_field(field) for field in fields)


# ---------------------------------------------------------------------------
# Matching and MTEXT
# ---------------------------------------------------------------------------

def test_contains_wildcard_regex_and_case():
    assert dwgtext.find_matches("W12 400x200", "w12", "contains", False) == ["W12"]
    assert dwgtext.find_matches("W12 400x200", "w12", "contains", True) == []
    assert dwgtext.find_matches("W12 400x200", "W12*", "wildcard", True) == ["W12 400x200"]
    assert dwgtext.find_matches("W12 400x200", "W99*", "wildcard", False) == []
    assert dwgtext.find_matches("W12 400x200", r"^(W\d+)\s.*$", "regex", False) == ["W12 400x200"]
    assert dwgtext.validate_rule("", "contains") == "請輸入搜尋文字"
    assert "無效" in dwgtext.validate_rule("(", "regex")
    assert dwgtext.validate_rule(r"(W\d+)", "regex", replacement=r"\1", for_replace=False) is None


def test_beam_regex_keeps_the_id_and_mtext_height():
    new, count = dwgtext.apply_replacement("W12 400x200", r"^(W\d+)\s.*$", r"\1", "regex", False)
    assert (new, count) == ("W12", 1)
    raw = r"{\H2.5;W12 400x200}"
    assert dwgtext.mtext_plain(raw) == "W12 400x200"
    spliced, lost = dwgtext.splice_mtext(raw, "W12")
    assert spliced == r"{\H2.5;W12}"
    assert lost is False
    plain_only, lost_plain = dwgtext.splice_mtext("備註 W12", "備註")
    assert plain_only == "備註" and lost_plain is False


def test_literal_replace_does_not_eat_backslashes():
    new, count = dwgtext.apply_replacement("a-b", "a", r"\1", "literal", False)
    assert new == r"\1-b" and count == 1


# ---------------------------------------------------------------------------
# Marker parsing, including space-expanded console output
# ---------------------------------------------------------------------------

def test_pipe_fields_keep_spaces_empty_block_and_escapes():
    body = "W12 |=| 400\n200"
    line = _dump_line("Model", "S-BEAM", "TEXT", "", body, "10.5", "20.25", "AB1", "0")
    parsed = dwgtext.parse_dump(line + "\nTEXTDUMP_SUMMARY|=|1\nTEXTDUMP_READY\n")
    assert parsed["ready"] is True
    assert parsed["summary"] == 1
    row = parsed["rows"][0]
    assert row["block"] == ""
    assert row["raw"] == body
    assert row["handle"] == "AB1"
    assert "TEXTDUMP_SUMMARY" not in [item["kind"] for item in parsed["rows"]]


def test_space_expanded_dump_fixture():
    parsed = dwgtext.parse_dump(SPACES.read_text(encoding="utf-8"))
    assert parsed["ready"] is True
    assert parsed["summary"] == 1
    row = parsed["rows"][0]
    assert (row["layout"], row["layer"], row["kind"], row["block"]) == ("Model", "S-BEAM", "TEXT", "TITLE")
    assert (row["raw"], row["x"], row["y"], row["handle"], row["formatted"]) == ("W12", "10.5", "20.25", "AB1", False)


def test_apply_summary_is_not_parsed_as_a_single_result():
    text = "TEXTAPPLY|=|AB1|=|ok\nTEXTAPPLY_SUMMARY|=|2|=|1\n"
    parsed = dwgtext.parse_apply(text)
    assert parsed["summary"] == (2, 1)
    assert parsed["results"] == [{"handle": "AB1", "status": "ok"}]


def test_generated_lisp_is_utf8_and_the_script_stays_ascii():
    dump = dwgtext.build_dump_lisp(False)
    assert "*tx-blocks* nil" in dump
    assert "*tx-blocks* T" in dwgtext.build_dump_lisp(True)
    assert "圖塊定義" in dump
    blob = fontfix.lisp_bytes(dump)
    assert blob.startswith(b"\xef\xbb\xbf")
    assert "TEXT_ERROR|=|" in dump
    script = dwgtext.build_dump_script([{"spath": "C:/temp/in_abc.dwg"}], "C:/temp/textdump.lsp")
    script.encode("ascii")
    assert "\t" not in script
    assert "TEXT_ERROR" not in script
    assert "TEXTDUMP|=|" not in script
    apply_src = dwgtext.build_apply_lisp([[{
        "handle": "AB1",
        "kind": "MTEXT",
        "new_raw": "備註",
        "color": None,
    }]])
    assert "tx-apply-0" in apply_src
    assert "(list " in apply_src
    assert fontfix.lisp_bytes(apply_src).startswith(b"\xef\xbb\xbf")
    apply_script = dwgtext.build_apply_script([{"spath": "C:/temp/in_abc.dwg"}], "C:/temp/textapply.lsp")
    apply_script.encode("ascii")
    assert "_.QSAVE" in apply_script
    assert "TEXT_ERROR" not in apply_script


# ---------------------------------------------------------------------------
# Batch runner
# ---------------------------------------------------------------------------

def _extract(src, dst, **kwargs):
    kwargs.setdefault("pattern", "W12")
    kwargs.setdefault("mode", "contains")
    return textbatch.run_extract(src, dst, FAKE, timeout=30, **kwargs)


def _replace(src, dst, **kwargs):
    kwargs.setdefault("find", r"^(W\d+)\s.*$")
    kwargs.setdefault("repl", r"\1")
    kwargs.setdefault("mode", "regex")
    return textbatch.run_replace(src, dst, FAKE, timeout=30, **kwargs)


def test_extract_chinese_path_leaves_the_source_and_writes_hits(tmp_path):
    src = tmp_path / "輸入 資料"
    drawing = src / "配置" / "圖面 01.dwg"
    _dwg(drawing)
    digest = _sha(drawing)
    dst = tmp_path / "輸出 資料"
    summary = _extract(src, dst, recursive=True)
    assert summary["fatal"] == ""
    assert (summary["dwg"], summary["ok"], summary["failed"]) == (1, 1, 0)
    assert summary["hits"] == 2
    assert _sha(drawing) == digest
    assert not list(dst.rglob("*.dwg"))
    with (dst / "logs" / "textextract_hits.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [row["handle"] for row in rows] == ["AB1", "AB2"]
    assert rows[0]["文字內容"] == "W12 400x200"
    assert rows[0]["符合部分"] == "W12"
    assert (dst / "logs" / "textextract_hits.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    results = (dst / "logs" / "textextract_results.csv").read_text(encoding="utf-8-sig")
    assert "已擷取" in results


def test_extract_regex_chinese_and_no_match(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    beam = _extract(src, tmp_path / "beam", pattern=r"^(W\d+)\s.*$", mode="regex", recursive=False)
    assert beam["hits"] == 3
    chinese = _extract(src, tmp_path / "zh", pattern="備註", mode="contains", recursive=False)
    assert chinese["hits"] == 1
    empty = _extract(src, tmp_path / "none", pattern="沒有這段", mode="contains", recursive=False)
    assert empty["ok"] == 1 and empty["hits"] == 0 and empty["failed"] == 0
    text = (tmp_path / "none" / "logs" / "textextract_results.csv").read_text(encoding="utf-8-sig")
    assert "無符合" in text


def test_replace_publishes_only_changed_drawings(tmp_path):
    src = tmp_path / "輸入"
    drawing = src / "梁.dwg"
    _dwg(drawing)
    digest = _sha(drawing)
    dst = tmp_path / "輸出"
    summary = _replace(src, dst, recursive=False)
    assert summary["fatal"] == ""
    assert summary["ok"] == 1 and summary["changes"] == 3
    assert _sha(drawing) == digest
    out = dst / "梁.dwg"
    assert out.is_file()
    assert out.read_bytes().startswith(b"AC1032")
    assert b"TEXTAPPLIED" in out.read_bytes()
    with (dst / "logs" / "textreplace_changes.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert {row["handle"] for row in rows} == {"AB1", "AB2", "BB1"}
    assert rows[0]["新文字"] == "W12"
    mtext = next(row for row in rows if row["handle"] == "AB2")
    assert mtext["原文字"] == "W12 400x200"
    results = (dst / "logs" / "textreplace_results.csv").read_text(encoding="utf-8-sig")
    assert "已輸出" in results


def test_dry_run_writes_the_change_list_and_no_dwg(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    dst = tmp_path / "out"
    summary = _replace(src, dst, recursive=False, dry_run=True, copy_unchanged=True)
    assert summary["ok"] == 1 and summary["changes"] == 3
    assert not list(dst.rglob("*.dwg"))
    text = (dst / "logs" / "textreplace_results.csv").read_text(encoding="utf-8-sig")
    assert "預覽" in text


def test_unchanged_file_is_not_copied_unless_asked(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    quiet = _replace(src, tmp_path / "quiet", find="沒有這段", repl="x", mode="literal", recursive=False)
    assert quiet["ok"] == 1 and quiet["unchanged"] == 1 and quiet["changes"] == 0
    assert not (tmp_path / "quiet" / "a.dwg").exists()
    text = (tmp_path / "quiet" / "logs" / "textreplace_results.csv").read_text(encoding="utf-8-sig")
    assert "無變更" in text
    copied = _replace(
        src, tmp_path / "copy", find="沒有這段", repl="x", mode="literal",
        recursive=False, copy_unchanged=True,
    )
    assert copied["unchanged"] == 1
    assert (tmp_path / "copy" / "a.dwg").is_file()
    assert b"TEXTAPPLIED" not in (tmp_path / "copy" / "a.dwg").read_bytes()


def test_readonly_source_is_not_modified(tmp_path):
    src = tmp_path / "in"
    drawing = src / "ro.dwg"
    _dwg(drawing)
    os.chmod(drawing, stat.S_IREAD)
    digest = _sha(drawing)
    try:
        summary = _extract(src, tmp_path / "out", recursive=False)
        assert summary["ok"] == 1 and summary["failed"] == 0
        assert _sha(drawing) == digest
    finally:
        os.chmod(drawing, stat.S_IWRITE | stat.S_IREAD)


def test_same_folder_is_rejected(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    summary = _extract(src, src)
    assert "相同" in summary["fatal"]
    assert summary["ok"] == 0


def test_text_modes_widgets(monkeypatch, tk_root, tmp_path):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    for child in tk_root.winfo_children():
        child.destroy()
    app = appmod.App(tk_root)
    labels = {button.cget("text") for button in app.mode_buttons}
    assert "文字擷取" in labels and "文字取代" in labels
    app.env_ok = True
    app._set_busy_ui(False)
    app.v_mode.set("textextract")
    app._refresh_mode_ui()
    assert app.mode_frames["textextract"].winfo_manager()
    assert str(app.btn_extract["state"]) == "normal"
    assert app.v_tx_case.get() is False and app.v_tx_blocks.get() is False
    app.v_mode.set("textreplace")
    app._refresh_mode_ui()
    assert app.v_rp_dry.get() is False and app.v_rp_recolor.get() is False
    assert str(app.btn_replace["state"]) == "normal"

    errors = []
    monkeypatch.setattr(appmod.messagebox, "showerror", lambda title, msg: errors.append(msg))
    folder = tmp_path / "same"
    folder.mkdir()
    app.v_in.set(str(folder))
    app.v_out.set(str(folder))
    app.v_mode.set("textextract")
    app._start_extract()
    assert errors and "相同" in errors[0]

    out = tmp_path / "out"
    out.mkdir()
    app.v_out.set(str(out))
    app._start_extract()
    assert any("搜尋文字" in msg or "請輸入" in msg for msg in errors)

    calls = {}

    def fake_extract(*args, **kwargs):
        calls["extract"] = kwargs
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, hits=4,
                    stopped=False, fatal="", failed_files=[], elapsed=0.1)

    monkeypatch.setattr(appmod.textbatch, "run_extract", fake_extract)
    app.v_tx_pattern.set("W12")
    app.v_tx_case.set(True)
    app._start_extract()
    deadline = time.time() + 5
    while app.summary is None and time.time() < deadline:
        tk_root.update()
        time.sleep(0.05)
    assert calls["extract"]["pattern"] == "W12"
    assert calls["extract"]["case_sensitive"] is True
    assert calls["extract"]["include_blocks"] is False
    assert "成功" in app.v_result.get()
    assert "符合筆數：4" in app.v_result.get()

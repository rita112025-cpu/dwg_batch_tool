"""Layer classification rules, marker parsing, and the fake accoreconsole path."""
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
import fontfix  # noqa: E402
import layerbatch  # noqa: E402
import layerclass  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"
SPACES = Path(__file__).resolve().parent / "fixtures" / "layerclass_spaces.log"
HEADER = b"AC1032" + b"\x00" * 32


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dwg(path, extra=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEADER + extra)


def test_layer_names_prefix_and_anonymous_blocks():
    assert layerclass.sanitize_layer_name("A/B:C") == "A_B_C"
    assert layerclass.sanitize_layer_name("  牆  ") == "牆"
    assert layerclass.block_layer_name("*U12") == "ANON_U12"
    assert layerclass.block_layer_name("*D3") == "ANON_D3"
    assert layerclass.block_layer_name("*") == "ANON_BLOCK"
    assert layerclass.block_layer_name("門/1", "SCADA_") == "SCADA_門_1"
    assert layerclass.block_layer_name("Door", "A_") == "A_Door"
    settings, notes, err = layerclass.compile_rules(
        "MLEADER=LEAD\nLINE=\nINSERT=NOPE\n", prefix="A_", source_mode="listed", source_layers="0, 牆",
    )
    assert err == ""
    assert ("MULTILEADER", "A_LEAD") in settings["pairs"]
    assert all(kind != "LINE" for kind, _layer in settings["pairs"])
    assert any("INSERT" in note for note in notes)
    assert [name.upper() for name in settings["sources"]] == ["0", "牆"]
    _settings, _notes, missing = layerclass.compile_rules("", insert_by_name=False, source_mode="all")
    assert "至少" in missing
    _settings, _notes, empty = layerclass.compile_rules("TEXT=TST", source_mode="listed", source_layers="  ")
    assert "來源圖層" in empty


def test_pipe_rows_keep_spaces_and_the_space_fixture_parses():
    body = "新 圖層"
    line = "LAYERROW|=|" + "|=|".join(
        layerclass.escape_field(part) for part in (body, "1", "TEXT", "2", "0", "1")
    )
    weird = "LAYERROW|=|" + "|=|".join(
        layerclass.escape_field(part) for part in ("A|=|B", "3", "HATCH", "1", "層 0", "0")
    )
    parsed = layerclass.parse_output(
        line + "\n" + weird + "\nLAYERLOCK|=|DEFPOINTS|=|skipped\n"
        "LAYERSUMMARY|=|3|=|1|=|1\nLAYERNOTE|=|寫入失敗 1 筆\nLAYERCLASS_READY\n"
    )
    assert parsed["ready"] is True
    assert parsed["summary"] == {"moved": 3, "created": 1, "locked": 1}
    assert parsed["rows"][0]["layer"] == body
    assert parsed["rows"][1]["layer"] == "A|=|B"
    assert parsed["rows"][1]["source"] == "層 0"
    assert parsed["locks"] == [{"layer": "DEFPOINTS", "action": "skipped"}]
    assert parsed["notes"] == ["寫入失敗 1 筆"]
    spaced = layerclass.parse_output(SPACES.read_text(encoding="utf-8"))
    assert spaced["ready"] is True
    assert spaced["summary"]["moved"] == 4
    assert spaced["rows"][0]["kind"] == "LWPOLYLINE"
    assert spaced["rows"][0]["count"] == 4
    assert "LAYERSUMMARY" not in [row["kind"] for row in spaced["rows"]]


def test_generated_lisp_is_utf8_and_the_script_stays_ascii():
    settings, _notes, err = layerclass.compile_rules(layerclass.DEFAULT_MAP_TEXT, prefix="")
    assert err == ""
    src = layerclass.build_lisp(settings, include_blocks=False, keep_appearance=True, dry_run=False)
    assert "*lc-blocks* nil" in src
    assert "*lc-insert* T" in src
    assert "*lc-keep* T" in src
    assert "*lc-model-only* T" in src
    assert '(cons "LWPOLYLINE" "PL")' in src
    assert "*lc-color* 0" in src
    assert fontfix.lisp_bytes(src).startswith(b"\xef\xbb\xbf")
    blocks = layerclass.build_lisp(
        settings, include_paper=True, include_blocks=True, unlock_locked=True,
        keep_appearance=False, dry_run=True,
    )
    assert "*lc-blocks* T" in blocks
    assert "*lc-model-only* nil" in blocks
    assert "*lc-unlock* T" in blocks
    assert "*lc-keep* nil" in blocks
    assert "*lc-dry* T" in blocks
    script = layerclass.build_script([{"spath": "C:/temp/in_abc.dwg"}], "C:/temp/layerclass.lsp", dry_run=True)
    script.encode("ascii")
    assert "\t" not in script
    assert "_.QSAVE" not in script
    assert "LAYER_ERROR" not in script
    saved = layerclass.build_script([{"spath": "C:/temp/in_abc.dwg"}], "C:/temp/layerclass.lsp", dry_run=False)
    assert "_.QSAVE" in saved
    assert "LAYERROW|=|" not in saved


def _run(src, dst, **kwargs):
    kwargs.setdefault("type_map", layerclass.DEFAULT_MAP_TEXT)
    return layerbatch.run(src, dst, FAKE, timeout=30, **kwargs)


def test_batch_copies_chinese_paths_and_leaves_the_source(tmp_path):
    src = tmp_path / "輸入 資料"
    drawing = src / "配置" / "圖面 01.dwg"
    _dwg(drawing)
    digest = _sha(drawing)
    dst = tmp_path / "輸出 資料"
    summary = _run(src, dst, recursive=True)
    assert summary["fatal"] == ""
    assert (summary["dwg"], summary["ok"], summary["failed"]) == (1, 1, 0)
    assert summary["moved"] == 8 and summary["created"] == 4
    assert _sha(drawing) == digest
    out = dst / "配置" / "圖面 01.dwg"
    assert out.is_file()
    assert out.read_bytes().startswith(b"AC1032")
    assert b"LAYERAPPLIED" in out.read_bytes()
    with (dst / "logs" / "layerclass_moves.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert [row["新建圖層"] for row in rows] == ["PL", "TST", "H", "TITLE"]
    assert rows[0]["物件類型"] == "LWPOLYLINE"
    assert rows[0]["搬移數量"] == "4"
    assert rows[0]["來源圖層"] == "0"
    assert rows[0]["顏色"] == "1"
    assert (dst / "logs" / "layerclass_moves.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    per = dst / "logs" / "layerclass" / "配置__圖面 01.csv"
    assert per.is_file()
    assert per.read_bytes().startswith(b"\xef\xbb\xbf")
    results = (dst / "logs" / "layerclass_results.csv").read_text(encoding="utf-8-sig")
    assert "已輸出" in results
    assert "DEFPOINTS:略過" in results
    log = (dst / "logs" / "layerclass.log").read_text(encoding="utf-8")
    assert "合計搬移 8" in log


def test_dry_run_writes_the_list_and_no_dwg(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    dst = tmp_path / "out"
    summary = _run(src, dst, recursive=False, dry_run=True)
    assert summary["ok"] == 1 and summary["moved"] == 8
    assert not list(dst.rglob("*.dwg"))
    text = (dst / "logs" / "layerclass_results.csv").read_text(encoding="utf-8-sig")
    assert "預覽" in text
    assert (dst / "logs" / "layerclass_moves.csv").is_file()


def test_empty_drawing_is_unchanged_and_not_copied(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg", extra=b"LAYER_TEST_EMPTY")
    dst = tmp_path / "out"
    summary = _run(src, dst, recursive=False)
    assert summary["ok"] == 1 and summary["unchanged"] == 1 and summary["moved"] == 0
    assert not (dst / "a.dwg").exists()
    text = (dst / "logs" / "layerclass_results.csv").read_text(encoding="utf-8-sig")
    assert "無變更" in text


def test_readonly_source_is_not_modified(tmp_path):
    src = tmp_path / "in"
    drawing = src / "ro.dwg"
    _dwg(drawing)
    os.chmod(drawing, stat.S_IREAD)
    digest = _sha(drawing)
    try:
        summary = _run(src, tmp_path / "out", recursive=False)
        assert summary["ok"] == 1 and summary["failed"] == 0
        assert _sha(drawing) == digest
    finally:
        os.chmod(drawing, stat.S_IWRITE | stat.S_IREAD)


def test_same_folder_is_rejected(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    summary = _run(src, src)
    assert "相同" in summary["fatal"]
    assert summary["ok"] == 0


def test_layer_mode_widgets(monkeypatch, tk_root, tmp_path):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    for child in tk_root.winfo_children():
        child.destroy()
    app = appmod.App(tk_root)
    assert any(button.cget("text") == "自動分圖層" for button in app.mode_buttons)
    app.env_ok = True
    app._set_busy_ui(False)
    app.v_mode.set("layerclass")
    app._refresh_mode_ui()
    assert app.mode_frames["layerclass"].winfo_manager()
    assert str(app.btn_layer["state"]) == "normal"
    assert app.v_ly_scope.get() == "layer0"
    assert app.v_ly_insert.get() is True
    assert app.v_ly_keep.get() is True
    assert app.v_ly_blocks.get() is False
    assert app.v_ly_unlock.get() is False
    assert "LWPOLYLINE=PL" in app.layer_map_text.get("1.0", "end")
    assert "HATCH=H" in app.layer_map_text.get("1.0", "end")

    errors = []
    monkeypatch.setattr(appmod.messagebox, "showerror", lambda title, msg: errors.append(msg))
    folder = tmp_path / "same"
    folder.mkdir()
    app.v_in.set(str(folder))
    app.v_out.set(str(folder))
    app._start_layerclass()
    assert errors and "相同" in errors[0]

    calls = {}

    def fake_run(*args, **kwargs):
        calls["kwargs"] = kwargs
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, moved=8, created=4,
                    unchanged=0, stopped=False, fatal="", failed_files=[], notes=[], elapsed=0.1)

    monkeypatch.setattr(appmod.layerbatch, "run", fake_run)
    out = tmp_path / "out"
    out.mkdir()
    app.v_out.set(str(out))
    app.v_ly_keep.set(False)
    app.v_ly_dry.set(True)
    app._start_layerclass()
    deadline = time.time() + 5
    while app.summary is None and time.time() < deadline:
        tk_root.update()
        time.sleep(0.05)
    assert calls["kwargs"]["keep_appearance"] is False
    assert calls["kwargs"]["dry_run"] is True
    assert calls["kwargs"]["insert_by_name"] is True
    assert calls["kwargs"]["source_mode"] == "layer0"
    assert "成功" in app.v_result.get()
    assert "搬移數量：8" in app.v_result.get()

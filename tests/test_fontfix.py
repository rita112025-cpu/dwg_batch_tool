"""Font-fix decisions, generated AutoLISP, and the batch runner.

Tests that talk to a real accoreconsole are marked ``accore`` and skip when
config.json does not point at one. Plumbing tests use tests/fake_accoreconsole.py.
"""
import csv
import hashlib
import json
import os
import stat
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import fontfix  # noqa: E402
import fontfix_batch  # noqa: E402
from common import decode_console, load_config  # noqa: E402  # decode_console used by the accore test
from convert_single import run_accore  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"
HEADER = b"AC1032" + b"\x00" * 32


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _dwg(path, extra=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEADER + extra)


def _ctx(**kwargs):
    return fontfix.FontContext(**kwargs)


# ---------------------------------------------------------------------------
# Matching rules (the prototype's spec, including the TrueType lesson)
# ---------------------------------------------------------------------------

def test_split_typeface_names():
    assert fontfix.split_typeface_names(
        "Microsoft JhengHei & Microsoft JhengHei UI (TrueType)"
    ) == ["MICROSOFT JHENGHEI", "MICROSOFT JHENGHEI UI"]
    assert fontfix.split_typeface_names("新細明體 (TrueType)") == ["新細明體"]
    assert fontfix.split_typeface_names("Arial (TrueType)") == ["ARIAL"]
    assert fontfix.split_typeface_names("Foo (Bar) & Baz (TrueType)") == ["FOO (BAR)", "BAZ"]
    assert fontfix.split_typeface_names("") == []
    assert fontfix.split_typeface_names(None) == []
    assert fontfix.typefaces_from_value_names([
        "Microsoft JhengHei & Microsoft JhengHei UI (TrueType)",
        "Arial (TrueType)",
    ]) == {"MICROSOFT JHENGHEI", "MICROSOFT JHENGHEI UI", "ARIAL"}


def test_read_installed_typefaces_returns_a_set():
    assert isinstance(fontfix.read_installed_typefaces(), set)


def test_ttf_typeface_is_not_the_same_as_the_font_file():
    """AutoCAD stores ``Microsoft JhengHei.ttf`` even when the file is ``msjh.ttc``."""
    installed = _ctx(font_dir_files=["msjh.ttc"], typefaces=["MICROSOFT JHENGHEI"])
    assert not fontfix.is_missing("Microsoft JhengHei.ttf", installed)
    file_only = _ctx(font_dir_files=["msjh.ttc"])
    assert fontfix.is_missing("Microsoft JhengHei.ttf", file_only)
    on_disk = _ctx(font_dir_files=["arial.ttf"])
    assert not fontfix.is_missing("arial.ttf", on_disk)
    assert fontfix.is_missing("not-installed.ttf", _ctx())


def test_extension_at_prefix_and_support_path():
    support = _ctx(support_files=["TXT.SHX", "romans.shx", "chineset.shx"])
    assert not fontfix.is_missing("txt", support)          # no extension → txt.shx
    assert not fontfix.is_missing("ROMANS.SHX", support)   # case-insensitive file hit
    assert fontfix.is_missing("genie", support)            # genie.shx is absent
    assert fontfix.is_missing("genie.shx", support)
    assert fontfix.is_missing("sino2.shx", support)
    assert not fontfix.is_missing("@PMingLiU.ttf", _ctx())  # vertical face is skipped
    assert not fontfix.is_missing("", support)
    assert not fontfix.is_missing(None, support)


def test_mapping_overrides_only_the_stored_name():
    mapping = fontfix.parse_mapping(
        "; comment\n\nsino2.shx = txt.shx\nGENIE.SHX=hztxt.shx\nbad line\n=missing\n"
        "sino2.shx=simplex.shx\n"
    )
    assert mapping == {"SINO2.SHX": "simplex.shx", "GENIE.SHX": "hztxt.shx"}
    ctx = _ctx(support_files=["TXT.SHX", "ROMANS.SHX", "CHINESET.SHX", "ROMANC.SHX"])
    changed = fontfix.plan_style("sino2.shx", "genie.shx", ctx, mapping=mapping)
    assert changed["new_primary"] == "simplex.shx"
    assert changed["new_big"] == "hztxt.shx"
    assert fontfix.plan_style("txt", "", ctx, mapping=mapping) is None


def test_plan_matches_the_validated_run():
    """Decisions taken from the successful FONTFIX log: installed faces stay, SHX gaps move."""
    ctx = _ctx(
        support_files=["romans.shx", "simplex.shx", "romanc.shx", "chineset.shx", "txt.shx",
                       "ltypeshp.shx", "isocp.shx"],
        font_dir_files=["arial.ttf", "arialbd.ttf", "tahoma.ttf", "gothic.ttf"],
        typefaces=["MICROSOFT JHENGHEI", "MICROSOFT YAHEI", "PMINGLIU", "MINGLIU"],
    )
    cases = {
        ("ACSE", "sino2.shx", ""): ("romans.shx", ""),
        ("DIM", "romanc.shx", "genie.shx"): ("romanc.shx", "chineset.shx"),
        ("S2H_161", "Sino2_6.shx", "chineset.shx"): ("romans.shx", "chineset.shx"),
        ("S2", "SINO2.shx", ""): ("romans.shx", ""),
        ("Ascii", "ascii.shx", "chinese.shx"): ("romans.shx", "chineset.shx"),
    }
    for (style, primary, big), (new_p, new_b) in cases.items():
        change = fontfix.plan_style(primary, big, ctx)
        assert change is not None, style
        assert (change["new_primary"], change["new_big"]) == (new_p, new_b), style
        assert fontfix.format_change(style, change).startswith(style)
    untouched = [
        ("中文直式", "@PMingLiU.ttf", ""),
        ("Standard", "txt", ""),
        ("Style-Arial", "arial.ttf", ""),
        ("微軟正黑體", "Microsoft JhengHei.ttf", ""),
        ("Microsoft YaHei", "Microsoft YaHei.ttf", ""),
        ("Style-新細明體", "PMingLiU.ttf", ""),
        ("Style-細明體", "MingLiU.ttf", ""),
        ("Annotative", "romans.shx", ""),
        ("shape", "ltypeshp.shx", ""),
    ]
    for style, primary, big in untouched:
        assert fontfix.plan_style(primary, big, ctx) is None, style


def test_directory_rules(tmp_path):
    src = tmp_path / "輸入"
    dst = tmp_path / "輸出"
    src.mkdir()
    assert fontfix.directory_problem(src, dst) is None
    assert "相同" in fontfix.directory_problem(src, src)
    nested = src / "out"
    assert "裡面" in fontfix.directory_problem(src, nested)
    assert "不存在" in fontfix.directory_problem(tmp_path / "missing", dst)
    assert fontfix.directory_problem("", dst)
    assert fontfix.directory_problem(src, "  ")


def test_folder_listing_mirrors_subfolders_and_ignores_case(tmp_path):
    src = tmp_path / "in"
    (src / "sub").mkdir(parents=True)
    _dwg(src / "A.DWG")
    _dwg(src / "sub" / "B.dwg")
    (src / "note.txt").write_text("x", encoding="utf-8")
    assert [p.name for p in fontfix.iter_dwgs(src, recursive=False)] == ["A.DWG"]
    both = fontfix.iter_dwgs(src, recursive=True)
    assert [p.name for p in both] == ["A.DWG", "B.dwg"]
    assert fontfix.output_for(both[1], src, tmp_path / "out", True) == tmp_path / "out" / "sub" / "B.dwg"
    assert fontfix.output_for(both[0], src, tmp_path / "out", False) == tmp_path / "out" / "A.DWG"


# ---------------------------------------------------------------------------
# Generated LISP / script
# ---------------------------------------------------------------------------

def test_generated_lisp_is_utf8_and_does_not_use_or_as_a_value():
    src = fontfix.build_lisp(
        "romans.shx", "chineset.shx",
        {"SINO2.SHX": "txt.shx"},
        ["MICROSOFT JHENGHEI", "新細明體", 'A"B'],
        [r"C:\Windows\Fonts"],
    )
    assert fontfix._lisp_parens_balanced(src)
    assert "(or (findfile" not in src
    assert "(findfile probe)" in src
    assert "(entmod ned)" in src
    assert "(substr fn 1 1)" in src
    assert '".TTF"' in src and '".TTC"' in src and '".OTF"' in src
    raw = fontfix.lisp_bytes(src)
    assert raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    assert "新細明體" in text
    assert '"A\\"B"' in text
    assert '*ff-primary* "romans.shx"' in text
    assert "SINO2.SHX" in text and "txt.shx" in text
    script = fontfix.build_script(
        [{"spath": "C:/temp/in_abc.dwg"}], "C:/temp/fontfix_run.lsp"
    )
    assert script.isascii()
    assert "_.QSAVE" in script and "_.SAVEAS" not in script
    assert "FONTFIX_ERROR\t" not in script          # a real tab would be parsed from the echo
    assert "FONTALT" in script and "txt.shx" in script
    versioned = fontfix.build_script(
        [{"spath": "C:/temp/in_abc.dwg", "save_version": "2013", "save_path": "C:/temp/out_abc.dwg"}],
        "C:/temp/fontfix_run.lsp",
    )
    assert "_.SAVEAS" in versioned and "\n2013\n" in versioned
    assert "_.QSAVE" not in versioned
    fontfix.script_bytes(script)


def test_parse_markers():
    text = (
        "FONTFIX_READY\n"
        "DWGBATCH_OPEN_0|in_abc.dwg|\n"
        "FONTFIX_REPLACED\tACSE\tsino2.shx\tromans.shx\t\t\n"
        "FONTFIX_REPLACED\tDIM\tromanc.shx\tromanc.shx\tgenie.shx\tchineset.shx\n"
        "FONTFIX_STILL\tWeird\tmissing.shx\t\n"
        "FONTFIX_SUMMARY\t2\t1\n"
        "DWGBATCH_DONE_0_\n"
    )
    parsed = fontfix.parse_fontfix_output(text)
    assert parsed["ready"] and parsed["summary"] == (2, 1)
    assert parsed["replacements"][0]["style"] == "ACSE"
    assert parsed["replacements"][0]["new_big"] == ""
    assert parsed["replacements"][1]["new_big"] == "chineset.shx"
    assert parsed["still_missing"][0]["style"] == "Weird"
    assert "ACSE | sino2.shx -> romans.shx" in fontfix.replacement_text(parsed["replacements"])
    assert "主=missing.shx" in fontfix.still_text(parsed["still_missing"])


# ---------------------------------------------------------------------------
# Batch runner with a fake console (no AutoCAD)
# ---------------------------------------------------------------------------

def _run(src, dst, **kwargs):
    return fontfix_batch.run(src, dst, FAKE, timeout=30, **kwargs)


def test_batch_copies_chinese_paths_and_leaves_the_source_alone(tmp_path):
    src = tmp_path / "輸入 資料"
    (src / "子").mkdir(parents=True)
    top = src / "圖面 01.dwg"
    nested = src / "子" / "圖面 02.dwg"
    _dwg(top)
    _dwg(nested)
    before = {top: _sha(top), nested: _sha(nested)}
    dst = tmp_path / "輸出 資料"
    seen = []
    summary = _run(src, dst, recursive=True, progress=lambda i, n, path: seen.append((i, n, path)))
    assert summary["fatal"] == ""
    assert (summary["dwg"], summary["ok"], summary["failed"], summary["skipped"]) == (2, 2, 0, 0)
    assert summary["styles_replaced"] == 2
    for origin, digest in before.items():
        assert _sha(origin) == digest
    assert (dst / "圖面 01.dwg").is_file()
    assert (dst / "子" / "圖面 02.dwg").is_file()
    assert (dst / "圖面 01.dwg").read_bytes()[:6] == b"AC1032"
    with (dst / "logs" / "fontfix_results.csv").open(encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert {row["狀態"] for row in rows} == {"已輸出"}
    assert all("missing.shx -> romans.shx" in row["替換明細"] for row in rows)
    assert "已輸出" in (dst / "logs" / "fontfix.log").read_text(encoding="utf-8")
    payload = [
        json.loads(line)
        for line in (dst / "logs" / "fontfix_results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert payload[0]["replacement_rows"][0]["new_primary"] == "romans.shx"
    assert seen and seen[-1][0] == 2 and seen[-1][1] == 2


def test_flat_output_when_subfolders_are_off(tmp_path):
    src = tmp_path / "in"
    (src / "sub").mkdir(parents=True)
    _dwg(src / "only.dwg")
    _dwg(src / "sub" / "skip.dwg")
    dst = tmp_path / "out"
    summary = _run(src, dst, recursive=False)
    assert summary["dwg"] == 1 and summary["ok"] == 1
    assert (dst / "only.dwg").is_file()
    assert not (dst / "sub").exists()


def test_readonly_source_is_not_modified(tmp_path):
    src = tmp_path / "in"
    dwg = src / "ro.dwg"
    _dwg(dwg)
    os.chmod(dwg, stat.S_IREAD)
    digest = _sha(dwg)
    try:
        summary = _run(src, tmp_path / "out", recursive=False)
        assert summary["ok"] == 1 and summary["failed"] == 0
        assert _sha(dwg) == digest
        assert not os.access(dwg, os.W_OK) or stat.S_IMODE(dwg.stat().st_mode) & stat.S_IWRITE == 0
    finally:
        os.chmod(dwg, stat.S_IWRITE | stat.S_IREAD)


def test_copy_failure_does_not_touch_the_source(tmp_path, monkeypatch):
    src = tmp_path / "in"
    dwg = src / "locked.dwg"
    _dwg(dwg)
    digest = _sha(dwg)

    def locked(source, dest):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(fontfix_batch, "_copy_bytes", locked)
    summary = _run(src, tmp_path / "out", recursive=False)
    assert summary["failed"] == 1 and summary["ok"] == 0
    assert "獨占鎖定" in summary["failed_files"][0]
    assert _sha(dwg) == digest
    assert not (tmp_path / "out" / "locked.dwg").exists()


def test_existing_output_is_skipped_until_overwrite(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    dst = tmp_path / "out"
    first = _run(src, dst, recursive=False)
    assert first["ok"] == 1
    written = (dst / "a.dwg").read_bytes()
    logs_before = list((dst / "logs").glob("fontfix_batch_*.stdout.log"))
    second = _run(src, dst, recursive=False, overwrite=False)
    assert second["skipped"] == 1 and second["ok"] == 0
    assert (dst / "a.dwg").read_bytes() == written
    assert list((dst / "logs").glob("fontfix_batch_*.stdout.log")) == logs_before
    third = _run(src, dst, recursive=False, overwrite=True, version="2013")
    assert third["ok"] == 1
    assert (dst / "a.dwg").read_bytes().endswith(b"\nSAVEAS")


def test_messagebox_restarts_and_keeps_going(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    _dwg(src / "b.dwg", extra=b"FONTFIX_TEST_CRASH")
    _dwg(src / "c.dwg")
    dst = tmp_path / "out"
    summary = _run(src, dst, recursive=False)
    assert (summary["ok"], summary["failed"]) == (2, 1)
    assert (dst / "a.dwg").is_file() and (dst / "c.dwg").is_file()
    assert not (dst / "b.dwg").exists()
    assert "對話框" in summary["failed_files"][0]
    assert len(list((dst / "logs").glob("fontfix_batch_*.stdout.log"))) == 2


def test_stop_before_start_writes_nothing(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    event = __import__("threading").Event()
    event.set()
    summary = _run(src, tmp_path / "out", recursive=False, stop_event=event)
    assert summary["stopped"] and summary["ok"] == 0
    assert not (tmp_path / "out" / "a.dwg").exists()


def test_same_folder_is_rejected(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    summary = _run(src, src)
    assert "相同" in summary["fatal"]
    assert summary["ok"] == 0


def test_stdout_log_decodes_the_fake_console(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    dst = tmp_path / "out"
    _run(src, dst, recursive=False)
    log = next((dst / "logs").glob("fontfix_batch_*.stdout.log"))
    text = log.read_text(encoding="utf-8")
    assert "FONTFIX_READY" in text and "FONTFIX_REPLACED" in text


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

def test_fontfix_mode_widgets_and_same_folder_guard(monkeypatch, tk_root, tmp_path):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    for child in tk_root.winfo_children():
        child.destroy()
    app = appmod.App(tk_root)
    assert any(button.cget("text") == "缺字體修復" for button in app.mode_buttons)
    app.v_mode.set("fontfix")
    app._refresh_mode_ui()
    assert app.mode_frames["fontfix"].winfo_manager()
    assert sum(bool(frame.winfo_manager()) for frame in app.mode_frames.values()) == 1
    assert app.v_ff_primary.get() == "romans.shx"
    assert app.v_ff_big.get() == "chineset.shx"
    assert app.ff_version_box.get() == "維持目前格式"
    assert str(app.btn_fontfix["state"]) == "disabled"
    app.env_ok = True
    app._set_busy_ui(False)
    assert str(app.btn_fontfix["state"]) == "normal"

    errors = []
    monkeypatch.setattr(appmod.messagebox, "showerror", lambda title, msg: errors.append(msg))
    folder = tmp_path / "same"
    folder.mkdir()
    app.v_in.set(str(folder))
    app.v_out.set(str(folder))
    app._start_fontfix()
    assert errors and "相同" in errors[0]
    assert app.worker is None

    calls = {}

    def fake_run(*args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return dict(dwg=1, ok=1, failed=0, skipped=0, warnings=0, styles_replaced=2,
                    stopped=False, fatal="", failed_files=[], still_files=[], elapsed=0.2)

    monkeypatch.setattr(appmod.fontfix_batch, "run", fake_run)
    out = tmp_path / "out"
    out.mkdir()
    app.v_out.set(str(out))
    app.ff_map_text.insert("1.0", "sino2.shx=txt.shx\n")
    app.opts["recursive"].set(True)
    app._start_fontfix()
    deadline = time.time() + 5
    while app.summary is None and time.time() < deadline:
        tk_root.update()
        time.sleep(0.05)
    assert calls["kwargs"]["mapping"]["SINO2.SHX"] == "txt.shx"
    assert calls["kwargs"]["recursive"] is True
    assert calls["kwargs"]["version"] == ""
    assert calls["kwargs"]["primary"] == "romans.shx"
    assert "成功" in app.v_result.get()
    assert str(app.btn_fontfix["state"]) == "normal"


# ---------------------------------------------------------------------------
# Real AutoCAD. Skipped on machines without accoreconsole.
# ---------------------------------------------------------------------------

def _scripted_accore(accore, lines, folder):
    folder.mkdir(parents=True, exist_ok=True)
    scr = folder / "job.scr"
    text = "\n".join(lines) + "\n"
    scr.write_bytes(fontfix.script_bytes(text))
    rc, out, err, why = run_accore([str(accore), "/s", str(scr), "/l", "en-US"], 180)
    log = folder / "console.txt"
    log.write_text(decode_console(out) + "\n" + decode_console(err), encoding="utf-8")
    return rc, why, log.read_text(encoding="utf-8")


@pytest.mark.accore
def test_accoreconsole_replaces_missing_fonts_and_keeps_installed_ones(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    from convert_single import safe_tmpdir
    accore = load_config()["accoreconsole"]
    # ASCII, no spaces: accoreconsole splits an unquoted SAVEAS name on spaces,
    # and the script is read in the ANSI code page.
    work = safe_tmpdir(tmp_path / "logs")
    dxf = work / "setup.dxf"
    doc = ezdxf.new()
    doc.styles.new("MISSING", dxfattribs={"font": "missingfont.shx", "bigfont": "missingbig.shx"})
    doc.styles.new("KEEP", dxfattribs={"font": "txt.shx"})
    doc.styles.new("VERT", dxfattribs={"font": "@PMingLiU.ttf"})
    doc.styles.new("JHEI", dxfattribs={"font": "Microsoft JhengHei.ttf"})
    doc.saveas(dxf)
    dwg = work / "setup.dwg"
    rc, why, console = _scripted_accore(accore, [
        "FILEDIA", "0", "CMDDIA", "0", "EXPERT", "5", "FONTALT", "txt.shx",
        "_.OPEN", f'"{dxf.as_posix()}"',
        "_.SAVEAS", "2018", f'"{dwg.as_posix()}"',
        "_.QUIT",
    ], work / "to-dwg")
    assert dwg.is_file() and dwg.stat().st_size > 6, console
    assert not why, console

    src = tmp_path / "in"
    src.mkdir()
    target = src / "setup.dwg"
    target.write_bytes(dwg.read_bytes())
    digest = _sha(target)
    dst = tmp_path / "out"
    summary = fontfix_batch.run(src, dst, accore, recursive=False, timeout=180)
    assert summary["fatal"] == "", summary
    assert summary["ok"] == 1 and summary["failed"] == 0, summary
    assert _sha(target) == digest
    fixed = dst / "setup.dwg"
    assert fixed.is_file()
    rows = json.loads((dst / "logs" / "fontfix_results.jsonl").read_text(encoding="utf-8").splitlines()[0])
    replaced = {(row["style"], row["old_primary"], row["new_primary"], row["old_big"], row["new_big"])
                for row in rows["replacement_rows"]}
    assert ("MISSING", "missingfont.shx", "romans.shx", "missingbig.shx", "chineset.shx") in replaced
    assert all(row[1].lower() != "txt.shx" for row in replaced)
    assert all(not row[1].startswith("@") for row in replaced)
    assert all("jhenghei" not in row[1].lower() for row in replaced)

    back = work / "back.dxf"
    rc, why, console = _scripted_accore(accore, [
        "FILEDIA", "0", "CMDDIA", "0", "EXPERT", "5", "FONTALT", "txt.shx",
        "_.OPEN", f'"{fixed.as_posix()}"',
        "_.SAVEAS", "DXF", "", f'"{back.as_posix()}"',
        "_.QUIT",
    ], work / "to-dxf")
    assert back.is_file(), console
    assert not why, console
    saved = ezdxf.readfile(back)
    styles = {style.dxf.name: (style.dxf.font or "", getattr(style.dxf, "bigfont", "") or "")
              for style in saved.styles}
    assert styles["MISSING"][0].lower() == "romans.shx"
    assert styles["MISSING"][1].lower() == "chineset.shx"
    assert styles["VERT"][0].startswith("@")
    assert "jhenghei" in styles["JHEI"][0].lower()

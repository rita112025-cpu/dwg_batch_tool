"""PROXYGRAPHICS script generation and batch plumbing tests."""
import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import proxybatch  # noqa: E402
import proxygraphics  # noqa: E402
from common import load_config  # noqa: E402

FAKE = ROOT / "tests" / "fake_accoreconsole.py"
HEADER = b"AC1032" + b"\x00" * 32


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_script_sets_verifies_and_saves_proxygraphics():
    lisp = proxygraphics.build_lisp()
    assert '(setvar "PROXYGRAPHICS" 1)' in lisp
    assert '(getvar "PROXYGRAPHICS")' in lisp
    script = proxygraphics.build_script(
        [{"spath": "C:/temp/in_abc.dwg"}], "C:/temp/proxygraphics.lsp"
    )
    assert "pg-run" in script
    assert '(command "_.QSAVE")' in script
    assert "*pg-before*" in script
    assert "\n_.QSAVE\n" not in script
    assert "_.SAVEAS" not in script
    assert script.isascii()


def test_parse_output_requires_verified_value():
    parsed = proxygraphics.parse_output(
        "PROXYGRAPHICS_READY\nPROXYGRAPHICS_RESULT|=|0|=|1\n"
    )
    assert parsed == {"ready": True, "before": 0, "after": 1, "errors": []}
    failed = proxygraphics.parse_output("PROXYGRAPHICS_ERROR|=|read only\n")
    assert failed["errors"] == ["read only"]


def test_batch_writes_copy_and_preserves_source(tmp_path):
    src = tmp_path / "input"
    out = tmp_path / "output"
    (src / "sub").mkdir(parents=True)
    drawing = src / "sub" / "tray.dwg"
    drawing.write_bytes(HEADER + b"CABLE_TRAY")
    before = _sha(drawing)

    result = proxybatch.run(src, out, FAKE, recursive=True)

    produced = out / "sub" / "tray.dwg"
    assert not result["fatal"]
    assert (result["dwg"], result["ok"], result["failed"]) == (1, 1, 0)
    assert _sha(drawing) == before
    assert produced.is_file()
    assert b"PROXYGRAPHICS=1" in produced.read_bytes()
    assert (out / "logs" / "proxygraphics_results.csv").is_file()

    skipped = proxybatch.run(src, out, FAKE, recursive=True)
    assert (skipped["ok"], skipped["skipped"], skipped["failed"]) == (0, 1, 0)


def test_already_enabled_proxygraphics_is_not_resaved(tmp_path):
    src = tmp_path / "input"
    src.mkdir()
    drawing = src / "tray.dwg"
    drawing.write_bytes(HEADER + b"\nPROXYGRAPHICS=1")
    original = drawing.read_bytes()
    out = tmp_path / "output"

    result = proxybatch.run(src, out, FAKE, recursive=False)

    assert not result["fatal"]
    assert (result["ok"], result["failed"]) == (1, 0)
    rec = result["results"][0]
    assert (rec["before"], rec["after"]) == (1, 1)
    assert (out / "tray.dwg").read_bytes() == original


def test_batch_loads_lisp_for_each_drawing_and_preserves_sources(tmp_path):
    src = tmp_path / "input"
    out = tmp_path / "output"
    src.mkdir()
    originals = {}
    for name in ("first.dwg", "second.dwg", "third.dwg"):
        drawing = src / name
        drawing.write_bytes(HEADER + name.encode("ascii"))
        originals[name] = _sha(drawing)

    result = proxybatch.run(src, out, FAKE)

    assert not result["fatal"]
    assert (result["dwg"], result["ok"], result["failed"]) == (3, 3, 0)
    for name, digest in originals.items():
        assert _sha(src / name) == digest
        assert b"PROXYGRAPHICS=1" in (out / name).read_bytes()


def test_script_loads_lisp_inside_each_opened_drawing():
    script = proxygraphics.build_script(
        [{"spath": "C:/temp/first.dwg"}, {"spath": "C:/temp/second.dwg"}],
        "C:/temp/proxygraphics.lsp",
    )
    sections = script.split("_.OPEN\n")
    assert '(load ' not in sections[0]
    assert len(sections) == 3
    for section in sections[1:]:
        assert section.index("ATCH_OPEN_") < section.index('(load ')
        assert section.index('(load ') < section.index("(pg-run)")
        assert section.index("(pg-run)") < section.index('(command "_.QSAVE")')


@pytest.mark.accore
def test_real_accore_processes_multiple_drawings(tmp_path):
    source = ROOT / "tests" / "fixtures" / "sample.dwg"
    src = tmp_path / "input"
    out = tmp_path / "output"
    src.mkdir()
    for name in ("first.dwg", "second.dwg", "third.dwg"):
        (src / name).write_bytes(source.read_bytes())

    result = proxybatch.run(src, out, load_config().get("accoreconsole", ""))

    assert not result["fatal"]
    assert (result["ok"], result["failed"]) == (3, 0), result["results"]
    for rec in result["results"]:
        assert rec["after"] == 1
        assert _sha(Path(rec["file"])) == _sha(source)
        assert Path(rec["output"]).is_file()


@pytest.mark.accore
def test_real_accore_does_not_resave_when_proxygraphics_is_already_on(tmp_path):
    source = ROOT / "tests" / "fixtures" / "sample.dwg"
    src = tmp_path / "input"
    out = tmp_path / "output"
    src.mkdir()
    (src / "sample.dwg").write_bytes(source.read_bytes())
    accore = load_config().get("accoreconsole", "")
    first = proxybatch.run(src, out, accore)
    assert (first["ok"], first["failed"]) == (1, 0), first["results"]
    saved = (out / "sample.dwg").read_bytes()

    again_src = tmp_path / "again"
    again_out = tmp_path / "again-out"
    again_src.mkdir()
    (again_src / "sample.dwg").write_bytes(saved)
    second = proxybatch.run(again_src, again_out, accore)
    assert not second["fatal"]
    assert (second["ok"], second["failed"]) == (1, 0), second["results"]
    assert (second["results"][0]["before"], second["results"][0]["after"]) == (1, 1)
    assert (again_out / "sample.dwg").read_bytes() == saved


@pytest.mark.accore
def test_real_accore_sets_and_saves_proxygraphics(tmp_path):
    source = ROOT / "tests" / "fixtures" / "sample.dwg"
    src = tmp_path / "input"
    out = tmp_path / "output"
    src.mkdir()
    drawing = src / "sample.dwg"
    drawing.write_bytes(source.read_bytes())
    before = _sha(drawing)

    result = proxybatch.run(src, out, load_config().get("accoreconsole", ""))

    assert not result["fatal"]
    assert (result["ok"], result["failed"]) == (1, 0)
    assert result["results"][0]["after"] == 1
    assert _sha(drawing) == before
    assert (out / "sample.dwg").is_file()

"""DXF inspection / comparison on synthetic drawings (no AutoCAD needed)."""
import sys
from pathlib import Path

import ezdxf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import dxfcheck  # noqa: E402


def _make(path, walls, flors, extra_circle=False):
    doc = ezdxf.new()
    doc.layers.add("A-WALL")
    doc.layers.add("A-FLOR")
    msp = doc.modelspace()
    for i in range(walls):
        msp.add_line((i, 0), (i, 5), dxfattribs={"layer": "A-WALL"})
    for i in range(flors):
        msp.add_lwpolyline([(0, i), (3, i), (3, i + 1)], dxfattribs={"layer": "a-flor"})
    if extra_circle:
        msp.add_circle((0, 0), 1, dxfattribs={"layer": "A-FLOR"})
    block = doc.blocks.new("TB-A1")
    block.add_attdef("DWGNO", (0, 0))
    ref = msp.add_blockref("TB-A1", (0, 0))
    ref.add_auto_attribs({"DWGNO": "SB16-B1"})
    doc.saveas(path)
    return path


def test_count_entities_case_insensitive_layers(tmp_path):
    doc = ezdxf.readfile(_make(tmp_path / "a.dxf", 3, 2))
    counts = dxfcheck.count_entities(doc, "A-WALL,A-FLOR*")
    assert counts["A-WALL"]["LINE"] == 3
    assert sum(counts["a-flor"].values()) == 2


def test_compare_reports_differences_by_type(tmp_path):
    left = ezdxf.readfile(_make(tmp_path / "a.dxf", 3, 2))
    right = ezdxf.readfile(_make(tmp_path / "b.dxf", 4, 2, extra_circle=True))
    rows = dxfcheck.compare_counts(
        dxfcheck.count_entities(left), dxfcheck.count_entities(right), "A-WALL,A-FLOR,A-MISSING")
    by_layer = {r["layer"].upper(): r for r in rows}
    assert by_layer["A-WALL"]["diff"] == 1
    assert by_layer["A-FLOR"]["by_type"] == {"CIRCLE": (0, 1)}
    assert by_layer["A-MISSING"]["left"] == by_layer["A-MISSING"]["right"] == 0


def test_title_block_attributes_and_extents(tmp_path):
    doc = ezdxf.readfile(_make(tmp_path / "a.dxf", 3, 1))
    blocks = dxfcheck.title_block_attributes(doc, "tb-*")
    assert blocks[0]["attribs"] == {"DWGNO": "SB16-B1"}
    low, high = dxfcheck.layer_extents(doc, "a-wall")
    assert low[:2] == (0, 0) and high[:2] == (2, 5)
    assert dxfcheck.layer_extents(doc, "NOPE") is None


def test_cli_exit_codes(tmp_path, capsys):
    a = _make(tmp_path / "a.dxf", 3, 2)
    b = _make(tmp_path / "b.dxf", 3, 2)
    c = _make(tmp_path / "c.dxf", 5, 2)
    assert dxfcheck.main(["compare", str(a), str(b), "--layers", "A-WALL,A-FLOR"]) == 0
    assert dxfcheck.main(["compare", str(a), str(c), "--layers", "A-WALL,A-FLOR"]) == 1
    assert "+2" in capsys.readouterr().out
    assert dxfcheck.main(["compare", str(a), str(tmp_path / "x.dxf"), "--layers", "A-WALL"]) == 2

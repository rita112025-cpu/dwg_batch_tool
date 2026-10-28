"""DWG/DXF revision diff: fixtures are built with ezdxf, no AutoCAD needed."""
import csv
import json
import sys
from pathlib import Path

import ezdxf
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import dwgdiff  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"


def _base():
    doc = ezdxf.new("R2018")
    doc.layers.add("WALL", color=1)
    doc.layers.add("OLD", color=2)
    doc.blocks.new("TAG").add_attdef("NO", (0, 0))
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "WALL"})
    msp.add_text("HELLO", dxfattribs={"insert": (1, 1), "layer": "WALL"})
    msp.add_mtext("NOTE A", dxfattribs={"insert": (5, 5)})
    msp.add_circle((3, 3), 1)
    msp.add_lwpolyline([(0, 0), (1, 1), (2, 0)])
    ref = msp.add_blockref("TAG", (20, 20))
    ref.add_attrib("NO", "P-01", (20, 20))
    msp.add_linear_dim(base=(0, 5), p1=(0, 0), p2=(10, 0)).render()
    return doc


def _save(doc, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)
    return path


def _revised():
    doc = _base()
    doc.layers.get("WALL").dxf.color = 3
    doc.layers.remove("OLD")
    doc.layers.add("NEW")
    msp = doc.modelspace()
    for e in msp:
        kind = e.dxftype()
        if kind == "TEXT":
            e.dxf.text = "WORLD"
        elif kind == "MTEXT":
            e.text = "NOTE B"
        elif kind == "INSERT":
            e.attribs[0].dxf.text = "P-02"
        elif kind == "CIRCLE":
            e.dxf.center = (3.0005, 3)  # within tolerance
        elif kind == "LINE":
            msp.delete_entity(e)
    msp.add_point((7, 7))
    return doc


def test_identical_files(tmp_path):
    a = _save(_base(), tmp_path / "a.dxf")
    out = tmp_path / "out"
    result = dwgdiff.run(a, a, out)
    assert result["identical"] == 1 and result["changed"] == 0
    assert Path(result["outputs"]["summary"]).is_file()


def test_changes_detected(tmp_path):
    doc = _base()
    a = _save(doc, tmp_path / "a.dxf")
    b = _save(_revised_from(doc), tmp_path / "b.dxf")
    result = dwgdiff.run(a, b, tmp_path / "out")
    assert result["changed"] == 1
    rows = list(csv.DictReader(open(result["outputs"]["details"], encoding="utf-8-sig")))
    got = {(r["category"], r["change_type"], r["field"]) for r in rows}
    assert ("LAYER", "MODIFIED", "color") in got
    assert ("LAYER", "REMOVED", "layer") in got
    assert ("LAYER", "ADDED", "layer") in got
    assert ("TEXT", "MODIFIED", "text") in got
    assert ("MTEXT", "MODIFIED", "text") in got
    assert ("ATTRIBUTE", "MODIFIED", "NO") in got
    assert ("ENTITY", "REMOVED", "entity") in got
    assert ("ENTITY", "ADDED", "entity") in got
    assert not any(r["entity_type"] == "CIRCLE" for r in rows)
    report = json.loads(Path(result["outputs"]["json"]).read_text(encoding="utf-8"))
    assert report["pairs"][0]["total_changes"] == len(rows)


def _revised_from(doc):
    """Same handles as *doc*: edit a copy read back from disk."""
    import io
    stream = io.StringIO()
    doc.write(stream)
    copy = ezdxf.read(io.StringIO(stream.getvalue()))
    copy.layers.get("WALL").dxf.color = 3
    copy.layers.remove("OLD")
    copy.layers.add("NEW")
    msp = copy.modelspace()
    for e in list(msp):
        kind = e.dxftype()
        if kind == "TEXT":
            e.dxf.text = "WORLD"
        elif kind == "MTEXT":
            e.text = "NOTE B"
        elif kind == "INSERT":
            e.attribs[0].dxf.text = "P-02"
        elif kind == "CIRCLE":
            e.dxf.center = (3.0005, 3)
        elif kind == "LINE":
            msp.delete_entity(e)
    msp.add_point((7, 7))
    return copy


def test_geometry_fallback_without_handles(tmp_path):
    a = _save(_base(), tmp_path / "a.dxf")
    b = _save(_base(), tmp_path / "b.dxf")
    result = dwgdiff.run(a, b, tmp_path / "out", use_handles=False)
    assert result["identical"] == 1


def test_tolerance_match_and_move():
    rec = lambda x: {"handle": "", "type": "POINT", "layer": "0", "layout": "Model", "category": "ENTITY",  # noqa: E731
                     "geom": {"location": (x, 0.0)}, "props": {}, "content": {}, "attribs": {}, "position": ""}
    pairs, removed, added = dwgdiff.match_entities([rec(0.0004)], [rec(0.0006)], 0.001)
    assert len(pairs) == 1 and not removed and not added
    changes = dwgdiff.compare_entities(rec(0.0), rec(5.0), 0.001)
    assert changes[0]["field"] == "geometry"


def test_folder_pairing_and_dwg_conversion(tmp_path):
    old, new = tmp_path / "old", tmp_path / "new"
    _save(_base(), old / "sub" / "x.dxf")
    _save(_base(), new / "sub" / "x.dxf")
    _save(_base(), old / "gone.dxf")
    _save(_base(), new / "fresh.dxf")
    # a "DWG" holding DXF text: the fake console writes it through as the DXF
    _save(_base(), tmp_path / "tmp.dxf")
    (old / "d.dwg").write_bytes((tmp_path / "tmp.dxf").read_bytes())
    (new / "d.dwg").write_bytes((tmp_path / "tmp.dxf").read_bytes())
    wrapper = tmp_path / "accore.cmd"
    wrapper.write_text(f'@"{sys.executable}" "{FAKE}" %*\n', encoding="ascii")
    stages = []
    result = dwgdiff.run(old, new, tmp_path / "out", str(wrapper),
                         on_progress=lambda *args: stages.append(args[0]))
    by_pair = {r["pair"]: r["status"] for r in result["results"]}
    assert by_pair["gone"] == "ONLY_IN_OLD" and by_pair["fresh"] == "ONLY_IN_NEW"
    assert by_pair["sub/x"] == "IDENTICAL"
    assert by_pair["d"] == "IDENTICAL", result["failed_files"]
    assert "DWG 轉 DXF" in stages


def test_dwg_without_accore_fails_pair(tmp_path):
    a = tmp_path / "a.dwg"
    a.write_bytes(b"AC1032")
    result = dwgdiff.run(a, a, tmp_path / "out", "")
    assert result["failed"] == 1 and "accoreconsole" in result["failed_files"][0]


def test_bad_inputs(tmp_path):
    assert dwgdiff.run(tmp_path / "nope.dxf", tmp_path, tmp_path / "o")["fatal"]
    assert dwgdiff.run(tmp_path, tmp_path, tmp_path / "o", tolerance="x")["fatal"]
    txt = tmp_path / "a.txt"
    txt.write_text("x")
    assert "不支援" in dwgdiff.run(txt, txt, tmp_path / "o")["fatal"]
    f = _save(_base(), tmp_path / "f.dxf")
    assert dwgdiff.run(f, tmp_path, tmp_path / "o")["fatal"]


def test_corrupt_dxf_and_stop(tmp_path):
    bad = tmp_path / "bad.dxf"
    bad.write_text("garbage", encoding="ascii")
    result = dwgdiff.run(bad, bad, tmp_path / "out")
    assert result["failed"] == 1

    class Stopped:
        def is_set(self):
            return True
    good = _save(_base(), tmp_path / "g.dxf")
    assert dwgdiff.run(good, good, tmp_path / "out2", stop_event=Stopped())["stopped"]


def test_main(tmp_path, capsys):
    a = _save(_base(), tmp_path / "a.dxf")
    assert dwgdiff.main([str(a), str(a), str(tmp_path / "out")]) == 0
    assert dwgdiff.main([str(tmp_path / "x.dxf"), str(a), str(tmp_path / "out")]) == 2

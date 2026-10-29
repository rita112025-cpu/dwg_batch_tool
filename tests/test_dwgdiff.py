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


def test_gui_diff_mode(tmp_path, monkeypatch, tk_root):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    for widget in tk_root.winfo_children():
        widget.destroy()
    a = appmod.App(tk_root)
    a.v_mode.set("dwgdiff")
    a._refresh_mode_ui()
    assert a.mode_frames["dwgdiff"].winfo_manager()
    errors = []
    monkeypatch.setattr(appmod.messagebox, "showerror", lambda *args: errors.append(args))
    a._start_diff()
    assert errors and a.worker is None
    doc = _base()
    old = _save(doc, tmp_path / "a.dxf")
    new = _save(_revised_from(doc), tmp_path / "b.dxf")
    a.v_df_old.set(str(old))
    a.v_df_new.set(str(new))
    a.v_out.set(str(tmp_path / "out"))
    a._start_diff()
    a.worker.join(timeout=30)
    message = a.q.get(timeout=5)
    while message[0] != "diff_done":
        message = a.q.get(timeout=5)
    a._on_diff_done(message[1])
    assert "有差異：1" in a.v_result.get()
    assert str(a.btn_diff["state"]) == "normal"
    assert appmod.get_primary_result("dwgdiff", tmp_path / "out").name == "diff_summary.csv"
    a._on_diff_done(dict(pairs=0, fatal="", failed_files=[f"x{i}" for i in range(10)]))
    assert "沒有找到" in a.v_result.get() and "另有 2 個" in a.v_result.get()


def test_more_entity_kinds_and_fields():
    doc = ezdxf.new("R2018")
    msp = doc.modelspace()
    msp.add_polyline2d([(0, 0), (1, 0), (1, 1)], close=True)
    msp.add_spline(fit_points=[(0, 0), (1, 2), (3, 1)])
    hatch = msp.add_hatch()
    hatch.paths.add_polyline_path([(0, 0), (2, 0), (2, 2)])
    snap = dwgdiff.snapshot(doc, include_paper=False)
    kinds = {rec["type"]: rec for rec in snap["entities"]}
    assert kinds["POLYLINE"]["geom"]["closed"] == (1.0,)
    assert kinds["SPLINE"]["geom"]["fit"]
    assert "bbox" in kinds["HATCH"]["geom"] and kinds["HATCH"]["position"]

    def rec(**kw):
        base = {"handle": "1", "type": "INSERT", "layer": "0", "layout": "Model", "category": "BLOCK",
                "geom": {"insert": (0.0, 0.0)}, "props": {}, "content": {"block": "B"}, "attribs": {},
                "position": "0,0"}
        base.update(kw)
        return base
    changes = dwgdiff.compare_entities(
        rec(attribs={"A": "1", "B": "2"}),
        rec(layer="X", props={"color": "1"}, attribs={"B": "2", "C": "3"}), 0.001)
    got = {(c["category"], c["change_type"], c["field"]) for c in changes}
    assert ("BLOCK", "MODIFIED", "layer") in got and ("BLOCK", "MODIFIED", "properties") in got
    assert ("ATTRIBUTE", "REMOVED", "A") in got and ("ATTRIBUTE", "ADDED", "C") in got
    assert not dwgdiff.geom_close({"a": (1.0,)}, {"b": (1.0,)}, 1)
    assert not dwgdiff.geom_close({"a": (1.0,)}, {"a": (1.0, 2.0)}, 1)
    assert "measurement=5" in dwgdiff._describe(rec(content={"measurement": "5"}, attribs={"T": "v"}))
    same_handle_other_type = dwgdiff.match_entities([rec()], [rec(type="TEXT")], 0.001)
    assert len(same_handle_other_type[1]) == 1 and len(same_handle_other_type[2]) == 1

    old_layers = {"a": dwgdiff.layer_record(ezdxf.new().layers.add("A"))}
    layer = ezdxf.new().layers.add("A")
    layer.off()
    layer.lock()
    new_layers = {"a": dwgdiff.layer_record(layer)}
    fields = {c["field"] for c in dwgdiff.diff_layers(old_layers, new_layers)}
    assert {"on", "locked"} <= fields

    class Broken:
        def get_measurement(self):
            raise ValueError("no")
    assert dwgdiff._dimension_value(Broken()) == ""

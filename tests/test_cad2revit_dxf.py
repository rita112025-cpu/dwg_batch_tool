"""Model-space parsing for every entity Phase 1 keeps."""

import json
from pathlib import Path

from cad2revit.dxf_read import read_dxf
from cad2revit_synth import sample_drawing

ROOT = Path(__file__).resolve().parents[1]


def _by_type(extracted, kind):
    return [item for item in extracted["entities"] if item["type"] == kind]


def test_header_units_and_insertion_base(tmp_path):
    extracted = read_dxf(sample_drawing(tmp_path / "plan.dxf"))
    header = extracted["header"]
    assert header["dxf_version"] == "AC1032"
    assert header["insunits"] == 4
    assert header["insunits_name"] == "Millimeters"
    assert header["measurement"] == 1
    assert header["lunits"] == 2
    assert header["insbase"] == [100, 200, 300]


def test_line_keeps_coordinates_and_slope(tmp_path):
    extracted = read_dxf(sample_drawing(tmp_path / "plan.dxf"))
    lines = [item for item in _by_type(extracted, "LINE") if item["layer"] == "E-TRAY-MAIN"]
    assert len(lines) == 2
    flat = next(item for item in lines if item["start"][2] == item["end"][2])
    sloped = next(item for item in lines if item["start"][2] != item["end"][2])
    assert flat["layer"] == "E-TRAY-MAIN"
    assert flat["start"] == [0, 0, 1]
    assert flat["end"] == [10, 0, 1]
    assert flat["handle"]
    assert sloped["start"] == [0, 1, 0]
    assert sloped["end"] == [4, 1, 2]


def test_lwpolyline_elevation_bulge_and_closed_flag(tmp_path):
    extracted = read_dxf(sample_drawing(tmp_path / "plan.dxf"))
    poly = _by_type(extracted, "LWPOLYLINE")[0]
    assert poly["layer"] == "E-COND-BRANCH"
    assert poly["closed"] is True
    assert [point["z"] for point in poly["points"]] == [2.5, 2.5, 2.5]
    assert poly["points"][0]["bulge"] == 0.5
    assert poly["points"][0]["x"] == 0
    assert poly["points"][1]["x"] == 5


def test_polyline_2d_uses_elevation_and_3d_uses_vertex_z(tmp_path):
    extracted = read_dxf(sample_drawing(tmp_path / "plan.dxf"))
    polylines = _by_type(extracted, "POLYLINE")
    flat = next(item for item in polylines if len(item["points"]) == 3)
    sloped = next(item for item in polylines if len(item["points"]) == 2)
    assert flat["closed"] is True
    assert [point["z"] for point in flat["points"]] == [4, 4, 4]
    assert sloped["points"][0]["z"] == 1
    assert sloped["points"][1]["z"] == 2


def test_arc_center_radius_angles_and_endpoints(tmp_path):
    extracted = read_dxf(sample_drawing(tmp_path / "plan.dxf"))
    arc = _by_type(extracted, "ARC")[0]
    assert arc["layer"] == "E-TRAY-MAIN"
    assert arc["center"] == [0, 0, 3]
    assert arc["radius"] == 5
    assert arc["start_angle_deg"] == 0
    assert arc["end_angle_deg"] == 90
    assert arc["start"] == [5, 0, 3]
    assert arc["end"] == [0, 5, 3]


def test_text_and_mtext_content_insertion_rotation_height(tmp_path):
    extracted = read_dxf(sample_drawing(tmp_path / "plan.dxf"))
    text = _by_type(extracted, "TEXT")[0]
    mtext = _by_type(extracted, "MTEXT")[0]
    assert text["text"] == "NOTE-A"
    assert text["insertion"] == [1, 2, 3]
    assert text["rotation_deg"] == 15
    assert text["height"] == 2.5
    assert "合成" in mtext["text"] and "文字" in mtext["text"]
    assert mtext["insertion"] == [4, 5, 6]
    assert mtext["rotation_deg"] == 30
    assert mtext["height"] == 3.5


def test_block_reference_name_insertion_rotation_scale_and_attributes(tmp_path):
    extracted = read_dxf(sample_drawing(tmp_path / "plan.dxf"))
    inserts = {item["name"]: item for item in _by_type(extracted, "INSERT")}
    panel = inserts["EQ-PANEL-A"]
    assert panel["insertion"] == [8, 9, 1.25]
    assert panel["rotation_deg"] == 45
    assert panel["scale"] == [2, 3, 4]
    assert panel["layer"] == "E-EQ"
    attribs = {item["tag"]: item for item in panel["attributes"]}
    assert attribs["ID"]["text"] == "P-01"
    assert attribs["ID"]["insertion"] == [8, 10, 1.25]
    assert attribs["ID"]["rotation_deg"] == 45
    assert attribs["ID"]["height"] == 1.2
    assert attribs["LOC"]["text"] == "R1"
    assert "WIDGET-1" in inserts


def test_block_geometry_and_paperspace_are_not_model_entities(tmp_path):
    path = sample_drawing(tmp_path / "plan.dxf")
    extracted = read_dxf(path)
    tray_lines = [
        item for item in extracted["entities"]
        if item["type"] == "LINE" and item["layer"] == "E-TRAY-MAIN"
    ]
    assert len(tray_lines) == 2
    assert extracted["layouts"]["read"] == ["Model"]
    assert "Layout1" in extracted["layouts"]["skipped_with_entities"]
    assert extracted["skipped_entity_types"]["CIRCLE"] == 1
    raw = Path(path).read_bytes()
    read_dxf(path)
    assert Path(path).read_bytes() == raw


def test_reader_does_not_open_the_file_for_write(tmp_path, monkeypatch):
    path = sample_drawing(tmp_path / "plan.dxf")
    real_open = open

    def guarded(file, mode="r", *args, **kwargs):
        if Path(file).resolve() == path.resolve() and any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(mode)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded)
    assert read_dxf(path)["header"]["insunits"] == 4


def test_schema_files_are_json():
    mapping = json.loads((ROOT / "cad2revit" / "schema" / "mapping.schema.json").read_text(encoding="utf-8"))
    elements = json.loads((ROOT / "cad2revit" / "schema" / "elements.schema.json").read_text(encoding="utf-8"))
    assert mapping["$id"] == "cad2revit.mapping.v1"
    assert elements["$id"] == "cad2revit.elements.v1"
    assert "source" in elements["required"]
    assert "elements" in elements["required"]

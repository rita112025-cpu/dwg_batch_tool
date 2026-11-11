"""Mapping validation, first-match rules, and unmatched names."""

import json
from pathlib import Path

import pytest

from cad2revit.classify import classify
from cad2revit.dxf_read import read_dxf
from cad2revit.guard import MappingError
from cad2revit.mapping import load_mapping, match_rule, validate_mapping
from cad2revit.units import convert_length
from cad2revit_synth import sample_drawing, tray_width

EXAMPLE = Path(__file__).resolve().parents[1] / "cad2revit" / "examples" / "mapping.example.json"


def _write(tmp_path, payload):
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _base():
    return {
        "schema": "cad2revit.mapping.v1",
        "layer_rules": [
            {
                "id": "tray",
                "pattern": "(?i)^E-TRAY-.+",
                "category": "Cable Tray",
                "family": "Cable Tray with Fittings",
                "type": "Ladder",
            }
        ],
        "block_rules": [
            {
                "id": "panel",
                "pattern": "(?i)^EQ-PANEL-.+",
                "category": "Electrical Equipment",
                "family": "Generic Panel",
                "type": "Recessed",
            }
        ],
        "defaults": {"Cable Tray": {"width": 300, "height": 100, "unit": "mm"}},
    }


def test_shipped_example_validates_and_has_no_directory():
    mapping = load_mapping(EXAMPLE)
    assert mapping["file_name"] == "mapping.example.json"
    assert len(mapping["sha256"]) == 64
    text = EXAMPLE.read_text(encoding="utf-8")
    assert ":\\" not in text
    assert "C:" not in text


def test_fullmatch_and_first_rule_wins(tmp_path):
    payload = _base()
    payload["layer_rules"].insert(
        0,
        {
            "id": "broad",
            "pattern": "(?i)^E-.+",
            "category": "Conduit",
            "parameters": {"diameter": 25, "unit": "mm"},
        },
    )
    mapping = load_mapping(_write(tmp_path, payload))
    assert match_rule(mapping["layer_rules"], "E-TRAY-MAIN")["id"] == "broad"
    tray = [rule for rule in mapping["layer_rules"] if rule["id"] == "tray"]
    assert match_rule(tray, "E-TRAY") is None
    assert match_rule(tray, "E-TRAY-MAIN")["id"] == "tray"
    assert match_rule(mapping["layer_rules"], "e-tray-main")["id"] == "broad"
    swapped = _base()
    swapped["layer_rules"].append(
        {
            "id": "broad",
            "pattern": "(?i)^E-.+",
            "category": "Conduit",
            "parameters": {"diameter": 25, "unit": "mm"},
        }
    )
    specific = load_mapping(_write(tmp_path, swapped))
    assert match_rule(specific["layer_rules"], "E-TRAY-MAIN")["id"] == "tray"


def test_invalid_mapping_reports_every_problem(tmp_path):
    payload = _base()
    payload["schema"] = "nope"
    payload["extra"] = 1
    payload["layer_rules"][0]["pattern"] = "["
    payload["layer_rules"].append(dict(payload["layer_rules"][0], id="tray", pattern="E-.+"))
    payload["block_rules"][0]["family"] = ""
    payload["defaults"]["Cable Tray"]["width"] = True
    errors = validate_mapping(payload)
    text = "\n".join(errors)
    assert "schema" in text
    assert "未知欄位" in text
    assert "正規表示式" in text
    assert "重複" in text
    assert "family" in text
    assert "數字" in text
    with pytest.raises(MappingError):
        load_mapping(_write(tmp_path, payload))


def test_unmatched_layers_and_blocks_are_reported(tmp_path):
    extracted = read_dxf(sample_drawing(tmp_path / "plan.dxf"))
    mapping = load_mapping(EXAMPLE)
    result = classify(extracted, mapping, "a" * 64)
    layers = {item["name"]: item["entity_count"] for item in result["unmatched"]["layers"]}
    blocks = {item["name"]: item["entity_count"] for item in result["unmatched"]["blocks"]}
    assert "E-RANDOM" in layers
    assert "E-TRAY-MAIN" not in layers
    assert "E-COND-BRANCH" not in layers
    assert blocks == {"WIDGET-1": 1}
    categories = {item["revit_category"] for item in result["elements"]}
    assert "Cable Tray" in categories
    assert "Conduit" in categories
    assert "Electrical Equipment" in categories
    assert all(item["type"] in ("TEXT", "MTEXT") for item in result["annotations"])
    assert len(result["annotations"]) == 2
    panel = next(item for item in result["elements"] if item["entity_type"] == "INSERT")
    assert panel["revit_family"] == "Generic Panel"
    assert panel["revit_type"] == "Recessed"
    assert panel["geometry"]["kind"] == "point"
    assert panel["attributes"][0]["tag"] == "ID"


def test_closed_polyline_becomes_one_element_per_segment(tmp_path):
    extracted = read_dxf(sample_drawing(tmp_path / "plan.dxf"))
    result = classify(extracted, load_mapping(EXAMPLE), "b" * 64)
    conduit = [item for item in result["elements"] if item["layer"] == "E-COND-BRANCH"]
    assert len(conduit) == 3
    assert {item["segment_index"] for item in conduit} == {0, 1, 2}
    bulged = next(item for item in conduit if item["geometry"]["bulge"])
    assert bulged["geometry"]["kind"] == "bulge"
    assert bulged["geometry"]["elevation"] == 2.5
    assert bulged["parameters"]["diameter"] == 25
    assert bulged["parameters"]["origin"]["diameter"] == "rule"
    ids = [item["element_id"] for item in result["elements"]]
    assert len(ids) == len(set(ids))


def test_width_comes_from_entity_only_when_units_convert(tmp_path):
    rule = {
        "schema": "cad2revit.mapping.v1",
        "layer_rules": [
            {"id": "tray", "pattern": "^E-TRAY-.+", "category": "Cable Tray"}
        ],
        "block_rules": [],
        "defaults": {"Cable Tray": {"width": 300, "height": 100, "unit": "mm"}},
    }
    mapping = load_mapping(_write(tmp_path, rule))
    mm = classify(read_dxf(tray_width(tmp_path / "mm.dxf", 4, 400)), mapping, "c" * 64)
    element = mm["elements"][0]
    assert element["parameters"]["width"] == 400
    assert element["parameters"]["height"] == 100
    assert element["parameters"]["origin"]["width"] == "entity"
    assert element["parameters"]["origin"]["height"] == "default"
    assert element["parameters"]["complete"] is True
    assert element["derived_width"] == 400

    unitless = classify(read_dxf(tray_width(tmp_path / "none.dxf", 0, 400)), mapping, "d" * 64)
    fallback = unitless["elements"][0]
    assert fallback["parameters"]["width"] == 300
    assert fallback["parameters"]["origin"]["width"] == "default"
    assert fallback["derived_width"] == 400

    feet = classify(read_dxf(tray_width(tmp_path / "ft.dxf", 2, 1)), mapping, "e" * 64)
    assert feet["elements"][0]["parameters"]["width"] == 304.8
    assert convert_length(1, 2, "mm") == 304.8
    assert convert_length(1, 0, "mm") is None


def test_explicit_rule_width_is_not_replaced(tmp_path):
    rule = _base()
    rule["layer_rules"][0]["parameters"] = {"width": 150, "height": 50, "unit": "mm"}
    mapping = load_mapping(_write(tmp_path, rule))
    result = classify(read_dxf(tray_width(tmp_path / "w.dxf", 4, 400)), mapping, "f" * 64)
    params = result["elements"][0]["parameters"]
    assert params["width"] == 150
    assert params["origin"]["width"] == "rule"
    assert result["elements"][0]["derived_width"] == 400

"""JSON and CSV writers for classified placement data.

Published files store the source file name relative to the input folder, the
source SHA-256, and the tool version. They do not store absolute paths.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from pathlib import Path

from cad2revit import __version__
from cad2revit.const import BATCH_SCHEMA, ELEMENTS_SCHEMA, TOOL_NAME
from cad2revit.guard import ProtectionError, assert_output_safe
from cad2revit.mapping import public_mapping

ELEMENT_COLUMNS = (
    "element_id",
    "source_relative_path",
    "source_sha256",
    "tool_version",
    "units_code",
    "units_name",
    "insbase_x",
    "insbase_y",
    "insbase_z",
    "layer",
    "entity_type",
    "source_handle",
    "segment_index",
    "rule_id",
    "revit_category",
    "revit_family",
    "revit_type",
    "geometry_kind",
    "x1",
    "y1",
    "z1",
    "x2",
    "y2",
    "z2",
    "center_x",
    "center_y",
    "center_z",
    "radius",
    "start_angle_deg",
    "end_angle_deg",
    "bulge",
    "elevation",
    "insert_x",
    "insert_y",
    "insert_z",
    "rotation_deg",
    "scale_x",
    "scale_y",
    "scale_z",
    "param_width",
    "param_height",
    "param_diameter",
    "param_unit",
    "param_width_origin",
    "param_height_origin",
    "param_diameter_origin",
    "parameters_complete",
    "attributes",
)

MANIFEST_COLUMNS = (
    "tool",
    "tool_version",
    "schema",
    "source_relative_path",
    "source_sha256",
    "source_sha256_after",
    "source_unchanged",
    "status",
    "error",
    "dxf_version",
    "insunits",
    "insunits_name",
    "measurement",
    "lunits",
    "insbase_x",
    "insbase_y",
    "insbase_z",
    "element_count",
    "unclassified_count",
    "annotation_count",
    "unmatched_layers",
    "unmatched_blocks",
    "conversion_method",
    "mapping_file",
    "mapping_sha256",
)

UNMATCHED_COLUMNS = ("source_relative_path", "kind", "name", "entity_count")


def build_document(source, extracted, classified, mapping, conversion, status="OK", error=""):
    """Assemble one drawing's public JSON document."""
    drawing = None if extracted is None else extracted.get("header")
    layouts = None if extracted is None else extracted.get("layouts")
    skipped = {} if extracted is None else extracted.get("skipped_entity_types") or {}
    classified = classified or {
        "elements": [],
        "unclassified": [],
        "annotations": [],
        "unmatched": {"layers": [], "blocks": []},
    }
    unmatched = classified["unmatched"]
    return {
        "schema": ELEMENTS_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": __version__,
        "generated_at": _now(),
        "status": status,
        "error": error or "",
        "source": {
            "relative_path": source["relative_path"],
            "sha256": source["sha256"],
            "sha256_after": source["sha256_after"],
            "unchanged": bool(source["unchanged"]),
        },
        "conversion": conversion,
        "drawing": drawing,
        "mapping": public_mapping(mapping),
        "layouts": layouts,
        "counts": {
            "elements": len(classified["elements"]),
            "unclassified": len(classified["unclassified"]),
            "annotations": len(classified["annotations"]),
            "unmatched_layers": len(unmatched["layers"]),
            "unmatched_blocks": len(unmatched["blocks"]),
        },
        "skipped_entity_types": skipped,
        "elements": classified["elements"],
        "unclassified": classified["unclassified"],
        "annotations": classified["annotations"],
        "unmatched": unmatched,
    }


def build_batch(mapping, files, fatal=None) -> dict:
    return {
        "schema": BATCH_SCHEMA,
        "tool": TOOL_NAME,
        "tool_version": __version__,
        "generated_at": _now(),
        "mapping": public_mapping(mapping) if mapping else {"file_name": "", "sha256": ""},
        "fatal": fatal,
        "files": files,
    }


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def element_row(document, element) -> dict:
    """Flatten one classified element. Empty cells are None."""
    geometry = element.get("geometry") or {}
    start = geometry.get("start") or (None, None, None)
    end = geometry.get("end") or (None, None, None)
    center = geometry.get("center") or (None, None, None)
    insert = geometry.get("insertion") or (None, None, None)
    scale = geometry.get("scale") or (None, None, None)
    params = element.get("parameters") or {}
    origin = params.get("origin") or {}
    drawing = document.get("drawing") or {}
    insbase = (drawing or {}).get("insbase") or (None, None, None)
    source = document["source"]
    point = geometry.get("kind") == "point"
    return {
        "element_id": element["element_id"],
        "source_relative_path": source["relative_path"],
        "source_sha256": source["sha256"],
        "tool_version": document["tool_version"],
        "units_code": None if not drawing else drawing.get("insunits"),
        "units_name": None if not drawing else drawing.get("insunits_name"),
        "insbase_x": insbase[0],
        "insbase_y": insbase[1],
        "insbase_z": insbase[2],
        "layer": element.get("layer"),
        "entity_type": element.get("entity_type"),
        "source_handle": element.get("source_handle"),
        "segment_index": element.get("segment_index"),
        "rule_id": element.get("rule_id"),
        "revit_category": element.get("revit_category"),
        "revit_family": element.get("revit_family"),
        "revit_type": element.get("revit_type"),
        "geometry_kind": geometry.get("kind"),
        "x1": None if point else start[0],
        "y1": None if point else start[1],
        "z1": None if point else start[2],
        "x2": None if point else end[0],
        "y2": None if point else end[1],
        "z2": None if point else end[2],
        "center_x": center[0],
        "center_y": center[1],
        "center_z": center[2],
        "radius": geometry.get("radius"),
        "start_angle_deg": geometry.get("start_angle_deg"),
        "end_angle_deg": geometry.get("end_angle_deg"),
        "bulge": geometry.get("bulge"),
        "elevation": geometry.get("elevation"),
        "insert_x": insert[0] if point else None,
        "insert_y": insert[1] if point else None,
        "insert_z": insert[2] if point else None,
        "rotation_deg": geometry.get("rotation_deg") if point else None,
        "scale_x": scale[0] if point else None,
        "scale_y": scale[1] if point else None,
        "scale_z": scale[2] if point else None,
        "param_width": params.get("width"),
        "param_height": params.get("height"),
        "param_diameter": params.get("diameter"),
        "param_unit": params.get("unit"),
        "param_width_origin": origin.get("width"),
        "param_height_origin": origin.get("height"),
        "param_diameter_origin": origin.get("diameter"),
        "parameters_complete": params.get("complete"),
        "attributes": json.dumps(
            element.get("attributes") or [], ensure_ascii=False, separators=(",", ":")
        ),
    }


def manifest_row(document) -> dict:
    drawing = document.get("drawing") or {}
    insbase = drawing.get("insbase") or (None, None, None)
    unmatched = document.get("unmatched") or {"layers": [], "blocks": []}
    source = document["source"]
    conversion = document.get("conversion") or {}
    mapping = document.get("mapping") or {}
    counts = document.get("counts") or {}
    return {
        "tool": document["tool"],
        "tool_version": document["tool_version"],
        "schema": document["schema"],
        "source_relative_path": source["relative_path"],
        "source_sha256": source["sha256"],
        "source_sha256_after": source["sha256_after"],
        "source_unchanged": source["unchanged"],
        "status": document["status"],
        "error": document.get("error") or "",
        "dxf_version": drawing.get("dxf_version"),
        "insunits": drawing.get("insunits"),
        "insunits_name": drawing.get("insunits_name"),
        "measurement": drawing.get("measurement"),
        "lunits": drawing.get("lunits"),
        "insbase_x": insbase[0],
        "insbase_y": insbase[1],
        "insbase_z": insbase[2],
        "element_count": counts.get("elements", 0),
        "unclassified_count": counts.get("unclassified", 0),
        "annotation_count": counts.get("annotations", 0),
        "unmatched_layers": "|".join(item["name"] for item in unmatched["layers"]),
        "unmatched_blocks": "|".join(item["name"] for item in unmatched["blocks"]),
        "conversion_method": conversion.get("method"),
        "mapping_file": mapping.get("file_name"),
        "mapping_sha256": mapping.get("sha256"),
    }


def unmatched_rows(document):
    source = document["source"]["relative_path"]
    rows = []
    for item in document["unmatched"]["layers"]:
        rows.append(
            {
                "source_relative_path": source,
                "kind": "layer",
                "name": item["name"],
                "entity_count": item["entity_count"],
            }
        )
    for item in document["unmatched"]["blocks"]:
        rows.append(
            {
                "source_relative_path": source,
                "kind": "block",
                "name": item["name"],
                "entity_count": item["entity_count"],
            }
        )
    return rows


def write_json(path, document, protected, input_root) -> None:
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    _write_bytes(path, payload.encode("utf-8"), protected, input_root)


def write_csv(path, rows, columns, protected, input_root) -> None:
    _write_bytes(path, _csv_bytes(rows, columns), protected, input_root)


def _csv_bytes(rows, columns) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=list(columns), extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: _cell(row.get(key)) for key in columns})
    return buffer.getvalue().encode("utf-8-sig")


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return value


def _write_bytes(path, data: bytes, protected, input_root) -> None:
    path = Path(path)
    assert_output_safe(path, protected, input_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".partial")
    assert_output_safe(partial, protected, input_root)
    try:
        partial.write_bytes(data)
        assert_output_safe(path, protected, input_root)
        partial.replace(path)
    except ProtectionError:
        partial.unlink(missing_ok=True)
        raise
    finally:
        if partial.exists():
            partial.unlink(missing_ok=True)

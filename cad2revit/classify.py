"""Turn model-space entities into classified placement records.

Linear entities (LINE, LWPOLYLINE, POLYLINE, ARC) use layer rules. INSERT uses
block rules and does not fall back to the layer, so an unmatched block stays
visible in the unmatched report. TEXT and MTEXT are annotations, not elements.

A polyline becomes one record per segment. Width is taken from the entity only
when the segment has a single positive width and the drawing units can be
converted into the mapping parameter unit. Otherwise the mapping value is used.
Height and diameter are never inferred from a centerline.
"""

from __future__ import annotations

from cad2revit.const import ANNOTATION_TYPES, SIZE_REQUIREMENTS
from cad2revit.mapping import match_rule
from cad2revit.units import convert_length

_POINT_EPS = 1e-9


def classify(extracted, mapping, source_sha256: str) -> dict:
    """Return elements, unclassified records, annotations, and unmatched names."""
    elements = []
    unclassified = []
    annotations = []
    prefix = (source_sha256 or "0" * 12)[:12]
    insunits = (extracted.get("header") or {}).get("insunits")
    for entity in extracted.get("entities") or []:
        kind = entity.get("type")
        if kind in ANNOTATION_TYPES:
            annotations.append(_annotation(entity))
            continue
        if kind == "INSERT":
            record, matched = _insert_record(entity, mapping, prefix)
            (elements if matched else unclassified).append(record)
            continue
        if kind == "LINE":
            record, matched = _linear_record(
                entity, mapping, prefix, insunits, entity["start"], entity["end"],
                {"kind": "linear", "bulge": 0.0}, None, None,
            )
            (elements if matched else unclassified).append(record)
            continue
        if kind == "ARC":
            geometry = {
                "kind": "arc",
                "center": entity["center"],
                "radius": entity["radius"],
                "start_angle_deg": entity["start_angle_deg"],
                "end_angle_deg": entity["end_angle_deg"],
                "bulge": None,
            }
            record, matched = _linear_record(
                entity, mapping, prefix, insunits, entity["start"], entity["end"],
                geometry, None, None,
            )
            (elements if matched else unclassified).append(record)
            continue
        if kind in ("LWPOLYLINE", "POLYLINE"):
            segments = _segments(entity)
            if not segments:
                unclassified.append(_degenerate(entity, prefix))
                continue
            for segment in segments:
                record, matched = _linear_record(
                    entity,
                    mapping,
                    prefix,
                    insunits,
                    segment["start"],
                    segment["end"],
                    {
                        "kind": "bulge" if segment["bulge"] else "linear",
                        "bulge": segment["bulge"],
                    },
                    segment["index"],
                    segment["derived_width"],
                )
                (elements if matched else unclassified).append(record)
            continue
    elements.sort(key=lambda item: item["element_id"])
    unclassified.sort(key=lambda item: item["element_id"])
    return {
        "elements": elements,
        "unclassified": unclassified,
        "annotations": annotations,
        "unmatched": _unmatched(unclassified),
    }


def _degenerate(entity, prefix) -> dict:
    handle = entity["handle"] or "poly"
    record = {
        "element_id": f"{prefix}-{handle}",
        "source_handle": entity["handle"],
        "segment_index": None,
        "layer": entity["layer"],
        "entity_type": entity["type"],
        "block_name": None,
        "geometry": {"kind": "empty", "start": None, "end": None, "elevation": None, "bulge": None},
        "attributes": [],
        "derived_width": None,
        "reason": "too_few_points",
        "rule_id": None,
        "revit_category": None,
        "revit_family": None,
        "revit_type": None,
        "parameters": _empty_parameters(),
    }
    return record


def _annotation(entity) -> dict:
    record = {
        "type": entity["type"],
        "handle": entity["handle"],
        "layer": entity["layer"],
        "text": entity["text"],
        "insertion": entity["insertion"],
        "rotation_deg": entity["rotation_deg"],
        "height": entity["height"],
    }
    if entity["type"] == "TEXT":
        record["halign"] = entity["halign"]
        record["valign"] = entity["valign"]
    else:
        record["attachment_point"] = entity["attachment_point"]
    return record


def _insert_record(entity, mapping, prefix):
    rule = match_rule(mapping["block_rules"], entity["name"])
    handle = entity["handle"] or "insert"
    record = {
        "element_id": f"{prefix}-{handle}",
        "source_handle": entity["handle"],
        "segment_index": None,
        "layer": entity["layer"],
        "entity_type": "INSERT",
        "block_name": entity["name"],
        "geometry": {
            "kind": "point",
            "insertion": entity["insertion"],
            "rotation_deg": entity["rotation_deg"],
            "scale": entity["scale"],
        },
        "attributes": entity["attributes"],
        "derived_width": None,
    }
    if rule is None:
        record["reason"] = "unmatched_block"
        record["rule_id"] = None
        record["revit_category"] = None
        record["revit_family"] = None
        record["revit_type"] = None
        record["parameters"] = _empty_parameters()
        return record, False
    _apply_rule(record, rule, mapping, None, None)
    return record, True


def _linear_record(entity, mapping, prefix, insunits, start, end, geometry, index, derived_width):
    rule = match_rule(mapping["layer_rules"], entity["layer"])
    handle = entity["handle"] or "entity"
    suffix = f"-{index}" if index is not None else ""
    geometry = dict(geometry)
    geometry["start"] = start
    geometry["end"] = end
    if start[2] == end[2]:
        geometry["elevation"] = start[2]
    else:
        geometry["elevation"] = None
    record = {
        "element_id": f"{prefix}-{handle}{suffix}",
        "source_handle": entity["handle"],
        "segment_index": index,
        "layer": entity["layer"],
        "entity_type": entity["type"],
        "block_name": None,
        "geometry": geometry,
        "attributes": [],
        "derived_width": derived_width,
    }
    if rule is None:
        record["reason"] = "unmatched_layer"
        record["rule_id"] = None
        record["revit_category"] = None
        record["revit_family"] = None
        record["revit_type"] = None
        record["parameters"] = _empty_parameters()
        return record, False
    _apply_rule(record, rule, mapping, derived_width, insunits)
    return record, True


def _apply_rule(record, rule, mapping, derived_width, insunits):
    record["reason"] = None
    record["rule_id"] = rule["id"]
    record["revit_category"] = rule["category"]
    record["revit_family"] = rule["family"] or None
    record["revit_type"] = rule["type"] or None
    record["parameters"] = resolve_parameters(
        rule, mapping.get("defaults") or {}, derived_width, insunits
    )


def resolve_parameters(rule, defaults, derived_width, insunits) -> dict:
    """Merge category defaults, rule parameters, and an optional entity width."""
    category = rule["category"]
    base = dict(defaults.get(category) or {})
    overlay = dict(rule.get("parameters") or {})
    explicit_width = "width" in overlay
    merged = {**base, **overlay}
    origin = {
        "width": _origin("width", overlay, base),
        "height": _origin("height", overlay, base),
        "diameter": _origin("diameter", overlay, base),
    }
    if (
        not explicit_width
        and derived_width is not None
        and derived_width > 0
        and merged.get("unit")
    ):
        converted = convert_length(derived_width, insunits, merged["unit"])
        if converted is not None and converted > 0:
            merged["width"] = converted
            origin["width"] = "entity"
    parameters = {
        "width": merged.get("width"),
        "height": merged.get("height"),
        "diameter": merged.get("diameter"),
        "unit": merged.get("unit"),
        "origin": origin,
    }
    required = SIZE_REQUIREMENTS.get(category, ())
    parameters["complete"] = all(parameters.get(key) not in (None, "") for key in required)
    if parameters.get("unit") in (None, "") and any(
        parameters.get(key) not in (None, "") for key in ("width", "height", "diameter")
    ):
        parameters["complete"] = False
    return parameters


def _origin(key, overlay, base):
    if key in overlay:
        return "rule"
    if key in base:
        return "default"
    return None


def _empty_parameters() -> dict:
    return {
        "width": None,
        "height": None,
        "diameter": None,
        "unit": None,
        "origin": {"width": None, "height": None, "diameter": None},
        "complete": False,
    }


def _segments(entity):
    points = list(entity.get("points") or [])
    closed = bool(entity.get("closed"))
    if len(points) >= 2 and _same_point(points[0], points[-1]):
        points = points[:-1]
        closed = True
    if len(points) < 2:
        return []
    pairs = [(points[index], points[index + 1], index) for index in range(len(points) - 1)]
    if closed and not _same_point(points[-1], points[0]):
        pairs.append((points[-1], points[0], len(points) - 1))
    const_width = float(entity.get("const_width") or 0)
    segments = []
    for start, end, index in pairs:
        segments.append(
            {
                "index": index,
                "start": [start["x"], start["y"], start["z"]],
                "end": [end["x"], end["y"], end["z"]],
                "bulge": start["bulge"] if start["bulge"] else 0.0,
                "derived_width": _segment_width(start, const_width),
            }
        )
    return segments


def _segment_width(point, const_width):
    start_width = float(point["start_width"])
    end_width = float(point["end_width"])
    if start_width > 0 and abs(start_width - end_width) <= _POINT_EPS:
        return start_width
    if start_width == 0 and end_width == 0 and const_width > 0:
        return const_width
    return None


def _same_point(left, right) -> bool:
    return (
        abs(left["x"] - right["x"]) <= _POINT_EPS
        and abs(left["y"] - right["y"]) <= _POINT_EPS
        and abs(left["z"] - right["z"]) <= _POINT_EPS
    )


def _unmatched(unclassified) -> dict:
    layers = {}
    blocks = {}
    for item in unclassified:
        if item.get("reason") == "unmatched_layer":
            layers[item["layer"]] = layers.get(item["layer"], 0) + 1
        elif item.get("reason") == "unmatched_block":
            blocks[item["block_name"]] = blocks.get(item["block_name"], 0) + 1
    return {
        "layers": [
            {"name": name, "entity_count": layers[name]} for name in sorted(layers)
        ],
        "blocks": [
            {"name": name, "entity_count": blocks[name]} for name in sorted(blocks)
        ],
    }


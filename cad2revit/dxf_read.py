"""Read the DXF entities Phase 1 can classify.

Only model space is returned as geometry. Paper-space entities are counted in
``layouts.skipped_with_entities`` and are not exploded out of block
definitions: a block reference is one INSERT, not the lines inside the block.

``ezdxf`` is already required by this repository, so the reader uses it for
ASCII and binary DXF instead of adding a second parser.
"""

from __future__ import annotations

import ezdxf

from cad2revit.units import insunits_name


def round6(value) -> float:
    return round(float(value), 6)


def point3(value):
    z = value[2] if len(value) > 2 else 0.0
    return [round6(value[0]), round6(value[1]), round6(z)]


def read_dxf(path) -> dict:
    """Parse ``path`` into header data and model-space entities.

    The file is opened by ezdxf for reading. This function does not save the
    document and does not write to ``path``.
    """
    doc = ezdxf.readfile(str(path))
    header = _header(doc)
    entities = []
    skipped = {}
    present = []
    skipped_layouts = []
    for layout in doc.layouts:
        present.append(layout.name)
        if layout.is_modelspace:
            for entity in layout:
                item = _entity(entity)
                if item is None:
                    name = entity.dxftype()
                    skipped[name] = skipped.get(name, 0) + 1
                else:
                    entities.append(item)
        elif any(True for _ in layout):
            skipped_layouts.append(layout.name)
    layers = []
    for layer in doc.layers:
        layers.append(layer.dxf.name)
    return {
        "header": header,
        "layers": layers,
        "entities": entities,
        "skipped_entity_types": skipped,
        "layouts": {
            "read": ["Model"],
            "present": present,
            "skipped_with_entities": skipped_layouts,
        },
    }


def _header(doc) -> dict:
    insunits = _header_int(doc, "$INSUNITS", 0)
    return {
        "dxf_version": str(doc.dxfversion),
        "insunits": insunits,
        "insunits_name": insunits_name(insunits),
        "measurement": _header_optional_int(doc, "$MEASUREMENT"),
        "lunits": _header_optional_int(doc, "$LUNITS"),
        "insbase": _header_point(doc, "$INSBASE"),
    }


def _header_int(doc, key, default):
    value = _header_raw(doc, key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _header_optional_int(doc, key):
    value = _header_raw(doc, key)
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _header_raw(doc, key):
    try:
        if key not in doc.header:
            return None
        return doc.header[key]
    except Exception:
        return None


def _header_point(doc, key):
    value = _header_raw(doc, key)
    if value is None:
        return [0.0, 0.0, 0.0]
    try:
        return point3(value)
    except Exception:
        return [0.0, 0.0, 0.0]


def _entity(entity):
    kind = entity.dxftype()
    if kind == "LINE":
        return _line(entity)
    if kind == "LWPOLYLINE":
        return _lwpolyline(entity)
    if kind == "POLYLINE":
        return _polyline(entity)
    if kind == "ARC":
        return _arc(entity)
    if kind == "TEXT":
        return _text(entity)
    if kind == "MTEXT":
        return _mtext(entity)
    if kind == "INSERT":
        return _insert(entity)
    return None


def _common(entity) -> dict:
    return {
        "type": entity.dxftype(),
        "handle": str(entity.dxf.get("handle") or ""),
        "layer": str(entity.dxf.get("layer") or "0"),
    }


def _line(entity):
    item = _common(entity)
    item["start"] = point3(entity.dxf.start)
    item["end"] = point3(entity.dxf.end)
    return item


def _lwpolyline(entity):
    item = _common(entity)
    elevation = round6(entity.dxf.elevation)
    const_width = round6(entity.dxf.const_width) if entity.dxf.hasattr("const_width") else 0.0
    points = []
    for x, y, start_width, end_width, bulge in entity.get_points("xyseb"):
        points.append(
            {
                "x": round6(x),
                "y": round6(y),
                "z": elevation,
                "bulge": round6(bulge),
                "start_width": round6(start_width),
                "end_width": round6(end_width),
            }
        )
    item["closed"] = bool(entity.closed)
    item["const_width"] = const_width
    item["points"] = points
    return item


def _polyline(entity):
    if entity.is_polygon_mesh or entity.is_poly_face_mesh:
        return None
    item = _common(entity)
    elevation = None
    if entity.is_2d_polyline:
        elevation = round6(entity.dxf.elevation.z)
    points = []
    for vertex in entity.vertices:
        loc = vertex.dxf.location
        z = elevation if elevation is not None else round6(loc.z)
        points.append(
            {
                "x": round6(loc.x),
                "y": round6(loc.y),
                "z": z,
                "bulge": round6(vertex.dxf.bulge),
                "start_width": round6(vertex.dxf.start_width),
                "end_width": round6(vertex.dxf.end_width),
            }
        )
    item["closed"] = bool(entity.is_closed)
    item["const_width"] = 0.0
    item["points"] = points
    return item


def _arc(entity):
    item = _common(entity)
    item["center"] = point3(entity.dxf.center)
    item["radius"] = round6(entity.dxf.radius)
    item["start_angle_deg"] = round6(entity.dxf.start_angle)
    item["end_angle_deg"] = round6(entity.dxf.end_angle)
    item["start"] = point3(entity.start_point)
    item["end"] = point3(entity.end_point)
    return item


def _text(entity):
    item = _common(entity)
    item["text"] = str(entity.dxf.text or "")
    item["insertion"] = point3(entity.dxf.insert)
    item["rotation_deg"] = round6(entity.dxf.get("rotation", 0))
    item["height"] = round6(entity.dxf.get("height", 0))
    item["halign"] = int(entity.dxf.get("halign", 0) or 0)
    item["valign"] = int(entity.dxf.get("valign", 0) or 0)
    return item


def _mtext(entity):
    item = _common(entity)
    try:
        text = entity.plain_text()
    except Exception:
        text = entity.text
    item["text"] = str(text or "")
    item["insertion"] = point3(entity.dxf.insert)
    item["rotation_deg"] = round6(entity.dxf.get("rotation", 0))
    item["height"] = round6(entity.dxf.get("char_height", 0))
    item["attachment_point"] = int(entity.dxf.get("attachment_point", 1) or 1)
    return item


def _insert(entity):
    item = _common(entity)
    item["name"] = str(entity.dxf.name or "")
    item["insertion"] = point3(entity.dxf.insert)
    item["rotation_deg"] = round6(entity.dxf.get("rotation", 0))
    item["scale"] = [
        round6(entity.dxf.get("xscale", 1)),
        round6(entity.dxf.get("yscale", 1)),
        round6(entity.dxf.get("zscale", 1)),
    ]
    attributes = []
    for attrib in entity.attribs:
        attributes.append(
            {
                "tag": str(attrib.dxf.tag or ""),
                "text": str(attrib.dxf.text or ""),
                "insertion": point3(attrib.dxf.insert),
                "rotation_deg": round6(attrib.dxf.get("rotation", 0)),
                "height": round6(attrib.dxf.get("height", 0)),
                "handle": str(attrib.dxf.get("handle") or ""),
            }
        )
    item["attributes"] = attributes
    return item

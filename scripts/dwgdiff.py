"""Compare two revisions of drawings and report what changed.

Works on one file pair or on two folders paired by relative path (the suffix is
ignored, so ``A/x.dwg`` pairs with ``B/x.dxf``). DWG files are converted to DXF
with accoreconsole first (through ``convert_batch``); DXF files are read as-is.
Source files are never written.

Matching, per pair of drawings:
  1. same handle and same entity type
  2. same type + layer + layout + quantized geometry fingerprint
  3. same type + layer + layout and every coordinate within ``tolerance``
Anything left over is ADDED or REMOVED.

Outputs under ``<output_dir>/dwgdiff/``: diff_summary.csv (one row per pair),
diff_details.csv (one row per change) and diff_report.json.
"""
from __future__ import annotations

import csv
import datetime
import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ezdxf  # noqa: E402
from ezdxf import bbox, recover  # noqa: E402

import convert_batch  # noqa: E402
from common import log_error  # noqa: E402

DRAWING_SUFFIXES = (".dwg", ".dxf")
ADDED, REMOVED, MODIFIED = "ADDED", "REMOVED", "MODIFIED"

SUMMARY_FIELDS = [
    "pair", "old_file", "new_file", "status", "added", "removed", "modified",
    "total_changes", "layer_changes", "block_changes", "attribute_changes",
    "text_changes", "dimension_changes", "entity_changes", "error",
]
DETAIL_FIELDS = [
    "file", "category", "change_type", "entity_type", "layout", "layer", "handle",
    "field", "before", "after", "position",
]
CATEGORY_COLUMN = {
    "LAYER": "layer_changes",
    "BLOCK": "block_changes",
    "ATTRIBUTE": "attribute_changes",
    "TEXT": "text_changes",
    "MTEXT": "text_changes",
    "DIMENSION": "dimension_changes",
    "ENTITY": "entity_changes",
}
STATUS_TEXT = {
    "CHANGED": "有差異",
    "IDENTICAL": "相同",
    "ONLY_IN_OLD": "只在舊版",
    "ONLY_IN_NEW": "只在新版",
    "FAILED": "失敗",
    "STOPPED": "已停止",
}

# DXF attributes read generically from any entity that has them.
_GEOM_ATTRS = (
    "insert", "location", "center", "start", "end", "defpoint", "defpoint2", "defpoint3",
    "vtx0", "vtx1", "vtx2", "vtx3", "radius", "start_angle", "end_angle", "major_axis",
    "ratio", "rotation", "height", "char_height", "width", "xscale", "yscale", "zscale",
    "thickness",
)
_PROP_ATTRS = ("color", "linetype", "lineweight", "true_color")
_CATEGORY = {"INSERT": "BLOCK", "TEXT": "TEXT", "MTEXT": "MTEXT", "DIMENSION": "DIMENSION"}


# ---------- reading ----------

def read_dxf(path):
    """Read a DXF, falling back to ezdxf's recover mode for damaged files."""
    try:
        return ezdxf.readfile(str(path))
    except ezdxf.DXFStructureError:
        doc, _auditor = recover.readfile(str(path))
        return doc


def _floats(value):
    if isinstance(value, (int, float)):
        return (float(value),)
    return tuple(float(v) for v in value)


def _geometry(entity):
    geom = {}
    for name in _GEOM_ATTRS:
        if entity.dxf.hasattr(name):
            try:
                geom[name] = _floats(entity.dxf.get(name))
            except (TypeError, ValueError):
                continue
    kind = entity.dxftype()
    if kind == "LWPOLYLINE":
        geom["points"] = tuple(c for p in entity.get_points("xyb") for c in p)
        geom["closed"] = (float(entity.closed),)
    elif kind == "POLYLINE":
        geom["points"] = tuple(c for v in entity.vertices for c in _floats(v.dxf.location))
        geom["closed"] = (float(entity.is_closed),)
    elif kind == "SPLINE":
        geom["points"] = tuple(c for p in entity.control_points for c in _floats(p))
        geom["fit"] = tuple(c for p in entity.fit_points for c in _floats(p))
    elif not geom:
        try:
            box = bbox.extents([entity], fast=True)
            if box.has_data:
                geom["bbox"] = _floats(box.extmin) + _floats(box.extmax)
        except Exception:  # noqa: BLE001  (bbox is best effort for exotic entities)
            pass
    return geom


def _position(geom):
    for name in ("insert", "location", "center", "start", "defpoint", "vtx0", "points", "bbox"):
        if geom.get(name):
            values = geom[name]
            return f"{values[0]:.3f},{values[1] if len(values) > 1 else 0.0:.3f}"
    return ""


def _dimension_value(entity):
    try:
        value = entity.get_measurement()
    except Exception:  # noqa: BLE001  (not every dimension subtype can be measured)
        return ""
    if isinstance(value, (int, float)):
        return f"{float(value):.6g}"
    return ",".join(f"{v:.6g}" for v in _floats(value))


def entity_record(entity, layout):
    """Plain dict snapshot of one entity (everything later compared)."""
    kind = entity.dxftype()
    geom = _geometry(entity)
    rec = {
        "handle": entity.dxf.handle or "",
        "type": kind,
        "layer": entity.dxf.get("layer", "0"),
        "layout": layout,
        "category": _CATEGORY.get(kind, "ENTITY"),
        "geom": geom,
        "props": {name: str(entity.dxf.get(name)) for name in _PROP_ATTRS if entity.dxf.hasattr(name)},
        "content": {},
        "attribs": {},
        "position": _position(geom),
    }
    if kind in ("TEXT", "ATTDEF"):
        rec["content"]["text"] = entity.dxf.get("text", "")
    elif kind == "MTEXT":
        rec["content"]["text"] = entity.text
    elif kind == "INSERT":
        rec["content"]["block"] = entity.dxf.name
        for attrib in entity.attribs:
            rec["attribs"][attrib.dxf.tag] = attrib.dxf.get("text", "")
    elif kind == "DIMENSION":
        rec["content"]["measurement"] = _dimension_value(entity)
        rec["content"]["text"] = entity.dxf.get("text", "")
    return rec


def layer_record(layer):
    return {
        "name": layer.dxf.name,
        "color": str(abs(layer.dxf.get("color", 7))),
        "linetype": layer.dxf.get("linetype", "Continuous"),
        "lineweight": str(layer.dxf.get("lineweight", -3)),
        "on": str(layer.is_on()),
        "frozen": str(layer.is_frozen()),
        "locked": str(layer.is_locked()),
        "plot": str(layer.dxf.get("plot", 1)),
    }


def snapshot(doc, include_paper=True):
    """``{"layers": {casefold name: rec}, "entities": [rec, ...]}`` for one document."""
    layers = {layer.dxf.name.casefold(): layer_record(layer) for layer in doc.layers}
    entities = []
    layouts = list(doc.layouts) if include_paper else [doc.modelspace()]
    for layout in layouts:
        for entity in layout:
            entities.append(entity_record(entity, layout.name))
    return {"layers": layers, "entities": entities}


# ---------- comparing ----------

def _change(category, change_type, rec=None, field="", before="", after="", layer=None):
    rec = rec or {}
    return {
        "category": category,
        "change_type": change_type,
        "entity_type": rec.get("type", ""),
        "layout": rec.get("layout", ""),
        "layer": rec.get("layer", "") if layer is None else layer,
        "handle": rec.get("handle", ""),
        "field": field,
        "before": before,
        "after": after,
        "position": rec.get("position", ""),
    }


def diff_layers(old_layers, new_layers):
    changes = []
    for key in sorted(set(old_layers) | set(new_layers)):
        before, after = old_layers.get(key), new_layers.get(key)
        if before is None:
            changes.append(_change("LAYER", ADDED, field="layer", after=after["name"], layer=after["name"]))
        elif after is None:
            changes.append(_change("LAYER", REMOVED, field="layer", before=before["name"], layer=before["name"]))
        else:
            for field in ("color", "linetype", "lineweight", "on", "frozen", "locked", "plot"):
                if before[field] != after[field]:
                    changes.append(_change("LAYER", MODIFIED, field=field, before=before[field],
                                           after=after[field], layer=after["name"]))
    return changes


def _fmt_values(values):
    return "(" + ",".join(f"{v:.6g}" for v in values) + ")" if len(values) > 1 else f"{values[0]:.6g}"


def _fmt_geom(geom):
    return "; ".join(f"{name}={_fmt_values(values)}" for name, values in geom.items() if values)


def geom_close(a, b, tolerance):
    if a.keys() != b.keys():
        return False
    for name, values in a.items():
        other = b[name]
        if len(values) != len(other):
            return False
        if any(abs(x - y) > tolerance for x, y in zip(values, other)):
            return False
    return True


def fingerprint(rec, tolerance):
    """Hashable key: type + layer + layout + geometry snapped to a ``tolerance`` grid."""
    step = tolerance if tolerance > 0 else 1e-9
    snapped = tuple(
        (name, tuple(int(round(v / step)) for v in values))
        for name, values in sorted(rec["geom"].items())
    )
    return (rec["type"], rec["layer"].casefold(), rec["layout"], rec["content"].get("block", ""), snapped)


def compare_entities(before, after, tolerance):
    """Changes between two records already known to be the same object."""
    changes = []
    category = after["category"]
    if before["layer"] != after["layer"]:
        changes.append(_change(category, MODIFIED, after, "layer", before["layer"], after["layer"]))
    if before["layout"] != after["layout"]:
        changes.append(_change(category, MODIFIED, after, "layout", before["layout"], after["layout"]))
    if not geom_close(before["geom"], after["geom"], tolerance):
        changes.append(_change(category, MODIFIED, after, "geometry",
                               _fmt_geom(before["geom"]), _fmt_geom(after["geom"])))
    for field in sorted(set(before["content"]) | set(after["content"])):
        old_value, new_value = before["content"].get(field, ""), after["content"].get(field, "")
        if old_value != new_value:
            changes.append(_change(category, MODIFIED, after, field, old_value, new_value))
    if before["props"] != after["props"]:
        fmt = lambda props: "; ".join(f"{k}={v}" for k, v in sorted(props.items()))  # noqa: E731
        changes.append(_change(category, MODIFIED, after, "properties", fmt(before["props"]), fmt(after["props"])))
    for tag in sorted(set(before["attribs"]) | set(after["attribs"])):
        if tag not in before["attribs"]:
            changes.append(_change("ATTRIBUTE", ADDED, after, tag, "", after["attribs"][tag]))
        elif tag not in after["attribs"]:
            changes.append(_change("ATTRIBUTE", REMOVED, after, tag, before["attribs"][tag], ""))
        elif before["attribs"][tag] != after["attribs"][tag]:
            changes.append(_change("ATTRIBUTE", MODIFIED, after, tag, before["attribs"][tag], after["attribs"][tag]))
    return changes


def _describe(rec):
    parts = []
    if rec["content"].get("block"):
        parts.append(f"block={rec['content']['block']}")
    if rec["content"].get("text"):
        parts.append(f"text={rec['content']['text']}")
    if rec["content"].get("measurement"):
        parts.append(f"measurement={rec['content']['measurement']}")
    parts.extend(f"{tag}={value}" for tag, value in sorted(rec["attribs"].items()))
    if not parts:
        parts.append(_fmt_geom(rec["geom"]))
    return "; ".join(part for part in parts if part)


def match_entities(old, new, tolerance, use_handles=True):
    """Return ``(pairs, removed, added)``; ``pairs`` is a list of ``(old_rec, new_rec)``."""
    pairs = []
    left = list(old)
    right = list(new)
    if use_handles:
        by_handle = {rec["handle"]: rec for rec in right if rec["handle"]}
        used = set()
        rest = []
        for rec in left:
            other = by_handle.get(rec["handle"]) if rec["handle"] else None
            if other is not None and other["type"] == rec["type"] and id(other) not in used:
                pairs.append((rec, other))
                used.add(id(other))
            else:
                rest.append(rec)
        left = rest
        right = [rec for rec in right if id(rec) not in used]

    buckets = {}
    for rec in right:
        buckets.setdefault(fingerprint(rec, tolerance), []).append(rec)
    rest = []
    for rec in left:
        bucket = buckets.get(fingerprint(rec, tolerance))
        if bucket:
            pairs.append((rec, bucket.pop(0)))
        else:
            rest.append(rec)
    left = rest
    right = [rec for bucket in buckets.values() for rec in bucket]

    groups = {}
    for rec in right:
        groups.setdefault((rec["type"], rec["layer"].casefold(), rec["layout"]), []).append(rec)
    rest = []
    for rec in left:
        group = groups.get((rec["type"], rec["layer"].casefold(), rec["layout"]), [])
        found = next((i for i, other in enumerate(group) if geom_close(rec["geom"], other["geom"], tolerance)), None)
        if found is None:
            rest.append(rec)
        else:
            pairs.append((rec, group.pop(found)))
    added = [rec for group in groups.values() for rec in group]
    return pairs, rest, added


def diff_snapshots(old, new, tolerance=0.001, use_handles=True):
    """All changes between two snapshots, in a stable order."""
    changes = diff_layers(old["layers"], new["layers"])
    pairs, removed, added = match_entities(old["entities"], new["entities"], tolerance, use_handles)
    for rec in removed:
        changes.append(_change(rec["category"], REMOVED, rec, "entity", _describe(rec), ""))
    for rec in added:
        changes.append(_change(rec["category"], ADDED, rec, "entity", "", _describe(rec)))
    for before, after in pairs:
        changes.extend(compare_entities(before, after, tolerance))
    return changes


def diff_documents(old_doc, new_doc, tolerance=0.001, use_handles=True, include_paper=True):
    return diff_snapshots(snapshot(old_doc, include_paper), snapshot(new_doc, include_paper),
                          tolerance, use_handles)


# ---------- pairing ----------

def _key(rel):
    return rel.with_suffix("").as_posix().casefold()


def _drawings(root, recursive):
    pattern = "**/*" if recursive else "*"
    found = {}
    for path in sorted(Path(root).glob(pattern)):
        if not path.is_file() or path.suffix.lower() not in DRAWING_SUFFIXES:
            continue
        rel = path.relative_to(root)
        key = _key(rel)
        # A DXF beside a DWG of the same name wins: no conversion needed.
        if key not in found or path.suffix.lower() == ".dxf":
            found[key] = (rel, path)
    return found


def pair_inputs(old, new, recursive=True):
    """List of ``(label, old_path or None, new_path or None)``; raises ValueError on bad input."""
    old, new = Path(old), Path(new)
    if old.is_file() and new.is_file():
        for path in (old, new):
            if path.suffix.lower() not in DRAWING_SUFFIXES:
                raise ValueError(f"不支援的檔案格式：{path.name}（只接受 DWG / DXF）")
        return [(new.name, old, new)]
    if old.is_dir() and new.is_dir():
        old_files, new_files = _drawings(old, recursive), _drawings(new, recursive)
        pairs = []
        for key in sorted(set(old_files) | set(new_files)):
            left, right = old_files.get(key), new_files.get(key)
            label = (right or left)[0].with_suffix("").as_posix()
            pairs.append((label, left[1] if left else None, right[1] if right else None))
        return pairs
    for path in (old, new):
        if not path.exists():
            raise ValueError(f"找不到：{path}")
    raise ValueError("舊版與新版必須同為檔案或同為資料夾")


# ---------- outputs ----------

def _summary_row(label, old_path, new_path, status, changes, error=""):
    row = {field: 0 for field in SUMMARY_FIELDS}
    row.update(pair=label, old_file="" if old_path is None else str(old_path),
               new_file="" if new_path is None else str(new_path), status=status, error=error)
    for change in changes:
        row[change["change_type"].lower()] += 1
        row[CATEGORY_COLUMN[change["category"]]] += 1
    row["total_changes"] = len(changes)
    return row


def write_outputs(out_dir, rows, details, meta):
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": out_dir / "diff_summary.csv",
        "details": out_dir / "diff_details.csv",
        "json": out_dir / "diff_report.json",
    }
    with paths["summary"].open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, SUMMARY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with paths["details"].open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, DETAIL_FIELDS)
        writer.writeheader()
        for label, changes in details:
            for change in changes:
                writer.writerow(dict(change, file=label))
    report = dict(meta, pairs=[dict(row, changes=changes) for row, (_label, changes) in zip(rows, details)])
    paths["json"].write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return {key: str(path) for key, path in paths.items()}


# ---------- conversion ----------

def _convert(jobs, accore, output_dir, timeout, stop_event, on_progress):
    """Convert DWGs with one accoreconsole batch. ``jobs``: {dwg path: dxf path}.

    Returns {dwg path: error ('' on success)}."""
    if not jobs:
        return {}
    if not str(accore or "").strip() or not Path(accore).is_file():
        return {dwg: "找不到 accoreconsole.exe，無法把 DWG 轉成 DXF" for dwg in jobs}
    total = len(jobs)
    results = convert_batch.convert_many(
        list(jobs.items()), accore, {"output_dir": str(output_dir)}, timeout=timeout, overwrite=True,
        stop_event=stop_event, on_start=lambda i, n, path: on_progress("DWG 轉 DXF", i, total, path),
    )
    errors = {dwg: "已停止" for dwg in jobs}
    for rec in results:
        source = Path(rec["source"])
        if source not in errors:
            continue
        if rec["status"] == "DXF_VALID":
            errors[source] = ""
        elif rec["status"] == "STOPPED":
            errors[source] = "已停止"
        else:
            errors[source] = f"DWG 轉 DXF 失敗：{rec['status']} {rec.get('error', '')}".strip()
    return errors


def _empty_summary():
    return {
        "pairs": 0, "ok": 0, "changed": 0, "identical": 0, "failed": 0, "skipped": 0,
        "added": 0, "removed": 0, "modified": 0, "stopped": False, "fatal": "",
        "failed_files": [], "elapsed": 0.0, "results": [], "outputs": {},
    }


def run(
    old,
    new,
    output_dir,
    accore="",
    *,
    tolerance=0.001,
    recursive=True,
    use_handles=True,
    include_paper=True,
    timeout=300,
    stop_event=None,
    on_progress=None,
    log=None,
):
    """Compare ``old`` against ``new`` (two files or two folders) and write the reports.

    ``on_progress(stage, index, total, current)``; ``log(level, message)``.
    """
    on_progress = on_progress or (lambda stage, index, total, current: None)
    log = log or (lambda level, message: None)
    started = time.time()
    summary = _empty_summary()
    try:
        tolerance = float(tolerance)
    except (TypeError, ValueError):
        tolerance = -1.0
    if not math.isfinite(tolerance) or tolerance < 0:
        summary["fatal"] = "座標容差必須是 0 以上的數字"
        return summary
    try:
        pairs = pair_inputs(old, new, recursive)
    except ValueError as exc:
        summary["fatal"] = str(exc)
        return summary
    out_root = Path(output_dir)
    out_dir = out_root / "dwgdiff"
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        summary["fatal"] = f"輸出資料夾無法寫入：{exc}"
        return summary
    summary["pairs"] = len(pairs)
    log("INFO", f"差異比對開始：{old} -> {new}，共 {len(pairs)} 組")

    conversions = {}
    sides = {}
    for index, (label, old_path, new_path) in enumerate(pairs):
        for side, path in (("old", old_path), ("new", new_path)):
            if path is not None and path.suffix.lower() == ".dwg":
                # One DWG can be on both sides (or in several pairs): convert it once.
                target = out_dir / "dxf" / side / f"{index:04d}_{path.stem}.dxf"
                sides[(index, side)] = conversions.setdefault(path, target)
            elif path is not None:
                sides[(index, side)] = path
    errors = _convert(conversions, accore, out_root, timeout, stop_event, on_progress)

    rows, details = [], []
    for index, (label, old_path, new_path) in enumerate(pairs):
        changes, error = [], ""
        if stop_event is not None and stop_event.is_set():
            status, error = "STOPPED", "已停止"
            summary["stopped"] = True
        elif old_path is None or new_path is None:
            status = "ONLY_IN_NEW" if old_path is None else "ONLY_IN_OLD"
            summary["skipped"] += 1
        else:
            on_progress("差異比對", index + 1, len(pairs), label)
            error = errors.get(old_path, "") or errors.get(new_path, "")
            if not error:
                try:
                    old_doc = read_dxf(sides[(index, "old")])
                    new_doc = read_dxf(sides[(index, "new")])
                    changes = diff_documents(old_doc, new_doc, tolerance, use_handles, include_paper)
                except Exception as exc:  # noqa: BLE001  (one bad drawing must not stop the batch)
                    error = f"讀取 DXF 失敗：{type(exc).__name__}: {exc}"
            if error:
                status = "STOPPED" if error == "已停止" else "FAILED"
                if status == "STOPPED":
                    summary["stopped"] = True
                else:
                    summary["failed"] += 1
                    summary["failed_files"].append(f"{label}: {error}")
                    log("ERROR", f"失敗 {label}: {error}")
                    log_error(out_root / "logs", f"dwgdiff {label}: {error}")
            else:
                status = "CHANGED" if changes else "IDENTICAL"
                summary["ok"] += 1
                summary["changed" if changes else "identical"] += 1
                log("OK", f"{STATUS_TEXT[status]} {label}（{len(changes)} 筆變更）")
        row = _summary_row(label, old_path, new_path, status, changes, error)
        for key in ("added", "removed", "modified"):
            summary[key] += row[key]
        rows.append(row)
        details.append((label, changes))
    summary["results"] = rows
    meta = {
        "generated": datetime.datetime.now().isoformat(timespec="seconds"),
        "old": str(old),
        "new": str(new),
        "tolerance": tolerance,
        "use_handles": use_handles,
        "include_paper": include_paper,
        "summary": {key: summary[key] for key in (
            "pairs", "ok", "changed", "identical", "failed", "skipped", "added", "removed", "modified", "stopped")},
    }
    try:
        summary["outputs"] = write_outputs(out_dir, rows, details, meta)
    except OSError as exc:
        summary["fatal"] = f"無法寫入比對結果：{exc}"
    summary["elapsed"] = round(time.time() - started, 2)
    return summary


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="Compare two DWG/DXF revisions (files or folders).")
    parser.add_argument("old")
    parser.add_argument("new")
    parser.add_argument("output_dir")
    parser.add_argument("--accore", default="")
    parser.add_argument("--tolerance", type=float, default=0.001)
    parser.add_argument("--no-handles", action="store_true", help="match by geometry only")
    args = parser.parse_args(argv)
    result = run(args.old, args.new, args.output_dir, args.accore, tolerance=args.tolerance,
                 use_handles=not args.no_handles,
                 log=lambda level, message: print(f"[{level}] {message}"))
    if result["fatal"]:
        print(f"[ERROR] {result['fatal']}")
        return 2
    print(json.dumps(result["outputs"], ensure_ascii=False))
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())

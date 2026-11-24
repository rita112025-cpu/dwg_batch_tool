"""Inspect and compare DXF files (already exported from DWG by AutoCAD) with ezdxf.

Pure functions on ezdxf documents plus a small CLI:

* ``inspect``: per-layer entity counts (by type), geometry extents and title-block
  attributes of one DXF.
* ``compare``: entity counts of chosen layers (e.g. ``A-WALL``, ``A-FLOR``) in two DXFs.

Layer names are matched case-insensitively (like AutoCAD) and may use wildcards.
Only modelspace is counted unless ``--all-layouts`` is given; block contents are not
exploded (an INSERT counts as one entity).
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import sys
from collections import Counter
from pathlib import Path

import ezdxf
from ezdxf import bbox

from titleblock import name_matches, parse_patterns


def _layouts(doc, all_layouts):
    if all_layouts:
        return list(doc.layouts)
    return [doc.modelspace()]


def _layer_matches(layer, patterns):
    folded = layer.casefold()
    return any(fnmatch.fnmatchcase(folded, p.casefold()) for p in patterns)


def count_entities(doc, layers=None, all_layouts=False):
    """Return ``{layer: Counter(dxftype -> n)}`` for layers matching ``layers`` (all if None)."""
    patterns = parse_patterns(layers) if layers else None
    result = {}
    for layout in _layouts(doc, all_layouts):
        for entity in layout:
            layer = entity.dxf.layer
            if patterns is not None and not _layer_matches(layer, patterns):
                continue
            result.setdefault(layer, Counter())[entity.dxftype()] += 1
    return result


def layer_extents(doc, layer, all_layouts=False):
    """Bounding box ``(min_xyz, max_xyz)`` of a layer's entities, or None when empty/unmeasurable."""
    entities = [e for layout in _layouts(doc, all_layouts) for e in layout
                if e.dxf.layer.casefold() == layer.casefold()]
    if not entities:
        return None
    box = bbox.extents(entities, fast=True)
    if not box.has_data:
        return None
    return tuple(box.extmin), tuple(box.extmax)


def title_block_attributes(doc, block_patterns, all_layouts=True):
    """List ``{layout, block_name, handle, attribs}`` for INSERTs whose block name matches."""
    patterns = parse_patterns(block_patterns)
    found = []
    for layout in _layouts(doc, all_layouts):
        for insert in layout.query("INSERT"):
            if not name_matches([insert.dxf.name], patterns):
                continue
            found.append({
                "layout": layout.name,
                "block_name": insert.dxf.name,
                "handle": insert.dxf.handle,
                "attribs": {a.dxf.tag: a.dxf.text for a in insert.attribs},
            })
    return found


def compare_counts(left, right, layers):
    """Compare two ``count_entities`` results over ``layers``.

    Returns rows ``{layer, left, right, diff, by_type}`` where ``by_type`` maps each
    entity type that differs to ``(left_n, right_n)``. Wildcards in ``layers`` are
    expanded against the layer names found in either side.
    """
    patterns = parse_patterns(layers)
    names = {}
    for name in list(left) + list(right):
        if _layer_matches(name, patterns):
            names.setdefault(name.casefold(), name)
    # Literal (non-wildcard) layers are reported even if absent from both files.
    for pattern in patterns:
        if not any(ch in pattern for ch in "*?["):
            names.setdefault(pattern.casefold(), pattern)

    def lookup(counts, key):
        merged = Counter()
        for name, counter in counts.items():
            if name.casefold() == key:
                merged.update(counter)
        return merged

    rows = []
    for key in sorted(names):
        a, b = lookup(left, key), lookup(right, key)
        types = set(a) | set(b)
        rows.append({
            "layer": names[key],
            "left": sum(a.values()),
            "right": sum(b.values()),
            "diff": sum(b.values()) - sum(a.values()),
            "by_type": {t: (a[t], b[t]) for t in sorted(types) if a[t] != b[t]},
        })
    return rows


def _open(path):
    return ezdxf.readfile(str(path))


def _format_compare(rows, left_name, right_name):
    lines = [f"{'layer':<20}{left_name:>12}{right_name:>12}{'diff':>8}"]
    for row in rows:
        lines.append(f"{row['layer']:<20}{row['left']:>12}{row['right']:>12}{row['diff']:>+8}")
        for etype, (a, b) in row["by_type"].items():
            lines.append(f"  {etype:<18}{a:>12}{b:>12}{b - a:>+8}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)

    p_inspect = sub.add_parser("inspect", help="layer counts and title-block attributes of one DXF")
    p_inspect.add_argument("dxf", type=Path)
    p_inspect.add_argument("--layers", help="layer names / wildcards, comma separated (default: all)")
    p_inspect.add_argument("--title-block", help="title-block block name patterns")
    p_inspect.add_argument("--all-layouts", action="store_true")

    p_cmp = sub.add_parser("compare", help="compare entity counts of layers in two DXFs")
    p_cmp.add_argument("left", type=Path)
    p_cmp.add_argument("right", type=Path)
    p_cmp.add_argument("--layers", required=True, help="e.g. A-WALL,A-FLOR")
    p_cmp.add_argument("--all-layouts", action="store_true")
    p_cmp.add_argument("--json", action="store_true", dest="as_json")

    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            doc = _open(args.dxf)
            counts = count_entities(doc, args.layers, args.all_layouts)
            out = {"layers": {k: dict(v) for k, v in sorted(counts.items())}}
            if args.title_block:
                out["title_blocks"] = title_block_attributes(doc, args.title_block)
            print(json.dumps(out, ensure_ascii=False, indent=2))
            return 0
        left = count_entities(_open(args.left), None, args.all_layouts)
        right = count_entities(_open(args.right), None, args.all_layouts)
        rows = compare_counts(left, right, args.layers)
    except (OSError, ezdxf.DXFError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(_format_compare(rows, args.left.stem, args.right.stem))
    return 1 if any(r["diff"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())

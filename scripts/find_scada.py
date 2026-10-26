"""SCADA keyword search over TEXT/MTEXT/ATTRIB/block names/layer names.

Matching itself lives in scada_rules.py (single implementation). Outputs:
  scada_hits.csv      one row per (entity, keyword)      - detail, excluded noise not included
  object_hits.csv     one row per entity, keywords merged - 'SCADA|TRAY'
  scada_excluded.csv  audit list of hits classified excluded_noise (e.g. building 'System Panel')
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, resolve, log_error
from analyze_dxf import iter_layouts, xyz, mtext_plain, canonical_text
import scada_rules
from scada_rules import HIT_FIELDS, OBJECT_FIELDS, EXCLUDED

import ezdxf

FIELDS = HIT_FIELDS  # kept for backward compatibility


def search_doc(doc, rel, rules):
    """Returns (hits, excluded) row lists. `rules` may also be a plain keyword list."""
    if not isinstance(rules, scada_rules.Rules):
        rules = scada_rules.Rules(rules)
    hits, excluded = [], []

    def add(src, layer, block, text, pt, handle):
        x, y, z = pt
        for m in scada_rules.find_matches(text, rules):
            row = dict(file=rel, keyword=m["keyword"], source_type=src, layer=layer, block=block,
                       text=text, x=x, y=y, z=z, handle=handle, confidence=m["confidence"],
                       hit_source=src)
            (excluded if m["confidence"] == EXCLUDED else hits).append(row)

    for l in doc.layers:
        name = canonical_text(l.dxf.name)
        add("LAYER_NAME", name, "", name, ("", "", ""), l.dxf.get("handle", ""))
    for layout in iter_layouts(doc):
        ctx = "" if layout.is_any_layout else canonical_text(layout.name)
        for e in layout:
            t = e.dxftype()
            layer, h = canonical_text(e.dxf.get("layer", "0")), e.dxf.get("handle", "")
            if t == "TEXT":
                add("TEXT", layer, ctx, canonical_text(e.dxf.text), xyz(e.dxf.insert), h)
            elif t == "MTEXT":
                add("MTEXT", layer, ctx, mtext_plain(e), xyz(e.dxf.insert), h)
            elif t == "INSERT":
                block = canonical_text(e.dxf.name)
                add("BLOCK_NAME", layer, block, block, xyz(e.dxf.insert), h)
                for a in e.attribs:
                    add("ATTRIB", canonical_text(a.dxf.get("layer", "")) or layer, block,
                        canonical_text(a.dxf.text), xyz(a.dxf.insert), a.dxf.get("handle", ""))
    return hits, excluded


def _write(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_results(cfg, hits, excluded, failed_files=None, stopped=False, log=None):
    """Write collected search rows and return the standard run summary."""
    log = log or (lambda level, msg: print(f"[{level}] {msg}"))
    failed_files = list(failed_files or [])
    out = resolve(cfg, "output_dir")
    objects = scada_rules.build_object_hits(hits)
    (out / "csv").mkdir(parents=True, exist_ok=True)
    _write(out / "csv" / "scada_hits.csv", hits, HIT_FIELDS)
    _write(out / "csv" / "object_hits.csv", objects, OBJECT_FIELDS)
    _write(out / "csv" / "scada_excluded.csv", excluded, HIT_FIELDS)
    if excluded:
        log("INFO", f"excluded noise hits: {len(excluded)} (see scada_excluded.csv)")
    return dict(hits=len(hits), objects=len(objects), excluded=len(excluded),
                failed=len(failed_files), failed_files=failed_files, stopped=stopped)


def run(cfg, log=None, progress=None, stop_event=None, files=None):
    """Returns dict(hits, objects, excluded, failed, stopped). Always writes the three CSVs
    (header only when nothing matched)."""
    log = log or (lambda level, msg: print(f"[{level}] {msg}"))
    progress = progress or (lambda i, n, cur: None)
    out = resolve(cfg, "output_dir")
    root = out / "dxf"
    rules = scada_rules.build_rules(cfg)
    hits, excluded = [], []
    failed_files = []
    stopped = False
    files = sorted(Path(p) for p in files) if files is not None else sorted(root.rglob("*.dxf"))
    for n, p in enumerate(files, 1):
        if stop_event is not None and stop_event.is_set():
            stopped = True
            break
        progress(n, len(files), str(p))
        try:
            h, x = search_doc(ezdxf.readfile(str(p)), str(p.relative_to(root)), rules)
            hits += h
            excluded += x
        except Exception as e:  # noqa: BLE001
            failed_files.append(f"{p.name}: {type(e).__name__}: {e}")
            log_error(cfg, f"find_scada {p}: {type(e).__name__}: {e}")
            log("ERROR", f"search {p.name}: {type(e).__name__}: {e}")
    return write_results(cfg, hits, excluded, failed_files, stopped, log)


def main(cfg=None):
    s = run(cfg or load_config())
    print(f"keyword hits: {s['hits']}  unique matched objects: {s['objects']}  "
          f"excluded noise: {s['excluded']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

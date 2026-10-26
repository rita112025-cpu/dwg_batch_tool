"""Parse DXF files under output/dxf into CSV + JSON."""
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, resolve, log_error

import ezdxf
import scada_rules


def iter_layouts(doc):
    for layout in doc.layouts:
        yield layout
    # non-layout block definitions also hold searchable text
    for blk in doc.blocks:
        if not blk.name.startswith("*") and not blk.is_any_layout:
            yield blk


def xyz(p):
    try:
        return round(p[0], 4), round(p[1], 4), round(p[2] if len(p) > 2 else 0.0, 4)
    except Exception:  # noqa: BLE001
        return "", "", ""


_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_DXF_UNICODE = re.compile(r"\\U\+([0-9A-Fa-f]{4})")
_LEFTOVER_ESCAPE = re.compile(r"\\[UM]\+[0-9A-Fa-f]")


def decode_dxf_unicode(text):
    """Decode DXF '\\U+XXXX' escapes (exactly 4 hex digits). Malformed escapes and surrogate code
    points are left as-is so text_quality can flag them. ezdxf's own decoder is not used because it
    turns a truncated '\\U+4ED' into a wrong character."""
    if not text or "\\U+" not in text:
        return text

    def one(m):
        code = int(m.group(1), 16)
        return m.group(0) if 0xD800 <= code <= 0xDFFF else chr(code)
    return _DXF_UNICODE.sub(one, text)


def canonical_text(raw):
    """TEXT / ATTRIB / names -> canonical text used everywhere downstream."""
    return decode_dxf_unicode(raw or "") if raw is not None else raw


def text_quality(text):
    """OK / EMPTY / SUSPECT_ENCODING. Text is never altered, only flagged.
    Suspect = contains U+FFFD, control characters or an escape left undecoded (\\U+ / \\M+), or is made
    mostly (>= 50%) of '?'. A normal sentence that merely contains a '?' is OK."""
    if text is None or not str(text).strip():
        return "EMPTY"
    t = "".join(str(text).split())
    if "�" in t or _CONTROL.search(str(text)) or _LEFTOVER_ESCAPE.search(str(text)):
        return "SUSPECT_ENCODING"
    if t.count("?") * 2 >= len(t):
        return "SUSPECT_ENCODING"
    return "OK"


def mtext_plain(e):
    """MTEXT -> canonical text. Formatting codes are stripped FIRST and '\\U+XXXX' decoded after, so an
    escaped '\\', '{' or '}' can never be re-read as a formatting code."""
    try:
        text = e.plain_text()
    except Exception:  # noqa: BLE001
        text = e.text
    return decode_dxf_unicode(text)


def analyze_doc(doc, rel_name):
    """Analyze an already loaded DXF document."""
    res = dict(layers=[], texts=[], blocks=[], dimensions=[], attribs=[], entity_types=Counter())
    layer_count = Counter()
    for layout in iter_layouts(doc):
        in_block_def = not layout.is_any_layout
        for e in layout:
            t = e.dxftype()
            res["entity_types"][t] += 1
            raw_layer = e.dxf.get("layer", "0")
            layer = canonical_text(raw_layer)
            layer_count[layer] += 1
            h = e.dxf.get("handle", "")
            ctx = f"BLOCKDEF:{canonical_text(layout.name)}" if in_block_def else ""
            if t in ("TEXT", "MTEXT"):
                x, y, z = xyz(e.dxf.insert)
                text = canonical_text(e.dxf.text) if t == "TEXT" else mtext_plain(e)
                res["texts"].append(dict(file=rel_name, layer=layer, entity_type=t, text=text,
                                         x=x, y=y, z=z, rotation=e.dxf.get("rotation", 0),
                                         handle=h, context=ctx, text_quality=text_quality(text)))
            elif t == "INSERT":
                x, y, z = xyz(e.dxf.insert)
                name = canonical_text(e.dxf.name)
                res["blocks"].append(dict(
                    file=rel_name, layer=layer, block_name=name, x=x, y=y, z=z,
                    rotation=e.dxf.get("rotation", 0), xscale=e.dxf.get("xscale", 1),
                    yscale=e.dxf.get("yscale", 1), zscale=e.dxf.get("zscale", 1),
                    handle=h, context=ctx))
                for a in e.attribs:
                    ax, ay, az = xyz(a.dxf.insert)
                    al = canonical_text(a.dxf.get("layer", raw_layer))
                    tag, atext = canonical_text(a.dxf.tag), canonical_text(a.dxf.text)
                    res["attribs"].append(dict(file=rel_name, layer=al, block_name=name,
                                               tag=tag, text=atext, x=ax, y=ay, z=az,
                                               handle=a.dxf.get("handle", ""), parent_handle=h))
                    res["texts"].append(dict(file=rel_name, layer=al, entity_type="ATTRIB",
                                             text=atext, x=ax, y=ay, z=az,
                                             rotation=a.dxf.get("rotation", 0),
                                             handle=a.dxf.get("handle", ""), context=f"tag={tag}",
                                             text_quality=text_quality(atext)))
            elif t == "DIMENSION":
                try:
                    meas = round(e.get_measurement(), 4)
                except Exception:  # noqa: BLE001
                    meas = ""
                res["dimensions"].append(dict(file=rel_name, layer=layer, dimtype=e.dimtype,
                                              measurement=meas,
                                              text_override=canonical_text(e.dxf.get("text", "")),
                                              handle=h))
    for l in doc.layers:
        lname = canonical_text(l.dxf.name)
        res["layers"].append(dict(file=rel_name, layer=lname, color=l.dxf.get("color", 7),
                                  linetype=l.dxf.get("linetype", ""),
                                  entity_count=layer_count.get(lname, 0)))
    info = dict(dxf_version=doc.dxfversion, entity_count=sum(res["entity_types"].values()),
                layer_count=len(res["layers"]),
                block_count=len([b for b in doc.blocks if not b.name.startswith("*")]),
                text_count=len(res["texts"]))
    return res, info


def analyze(dxf_path, rel_name):
    """Load and analyze one DXF file."""
    return analyze_doc(ezdxf.readfile(str(dxf_path)), rel_name)


def write_csv(path, rows, fields):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run(cfg, write_csv_files=True, write_json_files=True, log=None, progress=None, stop_event=None,
        files=None, document_callback=None):
    """Analyze every DXF under output/dxf. Returns dict(total, ok, failed, stopped)."""
    log = log or (lambda level, msg: print(f"[{level}] {msg}"))
    progress = progress or (lambda i, n, cur: None)
    out = resolve(cfg, "output_dir")
    dxf_root = out / "dxf"
    (out / "csv").mkdir(parents=True, exist_ok=True)
    (out / "json").mkdir(parents=True, exist_ok=True)
    idx, layers, texts, blocks, dims, attribs = [], [], [], [], [], []
    files = sorted(Path(p) for p in files) if files is not None else sorted(dxf_root.rglob("*.dxf"))
    rules = scada_rules.build_rules(cfg)
    stats = dict(total=len(files), ok=0, failed=0, stopped=False, suspect=0, failed_files=[])
    for n, p in enumerate(files, 1):
        if stop_event is not None and stop_event.is_set():
            stats["stopped"] = True
            break
        progress(n, len(files), str(p))
        rel = str(p.relative_to(dxf_root))
        src = str(resolve(cfg, "input_dir") / Path(rel).with_suffix(".dwg"))
        row = dict(source_file=src, dxf_file=str(p), dxf_version="", entity_count=0, layer_count=0,
                   block_count=0, text_count=0, status="")
        try:
            doc = ezdxf.readfile(str(p))
            res, info = analyze_doc(doc, rel)
            if document_callback is not None:
                document_callback(doc, rel)
            row.update(info, status="OK")
            layers += res["layers"]
            texts += res["texts"]
            blocks += res["blocks"]
            dims += res["dimensions"]
            attribs += res["attribs"]
            if write_json_files:
                jp = out / "json" / Path(rel).with_suffix(".json")
                jp.parent.mkdir(parents=True, exist_ok=True)
                jp.write_text(json.dumps(dict(file=rel, index=info, entity_types=res["entity_types"],
                                              layers=res["layers"], texts=res["texts"],
                                              blocks=res["blocks"], attribs=res["attribs"],
                                              dimensions=res["dimensions"]),
                                         ensure_ascii=False, indent=1), encoding="utf-8")
            stats["ok"] += 1
            log("OK", f"analyzed {rel}")
        except Exception as e:  # noqa: BLE001
            row["status"] = f"ERROR: {type(e).__name__}: {e}"
            log_error(cfg, f"analyze {p}: {row['status']}")
            stats["failed"] += 1
            stats["failed_files"].append(f"{rel}: {row['status']}")
            log("ERROR", f"analyze {rel}: {row['status']}")
        idx.append(row)
    stats["suspect"] = sum(1 for t in texts if t["text_quality"] == "SUSPECT_ENCODING")
    log("INFO", f"Suspect text records: {stats['suspect']}")
    if write_csv_files:
        c = out / "csv"
        write_csv(c / "file_index.csv", idx, ["source_file", "dxf_file", "dxf_version", "entity_count",
                                              "layer_count", "block_count", "text_count", "status"])
        write_csv(c / "layers.csv", layers, ["file", "layer", "color", "linetype", "entity_count"])
        write_csv(c / "texts.csv", texts, ["file", "layer", "entity_type", "text", "x", "y", "z",
                                           "rotation", "handle", "context", "text_quality"])
        write_csv(c / "blocks.csv", blocks, ["file", "layer", "block_name", "x", "y", "z", "rotation",
                                             "xscale", "yscale", "zscale", "handle", "context"])
        write_csv(c / "block_summary.csv", scada_rules.build_block_summary(blocks, rules, attribs),
                  scada_rules.BLOCK_SUMMARY_FIELDS)
        write_csv(c / "dimensions.csv", dims, ["file", "layer", "dimtype", "measurement",
                                               "text_override", "handle"])
        write_csv(c / "attribs.csv", attribs, ["file", "layer", "block_name", "tag", "text", "x", "y",
                                               "z", "handle", "parent_handle"])
    return stats


def main(cfg=None):
    s = run(cfg or load_config())
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

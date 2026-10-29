"""Compare two revisions of the same drawings, entity by entity.

``dwgdiff_dump.lsp`` prints one ``DIFFROW`` line per entity (handle, type,
layout, layer, insertion point, size/geometry, text, block name) and one
``DIFFATTR`` line per block attribute. Fields use ``|=|`` and are parsed the
same way as the font-fix and text markers.

Matching: entities are paired by handle first (same handle and same type).
Whatever is left is paired by type + layout + insertion point within the
tolerance. Unpaired old entities are 刪除, unpaired new entities are 新增, and
paired entities with any differing property are 修改 (one CSV row per property).

Batch: the old and new folders are paired by relative path (by file name when
subfolders are off). Every DWG is copied to an ASCII temp file and only that
copy is opened; nothing is saved and the originals are never opened for write.
"""
from __future__ import annotations

import csv
import datetime
import html
import json
import math
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dwgtext  # noqa: E402
import fontfix  # noqa: E402
from common import log_error  # noqa: E402
from convert_single import safe_tmpdir, sha256  # noqa: E402
from textbatch import _copy_bytes, _launch_all, _log_line  # noqa: E402

MARKER_SEP = fontfix.MARKER_SEP
LISP_SOURCE = Path(__file__).resolve().parent / "dwgdiff_dump.lsp"
DEFAULT_TOLERANCE = 1e-6
HTML_ROW_LIMIT = 2000

CHANGE_LABELS = {"ADDED": "新增", "DELETED": "刪除", "MODIFIED": "修改"}
MATCH_LABELS = {"handle": "handle", "position": "座標+類型", "": ""}
STATUS_TEXT = {
    "SAME": "無差異",
    "DIFF": "有差異",
    "FAILED": "失敗",
    "STOPPED": "已停止",
    "ONLY_OLD": "只有舊版",
    "ONLY_NEW": "只有新版",
}

# geom keys printed by the LISP -> column label
GEOM_LABELS = {
    "x2": "終點 X",
    "y2": "終點 Y",
    "z2": "終點 Z",
    "len": "長度",
    "r": "半徑",
    "a0": "起始角",
    "a1": "結束角",
    "mx": "長軸 X",
    "my": "長軸 Y",
    "ratio": "短長軸比",
    "p0": "起始參數",
    "p1": "結束參數",
    "h": "字高",
    "w": "寬度",
    "wf": "寬度係數",
    "rot": "旋轉角",
    "sx": "X 比例",
    "sy": "Y 比例",
    "sz": "Z 比例",
    "meas": "標註值",
    "n": "頂點數",
    "closed": "封閉",
    "verts": "頂點座標",
    "pat": "填充樣式",
    "scale": "填充比例",
    "ang": "填充角度",
    "loops": "邊界數",
    "deg": "階數",
    "nctl": "控制點數",
    "nfit": "擬合點數",
}
# entget leaves some groups out when they hold the default value
GEOM_DEFAULTS = {"sx": "1", "sy": "1", "sz": "1", "rot": "0", "wf": "1", "closed": "0"}

CHANGE_FIELDS = [
    "比對檔案", "舊版檔案", "新版檔案", "變更類型", "比對方式", "舊 handle", "新 handle",
    "物件類型", "配置", "圖層", "X", "Y", "變更屬性", "舊值", "新值",
]
RESULT_FIELDS = [
    "比對檔案", "舊版檔案", "新版檔案", "狀態", "新增", "刪除", "修改", "未變更",
    "handle 配對", "座標配對", "錯誤", "秒數",
]


# ---------------------------------------------------------------------------
# LISP and script
# ---------------------------------------------------------------------------

def build_dump_lisp():
    """The LISP source from ``dwgdiff_dump.lsp``. The caller writes it with ``fontfix.lisp_bytes``."""
    src = LISP_SOURCE.read_text(encoding="utf-8-sig")
    if MARKER_SEP not in src or not fontfix._lisp_parens_balanced(src):
        raise RuntimeError("dwgdiff_dump.lsp is damaged (separator or parentheses)")
    return src


def build_dump_script(jobs, lisp_path):
    """ASCII script: load the LISP, then OPEN -> dump per drawing. Nothing is saved."""
    lines = dwgtext._preamble(lisp_path)
    for i, job in enumerate(jobs):
        lines.append("_.OPEN")
        lines.append(f'"{job["spath"]}"')
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_OPEN_{i}|" (getvar "DWGNAME") "|\\n"))')
        lines.append(
            "(if (and (boundp 'dd-dump-current) dd-dump-current) "
            f'(dd-dump-current) (princ (strcat "\\nDIFF_" "ERROR{MARKER_SEP}lisp not loaded\\n")))'
        )
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_DONE_{i}_\\n"))')
    lines.append("_.QUIT")
    text = "\n".join(lines) + "\n"
    fontfix.script_bytes(text)
    return text


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_geom(text):
    """``r=2.5;n=4`` -> ``{"r": "2.5", "n": "4"}``. Later keys win."""
    geom = {}
    for part in (text or "").split(";"):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        key = key.strip()
        if key:
            geom[key] = value.strip()
    return geom


def parse_dump(text):
    """Parse one drawing's console output into rows, summary count, errors and ready flag."""
    rows = []
    attrs = {}
    summary = None
    errors = []
    ready = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line == "DIFFDUMP_READY":
            ready = True
            continue
        rest = dwgtext._marker_rest(line, "DIFFROW")
        if rest is not None:
            handle, kind, layout, layer, x, y, z, geom, body, block = dwgtext._split_fields(rest, 10)
            if not handle and not kind:
                continue
            rows.append({
                "handle": handle.upper(),
                "kind": kind.upper(),
                "layout": layout,
                "layer": layer,
                "x": x,
                "y": y,
                "z": z,
                "geom": parse_geom(geom),
                "text": body,
                "block": block,
                "attrs": {},
            })
            continue
        rest = dwgtext._marker_rest(line, "DIFFATTR")
        if rest is not None:
            owner, tag, value = dwgtext._split_fields(rest, 3)
            bucket = attrs.setdefault(owner.upper(), {})
            key = tag.upper() or "?"
            if key in bucket:  # duplicate tags in one insert keep their order
                n = 2
                while f"{key}#{n}" in bucket:
                    n += 1
                key = f"{key}#{n}"
            bucket[key] = value
            continue
        rest = dwgtext._marker_rest(line, "DIFFDUMP_SUMMARY")
        if rest is not None:
            count = dwgtext._summary_one(rest)
            if count is not None:
                summary = count
            continue
        rest = dwgtext._marker_rest(line, "DIFF_ERROR")
        if rest is not None:
            message = dwgtext._split_fields(rest, 1)[0].strip()
            if message:
                errors.append(message)
    for row in rows:
        row["attrs"] = attrs.get(row["handle"], {})
    return {"rows": rows, "summary": summary, "errors": errors, "ready": ready}


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _num(value):
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def same_value(old, new, tolerance=DEFAULT_TOLERANCE):
    """Numbers within ``tolerance`` are equal. Anything else is compared as text."""
    old = "" if old is None else str(old)
    new = "" if new is None else str(new)
    a, b = _num(old), _num(new)
    if a is not None and b is not None:
        return abs(a - b) <= tolerance
    return old == new


def same_number_list(old, new, tolerance=DEFAULT_TOLERANCE):
    """``1,2 3,4`` style vertex lists, compared number by number."""
    a = (old or "").replace(",", " ").split()
    b = (new or "").replace(",", " ").split()
    if len(a) != len(b):
        return False
    return all(same_value(x, y, tolerance) for x, y in zip(a, b))


def _fmt(value):
    """Shorter display for numbers (``10.50000000`` -> ``10.5``). Text is unchanged."""
    number = _num(value)
    if number is None:
        return "" if value is None else str(value)
    text = f"{number:.6f}".rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def position_text(row):
    parts = [_fmt(row.get(axis, "")) for axis in ("x", "y", "z")]
    if not any(parts):
        return ""
    return ", ".join(part or "?" for part in parts)


def _geom_value(row, key):
    value = row["geom"].get(key)
    if value is None or value == "":
        return GEOM_DEFAULTS.get(key, value or "")
    return value


def diff_properties(old, new, tolerance=DEFAULT_TOLERANCE):
    """``[(property, old_value, new_value), ...]`` for two paired entities."""
    changes = []
    if old["layout"] != new["layout"]:
        changes.append(("配置", old["layout"], new["layout"]))
    if old["layer"] != new["layer"]:
        changes.append(("圖層", old["layer"], new["layer"]))
    if not all(same_value(old[axis], new[axis], tolerance) for axis in ("x", "y", "z")):
        changes.append(("位置", position_text(old), position_text(new)))
    keys = list(old["geom"]) + [key for key in new["geom"] if key not in old["geom"]]
    for key in keys:
        a, b = _geom_value(old, key), _geom_value(new, key)
        if key == "verts":
            equal = same_number_list(a, b, tolerance)
        else:
            equal = same_value(a, b, tolerance)
        if not equal:
            label = GEOM_LABELS.get(key, key)
            if key == "verts":
                changes.append((label, a, b))
            else:
                changes.append((label, _fmt(a), _fmt(b)))
    if old["text"] != new["text"]:
        changes.append(("文字內容", old["text"], new["text"]))
    if old["block"] != new["block"]:
        changes.append(("圖塊名稱", old["block"], new["block"]))
    tags = list(old["attrs"]) + [tag for tag in new["attrs"] if tag not in old["attrs"]]
    for tag in tags:
        a = old["attrs"].get(tag)
        b = new["attrs"].get(tag)
        if a != b:
            changes.append((f"屬性 {tag}", "" if a is None else a, "" if b is None else b))
    return changes


def describe(row):
    """One-line summary used as the value of an added or deleted entity."""
    if row["text"]:
        return f"{row['kind']}「{row['text']}」"
    if row["block"]:
        return f"{row['kind']} {row['block']}"
    return row["kind"]


def _cell(row, size):
    x, y = _num(row["x"]), _num(row["y"])
    if x is None or y is None:
        return None
    return (math.floor(x / size), math.floor(y / size))


def _near(old, new, tolerance):
    return all(same_value(old[axis], new[axis], tolerance) for axis in ("x", "y", "z"))


def compare_rows(old_rows, new_rows, tolerance=DEFAULT_TOLERANCE, use_handle=True):
    """Pair and classify entities. Returns counts and the change list.

    Each change has ``change`` (ADDED / DELETED / MODIFIED), ``match`` (handle,
    position or empty), both handles, the entity type, layout, layer, X, Y,
    ``property``, ``old`` and ``new``. A modified entity yields one change per
    differing property.
    """
    tolerance = abs(float(tolerance))
    pairs = []
    old_left = []
    new_by_handle = {}
    if use_handle:
        for row in new_rows:
            if row["handle"] and row["handle"] not in new_by_handle:
                new_by_handle[row["handle"]] = row
    used_new = set()
    for row in old_rows:
        other = new_by_handle.get(row["handle"]) if use_handle and row["handle"] else None
        if other is not None and id(other) not in used_new and other["kind"] == row["kind"]:
            pairs.append((row, other, "handle"))
            used_new.add(id(other))
        else:
            old_left.append(row)
    new_left = [row for row in new_rows if id(row) not in used_new]

    # Second pass: type + layout + insertion point. A grid keeps it near-linear.
    size = max(tolerance, 1e-9)
    grid = {}
    for row in new_left:
        cell = _cell(row, size)
        if cell is not None:
            grid.setdefault((row["kind"], row["layout"], cell), []).append(row)
    by_position = set()
    deleted = []
    for row in old_left:
        cell = _cell(row, size)
        best, best_score = None, None
        if cell is not None:
            nearby = (
                candidate
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for candidate in grid.get((row["kind"], row["layout"], (cell[0] + dx, cell[1] + dy)), ())
            )
            for candidate in nearby:
                if id(candidate) in used_new or not _near(row, candidate, tolerance):
                    continue
                score = len(diff_properties(row, candidate, tolerance))
                if best_score is None or score < best_score:
                    best, best_score = candidate, score
                    if score == 0:  # identical entity: no better candidate exists
                        break
        if best is None:
            deleted.append(row)
        else:
            used_new.add(id(best))
            by_position.add(id(best))
            pairs.append((row, best, "position"))
    added = [row for row in new_rows if id(row) not in used_new]

    changes = []
    modified = unchanged = 0
    for old, new, how in pairs:
        props = diff_properties(old, new, tolerance)
        if not props:
            unchanged += 1
            continue
        modified += 1
        for name, before, after in props:
            changes.append(_change("MODIFIED", how, old, new, name, before, after))
    for row in deleted:
        changes.append(_change("DELETED", "", row, None, "", describe(row), ""))
    for row in added:
        changes.append(_change("ADDED", "", None, row, "", "", describe(row)))
    return {
        "changes": changes,
        "added": len(added),
        "deleted": len(deleted),
        "modified": modified,
        "unchanged": unchanged,
        "by_handle": sum(1 for _o, _n, how in pairs if how == "handle"),
        "by_position": len(by_position),
    }


def _change(kind, how, old, new, prop, before, after):
    shown = new if new is not None else old
    return {
        "change": kind,
        "match": how,
        "old_handle": old["handle"] if old else "",
        "new_handle": new["handle"] if new else "",
        "kind": shown["kind"],
        "layout": shown["layout"],
        "layer": shown["layer"],
        "x": _fmt(shown["x"]),
        "y": _fmt(shown["y"]),
        "property": prop,
        "old": before,
        "new": after,
    }


def change_row(pair, item):
    return {
        "比對檔案": pair.get("name", ""),
        "舊版檔案": pair.get("old", ""),
        "新版檔案": pair.get("new", ""),
        "變更類型": CHANGE_LABELS.get(item["change"], item["change"]),
        "比對方式": MATCH_LABELS.get(item["match"], item["match"]),
        "舊 handle": item["old_handle"],
        "新 handle": item["new_handle"],
        "物件類型": item["kind"],
        "配置": item["layout"],
        "圖層": item["layer"],
        "X": item["x"],
        "Y": item["y"],
        "變更屬性": item["property"],
        "舊值": item["old"],
        "新值": item["new"],
    }


def result_row(rec):
    return {
        "比對檔案": rec.get("name", ""),
        "舊版檔案": rec.get("old", ""),
        "新版檔案": rec.get("new", ""),
        "狀態": STATUS_TEXT.get(rec.get("status", ""), rec.get("status", "")),
        "新增": rec.get("added", 0),
        "刪除": rec.get("deleted", 0),
        "修改": rec.get("modified", 0),
        "未變更": rec.get("unchanged", 0),
        "handle 配對": rec.get("by_handle", 0),
        "座標配對": rec.get("by_position", 0),
        "錯誤": rec.get("error", ""),
        "秒數": rec.get("elapsed_seconds", 0),
    }


# ---------------------------------------------------------------------------
# Folders and pairing
# ---------------------------------------------------------------------------

def _inside(child, parent):
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def folder_problem(old_dir, new_dir, output_dir):
    """Return a Traditional Chinese error, or None when the three folders may be used."""
    texts = [str(value or "").strip() for value in (old_dir, new_dir, output_dir)]
    if not texts[0]:
        return "請選擇舊版資料夾"
    if not texts[1]:
        return "請選擇新版資料夾"
    if not texts[2]:
        return "請指定輸出資料夾"
    old, new, out = (Path(text) for text in texts)
    if not old.is_dir():
        return "舊版資料夾不存在"
    if not new.is_dir():
        return "新版資料夾不存在"
    try:
        old_r, new_r, out_r = old.resolve(), new.resolve(), out.resolve()
    except OSError as exc:
        return f"無法讀取資料夾：{exc}"

    def same(a, b):
        return os.path.normcase(str(a)) == os.path.normcase(str(b))

    if same(old_r, new_r):
        return "舊版與新版資料夾不可相同"
    for label, folder in (("舊版", old_r), ("新版", new_r)):
        if same(out_r, folder):
            return f"輸出資料夾不可與{label}資料夾相同"
        if _inside(out_r, folder):
            return f"輸出資料夾不可放在{label}資料夾裡面"
    return None


def _pair_key(path, root, recursive):
    path = Path(path)
    rel = path.resolve().relative_to(Path(root).resolve()).as_posix() if recursive else path.name
    return os.path.normcase(rel).casefold(), rel


def pair_files(old_dir, new_dir, recursive=True):
    """Pair DWGs by relative path (file name when not recursive), ignoring case.

    Returns ``(pairs, only_old, only_new)``; each pair is ``(name, old_path, new_path)``.
    """
    olds, news = {}, {}
    for target, root in ((olds, old_dir), (news, new_dir)):
        for path in fontfix.iter_dwgs(root, recursive):
            key, rel = _pair_key(path, root, recursive)
            target.setdefault(key, (rel, path))
    pairs = [(olds[k][0], olds[k][1], news[k][1]) for k in sorted(olds) if k in news]
    only_old = [(olds[k][0], olds[k][1]) for k in sorted(olds) if k not in news]
    only_new = [(news[k][0], news[k][1]) for k in sorted(news) if k not in olds]
    return pairs, only_old, only_new


# ---------------------------------------------------------------------------
# HTML report
# ---------------------------------------------------------------------------

_CSS = """
body{font-family:"Microsoft JhengHei","PingFang TC","Noto Sans TC",sans-serif;margin:24px;color:#1f2328;background:#fff}
h1{font-size:20px;margin:0 0 4px}
.meta{color:#57606a;font-size:13px;margin-bottom:16px}
table{border-collapse:collapse;width:100%;font-size:13px;margin:8px 0 16px}
th,td{border:1px solid #d0d7de;padding:4px 6px;text-align:left;vertical-align:top;word-break:break-all}
th{background:#f6f8fa}
td.n{text-align:right}
.tag{display:inline-block;padding:0 6px;border-radius:3px;font-size:12px;white-space:nowrap}
.ADDED{background:#E2F3E5;color:#14532D}
.DELETED{background:#FADADA;color:#7A1212}
.MODIFIED{background:#FFF0C7;color:#5F4100}
.FAILED,.STOPPED{background:#FADADA;color:#7A1212}
.ONLY_OLD,.ONLY_NEW{background:#E9EAEC;color:#3B3F45}
.SAME{background:#E2F3E5;color:#14532D}
.DIFF{background:#FFF0C7;color:#5F4100}
details{margin:8px 0;border:1px solid #d0d7de;border-radius:4px;padding:6px 10px}
summary{cursor:pointer;font-weight:600}
.note{color:#57606a;font-size:12px}
"""


def _e(value):
    return html.escape("" if value is None else str(value))


def build_html(results, old_dir, new_dir, tolerance, use_handle, changes_csv):
    """Self-contained HTML report (no external files or scripts)."""
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    totals = {key: sum(int(rec.get(key) or 0) for rec in results)
              for key in ("added", "deleted", "modified", "unchanged")}
    out = [
        "<!doctype html>",
        '<html lang="zh-Hant"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>DWG 圖面差異比對報告</title>",
        f"<style>{_CSS}</style></head><body>",
        "<h1>DWG 圖面差異比對報告</h1>",
        '<div class="meta">',
        f"產生時間：{_e(stamp)}<br>舊版資料夾：{_e(old_dir)}<br>新版資料夾：{_e(new_dir)}<br>",
        f"配對方式：{'handle 為主，座標+類型為輔' if use_handle else '只用座標+類型'}　"
        f"座標容許誤差：{_e(tolerance)}<br>",
        f"完整變更清單：{_e(changes_csv)}",
        "</div>",
        f"<p>新增 {totals['added']}　刪除 {totals['deleted']}　修改 {totals['modified']}　"
        f"未變更 {totals['unchanged']}</p>",
        "<table><thead><tr><th>比對檔案</th><th>狀態</th><th>新增</th><th>刪除</th><th>修改</th>"
        "<th>未變更</th><th>錯誤</th></tr></thead><tbody>",
    ]
    for rec in results:
        status = rec.get("status", "")
        out.append(
            f"<tr><td>{_e(rec.get('name'))}</td>"
            f'<td><span class="tag {_e(status)}">{_e(STATUS_TEXT.get(status, status))}</span></td>'
            f'<td class="n">{int(rec.get("added") or 0)}</td><td class="n">{int(rec.get("deleted") or 0)}</td>'
            f'<td class="n">{int(rec.get("modified") or 0)}</td><td class="n">{int(rec.get("unchanged") or 0)}</td>'
            f"<td>{_e(rec.get('error'))}</td></tr>"
        )
    out.append("</tbody></table>")
    for rec in results:
        rows = rec.get("change_rows") or []
        if not rows:
            continue
        out.append(f"<details><summary>{_e(rec.get('name'))}（{len(rows)} 筆變更）</summary>")
        out.append(f'<div class="note">舊版：{_e(rec.get("old"))}<br>新版：{_e(rec.get("new"))}</div>')
        out.append(
            "<table><thead><tr><th>變更類型</th><th>比對方式</th><th>舊 handle</th><th>新 handle</th>"
            "<th>物件類型</th><th>配置</th><th>圖層</th><th>X</th><th>Y</th><th>變更屬性</th>"
            "<th>舊值</th><th>新值</th></tr></thead><tbody>"
        )
        for item in rows[:HTML_ROW_LIMIT]:
            kind = item["change"]
            out.append(
                f'<tr><td><span class="tag {_e(kind)}">{_e(CHANGE_LABELS.get(kind, kind))}</span></td>'
                f"<td>{_e(MATCH_LABELS.get(item['match'], item['match']))}</td>"
                f"<td>{_e(item['old_handle'])}</td><td>{_e(item['new_handle'])}</td>"
                f"<td>{_e(item['kind'])}</td><td>{_e(item['layout'])}</td><td>{_e(item['layer'])}</td>"
                f"<td>{_e(item['x'])}</td><td>{_e(item['y'])}</td><td>{_e(item['property'])}</td>"
                f"<td>{_e(item['old'])}</td><td>{_e(item['new'])}</td></tr>"
            )
        out.append("</tbody></table>")
        if len(rows) > HTML_ROW_LIMIT:
            out.append(f'<div class="note">只顯示前 {HTML_ROW_LIMIT} 筆，其餘 {len(rows) - HTML_ROW_LIMIT} 筆見 CSV。</div>')
        out.append("</details>")
    out.append("</body></html>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# Batch
# ---------------------------------------------------------------------------

def _empty_summary():
    return {
        "dwg": 0,
        "ok": 0,
        "failed": 0,
        "skipped": 0,
        "warnings": 0,
        "added": 0,
        "deleted": 0,
        "modified": 0,
        "unchanged": 0,
        "changed_files": 0,
        "unpaired": [],
        "stopped": False,
        "fatal": "",
        "failed_files": [],
        "elapsed": 0.0,
        "results": [],
    }


def _blank_rec(name, old, new, status=""):
    return {
        "name": name,
        "old": "" if old is None else str(old),
        "new": "" if new is None else str(new),
        "status": status,
        "added": 0,
        "deleted": 0,
        "modified": 0,
        "unchanged": 0,
        "by_handle": 0,
        "by_position": 0,
        "error": "",
        "elapsed_seconds": 0.0,
        "change_rows": [],
    }


def _read_dump(job, ready):
    """Rows for one temp DWG, or ``(None, error)``; ``"STOPPED"`` when it never ran."""
    if job.get("launch_error"):
        return None, job["launch_error"]
    if not job.get("console"):
        return None, "STOPPED" if job.get("stopped_here") else ""
    opened = job.get("opened_name") or ""
    expected = Path(job["spath"]).name
    if opened and opened.casefold() != expected.casefold():
        return None, f"開啟的圖面與預期不同（{opened}）"
    if not ready:
        return None, "差異比對 LISP 沒有載入（見 logs 裡的 stdout）"
    parsed = parse_dump(job["console"])
    if parsed["errors"]:
        return None, "；".join(parsed["errors"])
    if parsed["summary"] is None:
        return None, "沒有收到圖元清單"
    if parsed["summary"] != len(parsed["rows"]):
        return None, f"圖元清單不完整（應有 {parsed['summary']} 筆，收到 {len(parsed['rows'])} 筆）"
    return parsed["rows"], ""


def run(
    old_dir,
    new_dir,
    output_dir,
    accore,
    *,
    recursive=True,
    tolerance=DEFAULT_TOLERANCE,
    use_handle=True,
    timeout=300,
    stop_event=None,
    progress=None,
    log=None,
):
    """Compare every DWG pair. Writes only under ``output_dir/logs``.

    Outputs: ``dwgdiff_changes.csv`` (one row per change), ``dwgdiff_results.csv``
    and ``.jsonl`` (one row per pair), ``dwgdiff_report.html`` and ``dwgdiff.log``.
    """
    log = log or (lambda level, message: None)
    progress = progress or (lambda index, total, path: None)
    started = time.time()
    summary = _empty_summary()
    problem = folder_problem(old_dir, new_dir, output_dir)
    if problem:
        summary["fatal"] = problem
        return summary
    try:
        tolerance = abs(float(tolerance))
    except (TypeError, ValueError):
        summary["fatal"] = "座標容許誤差必須是數字"
        return summary
    if not math.isfinite(tolerance):
        summary["fatal"] = "座標容許誤差必須是數字"
        return summary
    if not str(accore or "").strip() or not Path(accore).is_file():
        summary["fatal"] = "找不到 accoreconsole.exe"
        return summary
    dst_root = Path(output_dir)
    logs = dst_root / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        text_log = (logs / "dwgdiff.log").open("w", encoding="utf-8")
        result_file = (logs / "dwgdiff_results.csv").open("w", newline="", encoding="utf-8-sig")
        jsonl_file = (logs / "dwgdiff_results.jsonl").open("w", encoding="utf-8")
        change_file = (logs / "dwgdiff_changes.csv").open("w", newline="", encoding="utf-8-sig")
    except OSError as exc:
        summary["fatal"] = f"輸出資料夾無法寫入：{exc}"
        return summary
    result_writer = csv.DictWriter(result_file, RESULT_FIELDS)
    result_writer.writeheader()
    change_writer = csv.DictWriter(change_file, CHANGE_FIELDS)
    change_writer.writeheader()

    pairs, only_old, only_new = pair_files(old_dir, new_dir, recursive)
    summary["dwg"] = len(pairs)
    total_files = len(pairs) * 2
    _log_line(text_log, f"差異比對開始: {old_dir} vs {new_dir}，配對 {len(pairs)} 組，"
                        f"只有舊版 {len(only_old)}，只有新版 {len(only_new)}")
    temps = []
    started_files = 0

    def emit(rec):
        summary["results"].append(rec)
        result_writer.writerow(result_row(rec))
        result_file.flush()
        jsonl_file.write(json.dumps(rec, ensure_ascii=False) + "\n")
        jsonl_file.flush()
        for item in rec.get("change_rows") or []:
            change_writer.writerow(change_row(rec, item))
        change_file.flush()
        name = rec["name"]
        status = rec["status"]
        if status in {"ONLY_OLD", "ONLY_NEW"}:
            summary["skipped"] += 1
            summary["warnings"] += 1
            summary["unpaired"].append(f"{name}（{STATUS_TEXT[status]}）")
            _log_line(text_log, f"未配對 {name}（{STATUS_TEXT[status]}）")
            log("WARN", f"未配對 {name}（{STATUS_TEXT[status]}）")
        elif status == "FAILED":
            summary["failed"] += 1
            summary["failed_files"].append(f"{name}: {rec.get('error', '')}".strip())
            _log_line(text_log, f"失敗 {name}: {rec.get('error', '')}")
            log("ERROR", f"失敗 {name}: {rec.get('error', '')}")
            log_error({"output_dir": str(dst_root)}, f"dwgdiff {name} -> {rec.get('error', '')}")
        elif status == "STOPPED":
            summary["stopped"] = True
            _log_line(text_log, f"已停止於 {name}")
            log("WARN", f"已停止於 {name}")
        else:
            summary["ok"] += 1
            for key in ("added", "deleted", "modified", "unchanged"):
                summary[key] += int(rec.get(key) or 0)
            if status == "DIFF":
                summary["changed_files"] += 1
            note = (f"{STATUS_TEXT[status]} {name}（新增 {rec['added']}，刪除 {rec['deleted']}，"
                    f"修改 {rec['modified']}）")
            _log_line(text_log, note)
            log("OK", note)

    try:
        jobs = []
        pair_jobs = []
        for name, old_path, new_path in pairs:
            rec = _blank_rec(name, old_path, new_path)
            sides = []
            for side, src in (("old", old_path), ("new", new_path)):
                job = {"rec": rec, "src": src, "side": side}
                try:
                    job["sha_before"] = sha256(src)
                except OSError as exc:
                    rec["status"] = "FAILED"
                    rec["error"] = f"無法讀取來源（檔案可能被獨占鎖定）：{exc}"
                sides.append(job)
            pair_jobs.append((rec, sides))
        todo = [job for rec, sides in pair_jobs if not rec["status"] for job in sides]
        lisp_path = None
        if todo:
            tmpdir = safe_tmpdir(logs)
            lisp_path = tmpdir / "dwgdiff_dump.lsp"
            lisp_path.write_bytes(fontfix.lisp_bytes(build_dump_lisp()))
            for job in todo:
                if job["rec"]["status"]:
                    continue
                token = uuid.uuid4().hex[:10]
                tmp_in = tmpdir / f"{job['side']}_{token}.dwg"
                try:
                    _copy_bytes(job["src"], tmp_in)
                except OSError as exc:
                    job["rec"]["status"] = "FAILED"
                    job["rec"]["error"] = f"無法讀取來源（檔案可能被獨占鎖定）：{exc}"
                    continue
                temps.append(tmp_in)
                job["spath"] = str(tmp_in).replace("\\", "/")
                jobs.append(job)
            jobs = [job for job in jobs if not job["rec"]["status"]]

        def on_start(job):
            nonlocal started_files
            started_files += 1
            progress(min(total_files, started_files), total_files, str(job["src"]))

        stopped = False
        if jobs:
            stopped = _launch_all(
                jobs, accore, lisp_path,
                lambda chunk: build_dump_script(chunk, lisp_path),
                timeout, stop_event, logs, "dwgdiff", on_start,
            )
        if stopped:
            summary["stopped"] = True

        for rec, sides in pair_jobs:
            if not rec["status"]:
                dumps = {}
                errors = []
                never_ran = False
                for job in sides:
                    ready = "DIFFDUMP_READY" in (job.get("stdout_ready") or "")
                    rows, err = _read_dump(job, ready)
                    if rows is None:
                        label = "舊版" if job["side"] == "old" else "新版"
                        if err in {"STOPPED", ""}:
                            never_ran = True
                        else:
                            errors.append(f"{label}：{err}")
                    dumps[job["side"]] = rows
                    rec["elapsed_seconds"] = round(rec["elapsed_seconds"] + (job.get("elapsed") or 0.0), 2)
                if errors:
                    rec["status"] = "FAILED"
                    rec["error"] = "；".join(errors)
                elif never_ran:
                    if summary["stopped"]:
                        rec["status"] = "STOPPED"
                        rec["error"] = "已停止"
                    else:
                        rec["status"] = "FAILED"
                        rec["error"] = "沒有收到圖元清單"
                else:
                    result = compare_rows(dumps["old"], dumps["new"], tolerance, use_handle)
                    for key in ("added", "deleted", "modified", "unchanged", "by_handle", "by_position"):
                        rec[key] = result[key]
                    rec["change_rows"] = result["changes"]
                    rec["status"] = "DIFF" if result["changes"] else "SAME"
            for job in sides:
                if job.get("sha_before") is None:
                    continue
                try:
                    if sha256(job["src"]) != job["sha_before"]:
                        rec["status"] = "FAILED"
                        rec["error"] = ((rec.get("error") or "") + " 來源 DWG 的內容在處理中被改變").strip()
                except OSError as exc:
                    rec["error"] = ((rec.get("error") or "") + f" 無法再讀取來源：{exc}").strip()
            emit(rec)
        if not summary["stopped"]:
            for name, path in only_old:
                emit(_blank_rec(name, path, None, "ONLY_OLD"))
            for name, path in only_new:
                emit(_blank_rec(name, None, path, "ONLY_NEW"))
        report = logs / "dwgdiff_report.html"
        report.write_text(
            build_html(summary["results"], old_dir, new_dir, tolerance, use_handle,
                       logs / "dwgdiff_changes.csv"),
            encoding="utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        summary["fatal"] = f"{type(exc).__name__}: {exc}"
        log_error({"output_dir": str(dst_root)}, f"dwgdiff crashed: {summary['fatal']}")
        log("ERROR", summary["fatal"])
    finally:
        end = "批次結束" if not summary["stopped"] else "批次結束（已停止）"
        if summary["fatal"]:
            end = f"批次結束（{summary['fatal']}）"
        try:
            _log_line(text_log, end)
            text_log.close()
            result_file.close()
            jsonl_file.close()
            change_file.close()
        except Exception:  # noqa: BLE001
            pass
        for temp in temps:
            try:
                Path(temp).unlink(missing_ok=True)
            except OSError:
                pass
        summary["elapsed"] = round(time.time() - started, 2)
    return summary


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description="比對新舊兩版 DWG 的圖元差異（兩個資料夾依檔名配對）")
    parser.add_argument("old_dir")
    parser.add_argument("new_dir")
    parser.add_argument("output_dir")
    parser.add_argument("--accore", default="")
    parser.add_argument("--tolerance", type=float, default=DEFAULT_TOLERANCE, help="座標與尺寸容許誤差")
    parser.add_argument("--no-handle", action="store_true", help="不用 handle 配對，只用座標+類型")
    parser.add_argument("--no-recursive", action="store_true")
    args = parser.parse_args(argv)
    from common import load_config
    cfg = load_config()
    accore = args.accore or cfg.get("accoreconsole", "")
    summary = run(
        args.old_dir, args.new_dir, args.output_dir, accore,
        recursive=not args.no_recursive, tolerance=args.tolerance,
        use_handle=not args.no_handle, timeout=int(cfg.get("timeout_seconds", 300)),
        log=lambda level, message: print(f"[{level}] {message}"),
    )
    print(
        f"配對 {summary['dwg']}  成功 {summary['ok']}  失敗 {summary['failed']}"
        f"  未配對 {summary['skipped']}  新增 {summary['added']}  刪除 {summary['deleted']}"
        f"  修改 {summary['modified']}"
    )
    if summary["fatal"]:
        print(f"錯誤：{summary['fatal']}")
    return 0 if not summary["fatal"] and not summary["failed"] and not summary["stopped"] else 1


if __name__ == "__main__":
    sys.exit(main())

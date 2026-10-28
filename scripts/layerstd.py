"""Check DWG/DXF layers against a layer standard and write CSV + HTML reports.

The standard is a CSV (or JSON) file. Each CSV row has a ``kind``:

* ``exact`` / ``wildcard`` / ``regex``: a layer rule. ``pattern`` is matched
  against the layer name (case-insensitive, like AutoCAD). ``color``,
  ``linetype``, ``lineweight`` and ``plot`` are optional; a blank cell means
  "any". ``required=yes`` reports MISSING_REQUIRED_LAYER when no layer
  matches. The first matching rule wins.
* ``forbidden``: a layer matching ``pattern`` (wildcard) must not exist.
* ``setting``: a global option; ``pattern`` is the option name and ``value``
  its value (see ``DEFAULT_SETTINGS``).

Allowed values are separated by ``|`` or ``;``. Colors accept ranges
(``1-9``). Lineweights accept mm (``0.25``), hundredths (``25``) or
``default``. JSON uses ``{"settings": {...}, "rules": [{...}, ...]}`` with the
same keys as the CSV columns.

DWG files are converted with ``convert_batch.convert_many``; DXF files are
read directly. The checks themselves are pure functions on an ezdxf document.
"""
from __future__ import annotations

import csv
import datetime
import fnmatch
import html
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import ezdxf

STANDARD_FIELDS = ["kind", "pattern", "value", "color", "linetype", "lineweight", "plot",
                   "required", "description"]
DETAIL_FIELDS = ["file", "layer", "rule", "severity", "expected", "actual"]
SUMMARY_FIELDS = ["file", "status", "layers", "entities", "errors", "warnings", "violations",
                  "compliant_layers", "compliance_rate", "error"]

REPORT_DIR = "layer_check"
DETAILS_NAME = "layer_check_details.csv"
SUMMARY_NAME = "layer_check_summary.csv"
HTML_NAME = "layer_check_report.html"

ERROR = "error"
WARNING = "warning"
_LEVELS = ("error", "warning", "off")

RULE_KINDS = ("exact", "wildcard", "regex")
SYSTEM_LAYERS = ("0", "defpoints")

DEFAULT_SETTINGS = {
    "name_regex": "",            # every non-system layer name must fullmatch this (case-sensitive)
    "unlisted_layers": WARNING,  # layer matching no rule -> NAME_NONSTANDARD
    "layer0_entities": ERROR,    # drawing entities (not block content) on layer 0
    "empty_layers": WARNING,     # layer with no entity anywhere
}

RULE_LABELS = {
    "NAME_NONSTANDARD": "圖層名稱不符標準",
    "COLOR_MISMATCH": "顏色不符",
    "LINETYPE_MISMATCH": "線型不符",
    "LINEWEIGHT_MISMATCH": "線寬不符",
    "PLOT_MISMATCH": "列印設定不符",
    "ENTITIES_ON_LAYER_0": "圖層 0 上有圖元",
    "EMPTY_LAYER": "空圖層",
    "MISSING_REQUIRED_LAYER": "缺少必要圖層",
    "LAYER_FORBIDDEN": "禁止使用的圖層",
}

SAMPLE_STANDARD = """\
kind,pattern,value,color,linetype,lineweight,plot,required,description
setting,name_regex,"^[A-Z0-9][A-Z0-9_\\-]*$",,,,,,圖層名稱只用大寫英數、底線、減號
setting,unlisted_layers,warning,,,,,,不在標準內的圖層
setting,layer0_entities,error,,,,,,圖層 0 上不得有圖元（圖塊定義內除外）
setting,empty_layers,warning,,,,,,沒有任何圖元的圖層
exact,0,,7,Continuous,,,,
exact,Defpoints,,,,,no,,
wildcard,A-WALL*,,1|7,Continuous,0.35|0.50,yes,yes,建築牆
wildcard,A-DOOR*,,3,Continuous,0.25,yes,,門
wildcard,E-CABLE*,,4|5,Continuous|DASHED,0.25,yes,,電纜
wildcard,*-DIM,,2,Continuous,0.18,yes,,標註
wildcard,*-TEXT,,7,Continuous,0.18|0.25,yes,,文字
regex,^S-COL-\\d{2}$,,1-9,Continuous,default,yes,,結構柱（S-COL-01 等）
wildcard,X-NOPLOT*,,8,,,no,,參考用、不列印
forbidden,TEMP*,,,,,,,暫存圖層不得留在圖面
"""


@dataclass
class Rule:
    kind: str
    pattern: str
    colors: set | None = None
    linetypes: set | None = None
    lineweights: set | None = None
    plot: bool | None = None
    required: bool = False
    description: str = ""
    raw: dict = field(default_factory=dict)
    regex: re.Pattern | None = None

    def matches(self, name):
        if self.kind == "exact":
            return name.casefold() == self.pattern.casefold()
        if self.kind == "regex":
            return self.regex.fullmatch(name) is not None
        return fnmatch.fnmatchcase(name.casefold(), self.pattern.casefold())


@dataclass
class Standard:
    rules: list = field(default_factory=list)
    forbidden: list = field(default_factory=list)
    settings: dict = field(default_factory=lambda: dict(DEFAULT_SETTINGS))
    name_regex: re.Pattern | None = None
    notes: list = field(default_factory=list)


# ---------- value parsing ----------
def _split(text):
    return [part.strip() for part in re.split(r"[|;]", str(text or "")) if part.strip()]


def parse_bool(text):
    """``yes/no`` style cell -> True/False; blank -> None. Raises ValueError otherwise."""
    value = str(text or "").strip().casefold()
    if value == "":
        return None
    if value in ("y", "yes", "true", "1", "是", "on"):
        return True
    if value in ("n", "no", "false", "0", "否", "off"):
        return False
    raise ValueError(f"無法辨識的是/否值：{text}")


def parse_colors(text):
    """``1|7|10-12`` -> {1, 7, 10, 11, 12}; blank -> None."""
    parts = _split(text)
    if not parts:
        return None
    colors = set()
    for part in parts:
        if "-" in part:
            low, high = part.split("-", 1)
            colors.update(range(int(low), int(high) + 1))
        else:
            colors.add(int(part))
    bad = [c for c in colors if not 1 <= c <= 255]
    if bad:
        raise ValueError(f"顏色必須是 ACI 1-255：{text}")
    return colors


def parse_lineweight(token):
    """``0.25`` -> 25, ``25`` -> 25, ``default`` -> -3 (ezdxf/DXF hundredths of mm)."""
    value = str(token).strip().casefold()
    if value in ("default", "預設"):
        return -3
    if value in ("bylayer",):
        return -1
    if value in ("byblock",):
        return -2
    if "." in value:
        return int(round(float(value) * 100))
    return int(value)


def parse_lineweights(text):
    parts = _split(text)
    return {parse_lineweight(p) for p in parts} if parts else None


def format_lineweight(value):
    special = {-3: "default", -2: "byblock", -1: "bylayer"}
    if value in special:
        return special[value]
    return f"{value / 100:.2f}"


def _level(value, name):
    level = str(value or "").strip().casefold()
    if level not in _LEVELS:
        raise ValueError(f"{name} 只能是 error / warning / off：{value}")
    return level


# ---------- loading ----------
def build_standard(rows, settings=None):
    """Build a ``Standard`` from row dicts (CSV columns). Raises ValueError with row info."""
    std = Standard()
    for key, value in (settings or {}).items():
        _apply_setting(std, key, value)
    for lineno, row in enumerate(rows, start=2):
        row = {str(k or "").strip().casefold(): ("" if v is None else str(v).strip())
               for k, v in row.items()}
        kind = row.get("kind", "").casefold()
        pattern = row.get("pattern", "")
        if not kind and not pattern:
            continue
        if kind.startswith("#"):
            continue
        try:
            if kind == "setting":
                _apply_setting(std, pattern, row.get("value", ""))
            elif kind == "forbidden":
                if not pattern:
                    raise ValueError("缺少 pattern")
                std.forbidden.append(pattern)
            elif kind in RULE_KINDS:
                std.rules.append(_make_rule(kind, pattern, row))
            else:
                raise ValueError(f"不支援的 kind：{kind or '(空白)'}")
        except (ValueError, re.error) as exc:
            raise ValueError(f"標準檔第 {lineno} 列：{exc}") from exc
    return std


def _apply_setting(std, key, value):
    key = str(key or "").strip().casefold()
    if key == "name_regex":
        std.settings[key] = str(value or "")
        std.name_regex = re.compile(str(value)) if str(value or "") else None
    elif key in ("unlisted_layers", "layer0_entities", "empty_layers"):
        std.settings[key] = _level(value, key)
    else:
        raise ValueError(f"不支援的設定：{key}")


def _make_rule(kind, pattern, row):
    if not pattern:
        raise ValueError("缺少 pattern")
    rule = Rule(
        kind=kind,
        pattern=pattern,
        colors=parse_colors(row.get("color")),
        linetypes={t.casefold() for t in _split(row.get("linetype"))} or None,
        lineweights=parse_lineweights(row.get("lineweight")),
        plot=parse_bool(row.get("plot")),
        required=bool(parse_bool(row.get("required"))),
        description=row.get("description", ""),
        raw=dict(row),
    )
    if kind == "regex":
        rule.regex = re.compile(pattern, re.IGNORECASE)
    return rule


def load_standard(path):
    """Load a ``.csv`` or ``.json`` standard file. Raises ValueError / OSError."""
    path = Path(path)
    if path.suffix.casefold() == ".json":
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError("JSON 標準檔必須是物件")
        rows = []
        for item in data.get("rules", []):
            row = {k: _json_cell(v) for k, v in item.items()}
            row.setdefault("kind", "wildcard")
            rows.append(row)
        return build_standard(rows, data.get("settings") or {})
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        missing = {"kind", "pattern"} - {str(f or "").strip().casefold() for f in reader.fieldnames or []}
        if missing:
            raise ValueError(f"標準檔缺少欄位：{', '.join(sorted(missing))}")
        return build_standard(list(reader))


def _json_cell(value):
    if isinstance(value, list):
        return "|".join(str(v) for v in value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return "" if value is None else str(value)


def write_sample(path):
    Path(path).write_text(SAMPLE_STANDARD, encoding="utf-8-sig")
    return Path(path)


# ---------- reading a drawing ----------
def read_layers(doc):
    """Return a list of layer dicts with appearance and entity counts.

    ``layout_count`` counts model/paper space entities; ``block_count`` counts
    entities inside block definitions.
    """
    layout_counts, block_counts = {}, {}
    for block in doc.blocks:
        target = layout_counts if block.is_any_layout else block_counts
        for entity in block:
            key = entity.dxf.get("layer", "0").casefold()
            target[key] = target.get(key, 0) + 1
    layers = []
    for layer in doc.layers:
        name = layer.dxf.name
        key = name.casefold()
        layers.append(dict(
            name=name,
            color=abs(int(layer.dxf.get("color", 7))),
            linetype=layer.dxf.get("linetype", "Continuous"),
            lineweight=int(layer.dxf.get("lineweight", -3)),
            plot=bool(layer.dxf.get("plot", 1)),
            layout_count=layout_counts.get(key, 0),
            block_count=block_counts.get(key, 0),
        ))
    return layers


# ---------- checking ----------
def _violation(layer, rule, severity, expected, actual):
    return dict(layer=layer, rule=rule, severity=severity, expected=str(expected), actual=str(actual))


def _expected(values, fmt=str):
    return " | ".join(fmt(v) for v in sorted(values))


def _check_attributes(info, rule):
    found = []
    if rule.colors is not None and info["color"] not in rule.colors:
        found.append(_violation(info["name"], "COLOR_MISMATCH", ERROR,
                                rule.raw.get("color", ""), info["color"]))
    if rule.linetypes is not None and info["linetype"].casefold() not in rule.linetypes:
        found.append(_violation(info["name"], "LINETYPE_MISMATCH", ERROR,
                                rule.raw.get("linetype", ""), info["linetype"]))
    if rule.lineweights is not None and info["lineweight"] not in rule.lineweights:
        found.append(_violation(info["name"], "LINEWEIGHT_MISMATCH", ERROR,
                                _expected(rule.lineweights, format_lineweight),
                                format_lineweight(info["lineweight"])))
    if rule.plot is not None and info["plot"] != rule.plot:
        found.append(_violation(info["name"], "PLOT_MISMATCH", ERROR,
                                "yes" if rule.plot else "no", "yes" if info["plot"] else "no"))
    return found


def check_layers(layers, std):
    """Pure check of layer dicts against a ``Standard``. Returns violation dicts."""
    found = []
    settings = std.settings
    for info in layers:
        name = info["name"]
        system = name.casefold() in SYSTEM_LAYERS
        if any(fnmatch.fnmatchcase(name.casefold(), p.casefold()) for p in std.forbidden):
            found.append(_violation(name, "LAYER_FORBIDDEN", ERROR, "不得存在", name))
        if not system and std.name_regex is not None and not std.name_regex.fullmatch(name):
            found.append(_violation(name, "NAME_NONSTANDARD", ERROR,
                                    f"regex {settings['name_regex']}", name))
        rule = next((r for r in std.rules if r.matches(name)), None)
        if rule is not None:
            found.extend(_check_attributes(info, rule))
        elif not system and settings["unlisted_layers"] != "off":
            found.append(_violation(name, "NAME_NONSTANDARD", settings["unlisted_layers"],
                                    "符合標準檔任一規則", name))
        if name == "0" and info["layout_count"] and settings["layer0_entities"] != "off":
            found.append(_violation(name, "ENTITIES_ON_LAYER_0", settings["layer0_entities"],
                                    0, info["layout_count"]))
        if (not system and not info["layout_count"] and not info["block_count"]
                and settings["empty_layers"] != "off"):
            found.append(_violation(name, "EMPTY_LAYER", settings["empty_layers"], "> 0", 0))
    for rule in std.rules:
        if rule.required and not any(rule.matches(info["name"]) for info in layers):
            found.append(_violation(rule.pattern, "MISSING_REQUIRED_LAYER", ERROR, "存在", "缺少"))
    return found


def check_doc(doc, std):
    """Return ``(layers, violations)`` for an ezdxf document."""
    layers = read_layers(doc)
    return layers, check_layers(layers, std)


def summarize(file, layers, violations, status="OK", error=""):
    """One summary row. A layer is compliant when it has no error-level violation;
    missing required layers count as non-compliant layers."""
    bad = {v["layer"].casefold() for v in violations
           if v["severity"] == ERROR and v["rule"] != "MISSING_REQUIRED_LAYER"}
    missing = sum(1 for v in violations if v["rule"] == "MISSING_REQUIRED_LAYER")
    total = len(layers) + missing
    compliant = sum(1 for info in layers if info["name"].casefold() not in bad)
    rate = round(100.0 * compliant / total, 1) if total else 0.0
    return dict(
        file=str(file), status=status, layers=len(layers),
        entities=sum(i["layout_count"] + i["block_count"] for i in layers),
        errors=sum(1 for v in violations if v["severity"] == ERROR),
        warnings=sum(1 for v in violations if v["severity"] == WARNING),
        violations=len(violations), compliant_layers=compliant,
        compliance_rate=rate if status == "OK" else "", error=error,
    )


def check_file(path, std):
    """Read a DXF and check it. Returns ``(summary_row, detail_rows)``; never raises for bad files."""
    try:
        doc = ezdxf.readfile(str(path))
    except (OSError, ezdxf.DXFError) as exc:
        return summarize(path, [], [], status="FAILED", error=f"{type(exc).__name__}: {exc}"), []
    layers, violations = check_doc(doc, std)
    return summarize(path, layers, violations), [dict(file=str(path), **v) for v in violations]


# ---------- output ----------
def write_csv(path, fields, rows):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    return path


_CSS = """
body{font-family:"Segoe UI","Microsoft JhengHei",sans-serif;margin:24px;color:#1f2328;background:#fff}
h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:28px 0 8px}
.meta{color:#57606a;font-size:13px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
.card{border:1px solid #d0d7de;border-radius:6px;padding:10px 14px;min-width:120px}
.card b{display:block;font-size:22px}
table{border-collapse:collapse;width:100%;font-size:13px;margin-bottom:12px}
th,td{border:1px solid #d0d7de;padding:4px 8px;text-align:left;vertical-align:top}
th{background:#f6f8fa}
.error{color:#a40e26;font-weight:600}.warning{color:#7d4e00}
.ok{color:#1a7f37}.failed{color:#a40e26}
.bar{background:#eaeef2;border-radius:3px;height:8px;width:120px;display:inline-block;vertical-align:middle}
.bar i{display:block;height:8px;border-radius:3px;background:#2da44e}
"""


def render_html(summaries, details, standard_path="", generated=None):
    """Single-file HTML report with inline CSS and no external resources."""
    esc = html.escape
    generated = generated or datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    checked = [s for s in summaries if s["status"] == "OK"]
    errors = sum(int(s["errors"]) for s in summaries)
    warnings = sum(int(s["warnings"]) for s in summaries)
    rates = [float(s["compliance_rate"]) for s in checked]
    avg = f"{sum(rates) / len(rates):.1f}%" if rates else "—"
    out = [
        "<!doctype html><html lang=\"zh-Hant\"><head><meta charset=\"utf-8\">",
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>圖層標準檢查報告</title><style>{_CSS}</style></head><body>",
        "<h1>圖層標準檢查報告</h1>",
        f"<div class=\"meta\">產生時間：{esc(generated)}　標準檔：{esc(str(standard_path))}</div>",
        "<div class=\"cards\">",
        f"<div class=\"card\"><b>{len(summaries)}</b>檔案</div>",
        f"<div class=\"card\"><b>{len(summaries) - len(checked)}</b>無法檢查</div>",
        f"<div class=\"card\"><b class=\"error\">{errors}</b>錯誤</div>",
        f"<div class=\"card\"><b class=\"warning\">{warnings}</b>警告</div>",
        f"<div class=\"card\"><b>{avg}</b>平均合規率</div>",
        "</div><h2>各檔摘要</h2><table><tr><th>檔案</th><th>狀態</th><th>圖層</th>"
        "<th>錯誤</th><th>警告</th><th>合規率</th><th>說明</th></tr>",
    ]
    for s in summaries:
        if s["status"] == "OK":
            rate = float(s["compliance_rate"])
            rate_cell = (f"<span class=\"bar\"><i style=\"width:{rate:.0f}%\"></i></span> {rate:.1f}%")
            status = "<span class=\"ok\">已檢查</span>"
        else:
            rate_cell = "—"
            status = f"<span class=\"failed\">{esc(s['status'])}</span>"
        out.append(f"<tr><td>{esc(Path(s['file']).name)}</td><td>{status}</td><td>{s['layers']}</td>"
                   f"<td>{s['errors']}</td><td>{s['warnings']}</td><td>{rate_cell}</td>"
                   f"<td>{esc(s['error'])}</td></tr>")
    out.append("</table>")
    by_rule = {}
    for d in details:
        by_rule[d["rule"]] = by_rule.get(d["rule"], 0) + 1
    if by_rule:
        out.append("<h2>違規類型統計</h2><table><tr><th>規則</th><th>說明</th><th>數量</th></tr>")
        for rule, count in sorted(by_rule.items(), key=lambda kv: (-kv[1], kv[0])):
            out.append(f"<tr><td>{esc(rule)}</td><td>{esc(RULE_LABELS.get(rule, ''))}</td>"
                       f"<td>{count}</td></tr>")
        out.append("</table>")
    out.append("<h2>違規明細</h2>")
    if not details:
        out.append("<p class=\"ok\">沒有違規。</p>")
    for file in dict.fromkeys(d["file"] for d in details):
        out.append(f"<h3>{esc(Path(file).name)}</h3><table><tr><th>圖層</th><th>規則</th>"
                   "<th>嚴重度</th><th>標準</th><th>實際</th></tr>")
        for d in (d for d in details if d["file"] == file):
            sev = d["severity"]
            out.append(f"<tr><td>{esc(d['layer'])}</td><td>{esc(d['rule'])}</td>"
                       f"<td class=\"{esc(sev)}\">{'錯誤' if sev == ERROR else '警告'}</td>"
                       f"<td>{esc(d['expected'])}</td><td>{esc(d['actual'])}</td></tr>")
        out.append("</table>")
    out.append("</body></html>")
    return "\n".join(out)


def write_reports(out_dir, summaries, details, standard_path=""):
    """Write the details CSV, summary CSV and HTML report. Returns their paths."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = dict(
        details=write_csv(out_dir / DETAILS_NAME, DETAIL_FIELDS, details),
        summary=write_csv(out_dir / SUMMARY_NAME, SUMMARY_FIELDS, summaries),
        html=out_dir / HTML_NAME,
    )
    paths["html"].write_text(render_html(summaries, details, standard_path), encoding="utf-8")
    return paths


# ---------- batch entry ----------
def scan_inputs(input_dir, recursive=True):
    root = Path(input_dir)
    pattern = "**/*" if recursive else "*"
    return sorted(p for p in root.glob(pattern)
                  if p.is_file() and p.suffix.casefold() in (".dwg", ".dxf"))


def _empty_summary():
    return dict(dwg=0, ok=0, failed=0, skipped=0, warnings=0, errors=0, violations=0,
                stopped=False, fatal="", failed_files=[], notes=[], elapsed=0.0,
                reports={}, results=[])


def _convert_dwgs(dwgs, input_dir, out_dir, accore, overwrite, timeout, stop_event, log):
    """Convert DWGs to DXF under ``out_dir/dxf``. Returns {dwg: (dxf or None, error)}."""
    import convert_batch

    root = Path(input_dir)
    items = []
    for dwg in dwgs:
        try:
            rel = dwg.relative_to(root)
        except ValueError:
            rel = Path(dwg.name)
        items.append((dwg, out_dir / "dxf" / rel.with_suffix(".dxf")))
    if not str(accore or "").strip() or not Path(accore).is_file():
        return {dwg: (None, "找不到 accoreconsole.exe，無法轉換 DWG") for dwg, _dxf in items}
    log("INFO", f"轉換 {len(items)} 個 DWG 為 DXF…")
    records = convert_batch.convert_many(items, accore, {"output_dir": str(out_dir)}, timeout=timeout,
                                         overwrite=overwrite, stop_event=stop_event)
    by_source = {str(r["source"]): r for r in records}
    result = {}
    for dwg, dxf in items:
        rec = by_source.get(str(dwg))
        if rec is None or rec["status"] == "STOPPED":
            result[dwg] = (None, "STOPPED")
        elif rec["status"] in ("DXF_VALID", "SKIPPED_EXISTS"):
            result[dwg] = (dxf, "")
        else:
            result[dwg] = (None, f"{rec['status']} {rec.get('error', '')}".strip())
    return result


def run(input_dir, output_dir, standard_path, accore="", *, recursive=True, overwrite=False,
        timeout=300, stop_event=None, progress=None, log=None):
    """Check every DWG/DXF under ``input_dir`` and write reports to ``output_dir/layer_check``.

    ``progress(index, total, path)`` and ``log(level, message)`` are optional
    callbacks. Inputs are never modified.
    """
    log = log or (lambda level, message: None)
    progress = progress or (lambda index, total, path: None)
    started = time.time()
    summary = _empty_summary()
    try:
        std = load_standard(standard_path)
    except (OSError, ValueError, re.error) as exc:
        summary["fatal"] = f"標準檔讀取失敗：{exc}"
        return summary
    if not Path(input_dir).is_dir():
        summary["fatal"] = f"輸入資料夾不存在：{input_dir}"
        return summary
    out_dir = Path(output_dir) / REPORT_DIR
    files = scan_inputs(input_dir, recursive)
    summary["dwg"] = len(files)
    dwgs = [p for p in files if p.suffix.casefold() == ".dwg"]
    converted = {}
    if dwgs and not (stop_event is not None and stop_event.is_set()):
        converted = _convert_dwgs(dwgs, input_dir, out_dir, accore, overwrite, timeout, stop_event, log)
    summaries, details = [], []
    for index, path in enumerate(files, start=1):
        if stop_event is not None and stop_event.is_set():
            summary["stopped"] = True
            log("WARN", "已停止")
            break
        progress(index, len(files), str(path))
        target, error = (path, "") if path.suffix.casefold() == ".dxf" else converted.get(path, (None, "STOPPED"))
        if target is None:
            if error == "STOPPED":
                summary["stopped"] = True
                break
            row = summarize(path, [], [], status="FAILED", error=error)
            file_details = []
        else:
            row, file_details = check_file(target, std)
            row["file"] = str(path)
            for d in file_details:
                d["file"] = str(path)
        summaries.append(row)
        details.extend(file_details)
        if row["status"] == "OK":
            summary["ok"] += 1
            summary["errors"] += row["errors"]
            summary["violations"] += row["violations"]
            log("OK" if not row["violations"] else "WARN",
                f"{path.name}：錯誤 {row['errors']}、警告 {row['warnings']}、合規率 {row['compliance_rate']}%")
        else:
            summary["failed"] += 1
            summary["failed_files"].append(f"{path}: FAILED {row['error']}")
            log("ERROR", f"{path.name}：{row['error']}")
    summary["results"] = summaries
    try:
        summary["reports"] = {k: str(v) for k, v in
                              write_reports(out_dir, summaries, details, standard_path).items()}
    except OSError as exc:
        summary["fatal"] = f"報告無法寫入：{exc}"
    summary["elapsed"] = round(time.time() - started, 2)
    return summary

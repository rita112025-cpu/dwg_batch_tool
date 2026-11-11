"""Load and match a CAD → Revit mapping file.

Patterns are Python regular expressions matched against the whole layer or
block name (``fullmatch``). The first rule in the file wins. Inline flags
such as ``(?i)`` select case-insensitive names.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from cad2revit.const import MAPPING_SCHEMA, PARAMETER_UNITS
from cad2revit.guard import MappingError
from cad2revit.hashing import file_sha256

_TOP_KEYS = {"schema", "layer_rules", "block_rules", "defaults"}
_RULE_KEYS = {"id", "pattern", "category", "family", "type", "parameters"}
_PARAMETER_KEYS = {"width", "height", "diameter", "unit"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_SIZE_KEYS = ("width", "height", "diameter")


def validate_mapping(data):
    """Return a list of Traditional Chinese errors. An empty list means valid."""
    errors = []
    if not isinstance(data, dict):
        return ["對照表的最外層必須是物件"]
    unknown = sorted(set(data) - _TOP_KEYS)
    for key in unknown:
        errors.append(f"對照表含有未知欄位：{key}")
    if data.get("schema") != MAPPING_SCHEMA:
        errors.append(f"schema 必須是 {MAPPING_SCHEMA}")
    layer_rules = data.get("layer_rules", [])
    block_rules = data.get("block_rules", [])
    if "layer_rules" in data and not isinstance(layer_rules, list):
        errors.append("layer_rules 必須是陣列")
        layer_rules = []
    if "block_rules" in data and not isinstance(block_rules, list):
        errors.append("block_rules 必須是陣列")
        block_rules = []
    if "defaults" in data and not isinstance(data.get("defaults"), dict):
        errors.append("defaults 必須是物件")
    seen_ids = set()
    for index, rule in enumerate(layer_rules):
        _check_rule(rule, f"layer_rules[{index}]", errors, seen_ids, require_family=False)
    for index, rule in enumerate(block_rules):
        _check_rule(rule, f"block_rules[{index}]", errors, seen_ids, require_family=True)
    defaults = data.get("defaults")
    if isinstance(defaults, dict):
        for name, params in defaults.items():
            label = f"defaults[{name}]"
            if not isinstance(name, str) or not name.strip():
                errors.append("defaults 的分類名稱不可空白")
                continue
            if not isinstance(params, dict):
                errors.append(f"{label} 必須是物件")
                continue
            _check_parameters(params, label, errors)
    return errors


def _check_rule(rule, label, errors, seen_ids, require_family):
    if not isinstance(rule, dict):
        errors.append(f"{label} 必須是物件")
        return
    for key in sorted(set(rule) - _RULE_KEYS):
        errors.append(f"{label} 含有未知欄位：{key}")
    rule_id = rule.get("id")
    if not isinstance(rule_id, str) or not _ID_RE.fullmatch(rule_id):
        errors.append(f"{label} 的 id 只可使用英數、底線、點與減號")
    elif rule_id in seen_ids:
        errors.append(f"{label} 的 id 重複：{rule_id}")
    else:
        seen_ids.add(rule_id)
    pattern = rule.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        errors.append(f"{label} 的 pattern 不可空白")
    elif len(pattern) > 500:
        errors.append(f"{label} 的 pattern 超過 500 字")
    else:
        try:
            re.compile(pattern)
        except re.error as exc:
            errors.append(f"{label} 的 pattern 不是合法的正規表示式：{exc}")
    category = rule.get("category")
    if not isinstance(category, str) or not category.strip():
        errors.append(f"{label} 的 category 不可空白")
    for key in ("family", "type"):
        value = rule.get(key)
        if require_family and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{label} 的 {key} 不可空白")
        elif value is not None and (not isinstance(value, str) or not value.strip()):
            errors.append(f"{label} 的 {key} 必須是非空白字串")
    if "parameters" in rule:
        params = rule.get("parameters")
        if not isinstance(params, dict):
            errors.append(f"{label} 的 parameters 必須是物件")
        else:
            _check_parameters(params, f"{label}.parameters", errors)


def _check_parameters(params, label, errors):
    for key in sorted(set(params) - _PARAMETER_KEYS):
        errors.append(f"{label} 含有未知欄位：{key}")
    has_size = False
    for key in _SIZE_KEYS:
        if key not in params:
            continue
        has_size = True
        value = params[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            errors.append(f"{label}.{key} 必須是數字")
        elif value <= 0:
            errors.append(f"{label}.{key} 必須大於 0")
    unit = params.get("unit")
    if unit is not None and unit not in PARAMETER_UNITS:
        errors.append(
            f"{label}.unit 必須是 {', '.join(PARAMETER_UNITS)} 其中之一"
        )
    if has_size and "unit" not in params:
        errors.append(f"{label} 有尺寸時必須指定 unit")


def load_mapping(path):
    """Read ``path``, validate it, and return a compiled mapping.

    The returned object keeps compiled patterns under a private key. Callers
    that write output must use ``public_mapping``, which has no absolute path.
    """
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise MappingError([f"無法讀取對照表：{exc.strerror or exc.__class__.__name__}"]) from exc
    except json.JSONDecodeError as exc:
        raise MappingError([f"對照表不是合法的 JSON：第 {exc.lineno} 行"]) from exc
    errors = validate_mapping(data)
    if errors:
        raise MappingError(errors)
    compiled = {
        "schema": data["schema"],
        "layer_rules": _compile_rules(data.get("layer_rules") or []),
        "block_rules": _compile_rules(data.get("block_rules") or []),
        "defaults": data.get("defaults") or {},
        "file_name": file_path.name,
        "sha256": file_sha256(file_path),
    }
    return compiled


def _compile_rules(rules):
    compiled = []
    for rule in rules:
        item = {
            "id": rule["id"],
            "pattern": rule["pattern"],
            "category": rule["category"].strip(),
            "family": (rule.get("family") or "").strip(),
            "type": (rule.get("type") or "").strip(),
            "parameters": dict(rule.get("parameters") or {}),
            "_regex": re.compile(rule["pattern"]),
        }
        compiled.append(item)
    return compiled


def match_rule(rules, name):
    """Return the first rule whose pattern matches the whole ``name``, or None."""
    for rule in rules:
        if rule["_regex"].fullmatch(name or ""):
            return rule
    return None


def public_mapping(mapping) -> dict:
    """Mapping identity safe to store in an output file."""
    return {
        "file_name": mapping.get("file_name") or "",
        "sha256": mapping.get("sha256") or "",
    }

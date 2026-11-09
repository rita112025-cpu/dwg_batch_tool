"""Title-block attribute read / write helpers (pure functions, no I/O).

The generated LISP lists every INSERT that carries attributes and prints one
``TBINSERT`` line per insert plus one ``TBATTR`` line per attribute. Python
filters the block names (wildcards), builds the editable CSV, and for write-back
builds a second UTF-8 LISP that ``entmod``s each changed ATTRIB by handle.

Marker fields use ``|=|`` and the same escaping as ``dwgtext``.
"""
from __future__ import annotations

import csv
import fnmatch
import io
import re

import dwgtext
import fontfix

MARKER_SEP = fontfix.MARKER_SEP

BASE_COLUMNS = ["file", "layout", "block_name", "handle"]
ATTRS_CSV = "titleblock_attrs.csv"

RESULT_FIELDS = ["檔案", "輸出", "版面", "圖框", "handle", "tag", "原值", "新值", "狀態", "錯誤"]
READ_FIELDS = ["檔案", "狀態", "圖框數", "錯誤", "秒數"]

READ_STATUS = {
    "DONE": "已讀取",
    "EMPTY": "無圖框",
    "FAILED": "失敗",
    "STOPPED": "已停止",
}
WRITE_STATUS = {
    "WRITTEN": "已寫入",
    "PREVIEW": "預覽",
    "UNCHANGED": "無變更",
    "FAILED": "失敗",
    "SKIPPED": "略過",
    "STOPPED": "已停止",
}

_SPLIT_RE = re.compile(r"[,;\n\r，；]+")
_EXCEL_NUMBER_RE = re.compile(r"^\d+[eE]\d+$")


# ---------------------------------------------------------------------------
# Block name patterns
# ---------------------------------------------------------------------------

def parse_patterns(text):
    """Split a comma / semicolon / newline separated list into block name patterns."""
    if isinstance(text, (list, tuple)):
        items = text
    else:
        items = _SPLIT_RE.split(text or "")
    return [str(item).strip() for item in items if str(item).strip()]


def validate_patterns(patterns):
    """Return a Traditional Chinese error, or None."""
    if not parse_patterns(patterns):
        return "請輸入圖框 Block 名稱（可用 * 與 ?，多個以逗號分隔）"
    return None


def name_matches(names, patterns):
    """True when any of ``names`` matches any pattern, case-insensitively."""
    for name in names:
        folded = (name or "").casefold()
        if not folded:
            continue
        for pattern in patterns:
            if fnmatch.fnmatchcase(folded, pattern.casefold()):
                return True
    return False


# ---------------------------------------------------------------------------
# Console output parsing
# ---------------------------------------------------------------------------

def _rests(text, keyword):
    for raw in (text or "").splitlines():
        rest = dwgtext._marker_rest(raw.strip(), keyword)
        if rest is not None:
            yield rest


def _errors(text):
    found = []
    for rest in _rests(text, "TB_ERROR"):
        message = dwgtext._split_fields(rest, 1)[0].strip()
        if message:
            found.append(message)
    return found


def tag_columns(attrs):
    """Return one column name per attribute. A repeated tag becomes ``TAG#2``, ``TAG#3``."""
    seen = {}
    columns = []
    for attr in attrs:
        tag = attr.get("tag") or ""
        seen[tag] = seen.get(tag, 0) + 1
        columns.append(tag if seen[tag] == 1 else f"{tag}#{seen[tag]}")
    return columns


def parse_dump(text):
    """Parse one drawing's ``TBINSERT`` / ``TBATTR`` output into insert dicts."""
    inserts = []
    by_handle = {}
    for rest in _rests(text, "TBINSERT"):
        layout, name, effname, handle = dwgtext._split_fields(rest, 4)
        if not handle:
            continue
        item = {
            "layout": layout,
            "name": name,
            "block_name": effname or name,
            "handle": handle,
            "attrs": [],
        }
        inserts.append(item)
        by_handle[handle.upper()] = item
    for rest in _rests(text, "TBATTR"):
        owner, tag, value, handle, flag = dwgtext._split_fields(rest, 5)
        item = by_handle.get(owner.upper())
        if item is None or not handle:
            continue
        item["attrs"].append({
            "tag": tag,
            "value": value,
            "handle": handle,
            "multiline": flag.strip() in {"1", "T", "t"},
        })
    for item in inserts:
        for column, attr in zip(tag_columns(item["attrs"]), item["attrs"]):
            attr["column"] = column
    summary = None
    for rest in _rests(text, "TBDUMP_SUMMARY"):
        count = dwgtext._summary_one(rest)
        if count is not None:
            summary = count
    ready = any(raw.strip() == "TBDUMP_READY" for raw in (text or "").splitlines())
    return {"inserts": inserts, "summary": summary, "errors": _errors(text), "ready": ready}


def parse_apply(text):
    summary = None
    for rest in _rests(text, "TBAPPLY_SUMMARY"):
        pair = dwgtext._summary_pair(rest)
        if pair is not None:
            summary = pair
    results = {}
    for rest in _rests(text, "TBAPPLY"):
        handle, status = dwgtext._split_fields(rest, 2)
        if handle:
            results[handle.upper()] = status
    return {"summary": summary, "results": results, "errors": _errors(text)}


def select_inserts(inserts, patterns):
    return [item for item in inserts if name_matches((item["block_name"], item["name"]), patterns)]


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------

def protect_handle(handle):
    """Keep Excel from reading a handle such as ``1E5`` as a number."""
    if _EXCEL_NUMBER_RE.match(handle or ""):
        return f'="{handle}"'
    return handle or ""


def clean_handle(value):
    text = (value or "").strip()
    if text.startswith('="') and text.endswith('"'):
        text = text[2:-1]
    return text.lstrip("'").strip()


def attr_rows(file_key, inserts):
    """Return ``(rows, columns)`` for one drawing's selected inserts."""
    rows = []
    columns = []
    for item in inserts:
        row = {
            "file": file_key,
            "layout": item["layout"],
            "block_name": item["block_name"],
            "handle": protect_handle(item["handle"]),
        }
        for attr in item["attrs"]:
            row[attr["column"]] = attr["value"]
            if attr["column"] not in columns:
                columns.append(attr["column"])
        rows.append(row)
    return rows, columns


def merge_columns(target, extra):
    for column in extra:
        if column not in target and column not in BASE_COLUMNS:
            target.append(column)
    return target


def decode_csv_bytes(blob):
    """UTF-8 (with or without BOM) first; Excel's non-UTF-8 save on Traditional Chinese Windows is cp950."""
    for encoding in ("utf-8-sig", "cp950"):
        try:
            return blob.decode(encoding)
        except UnicodeDecodeError:
            continue
    return blob.decode("utf-8-sig", errors="replace")


def read_attrs_csv(text):
    """Return ``(rows, tag_columns)``. Raise ``ValueError`` when the header is not ours."""
    reader = csv.DictReader(io.StringIO(text))
    header = [name.strip() for name in (reader.fieldnames or [])]
    missing = [name for name in BASE_COLUMNS if name not in header]
    if missing:
        raise ValueError("CSV 缺少欄位：" + "、".join(missing))
    reader.fieldnames = header
    tags = [name for name in header if name and name not in BASE_COLUMNS]
    rows = []
    for row in reader:
        if not any((value or "").strip() for key, value in row.items() if key in BASE_COLUMNS):
            continue
        row["handle"] = clean_handle(row.get("handle"))
        rows.append(row)
    return rows, tags


def _numbers_equal(left, right):
    try:
        return float(left) == float(right)
    except (TypeError, ValueError):
        return False


def plan_edits(csv_rows, inserts, tags):
    """Compare CSV rows with the drawing's current attributes.

    Returns ``(edits, records)``. ``edits`` are ``{"handle", "value"}`` for the
    LISP. ``records`` hold one entry per changed (or rejected) cell with
    ``layout``, ``block_name``, ``handle``, ``tag``, ``before``, ``after``,
    ``status`` (``""`` = to write, or ``SKIPPED`` / ``FAILED``), ``error`` and
    ``attr_handle``.
    """
    by_handle = {item["handle"].upper(): item for item in inserts}
    edits = []
    records = []
    for row in csv_rows:
        handle = row.get("handle") or ""
        item = by_handle.get(handle.upper())
        base = {
            "layout": row.get("layout") or "",
            "block_name": row.get("block_name") or "",
            "handle": handle,
        }
        if item is None:
            records.append(dict(base, tag="", before="", after="", status="FAILED",
                                error="圖面裡找不到這個圖框 handle", attr_handle=""))
            continue
        attrs = {attr["column"]: attr for attr in item["attrs"]}
        for tag in tags:
            after = row.get(tag)
            if after is None:
                continue
            attr = attrs.get(tag)
            if attr is None:
                if after.strip():
                    records.append(dict(base, tag=tag, before="", after=after, status="FAILED",
                                        error="這個圖框沒有此屬性", attr_handle=""))
                continue
            before = attr["value"]
            if after == before:
                continue
            record = dict(base, tag=tag, before=before, after=after, status="", error="",
                          attr_handle=attr["handle"])
            if attr["multiline"]:
                record.update(status="SKIPPED", error="多行屬性不支援寫回")
            elif _numbers_equal(before, after):
                record.update(status="SKIPPED", error="疑似 Excel 自動轉換數字格式，未寫入")
            else:
                edits.append({"handle": attr["handle"], "value": after})
            records.append(record)
    return edits, records


def result_row(record):
    return {
        "檔案": record.get("file", ""),
        "輸出": record.get("output", ""),
        "版面": record.get("layout", ""),
        "圖框": record.get("block_name", ""),
        "handle": record.get("handle", ""),
        "tag": record.get("tag", ""),
        "原值": record.get("before", ""),
        "新值": record.get("after", ""),
        "狀態": WRITE_STATUS.get(record.get("status", ""), record.get("status", "")),
        "錯誤": record.get("error", ""),
    }


def read_row(rec):
    return {
        "檔案": rec.get("file", ""),
        "狀態": READ_STATUS.get(rec.get("status", ""), rec.get("status", "")),
        "圖框數": rec.get("blocks", 0),
        "錯誤": rec.get("error", ""),
        "秒數": rec.get("elapsed_seconds", 0),
    }


# ---------------------------------------------------------------------------
# LISP and scripts
# ---------------------------------------------------------------------------

_LISP_HEAD = r""";;; titleblock.lsp — generated. UTF-8. Do not edit by hand.
;;; Lists attributed INSERTs, or writes ATTRIB values by handle.

(defun tb-str (v)
  (if (= (type v) 'STR) v ""))

(defun tb-esc (s / i n ch out)
  (setq s (tb-str s) i 1 n (strlen s) out "")
  (while (<= i n)
    (setq ch (substr s i 1))
    (cond
      ((= ch "\\") (setq out (strcat out "\\\\")))
      ((= ch (chr 10)) (setq out (strcat out "\\n")))
      ((= ch (chr 13)) (setq out (strcat out "\\r")))
      ((= ch (chr 9)) (setq out (strcat out "\\t")))
      ((and (<= (+ i 2) n) (= (substr s i 3) "@@SEP@@"))
        (setq out (strcat out "|\\=|"))
        (setq i (+ i 2)))
      (T (setq out (strcat out ch))))
    (setq i (1+ i)))
  out)

(defun tb-error-msg (msg)
  (princ (strcat "\nTB_ERROR@@SEP@@" (tb-esc msg) "\n"))
  (princ))

(defun tb-effname (en name / r)
  (if (and (= (substr name 1 1) "*") vl-catch-all-apply vlax-ename->vla-object)
    (progn
      (setq r (vl-catch-all-apply
                '(lambda () (vla-get-EffectiveName (vlax-ename->vla-object en)))))
      (if (= (type r) 'STR) r name))
    name))

(defun tb-emit-insert (layout name effname handle)
  (princ (strcat "\nTBINSERT@@SEP@@" (tb-esc layout) "@@SEP@@" (tb-esc name)
                 "@@SEP@@" (tb-esc effname) "@@SEP@@" (tb-esc handle) "\n"))
  (princ))

(defun tb-emit-attr (owner tag value handle flag)
  (princ (strcat "\nTBATTR@@SEP@@" (tb-esc owner) "@@SEP@@" (tb-esc tag)
                 "@@SEP@@" (tb-esc value) "@@SEP@@" (tb-esc handle) "@@SEP@@" flag "\n"))
  (princ))

(defun tb-dump-current (/ ss i n en ed layout name owner att aed olderr)
  (setq olderr *error* n 0 i 0)
  (defun *error* (msg)
    (tb-error-msg (tb-str msg))
    (princ "\nTBDUMP_SUMMARY@@SEP@@0\n")
    (setq *error* olderr)
    (princ))
  (if (and vl-load-com (not vlax-ename->vla-object)) (vl-load-com))
  (setq ss (ssget "_X" '((0 . "INSERT") (66 . 1))))
  (if ss
    (progn
      (setq n (sslength ss))
      (while (< i n)
        (setq en (ssname ss i) ed (entget en))
        (setq layout (tb-str (cdr (assoc 410 ed))))
        (if (= layout "") (setq layout "Model"))
        (setq name (tb-str (cdr (assoc 2 ed))) owner (tb-str (cdr (assoc 5 ed))))
        (tb-emit-insert layout name (tb-effname en name) owner)
        (setq att (entnext en))
        (while (and att (= (tb-str (cdr (assoc 0 (setq aed (entget att))))) "ATTRIB"))
          (tb-emit-attr owner
                        (tb-str (cdr (assoc 2 aed)))
                        (tb-str (cdr (assoc 1 aed)))
                        (tb-str (cdr (assoc 5 aed)))
                        (if (assoc 101 aed) "1" "0"))
          (setq att (entnext att)))
        (setq i (1+ i)))))
  (princ (strcat "\nTBDUMP_SUMMARY@@SEP@@" (itoa n) "\n"))
  (setq *error* olderr)
  (princ))
"""

_LISP_APPLY = r"""
(defun tb-apply-msg (handle status)
  (princ (strcat "\nTBAPPLY@@SEP@@" (tb-esc handle) "@@SEP@@" (tb-esc status) "\n"))
  (princ))

(defun tb-apply-rows (rows / row handle value en ed owner n fail)
  (setq n 0 fail 0)
  (foreach row rows
    (setq handle (nth 0 row)
          value (nth 1 row)
          en (if (= (type handle) 'STR) (handent handle) nil)
          ed (if en (entget en) nil))
    (cond
      ((not ed)
        (setq fail (1+ fail))
        (tb-apply-msg handle "missing"))
      ((/= (tb-str (cdr (assoc 0 ed))) "ATTRIB")
        (setq fail (1+ fail))
        (tb-apply-msg handle "not attrib"))
      ((entmod (subst (cons 1 value) (assoc 1 ed) ed))
        (setq owner (cdr (assoc 330 ed)))
        (if (= (type owner) 'ENAME) (entupd owner))
        (setq n (1+ n))
        (tb-apply-msg handle "ok"))
      (T
        (setq fail (1+ fail))
        (tb-apply-msg handle "entmod failed"))))
  (princ (strcat "\nTBAPPLY_SUMMARY@@SEP@@" (itoa n) "@@SEP@@" (itoa fail) "\n"))
  (princ)
  T)
"""


def _fill_lisp(body):
    text = body.replace("@@SEP@@", MARKER_SEP)
    if not fontfix._lisp_parens_balanced(text):
        raise RuntimeError("generated title-block LISP has unbalanced parentheses")
    return text


def build_dump_lisp():
    return _fill_lisp(_LISP_HEAD + '\n(princ "\\nTBDUMP_READY\\n")\n(princ)\n')


def build_apply_lisp(edits_by_index):
    """``edits_by_index[i]`` is the list of ``{"handle", "value"}`` for script file ``i``."""
    parts = [_fill_lisp(_LISP_HEAD + _LISP_APPLY)]
    for index, edits in enumerate(edits_by_index):
        rows = [
            "(list {h} {v})".format(h=dwgtext.tx_lisp_str(edit["handle"]), v=dwgtext.tx_lisp_str(edit["value"]))
            for edit in edits
        ]
        body = "nil" if not rows else "(list\n    " + "\n    ".join(rows) + ")"
        parts.append(f"(defun tb-apply-{index} () (tb-apply-rows {body}))")
    src = "\n".join(parts) + "\n"
    if not fontfix._lisp_parens_balanced(src):
        raise RuntimeError("generated title-block apply LISP has unbalanced parentheses")
    return src


def _build_script(jobs, lisp_path, call_of, save):
    lines = dwgtext._preamble(lisp_path)
    for i, job in enumerate(jobs):
        func = call_of(i, job)
        lines.append("_.OPEN")
        lines.append(f'"{job["spath"]}"')
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_OPEN_{i}|" (getvar "DWGNAME") "|\\n"))')
        lines.append(
            f"(if (and (boundp '{func}) {func}) "
            f'({func}) (princ (strcat "\\nTB_" "ERROR{MARKER_SEP}lisp not loaded\\n")))'
        )
        if save:
            lines.append("_.QSAVE")
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_DONE_{i}_\\n"))')
    lines.append("_.QUIT")
    text = "\n".join(lines) + "\n"
    fontfix.script_bytes(text)
    return text


def build_dump_script(jobs, lisp_path):
    return _build_script(jobs, lisp_path, lambda _i, _job: "tb-dump-current", False)


def build_apply_script(jobs, lisp_path):
    # tb-apply-N is numbered over the whole apply list; the OPEN/DONE markers count
    # within this launch, which restarts from the remaining files after a stall.
    return _build_script(jobs, lisp_path, lambda i, job: f"tb-apply-{job.get('apply_index', i)}", True)

"""Find and replace DWG text outside AutoLISP.

AutoLISP has no regular expressions. The generated LISP only walks TEXT, MTEXT,
ATTRIB and ATTDEF (model space, paper space, and optionally block definitions)
and prints one ``TEXTDUMP`` line per entity. Python applies contains / wildcard /
regex rules, and for replace builds a second UTF-8 LISP that ``entmod``s the new
text by handle.

Result fields use ``|=|``. accoreconsole expands a princ tab into spaces, so a
tab parser cannot be used. A run of spaces is still accepted for the captured
shape where every field is a single word.
"""
from __future__ import annotations

import difflib
import fnmatch
import re

import fontfix

MARKER_SEP = fontfix.MARKER_SEP

MATCH_MODES = (
    ("contains", "包含文字"),
    ("wildcard", "萬用字元（* 與 ?）"),
    ("regex", "正規表示式"),
)
REPLACE_MODES = (
    ("literal", "一般文字"),
    ("regex", "正規表示式"),
)
MATCH_LABELS = dict(MATCH_MODES)
REPLACE_LABELS = dict(REPLACE_MODES)

_MTEXT_TOGGLES = set("LlOoKk")
_MTEXT_PARAMS = set("CcHhQqTtWwAaFfPpSs")
_HEX = set("0123456789abcdefABCDEF")


def validate_rule(pattern, mode, *, replacement=None, for_replace=False):
    """Return a Traditional Chinese error, or None when the rule can be compiled."""
    pattern = "" if pattern is None else str(pattern)
    if pattern == "":
        return "請輸入搜尋文字"
    allowed = REPLACE_MODES if for_replace else MATCH_MODES
    if mode not in dict(allowed):
        return "不支援的比對方式"
    if mode == "regex" or (for_replace and mode == "regex"):
        try:
            compiled = re.compile(pattern)
        except re.error as exc:
            return f"正規表示式無效：{exc}"
        if for_replace:
            try:
                compiled.sub("" if replacement is None else str(replacement), "")
            except re.error as exc:
                return f"取代內容無效：{exc}"
    elif for_replace and mode != "literal":
        return "不支援的比對方式"
    return None


def _flags(case_sensitive):
    return 0 if case_sensitive else re.IGNORECASE


def compile_rule(pattern, mode, case_sensitive):
    flags = _flags(case_sensitive)
    if mode == "contains":
        return re.compile(re.escape(pattern), flags)
    if mode == "wildcard":
        return re.compile(fnmatch.translate(pattern), flags)
    if mode == "regex":
        return re.compile(pattern, flags)
    raise ValueError(f"unsupported match mode: {mode}")


def find_matches(plain, pattern, mode, case_sensitive):
    """Return each non-empty match. Wildcard rules match the whole text."""
    found = []
    for match in compile_rule(pattern, mode, case_sensitive).finditer(plain or ""):
        if match.end() == match.start():
            continue
        found.append(match.group(0))
    return found


def apply_replacement(plain, find, repl, mode, case_sensitive):
    """Return ``(new_text, count)``. ``repl`` may use ``\\1`` when mode is regex."""
    plain = plain or ""
    repl = "" if repl is None else str(repl)
    if mode == "literal":
        if case_sensitive:
            return plain.replace(find, repl), plain.count(find)
        regex = re.compile(re.escape(find), re.IGNORECASE)
        return regex.subn(lambda _match: repl, plain)
    return re.compile(find, _flags(case_sensitive)).subn(repl, plain)


def escape_mtext(plain):
    """Encode plain text as MTEXT, escaping braces, backslashes and newlines."""
    out = []
    for ch in plain or "":
        if ch == "\\":
            out.append("\\\\")
        elif ch == "{":
            out.append("\\{")
        elif ch == "}":
            out.append("\\}")
        elif ch == "\n":
            out.append("\\P")
        elif ch == "\u00a0":
            out.append("\\~")
        else:
            out.append(ch)
    return "".join(out)


def mtext_tokens(raw):
    """Split MTEXT into ``('char', text)`` and ``('code', raw)`` tokens.

    Stacked fractions (``\\S...;``) and font/height/color codes stay in the code
    token, so a search does not see the text hidden inside them.
    """
    raw = raw or ""
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "\\" and i + 1 < n:
            nxt = raw[i + 1]
            if nxt in "\\{}":
                yield ("char", nxt)
                i += 2
                continue
            if nxt == "~":
                yield ("char", "\u00a0")
                i += 2
                continue
            if nxt == "P":
                yield ("char", "\n")
                i += 2
                continue
            if nxt == "U" and i + 2 < n and raw[i + 2] == "+":
                end = i + 3
                while end < n and end < i + 9 and raw[end] in _HEX:
                    end += 1
                if end > i + 3:
                    yield ("char", chr(int(raw[i + 3:end], 16)))
                    i = end
                    continue
            if nxt in _MTEXT_TOGGLES:
                yield ("code", raw[i:i + 2])
                i += 2
                continue
            if nxt in _MTEXT_PARAMS:
                semi = raw.find(";", i + 2)
                if semi != -1 and semi - i < 250:
                    yield ("code", raw[i:semi + 1])
                    i = semi + 1
                    continue
        if ch in "{}":
            yield ("code", ch)
            i += 1
            continue
        yield ("char", ch)
        i += 1


def mtext_plain(raw):
    return "".join(val for kind, val in mtext_tokens(raw) if kind == "char")


def splice_mtext(raw, new_plain):
    """Write ``new_plain`` back into MTEXT, keeping format codes on unchanged characters.

    Returns ``(new_raw, format_lost)``. ``format_lost`` is true when the codes
    could not be kept and the text was rewritten as plain MTEXT.
    """
    tokens = list(mtext_tokens(raw))
    old = "".join(val for kind, val in tokens if kind == "char")
    new_plain = new_plain or ""
    if old == new_plain:
        return raw or "", False
    if not any(kind == "code" for kind, _val in tokens):
        return escape_mtext(new_plain), False
    pieces = []
    tok_i = 0
    char_i = 0

    def emit_until(target):
        nonlocal tok_i, char_i
        while char_i < target and tok_i < len(tokens):
            kind, val = tokens[tok_i]
            tok_i += 1
            if kind == "code":
                pieces.append(val)
            else:
                pieces.append(escape_mtext(val))
                char_i += 1

    def drop_until(target):
        nonlocal tok_i, char_i
        while char_i < target and tok_i < len(tokens):
            kind, val = tokens[tok_i]
            tok_i += 1
            if kind == "code":
                pieces.append(val)
            else:
                char_i += 1

    def leading_codes():
        nonlocal tok_i
        while tok_i < len(tokens) and tokens[tok_i][0] == "code":
            pieces.append(tokens[tok_i][1])
            tok_i += 1

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(a=old, b=new_plain, autojunk=False).get_opcodes():
        emit_until(i1)
        if tag == "equal":
            emit_until(i2)
        elif tag == "delete":
            drop_until(i2)
        elif tag == "replace":
            drop_until(i2)
            pieces.append(escape_mtext(new_plain[j1:j2]))
        elif tag == "insert":
            leading_codes()
            pieces.append(escape_mtext(new_plain[j1:j2]))
    while tok_i < len(tokens):
        kind, val = tokens[tok_i]
        tok_i += 1
        if kind == "code":
            pieces.append(val)
        else:
            pieces.append(escape_mtext(val))
    built = "".join(pieces)
    if mtext_plain(built) != new_plain:
        return escape_mtext(new_plain), True
    return built, False


def plain_of(row):
    raw = row.get("raw") or ""
    if row.get("formatted"):
        return mtext_plain(raw)
    return raw


def escape_field(text):
    """Escape one marker field the same way the generated LISP does."""
    out = []
    i = 0
    s = "" if text is None else str(text)
    while i < len(s):
        if s.startswith(MARKER_SEP, i):
            out.append("|\\=|")
            i += len(MARKER_SEP)
            continue
        ch = s[i]
        if ch == "\\":
            out.append("\\\\")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def unescape_field(text):
    out = []
    i = 0
    s = text or ""
    while i < len(s):
        if s.startswith("|\\=|", i):
            out.append(MARKER_SEP)
            i += 4
            continue
        if s[i] == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            mapped = {"\\": "\\", "n": "\n", "r": "\r", "t": "\t"}.get(nxt)
            if mapped is not None:
                out.append(mapped)
                i += 2
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _marker_rest(line, keyword):
    if not line.startswith(keyword):
        return None
    rest = line[len(keyword):]
    if rest == "" or rest[0] in " \t|" or rest.startswith(MARKER_SEP):
        return rest
    return None


def _split_fields(rest, count):
    if rest.startswith(MARKER_SEP) or MARKER_SEP in rest:
        parts = rest.split(MARKER_SEP)
        if parts and parts[0] == "":
            parts = parts[1:]
        parts = [unescape_field(part.strip()) for part in parts]
    elif "\t" in rest:
        parts = rest.split("\t")
        if parts and parts[0] == "":
            parts = parts[1:]
        parts = [part.strip() for part in parts]
    else:
        parts = rest.split()
    if len(parts) < count:
        parts.extend([""] * (count - len(parts)))
    return parts[:count]


def _summary_pair(rest):
    left, right = _split_fields(rest, 2)
    if not str(left).isdigit() or not str(right).isdigit():
        return None
    return int(left), int(right)


def _summary_one(rest):
    count = _split_fields(rest, 1)[0]
    if not str(count).isdigit():
        return None
    return int(count)


def parse_dump(text):
    """Parse ``TEXTDUMP`` lines from one drawing's console output."""
    rows = []
    for raw in (text or "").splitlines():
        rest = _marker_rest(raw.strip(), "TEXTDUMP")
        if rest is None:
            continue
        layout, layer, kind, block, body, x, y, handle, flag = _split_fields(rest, 9)
        if not kind and not handle and not body:
            continue
        rows.append({
            "layout": layout,
            "layer": layer,
            "kind": kind,
            "block": block,
            "raw": body,
            "x": x,
            "y": y,
            "handle": handle,
            "formatted": flag.strip() in {"1", "T", "t"},
        })
    summary = None
    for raw in (text or "").splitlines():
        rest = _marker_rest(raw.strip(), "TEXTDUMP_SUMMARY")
        if rest is None:
            continue
        count = _summary_one(rest)
        if count is not None:
            summary = count
    errors = []
    for raw in (text or "").splitlines():
        rest = _marker_rest(raw.strip(), "TEXT_ERROR")
        if rest is None:
            continue
        message = _split_fields(rest, 1)[0].strip()
        if message:
            errors.append(message)
    ready = any(raw.strip() == "TEXTDUMP_READY" for raw in (text or "").splitlines())
    return {"rows": rows, "summary": summary, "errors": errors, "ready": ready}


def parse_apply(text):
    summary = None
    for raw in (text or "").splitlines():
        rest = _marker_rest(raw.strip(), "TEXTAPPLY_SUMMARY")
        if rest is None:
            continue
        pair = _summary_pair(rest)
        if pair is not None:
            summary = pair
    results = []
    for raw in (text or "").splitlines():
        rest = _marker_rest(raw.strip(), "TEXTAPPLY")
        if rest is None:
            continue
        handle, status = _split_fields(rest, 2)
        if handle or status:
            results.append({"handle": handle, "status": status})
    errors = []
    for raw in (text or "").splitlines():
        rest = _marker_rest(raw.strip(), "TEXT_ERROR")
        if rest is None:
            continue
        message = _split_fields(rest, 1)[0].strip()
        if message:
            errors.append(message)
    return {"summary": summary, "results": results, "errors": errors}


def tx_lisp_escape(text):
    """Escape text for an AutoLISP string, keeping newlines as ``\\n``."""
    out = []
    for ch in "" if text is None else str(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return "".join(out)


def tx_lisp_str(text):
    return '"' + tx_lisp_escape(text) + '"'


_LISP_HEAD = r""";;; dwgtext.lsp — generated. UTF-8. Do not edit by hand.
;;; AutoLISP has no regular expressions. This file only lists or writes text.

(setq *tx-blocks* @@BLOCKS@@)

(defun tx-str (v)
  (if (= (type v) 'STR) v ""))

(defun tx-esc (s / i n ch out)
  (setq s (tx-str s) i 1 n (strlen s) out "")
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

(defun tx-num (v / s i n ch out)
  (if (not (numberp v))
    ""
    (progn
      (setq s (rtos v 2 8) i 1 n (strlen s) out "")
      (while (<= i n)
        (setq ch (substr s i 1))
        (if (= ch ",") (setq ch "."))
        (setq out (strcat out ch))
        (setq i (1+ i)))
      out)))

(defun tx-join-text (ed / rec out code)
  (setq out "")
  (foreach rec ed
    (setq code (car rec))
    (if (or (= code 1) (= code 3))
      (setq out (strcat out (tx-str (cdr rec))))))
  out)

(defun tx-emit (layout layer kind block body x y handle flag)
  (princ (strcat "\nTEXTDUMP@@SEP@@" (tx-esc layout) "@@SEP@@" (tx-esc layer)
                 "@@SEP@@" (tx-esc kind) "@@SEP@@" (tx-esc block) "@@SEP@@" (tx-esc body)
                 "@@SEP@@" (tx-esc x) "@@SEP@@" (tx-esc y) "@@SEP@@" (tx-esc handle)
                 "@@SEP@@" (tx-esc flag) "\n"))
  (princ))

(defun tx-point (ed / p)
  (setq p (cdr (assoc 10 ed)))
  (if (listp p) p (list 0 0 0)))

(defun tx-emit-ed (ed layout block / kind p flag)
  (setq kind (tx-str (cdr (assoc 0 ed)))
        p (tx-point ed)
        flag (if (= kind "MTEXT") "1" "0"))
  (tx-emit layout
           (tx-str (cdr (assoc 8 ed)))
           kind
           block
           (tx-join-text ed)
           (tx-num (car p))
           (tx-num (cadr p))
           (tx-str (cdr (assoc 5 ed)))
           flag)
  T)

(defun tx-owner-block (ed / owner oed)
  (setq owner (cdr (assoc 330 ed)))
  (if (= (type owner) 'ENAME)
    (progn
      (setq oed (entget owner))
      (if (= (tx-str (cdr (assoc 0 oed))) "INSERT")
        (tx-str (cdr (assoc 2 oed)))
        ""))
    ""))

(defun tx-skip-block-p (name)
  (setq name (strcase (tx-str name)))
  (or (= name "*MODEL_SPACE")
      (= (substr name 1 12) "*PAPER_SPACE")))

(defun tx-dump-space (/ ss i n en ed kind layout block)
  (setq ss (ssget "_X" '((0 . "TEXT,MTEXT,ATTRIB,ATTDEF"))) i 0 n 0)
  (if ss
    (progn
      (setq n (sslength ss))
      (while (< i n)
        (setq en (ssname ss i) ed (entget en) kind (tx-str (cdr (assoc 0 ed))))
        (setq layout (tx-str (cdr (assoc 410 ed))))
        (if (= layout "") (setq layout "Model"))
        (setq block (if (= kind "ATTRIB") (tx-owner-block ed) ""))
        (tx-emit-ed ed layout block)
        (setq i (1+ i)))))
  (if ss n 0))

(defun tx-dump-blocks (/ rec name en ed kind n)
  (setq rec (tblnext "BLOCK" T) n 0)
  (while rec
    (setq name (tx-str (cdr (assoc 2 rec))))
    (if (not (tx-skip-block-p name))
      (progn
        (setq en (cdr (assoc -2 rec)))
        (while en
          (setq ed (entget en) kind (tx-str (cdr (assoc 0 ed))))
          (if (member kind '("TEXT" "MTEXT" "ATTRIB" "ATTDEF"))
            (progn (tx-emit-ed ed "圖塊定義" name) (setq n (1+ n))))
          (setq en (entnext en)))))
    (setq rec (tblnext "BLOCK")))
  n)

(defun tx-error-msg (msg)
  (princ (strcat "\nTEXT_ERROR@@SEP@@" (tx-esc msg) "\n"))
  (princ))

(defun tx-dump-current (/ n olderr)
  (setq olderr *error* n 0)
  (defun *error* (msg)
    (tx-error-msg (tx-str msg))
    (princ "\nTEXTDUMP_SUMMARY@@SEP@@0\n")
    (setq *error* olderr)
    (princ))
  (setq n (tx-dump-space))
  (if *tx-blocks* (setq n (+ n (tx-dump-blocks))))
  (princ (strcat "\nTEXTDUMP_SUMMARY@@SEP@@" (itoa n) "\n"))
  (setq *error* olderr)
  (princ))
"""

_LISP_APPLY = r"""
(defun tx-chunks (s / n out i)
  (setq s (tx-str s) n (strlen s) out nil i 1)
  (if (= n 0)
    (list "")
    (progn
      (while (<= i n)
        (setq out (append out (list (substr s i 250))))
        (setq i (+ i 250)))
      out)))

(defun tx-strip-text (ed / out rec)
  (setq out nil)
  (foreach rec ed
    (if (not (member (car rec) '(1 3)))
      (setq out (append out (list rec)))))
  out)

(defun tx-put-text (ed kind value / base chunks n i code item)
  (setq base (tx-strip-text ed)
        chunks (if (= kind "MTEXT") (tx-chunks value) (list (tx-str value)))
        n (length chunks)
        i 0)
  (foreach item chunks
    (setq i (1+ i))
    (setq code (if (and (= kind "MTEXT") (< i n)) 3 1))
    (setq base (append base (list (cons code item)))))
  base)

(defun tx-color (ed color)
  (if (and (= (type color) 'INT) (>= color 0) (<= color 256))
    (if (assoc 62 ed)
      (subst (cons 62 color) (assoc 62 ed) ed)
      (append ed (list (cons 62 color))))
    ed))

(defun tx-apply-msg (handle status)
  (princ (strcat "\nTEXTAPPLY@@SEP@@" (tx-esc handle) "@@SEP@@" (tx-esc status) "\n"))
  (princ))

(defun tx-apply-rows (rows / row handle kind value color en ed result n fail)
  (setq n 0 fail 0)
  (foreach row rows
    (setq handle (nth 0 row)
          kind (nth 1 row)
          value (nth 2 row)
          color (nth 3 row)
          en (if (= (type handle) 'STR) (handent handle) nil))
    (if (not en)
      (progn (setq fail (1+ fail)) (tx-apply-msg handle "missing"))
      (progn
        (setq ed (tx-color (tx-put-text (entget en) kind value) color))
        (setq result (entmod ed))
        (if result
          (progn (setq n (1+ n)) (tx-apply-msg handle "ok"))
          (progn (setq fail (1+ fail)) (tx-apply-msg handle "entmod failed"))))))
  (princ (strcat "\nTEXTAPPLY_SUMMARY@@SEP@@" (itoa n) "@@SEP@@" (itoa fail) "\n"))
  (princ)
  T)
"""


def _fill_lisp(body, include_blocks):
    text = body.replace("@@SEP@@", MARKER_SEP).replace("@@BLOCKS@@", "T" if include_blocks else "nil")
    if not fontfix._lisp_parens_balanced(text):
        raise RuntimeError("generated text LISP has unbalanced parentheses")
    return text


def build_dump_lisp(include_blocks):
    src = _fill_lisp(_LISP_HEAD + '\n(princ "\\nTEXTDUMP_READY\\n")\n(princ)\n', include_blocks)
    return src


def build_apply_lisp(edits_by_index):
    """``edits_by_index[i]`` is the list of edits for script file ``i``.

    Each edit needs ``handle``, ``kind``, ``new_raw`` and ``color`` (``None`` leaves color).
    """
    parts = [_fill_lisp(_LISP_HEAD + _LISP_APPLY, False)]
    for index, edits in enumerate(edits_by_index):
        rows = []
        for edit in edits:
            color = -1 if edit.get("color") is None else int(edit["color"])
            rows.append(
                "(list {h} {k} {v} {c})".format(
                    h=tx_lisp_str(edit.get("handle") or ""),
                    k=tx_lisp_str(edit.get("kind") or "TEXT"),
                    v=tx_lisp_str(edit.get("new_raw") or ""),
                    c=color,
                )
            )
        body = "nil" if not rows else "(list\n    " + "\n    ".join(rows) + ")"
        parts.append(f"(defun tx-apply-{index} () (tx-apply-rows {body}))")
    src = "\n".join(parts) + "\n"
    if not fontfix._lisp_parens_balanced(src):
        raise RuntimeError("generated text-apply LISP has unbalanced parentheses")
    return src


def _preamble(lisp_path):
    lisp_path = str(lisp_path).replace("\\", "/")
    return [
        "FILEDIA",
        "0",
        "CMDDIA",
        "0",
        "EXPERT",
        "5",
        "FONTALT",
        "txt.shx",
        "SECURELOAD",
        "0",
        f'(load "{lisp_path}")',
    ]


def build_dump_script(jobs, lisp_path):
    lines = _preamble(lisp_path)
    for i, job in enumerate(jobs):
        lines.append("_.OPEN")
        lines.append(f'"{job["spath"]}"')
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_OPEN_{i}|" (getvar "DWGNAME") "|\\n"))')
        lines.append(
            "(if (and (boundp 'tx-dump-current) tx-dump-current) "
            f'(tx-dump-current) (princ (strcat "\\nTEXT_" "ERROR{MARKER_SEP}lisp not loaded\\n")))'
        )
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_DONE_{i}_\\n"))')
    lines.append("_.QUIT")
    text = "\n".join(lines) + "\n"
    fontfix.script_bytes(text)
    return text


def build_apply_script(jobs, lisp_path):
    lines = _preamble(lisp_path)
    for i, job in enumerate(jobs):
        # tx-apply-N is numbered over the whole apply list; the OPEN/DONE markers
        # count within this launch (a relaunch after a stall, or a worker slice).
        edit = job.get("apply_index", i)
        lines.append("_.OPEN")
        lines.append(f'"{job["spath"]}"')
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_OPEN_{i}|" (getvar "DWGNAME") "|\\n"))')
        lines.append(
            f"(if (and (boundp 'tx-apply-{edit}) tx-apply-{edit}) "
            f'(tx-apply-{edit}) (princ (strcat "\\nTEXT_" "ERROR{MARKER_SEP}lisp not loaded\\n")))'
        )
        lines.append("_.QSAVE")
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_DONE_{i}_\\n"))')
    lines.append("_.QUIT")
    text = "\n".join(lines) + "\n"
    fontfix.script_bytes(text)
    return text


HIT_FIELDS = ["檔案", "空間/版面", "圖層", "物件類型", "圖塊名稱", "文字內容", "符合部分", "X", "Y", "handle"]
EXTRACT_FIELDS = ["檔案", "狀態", "符合筆數", "錯誤", "秒數"]
CHANGE_FIELDS = ["檔案", "圖層", "物件類型", "handle", "原文字", "新文字", "次數"]
REPLACE_FIELDS = ["檔案", "輸出", "狀態", "變更筆數", "錯誤", "秒數"]

EXTRACT_STATUS = {
    "DONE": "已擷取",
    "EMPTY": "無符合",
    "FAILED": "失敗",
    "STOPPED": "已停止",
}
REPLACE_STATUS = {
    "SAVED": "已輸出",
    "UNCHANGED": "無變更",
    "PREVIEW": "預覽",
    "FAILED": "失敗",
    "SKIPPED": "略過",
    "STOPPED": "已停止",
}


def hit_row(record):
    return {
        "檔案": record.get("file", ""),
        "空間/版面": record.get("layout", ""),
        "圖層": record.get("layer", ""),
        "物件類型": record.get("kind", ""),
        "圖塊名稱": record.get("block", ""),
        "文字內容": record.get("plain", ""),
        "符合部分": record.get("match", ""),
        "X": record.get("x", ""),
        "Y": record.get("y", ""),
        "handle": record.get("handle", ""),
    }


def extract_row(rec):
    return {
        "檔案": rec.get("file", ""),
        "狀態": EXTRACT_STATUS.get(rec.get("status", ""), rec.get("status", "")),
        "符合筆數": rec.get("hits", 0),
        "錯誤": rec.get("error", ""),
        "秒數": rec.get("elapsed_seconds", 0),
    }


def change_row(record):
    return {
        "檔案": record.get("file", ""),
        "圖層": record.get("layer", ""),
        "物件類型": record.get("kind", ""),
        "handle": record.get("handle", ""),
        "原文字": record.get("old", ""),
        "新文字": record.get("new", ""),
        "次數": record.get("count", 0),
    }


def replace_row(rec):
    return {
        "檔案": rec.get("file", ""),
        "輸出": rec.get("output", ""),
        "狀態": REPLACE_STATUS.get(rec.get("status", ""), rec.get("status", "")),
        "變更筆數": rec.get("changes", 0),
        "錯誤": rec.get("error", ""),
        "秒數": rec.get("elapsed_seconds", 0),
    }

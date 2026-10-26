"""Classify DWG entities onto layers by object type.

AutoLISP walks the drawing and applies a type-to-layer map generated here.
Block references can use the block's effective name. Python only builds the
UTF-8 LISP, the ASCII script, and the CSV rows.

Result fields use ``|=|``. accoreconsole expands a princ tab into spaces, so a
tab parser cannot be used. A run of spaces is still accepted when every field
is a single word.
"""
from __future__ import annotations

import re

import fontfix

MARKER_SEP = fontfix.MARKER_SEP

# Characters AutoCAD rejects in a layer name, including the backtick.
_ILLEGAL = set('<>/\\":;?*|,=`')

DEFAULT_MAP_TEXT = """\
LWPOLYLINE=PL
POLYLINE=PL
TEXT=TST
MTEXT=TST
HATCH=H
LINE=LINE
ARC=ARC
CIRCLE=CIRCLE
DIMENSION=DIM
LEADER=LEAD
MULTILEADER=LEAD
"""

# DXF entity names. MLEADER is accepted as an alias of MULTILEADER.
_TYPE_ALIASES = {"MLEADER": "MULTILEADER"}

SOURCE_MODES = (
    ("layer0", "只處理圖層 0"),
    ("listed", "指定來源圖層"),
    ("all", "全部圖層"),
)
SOURCE_LABELS = dict(SOURCE_MODES)

MOVE_FIELDS = ["檔案", "新建圖層", "顏色", "物件類型", "搬移數量", "來源圖層"]
RESULT_FIELDS = ["檔案", "輸出", "狀態", "搬移數量", "新建圖層數", "鎖定圖層", "錯誤", "秒數"]

STATUS = {
    "SAVED": "已輸出",
    "PREVIEW": "預覽",
    "UNCHANGED": "無變更",
    "FAILED": "失敗",
    "SKIPPED": "略過",
    "STOPPED": "已停止",
}


def sanitize_layer_name(name, fallback="LAYER"):
    """Replace illegal layer characters and control codes. Trim to 255.

    An empty result uses ``fallback`` (pass ``""`` to allow an empty result).
    Spaces at the ends are removed. Spaces in the middle are kept.
    """
    chars = []
    for ch in "" if name is None else str(name):
        if ord(ch) < 32 or ch in _ILLEGAL:
            chars.append("_")
        else:
            chars.append(ch)
    text = "".join(chars).strip()
    if not text:
        text = "" if fallback is None else str(fallback)
    return text[:255]


def apply_prefix(prefix, name):
    """Sanitize ``prefix`` and ``name``, then concatenate. Empty name stays empty."""
    clean_prefix = sanitize_layer_name(prefix, fallback="") if prefix else ""
    clean = sanitize_layer_name(name, fallback="")
    if not clean:
        return ""
    if clean_prefix:
        return sanitize_layer_name(clean_prefix + clean, fallback="")[:255]
    return clean


def block_layer_name(effective, prefix=""):
    """Layer name for an INSERT.

    A normal effective name is sanitized and prefixed. Anonymous names
    (``*U``, ``*D``, and anything else starting with ``*``) become
    ``ANON_`` plus the remainder, so a missing dynamic-block name still
    gets a stable layer.
    """
    name = "" if effective is None else str(effective).strip()
    if name == "" or name.startswith("*"):
        rest = sanitize_layer_name(name[1:] if name.startswith("*") else name, fallback="")
        if not rest:
            rest = "BLOCK"
        name = "ANON_" + rest
    return apply_prefix(prefix, name)


def _canon_type(name):
    token = str(name or "").strip().upper()
    return _TYPE_ALIASES.get(token, token)


def parse_type_map(text):
    """Parse ``TYPE=LAYER`` lines. Returns ``(pairs, notes)``.

    A blank layer drops that type. ``INSERT`` is not a table row: block
    references use ``block_layer_name`` when that option is on. The last
    duplicate type wins. Lines starting with ``;`` or ``#`` are comments.
    """
    pairs = []
    notes = []
    seen = {}
    for lineno, raw in enumerate(str(text or "").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        if "=" not in line:
            notes.append(f"第 {lineno} 行沒有 =，已略過")
            continue
        kind, layer = line.split("=", 1)
        kind = _canon_type(kind)
        if not kind:
            notes.append(f"第 {lineno} 行沒有物件類型，已略過")
            continue
        if kind == "INSERT":
            notes.append("INSERT 不寫在對照表；圖塊會用圖塊名稱當圖層")
            continue
        target = layer.strip()
        if target == "":
            seen[kind] = ""
            continue
        clean = sanitize_layer_name(target, fallback="")
        if not clean:
            notes.append(f"{kind} 的圖層名稱無效，已略過")
            seen[kind] = ""
            continue
        if clean != target.strip():
            notes.append(f"{kind} 的圖層已改成 {clean}")
        seen[kind] = clean
    for kind, layer in seen.items():
        if layer:
            pairs.append((kind, layer))
    return pairs, notes


def parse_source_layers(text):
    parts = re.split(r"[\s,;]+", str(text or "").strip())
    found = []
    seen = set()
    for part in parts:
        if not part:
            continue
        key = part.upper()
        if key in seen:
            continue
        seen.add(key)
        found.append(part)
    return found


def compile_rules(
    map_text,
    prefix="",
    other="",
    source_mode="layer0",
    source_layers="0",
    insert_by_name=True,
):
    """Return ``(settings, notes, error)``. ``error`` is Traditional Chinese or ``""``."""
    if source_mode not in SOURCE_LABELS:
        return None, [], "不支援的處理範圍"
    pairs, notes = parse_type_map(map_text)
    clean_prefix = sanitize_layer_name(prefix, fallback="") if str(prefix or "").strip() else ""
    if str(prefix or "").strip() and not clean_prefix:
        notes.append("圖層前綴含非法字元，已忽略")
    prefixed = []
    for kind, layer in pairs:
        target = apply_prefix(clean_prefix, layer)
        if not target:
            notes.append(f"{kind} 的圖層名稱無效，已略過")
            continue
        prefixed.append((kind, target))
    other_layer = ""
    if str(other or "").strip():
        other_layer = apply_prefix(clean_prefix, other)
        if not other_layer:
            notes.append("其他物件的圖層名稱無效，已忽略")
    if source_mode == "all":
        sources = None
    elif source_mode == "layer0":
        sources = ["0"]
    else:
        sources = parse_source_layers(source_layers)
        if not sources:
            return None, notes, "請輸入來源圖層"
    if not prefixed and not other_layer and not insert_by_name:
        return None, notes, "請至少指定一種物件類型的圖層"
    settings = {
        "pairs": prefixed,
        "prefix": clean_prefix,
        "other": other_layer,
        "sources": sources,
        "insert_by_name": bool(insert_by_name),
    }
    return settings, notes, ""


def escape_field(text):
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


def lisp_escape(text):
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


def lisp_str(text):
    return '"' + lisp_escape(text) + '"'


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


def _parse_int(value):
    text = str(value or "").strip()
    if text.isdigit() or (text.startswith("-") and text[1:].isdigit()):
        return int(text)
    return None


def parse_output(text):
    """Parse ``LAYERROW`` / ``LAYERLOCK`` / ``LAYERSUMMARY`` lines for one drawing."""
    rows, locks, notes, errors = [], [], [], []
    summary = None
    ready = False
    for raw in (text or "").splitlines():
        line = raw.strip()
        if line == "LAYERCLASS_READY":
            ready = True
            continue
        rest = _marker_rest(line, "LAYERROW")
        if rest is not None:
            dest, color, kind, count, source, created = _split_fields(rest, 6)
            moved = _parse_int(count)
            if kind and moved is not None:
                rows.append({
                    "layer": dest,
                    "color": color,
                    "kind": kind,
                    "count": moved,
                    "source": source,
                    "created": str(created).strip() in {"1", "T", "t"},
                })
            continue
        rest = _marker_rest(line, "LAYERLOCK")
        if rest is not None:
            name, action = _split_fields(rest, 2)
            if name:
                locks.append({"layer": name, "action": action or "skipped"})
            continue
        rest = _marker_rest(line, "LAYERSUMMARY")
        if rest is not None:
            moved, created, locked = _split_fields(rest, 3)
            numbers = (_parse_int(moved), _parse_int(created), _parse_int(locked))
            if None not in numbers:
                summary = {"moved": numbers[0], "created": numbers[1], "locked": numbers[2]}
            continue
        rest = _marker_rest(line, "LAYERNOTE")
        if rest is not None:
            message = _split_fields(rest, 1)[0].strip()
            if message:
                notes.append(message)
            continue
        rest = _marker_rest(line, "LAYER_ERROR")
        if rest is not None:
            message = _split_fields(rest, 1)[0].strip()
            if message:
                errors.append(message)
    return {"rows": rows, "locks": locks, "summary": summary, "notes": notes, "errors": errors, "ready": ready}


def format_locks(locks):
    parts = []
    for item in locks or []:
        action = "已暫時解鎖" if item.get("action") == "unlocked" else "略過"
        parts.append(f"{item.get('layer', '')}:{action}")
    return "；".join(parts)


def move_row(record):
    return {
        "檔案": record.get("file", ""),
        "新建圖層": record.get("layer", ""),
        "顏色": record.get("color", ""),
        "物件類型": record.get("kind", ""),
        "搬移數量": record.get("count", 0),
        "來源圖層": record.get("source", ""),
    }


def result_row(rec):
    return {
        "檔案": rec.get("file", ""),
        "輸出": rec.get("output", ""),
        "狀態": STATUS.get(rec.get("status", ""), rec.get("status", "")),
        "搬移數量": rec.get("moved", 0),
        "新建圖層數": rec.get("created", 0),
        "鎖定圖層": rec.get("locks", ""),
        "錯誤": rec.get("error", ""),
        "秒數": rec.get("elapsed_seconds", 0),
    }


def _lisp_bool(value):
    return "T" if value else "nil"


def _lisp_map(pairs):
    if not pairs:
        return "nil"
    rows = "\n    ".join(f"(cons {lisp_str(kind)} {lisp_str(layer)})" for kind, layer in pairs)
    return "(list\n    " + rows + ")"


def _lisp_sources(sources):
    if not sources:
        return "nil"
    rows = " ".join(lisp_str(str(name).upper()) for name in sources)
    return "(list " + rows + ")"


_LISP = r""";;; layerclass.lsp — generated. UTF-8. Do not edit by hand.
;;; Move entities onto layers by DXF type. New layers cycle ACI colors 1-9.
;;; Existing layers keep their color. Inputs are copies; this file never
;;; selects the original path.

(setq *lc-map* @@MAP@@)
(setq *lc-prefix* @@PREFIX@@)
(setq *lc-other* @@OTHER@@)
(setq *lc-sources* @@SOURCES@@)
(setq *lc-model-only* @@MODEL@@)
(setq *lc-blocks* @@BLOCKS@@)
(setq *lc-insert* @@INSERT@@)
(setq *lc-unlock* @@UNLOCK@@)
(setq *lc-keep* @@KEEP@@)
(setq *lc-dry* @@DRY@@)
(setq *lc-bad* @@BAD@@)

(defun lc-str (v)
  (if (= (type v) 'STR) v ""))

(defun lc-esc (s / i n ch out)
  (setq s (lc-str s) i 1 n (strlen s) out "")
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

(defun lc-bad-p (ch / i n)
  (setq i 1 n (strlen *lc-bad*))
  (while (and (<= i n) (/= (substr *lc-bad* i 1) ch))
    (setq i (1+ i)))
  (<= i n))

(defun lc-sanitize (name fallback / i n ch out)
  (setq name (lc-str name) i 1 n (strlen name) out "")
  (while (<= i n)
    (setq ch (substr name i 1))
    (if (or (< (ascii ch) 32) (lc-bad-p ch))
      (setq out (strcat out "_"))
      (setq out (strcat out ch)))
    (setq i (1+ i)))
  (while (and (> (strlen out) 0) (= (substr out 1 1) " "))
    (setq out (substr out 2)))
  (while (and (> (strlen out) 0) (= (substr out (strlen out) 1) " "))
    (setq out (substr out 1 (1- (strlen out)))))
  (if (= out "") (setq out (lc-str fallback)))
  (if (> (strlen out) 255) (setq out (substr out 1 255)))
  out)

(defun lc-prefix (name / clean both)
  (setq clean (lc-sanitize name ""))
  (if (= clean "")
    ""
    (progn
      (if (> (strlen *lc-prefix*) 0)
        (setq both (lc-sanitize (strcat *lc-prefix* clean) ""))
        (setq both clean))
      (if (> (strlen both) 255) (substr both 1 255) both))))

(defun lc-strip-star (name)
  (if (= (substr (lc-str name) 1 1) "*")
    (substr name 2)
    (lc-str name)))

(defun lc-effective (en / name value)
  (setq name (lc-str (cdr (assoc 2 (entget en)))))
  (if (and (= (substr name 1 1) "*") (boundp 'getpropertyvalue) getpropertyvalue)
    (progn
      (setq value (getpropertyvalue en "EffectiveName"))
      (if (and (= (type value) 'STR) (/= value "") (/= (substr value 1 1) "*"))
        (setq name value))))
  name)

(defun lc-insert-layer (en / name rest)
  (setq name (lc-effective en))
  (if (or (= name "") (= (substr name 1 1) "*"))
    (progn
      (setq rest (lc-sanitize (lc-strip-star name) ""))
      (if (= rest "") (setq rest "BLOCK"))
      (setq name (strcat "ANON_" rest))))
  (lc-prefix name))

(defun lc-target (en kind / hit)
  (if (and (= kind "INSERT") *lc-insert*)
    (lc-insert-layer en)
    (progn
      (setq hit (assoc kind *lc-map*))
      (if hit (cdr hit) *lc-other*))))

(defun lc-source-p (name)
  (if (not *lc-sources*)
    T
    (member (strcase (lc-str name)) *lc-sources*)))

(defun lc-space-p (ed / layout)
  (if (not *lc-model-only*)
    T
    (progn
      (setq layout (strcase (lc-str (cdr (assoc 410 ed)))))
      (or (= layout "") (= layout "MODEL")))))

(defun lc-layer-ed (name)
  (entget (tblobjname "LAYER" name)))

(defun lc-abs (n)
  (cond
    ((not (numberp n)) 7)
    ((< n 0) (- n))
    (T n)))

(defun lc-layer-color (name / ed color)
  (setq ed (lc-layer-ed name))
  (setq color (if ed (cdr (assoc 62 ed)) 7))
  (lc-abs color))

(defun lc-layer-ltype (name / ed lt)
  (setq ed (lc-layer-ed name))
  (setq lt (if ed (cdr (assoc 6 ed)) nil))
  (if (or (not lt) (= lt "")) "Continuous" lt))

(defun lc-next-color ()
  (setq *lc-color* (1+ *lc-color*))
  (if (> *lc-color* 9) (setq *lc-color* 1))
  *lc-color*)

(defun lc-ensure (name / color)
  (if (tblsearch "LAYER" name)
    (cons name (lc-layer-color name))
    (progn
      (setq color (lc-next-color))
      (if (entmake (list (cons 0 "LAYER")
                         (cons 100 "AcDbSymbolTableRecord")
                         (cons 100 "AcDbLayerTableRecord")
                         (cons 2 name)
                         (cons 70 0)
                         (cons 62 color)
                         (cons 6 "Continuous")))
        (progn
          (setq *lc-created* (cons (strcase name) *lc-created*))
          (cons name color))
        nil))))

(defun lc-created-p (name)
  (if (member (strcase name) *lc-created*) 1 0))

(defun lc-locked-p (name / ed flags)
  (setq ed (lc-layer-ed name))
  (if (not ed)
    nil
    (progn
      (setq flags (cdr (assoc 70 ed)))
      (if (not flags) (setq flags 0))
      (= (logand flags 4) 4))))

(defun lc-note-lock (name action / key)
  (setq key (strcase (lc-str name)))
  (if (not (member key *lc-lock-seen*))
    (progn
      (setq *lc-lock-seen* (cons key *lc-lock-seen*))
      (setq *lc-locks* (cons (list name action) *lc-locks*)))))

(defun lc-unlock (name / ed flags)
  (setq ed (lc-layer-ed name))
  (if ed
    (progn
      (setq flags (cdr (assoc 70 ed)))
      (if (not flags) (setq flags 0))
      (entmod (subst (cons 70 (logand flags (~ 4))) (assoc 70 ed) ed))
      (lc-note-lock name "unlocked"))))

(defun lc-relock (/ item name ed flags)
  (foreach item *lc-locks*
    (if (= (cadr item) "unlocked")
      (progn
        (setq name (car item))
        (setq ed (lc-layer-ed name))
        (if ed
          (progn
            (setq flags (cdr (assoc 70 ed)))
            (if (not flags) (setq flags 0))
            (entmod (subst (cons 70 (logior flags 4)) (assoc 70 ed) ed))))))))

(defun lc-put (ed code value)
  (if (assoc code ed)
    (subst (cons code value) (assoc code ed) ed)
    (append ed (list (cons code value)))))

(defun lc-bylayer-color-p (ed / color)
  (setq color (cdr (assoc 62 ed)))
  (or (not color) (= color 256)))

(defun lc-bylayer-lt-p (ed / lt)
  (setq lt (cdr (assoc 6 ed)))
  (or (not lt) (= (strcase lt) "BYLAYER")))

(defun lc-keep-ed (ed old)
  (if (not *lc-keep*)
    ed
    (progn
      (if (lc-bylayer-color-p ed)
        (setq ed (lc-put ed 62 (lc-layer-color old))))
      (if (lc-bylayer-lt-p ed)
        (setq ed (lc-put ed 6 (lc-layer-ltype old))))
      ed)))

(defun lc-row-key (dest kind source)
  (strcat (strcase dest) "|" kind "|" (strcase source)))

(defun lc-add (dest color kind source created / key found row)
  (setq key (lc-row-key dest kind source) found nil)
  (foreach row *lc-rows*
    (if (= (car row) key) (setq found row)))
  (if found
    (setq *lc-rows* (subst
      (list key dest color kind source created (1+ (nth 6 found)))
      found *lc-rows*))
    (setq *lc-rows* (cons (list key dest color kind source created 1) *lc-rows*))))

(defun lc-fail (msg)
  (setq *lc-fails* (cons (lc-str msg) *lc-fails*)))

(defun lc-move-now (ed kind old target / made newed)
  (setq made (lc-ensure target))
  (if (not made)
    (lc-fail (strcat "無法建立圖層 " target))
    (progn
      (setq newed (subst (cons 8 (car made)) (assoc 8 ed) (lc-keep-ed ed old)))
      (if *lc-dry*
        (lc-add (car made) (cdr made) kind old (lc-created-p (car made)))
        (if (entmod newed)
          (lc-add (car made) (cdr made) kind old (lc-created-p (car made)))
          (lc-fail (strcat kind " " (lc-str (cdr (assoc 5 ed))))))))))

(defun lc-move (ed kind old target)
  (if (lc-locked-p old)
    (if *lc-unlock*
      (progn (lc-unlock old) (lc-move-now ed kind old target))
      (lc-note-lock old "skipped"))
    (lc-move-now ed kind old target)))

(defun lc-consider (en ed / kind old target)
  (setq kind (lc-str (cdr (assoc 0 ed)))
        old (lc-str (cdr (assoc 8 ed))))
  (if (and (lc-space-p ed) (lc-source-p old))
    (progn
      (setq target (lc-target en kind))
      (if (and target (/= target "") (/= (strcase target) (strcase old)))
        (lc-move ed kind old target)))))

(defun lc-dump-space (/ ss i n en)
  (setq ss (ssget "_X") i 0)
  (if ss
    (progn
      (setq n (sslength ss))
      (while (< i n)
        (setq en (ssname ss i))
        (lc-consider en (entget en))
        (setq i (1+ i)))))
  T)

(defun lc-skip-block-p (name)
  (setq name (strcase (lc-str name)))
  (or (= name "*MODEL_SPACE")
      (= (substr name 1 12) "*PAPER_SPACE")))

(defun lc-dump-blocks (/ rec name en)
  (setq rec (tblnext "BLOCK" T))
  (while rec
    (setq name (lc-str (cdr (assoc 2 rec))))
    (if (not (lc-skip-block-p name))
      (progn
        (setq en (cdr (assoc -2 rec)))
        (while en
          (lc-consider en (entget en))
          (setq en (entnext en)))))
    (setq rec (tblnext "BLOCK")))
  T)

(defun lc-moved (/ n row)
  (setq n 0)
  (foreach row *lc-rows*
    (setq n (+ n (nth 6 row))))
  n)

(defun lc-emit-rows (/ row flag)
  (foreach row (reverse *lc-rows*)
    (setq flag (if (= (nth 5 row) 1) "1" "0"))
    (princ (strcat "\nLAYERROW@@SEP@@" (lc-esc (nth 1 row))
                   "@@SEP@@" (itoa (lc-abs (nth 2 row)))
                   "@@SEP@@" (lc-esc (nth 3 row))
                   "@@SEP@@" (itoa (nth 6 row))
                   "@@SEP@@" (lc-esc (nth 4 row))
                   "@@SEP@@" flag "\n"))))

(defun lc-emit-locks (/ item)
  (foreach item (reverse *lc-locks*)
    (princ (strcat "\nLAYERLOCK@@SEP@@" (lc-esc (car item)) "@@SEP@@" (lc-esc (cadr item)) "\n"))))

(defun lc-run (/ olderr)
  (setq olderr *error*
        *lc-rows* nil
        *lc-locks* nil
        *lc-lock-seen* nil
        *lc-created* nil
        *lc-fails* nil
        *lc-color* 0)
  (defun *error* (msg)
    (princ (strcat "\nLAYER_ERROR@@SEP@@" (lc-esc (lc-str msg)) "\n"))
    (princ "\nLAYERSUMMARY@@SEP@@0@@SEP@@0@@SEP@@0\n")
    (princ "\nLAYERCLASS_READY\n")
    (setq *error* olderr)
    (princ))
  (lc-dump-space)
  (if *lc-blocks* (lc-dump-blocks))
  (lc-relock)
  (lc-emit-locks)
  (lc-emit-rows)
  (if *lc-fails*
    (princ (strcat "\nLAYERNOTE@@SEP@@" (lc-esc (strcat "寫入失敗 " (itoa (length *lc-fails*)) " 筆")) "\n")))
  (princ (strcat "\nLAYERSUMMARY@@SEP@@" (itoa (lc-moved))
                 "@@SEP@@" (itoa (length *lc-created*))
                 "@@SEP@@" (itoa (length *lc-locks*)) "\n"))
  (princ "\nLAYERCLASS_READY\n")
  (setq *error* olderr)
  (princ)
  T)
"""


def build_lisp(settings, *, include_paper=False, include_blocks=False, unlock_locked=False,
               keep_appearance=True, dry_run=False):
    bad = '<>/\\":;?*|,=`'
    text = _LISP
    text = text.replace("@@MAP@@", _lisp_map(settings["pairs"]))
    text = text.replace("@@PREFIX@@", lisp_str(settings.get("prefix") or ""))
    text = text.replace("@@OTHER@@", lisp_str(settings.get("other") or ""))
    text = text.replace("@@SOURCES@@", _lisp_sources(settings.get("sources")))
    text = text.replace("@@MODEL@@", _lisp_bool(not include_paper))
    text = text.replace("@@BLOCKS@@", _lisp_bool(include_blocks))
    text = text.replace("@@INSERT@@", _lisp_bool(settings.get("insert_by_name", True)))
    text = text.replace("@@UNLOCK@@", _lisp_bool(unlock_locked))
    text = text.replace("@@KEEP@@", _lisp_bool(keep_appearance))
    text = text.replace("@@DRY@@", _lisp_bool(dry_run))
    text = text.replace("@@BAD@@", lisp_str(bad))
    text = text.replace("@@SEP@@", MARKER_SEP)
    if not fontfix._lisp_parens_balanced(text):
        raise RuntimeError("generated layer LISP has unbalanced parentheses")
    return text


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


def build_script(jobs, lisp_path, dry_run=False):
    lines = _preamble(lisp_path)
    for i, job in enumerate(jobs):
        lines.append("_.OPEN")
        lines.append(f'"{job["spath"]}"')
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_OPEN_{i}|" (getvar "DWGNAME") "|\\n"))')
        lines.append(
            "(if (and (boundp 'lc-run) lc-run) "
            f'(lc-run) (princ (strcat "\\nLAYER_" "ERROR{MARKER_SEP}lisp not loaded\\n")))'
        )
        if not dry_run:
            lines.append("_.QSAVE")
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_DONE_{i}_\\n"))')
    lines.append("_.QUIT")
    text = "\n".join(lines) + "\n"
    fontfix.script_bytes(text)
    return text

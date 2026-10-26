"""Decide which DWG text-style fonts are missing, and generate the AutoLISP that replaces them.

The rules match the validated FONTFIX AutoLISP prototype:

* A font is missing only when every lookup fails: AutoCAD's support path (``findfile``),
  the Windows Fonts folders, and — for ``.ttf/.ttc/.otf`` — an installed TrueType typeface
  name from the Fonts registry. AutoCAD reports an installed face as ``<typeface>.ttf``
  even when the real file is ``msjh.ttc``, so a plain file check is not enough.
* Names without an extension are checked as ``.shx``. Names starting with ``@`` are skipped.
* Missing primary fonts become the default primary (``romans.shx``). Missing big fonts
  become the default big font (``chineset.shx``). An ``old=new`` map overrides that.
* Installed fonts are left alone.

AutoLISP ``or`` returns ``T`` rather than the value that succeeded. The generated LISP
never uses ``or`` to choose a path or a font name.

accoreconsole does not reliably host COM, so the generated LISP edits the STYLE table
with ``entget`` / ``entmod`` (DXF groups 3 and 4) instead of ActiveX. Installed
typeface names are collected in Python (``winreg``) and embedded in the UTF-8 LISP,
because that check has to be right even when ``vl-registry-*`` is unavailable.
``findfile`` and the Windows Fonts folders are still evaluated inside AutoCAD, where
the support path exists.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

TTF_EXTENSIONS = (".ttf", ".ttc", ".otf")

# SAVEAS format keywords. Empty keeps the current format via QSAVE.
# These are command-line keywords, not the numeric AcSaveAsType codes the
# ActiveX prototype used. They were not executed on the Linux build VM.
DWG_SAVE_VERSIONS = (
    ("", "維持目前格式"),
    ("2018", "AutoCAD 2018"),
    ("2013", "AutoCAD 2013"),
    ("2010", "AutoCAD 2010"),
    ("2007", "AutoCAD 2007"),
    ("2004", "AutoCAD 2004"),
    ("2000", "AutoCAD 2000"),
    ("R14", "AutoCAD R14"),
)
DWG_SAVE_CODES = {code for code, _label in DWG_SAVE_VERSIONS}

DEFAULT_PRIMARY = "romans.shx"
DEFAULT_BIG = "chineset.shx"

_REG_SUBKEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"
_REG_HIVES = ("HKEY_LOCAL_MACHINE", "HKEY_CURRENT_USER")


class FontContext:
    """What a font name can be matched against. Filenames and typefaces are uppercase."""

    def __init__(self, support_files=(), font_dir_files=(), typefaces=()):
        self.support_files = {str(x).upper() for x in support_files}
        self.font_dir_files = {str(x).upper() for x in font_dir_files}
        self.typefaces = {str(x).upper() for x in typefaces}


def split_typeface_names(value):
    """Split a Fonts-registry value name into uppercase typeface names.

    ``Microsoft JhengHei & Microsoft JhengHei UI (TrueType)`` becomes
    ``MICROSOFT JHENGHEI`` and ``MICROSOFT JHENGHEI UI``. The suffix from the
    last ``(`` is dropped, then the remainder is split on `` & ``.
    """
    if not isinstance(value, str):
        return []
    cut = value.rfind("(")
    if cut >= 0:
        value = value[:cut]
    value = value.strip().upper()
    if not value:
        return []
    parts = []
    while " & " in value:
        left, value = value.split(" & ", 1)
        left = left.strip()
        if left:
            parts.append(left)
        value = value.strip()
    if value:
        parts.append(value)
    return parts


def typefaces_from_value_names(names):
    found = set()
    for name in names:
        found.update(split_typeface_names(name))
    return found


def read_installed_typefaces():
    """Uppercase typeface names from HKLM and HKCU. Empty (not an error) off Windows."""
    if sys.platform != "win32":
        return set()
    import winreg

    hives = {
        "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
        "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    }
    names = []
    for label in _REG_HIVES:
        try:
            with winreg.OpenKey(hives[label], _REG_SUBKEY) as key:
                count = winreg.QueryInfoKey(key)[1]
                for index in range(count):
                    value_name, _data, _kind = winreg.EnumValue(key, index)
                    if value_name:
                        names.append(value_name)
        except OSError:
            continue
    return typefaces_from_value_names(names)


def lookup_filename(name):
    """Font name as a file. No extension means ``.shx`` (``txt`` → ``txt.shx``)."""
    if not isinstance(name, str) or name == "":
        return ""
    base = name.replace("\\", "/").split("/")[-1]
    if "." not in base:
        return name + ".shx"
    return name


def _file_key(name):
    base = name.replace("\\", "/").split("/")[-1]
    return base.upper()


def _extension(name):
    base = name.replace("\\", "/").split("/")[-1]
    if "." not in base:
        return ""
    return "." + base.rsplit(".", 1)[1]


def _stem(name):
    base = name.replace("\\", "/").split("/")[-1]
    if "." not in base:
        return base
    return base.rsplit(".", 1)[0]


def is_missing(name, ctx):
    """True when this style font should be replaced.

    Empty names and names starting with ``@`` are not missing (they are skipped).
    """
    if not isinstance(name, str) or name == "" or name.startswith("@"):
        return False
    probe = lookup_filename(name)
    key = _file_key(probe)
    if key in ctx.support_files or key in ctx.font_dir_files:
        return False
    ext = _extension(probe).lower()
    if ext in TTF_EXTENSIONS and _stem(probe).upper() in ctx.typefaces:
        return False
    return True


def parse_mapping(text):
    """Parse ``old=new`` lines. Later lines win. Blank lines and ``;`` comments are ignored.

    The key is the font name as stored on the style (case-insensitive), not the
    ``.shx`` name added only for the file lookup.
    """
    mapping = {}
    if not text:
        return mapping
    for raw in str(text).splitlines():
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if "=" not in line:
            continue
        old, new = line.split("=", 1)
        old, new = old.strip(), new.strip()
        if old and new:
            mapping[old.upper()] = new
    return mapping


def replacement_for(name, default, mapping):
    """Map override for the stored font name, otherwise ``default``."""
    if isinstance(name, str) and name.upper() in mapping:
        return mapping[name.upper()]
    return default


def plan_style(primary, big, ctx, primary_default=DEFAULT_PRIMARY, big_default=DEFAULT_BIG, mapping=None):
    """Return a change dict, or None when both fonts are installed or skipped."""
    mapping = mapping or {}
    primary = primary if isinstance(primary, str) else ""
    big = big if isinstance(big, str) else ""
    new_primary, new_big = primary, big
    changed = False
    if is_missing(primary, ctx):
        new_primary = replacement_for(primary, primary_default, mapping)
        changed = True
    if is_missing(big, ctx):
        new_big = replacement_for(big, big_default, mapping)
        changed = True
    if not changed:
        return None
    return {
        "old_primary": primary,
        "new_primary": new_primary,
        "old_big": big,
        "new_big": new_big,
    }


def format_change(style, change):
    """One prototype-style line: ``樣式 | old -> new | old -> new``."""
    return (
        f"{style} | {change['old_primary']} -> {change['new_primary']}"
        f" | {change['old_big']} -> {change['new_big']}"
    )


def format_still(style, primary, big):
    return f"{style} | 主={primary} | 大={big}"


def directory_problem(input_dir, output_dir):
    """Return a Traditional Chinese error, or None when the two folders may be used."""
    src_text = str(input_dir or "").strip()
    dst_text = str(output_dir or "").strip()
    if not src_text:
        return "請選擇輸入資料夾"
    if not dst_text:
        return "請指定輸出資料夾"
    src = Path(src_text)
    dst = Path(dst_text)
    if not src.is_dir():
        return "輸入資料夾不存在"
    try:
        src_resolved = src.resolve()
        dst_resolved = dst.resolve()
    except OSError as exc:
        return f"無法讀取資料夾：{exc}"
    if os.path.normcase(str(src_resolved)) == os.path.normcase(str(dst_resolved)):
        return "輸出資料夾不可與輸入資料夾相同"
    try:
        dst_resolved.relative_to(src_resolved)
    except ValueError:
        return None
    return "輸出資料夾不可放在輸入資料夾裡面"


def iter_dwgs(folder, recursive):
    folder = Path(folder)
    walker = folder.rglob("*") if recursive else folder.glob("*")
    found = [p for p in walker if p.is_file() and p.suffix.lower() == ".dwg"]
    return sorted(found, key=lambda p: os.path.normcase(str(p)))


def output_for(src, input_dir, output_dir, recursive):
    src = Path(src)
    if recursive:
        rel = src.resolve().relative_to(Path(input_dir).resolve())
    else:
        rel = Path(src.name)
    return Path(output_dir) / rel


def extra_font_dirs(accore=None):
    """Directories the LISP checks in addition to ``findfile`` and ``%WINDIR%\\Fonts``.

    The AutoCAD ``Fonts`` folder next to accoreconsole is included because a console
    profile does not always list it on the support path. Windows font folders are
    added here too; the LISP also reads ``WINDIR`` and ``LOCALAPPDATA`` itself.
    """
    dirs = []
    windir = os.environ.get("WINDIR") or os.environ.get("SystemRoot")
    if windir:
        dirs.append(str(Path(windir) / "Fonts"))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(str(Path(local) / "Microsoft" / "Windows" / "Fonts"))
    if accore:
        fonts = Path(accore).resolve().parent / "Fonts"
        dirs.append(str(fonts))
    unique = []
    seen = set()
    for item in dirs:
        key = os.path.normcase(str(Path(item)))
        if key not in seen:
            seen.add(key)
            unique.append(str(Path(item)))
    return unique


def lisp_escape(text):
    """Escape a string for an AutoLISP literal. Backslash and quote only."""
    out = []
    for ch in str(text):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch in "\r\n\t":
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def lisp_str(text):
    return '"' + lisp_escape(text) + '"'


def _lisp_parens_balanced(src):
    """Paren balance, ignoring comments and string literals. Used by tests and as a guard."""
    depth = 0
    i = 0
    in_str = False
    while i < len(src):
        ch = src[i]
        if in_str:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_str = False
            i += 1
            continue
        if ch == ";":
            i += 1
            while i < len(src) and src[i] != "\n":
                i += 1
            continue
        if ch == '"':
            in_str = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return False
        i += 1
    return depth == 0 and not in_str


def build_lisp(primary, big, mapping, typefaces, font_dirs):
    """UTF-8 (with BOM) AutoLISP source. The caller writes the bytes."""
    primary = primary or DEFAULT_PRIMARY
    big = big or DEFAULT_BIG
    pairs = []
    for key in sorted(mapping):
        pairs.append(f"({lisp_str(key)} . {lisp_str(mapping[key])})")
    map_literal = "nil" if not pairs else "(list\n  " + "\n  ".join(pairs) + ")"
    faces = "\n  ".join(lisp_str(name) for name in sorted({t.upper() for t in typefaces}))
    face_literal = "nil" if not faces else "(list\n  " + faces + ")"
    dirs = "\n  ".join(lisp_str(str(Path(p)).replace("\\", "/")) for p in font_dirs)
    dir_literal = "nil" if not dirs else "(list\n  " + dirs + ")"
    src = f""";;; fontfix_run.lsp — generated. Save as UTF-8. Do not edit by hand.
;;; AutoLISP `or` returns T, not the successful value. This file does not use `or`
;;; to choose a font path or a font name.

(setq *ff-primary* {lisp_str(primary)})
(setq *ff-big* {lisp_str(big)})
(setq *ff-map* {map_literal})
(setq *ff-faces* {face_literal})
(setq *ff-dirs* {dir_literal})

(defun ff-str (v)
  (if (= (type v) 'STR) v ""))

(defun ff-ext (fn / i)
  (setq fn (ff-str fn) i (strlen fn))
  (while (and (> i 0) (/= (substr fn i 1) "."))
    (setq i (1- i)))
  (if (> i 0) (strcase (substr fn i)) ""))

(defun ff-has-ext-p (fn)
  (/= (ff-ext fn) ""))

(defun ff-base (fn / e)
  (setq e (ff-ext fn))
  (if (= e "") fn (substr fn 1 (- (strlen fn) (strlen e)))))

(defun ff-ttf-p (fn)
  (member (ff-ext fn) '(".TTF" ".TTC" ".OTF")))

(defun ff-probe (fn)
  (if (ff-has-ext-p fn) fn (strcat fn ".shx")))

(defun ff-sep (dir / i n)
  (setq i 1 n (strlen dir))
  (while (and (<= i n) (/= (substr dir i 1) "/"))
    (setq i (1+ i)))
  (if (<= i n) "/" "\\\\"))

(defun ff-join (dir fn / n ch sep)
  (setq n (strlen dir)
        ch (if (> n 0) (substr dir n 1) "")
        sep (ff-sep dir))
  (if (or (= ch "/") (= ch "\\\\"))
    (strcat dir fn)
    (strcat dir sep fn)))

(defun ff-dir-hit-p (dir fn)
  (if (and (= (type dir) 'STR) (/= dir "") (findfile (ff-join dir fn)))
    T
    nil))

(defun ff-dirs-hit-p (fn / dirs hit d)
  (setq dirs *ff-dirs* hit nil)
  (while (and dirs (not hit))
    (setq d (car dirs) dirs (cdr dirs))
    (if (ff-dir-hit-p d fn) (setq hit T)))
  (if (not hit)
    (progn
      (setq d (getenv "WINDIR"))
      (if (= (type d) 'STR) (setq hit (ff-dir-hit-p (strcat d "\\\\Fonts") fn)))))
  (if (not hit)
    (progn
      (setq d (getenv "LOCALAPPDATA"))
      (if (= (type d) 'STR)
        (setq hit (ff-dir-hit-p (strcat d "\\\\Microsoft\\\\Windows\\\\Fonts") fn)))))
  (if hit T nil))

(defun ff-face-p (fn)
  (if (member (strcase (ff-base fn)) *ff-faces*) T nil))

(defun ff-exists-p (fn / probe)
  (setq probe (ff-probe fn))
  (cond
    ((findfile probe) T)
    ((ff-dirs-hit-p probe) T)
    ((and (ff-ttf-p probe) (ff-face-p probe)) T)
    (T nil)))

(defun ff-missing-p (fn)
  (and (= (type fn) 'STR)
       (/= fn "")
       (/= (substr fn 1 1) "@")
       (not (ff-exists-p fn))))

(defun ff-target (fn default / pair)
  (setq pair (assoc (strcase (ff-str fn)) *ff-map*))
  (if pair (cdr pair) default))

(defun ff-dxf (code ed / pair)
  (setq pair (assoc code ed))
  (ff-str (if pair (cdr pair) "")))

(defun ff-put (ed code val / old)
  (setq old (assoc code ed))
  (if old
    (subst (cons code val) old ed)
    (append ed (list (cons code val)))))

(defun ff-ename (rec / en name)
  (setq en (cdr (assoc -1 rec)))
  (if en
    en
    (progn
      (setq name (cdr (assoc 2 rec)))
      (if (= (type name) 'STR) (tblobjname "STYLE" name) nil))))

(defun ff-collect (/ rec names en)
  (setq rec (tblnext "STYLE" T) names nil)
  (while rec
    (setq en (ff-ename rec))
    (if en (setq names (cons en names)))
    (setq rec (tblnext "STYLE")))
  names)

(defun ff-clean (s / i n ch out)
  (setq s (ff-str s) i 1 n (strlen s) out "")
  (while (<= i n)
    (setq ch (substr s i 1))
    (if (or (= ch "|") (= ch (chr 9)) (= ch (chr 10)) (= ch (chr 13)))
      (setq out (strcat out " "))
      (setq out (strcat out ch)))
    (setq i (1+ i)))
  out)

(defun ff-emit (s)
  (princ (strcat "\\n" s "\\n"))
  (princ))

(defun ff-fix-one (en / ed name pf bf newpf newbf changed ned ok)
  (setq ed (entget en)
        name (ff-clean (ff-dxf 2 ed))
        pf (ff-dxf 3 ed)
        bf (ff-dxf 4 ed)
        newpf pf
        newbf bf
        changed nil
        ned ed
        ok nil)
  (if (ff-missing-p pf)
    (progn
      (setq newpf (ff-target pf *ff-primary*))
      (setq ned (ff-put ned 3 newpf))
      (setq changed T)))
  (if (ff-missing-p bf)
    (progn
      (setq newbf (ff-target bf *ff-big*))
      (setq ned (ff-put ned 4 newbf))
      (setq changed T)))
  (if changed
    (progn
      (setq ok (entmod ned))
      (if ok
        (ff-emit (strcat "FONTFIX_REPLACED|" name "|" (ff-clean pf) "|" (ff-clean newpf)
                         "|" (ff-clean bf) "|" (ff-clean newbf)))
        (ff-emit (strcat "FONTFIX_ERROR|" name " entmod failed")))))
  (if ok T nil))

(defun ff-verify (/ rec en ed name pf bf n)
  (setq rec (tblnext "STYLE" T) n 0)
  (while rec
    (setq en (ff-ename rec))
    (setq ed (if en (entget en) rec))
    (setq name (ff-clean (ff-dxf 2 ed))
          pf (ff-dxf 3 ed)
          bf (ff-dxf 4 ed))
    (if (or (ff-missing-p pf) (ff-missing-p bf))
      (progn
        (setq n (1+ n))
        (ff-emit (strcat "FONTFIX_STILL|" name "|" (ff-clean pf) "|" (ff-clean bf)))))
    (setq rec (tblnext "STYLE")))
  n)

(defun ff-fix-current (/ names n en still olderr)
  (setq olderr *error*
        names (ff-collect)
        n 0)
  (defun *error* (msg)
    (ff-emit (strcat "FONTFIX_ERROR|" (ff-clean msg)))
    (ff-emit "FONTFIX_SUMMARY|0|0")
    (setq *error* olderr)
    (princ))
  (foreach en names
    (if (ff-fix-one en) (setq n (1+ n))))
  (setq still (ff-verify))
  (ff-emit (strcat "FONTFIX_SUMMARY|" (itoa n) "|" (itoa still)))
  (setq *error* olderr)
  (princ))

(princ "\\nFONTFIX_READY\\n")
(princ)
"""
    if not _lisp_parens_balanced(src):
        raise RuntimeError("generated font-fix LISP has unbalanced parentheses")
    return src


def lisp_bytes(source):
    """UTF-8 with BOM. AutoCAD 2027 loads LISP as UTF-8 when the BOM is present."""
    if isinstance(source, str):
        source = source.encode("utf-8")
    if source.startswith(b"\xef\xbb\xbf"):
        return source
    return b"\xef\xbb\xbf" + source


def script_bytes(text):
    """accoreconsole reads scripts in the ANSI code page. This script is pure ASCII."""
    raw = text.encode("ascii")
    try:
        encoded = text.encode("mbcs")
    except LookupError:
        return raw
    if encoded.decode("mbcs") != text:
        raise ValueError("font-fix script is not ANSI-safe")
    return encoded


def build_script(jobs, lisp_path):
    """One ASCII script: load the LISP, then OPEN → fix → QSAVE or SAVEAS per drawing.

    Marker strings are split so a script echo cannot be mistaken for progress.
    ``jobs`` items need ``spath`` (forward-slash temp DWG) and, when a version
    keyword is set, ``save_version`` plus ``save_path``.
    """
    lisp_path = str(lisp_path).replace("\\", "/")
    lines = [
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
    for i, job in enumerate(jobs):
        lines.append("_.OPEN")
        lines.append(f'"{job["spath"]}"')
        lines.append(
            f'(princ (strcat "\\nDWGB" "ATCH_OPEN_{i}|" (getvar "DWGNAME") "|\\n"))'
        )
        lines.append(
            '(if (and (boundp \'ff-fix-current) ff-fix-current) '
            '(ff-fix-current) (princ (strcat "\\nFONTFIX_" "ERROR|lisp not loaded\\n")))'
        )
        if job.get("save_version"):
            lines.append("_.SAVEAS")
            lines.append(str(job["save_version"]))
            lines.append(f'"{job["save_path"]}"')
        else:
            lines.append("_.QSAVE")
        lines.append(f'(princ (strcat "\\nDWGB" "ATCH_DONE_{i}_\\n"))')
    lines.append("_.QUIT")
    text = "\n".join(lines) + "\n"
    script_bytes(text)  # fail here if a path is not ASCII
    return text


# Fields are separated by "|": accoreconsole's console turns a tab into spaces, so tabs cannot be parsed.
_REPLACED = __import__("re").compile(
    r"FONTFIX_REPLACED\|([^|\r\n]*)\|([^|\r\n]*)\|([^|\r\n]*)\|([^|\r\n]*)\|([^|\r\n]*)"
)
_STILL = __import__("re").compile(r"FONTFIX_STILL\|([^|\r\n]*)\|([^|\r\n]*)\|([^|\r\n]*)")
_SUMMARY = __import__("re").compile(r"FONTFIX_SUMMARY\|(\d+)\|(\d+)")
_ERROR = __import__("re").compile(r"FONTFIX_ERROR\|([^\r\n]*)")
_READY = __import__("re").compile(r"FONTFIX_READY")


def parse_fontfix_output(text):
    """Parse LISP markers from one drawing's console output."""
    replacements = []
    for match in _REPLACED.finditer(text or ""):
        replacements.append({
            "style": match.group(1),
            "old_primary": match.group(2),
            "new_primary": match.group(3),
            "old_big": match.group(4),
            "new_big": match.group(5),
        })
    still = []
    for match in _STILL.finditer(text or ""):
        still.append({"style": match.group(1), "primary": match.group(2), "big": match.group(3)})
    summary = None
    found = list(_SUMMARY.finditer(text or ""))
    if found:
        last = found[-1]
        summary = (int(last.group(1)), int(last.group(2)))
    errors = [match.group(1).strip() for match in _ERROR.finditer(text or "")]
    return {
        "replacements": replacements,
        "still_missing": still,
        "summary": summary,
        "errors": errors,
        "ready": bool(_READY.search(text or "")),
    }


def replacement_text(replacements):
    parts = []
    for row in replacements:
        parts.append(
            f"{row['style']} | {row['old_primary']} -> {row['new_primary']}"
            f" | {row['old_big']} -> {row['new_big']}"
        )
    return " || ".join(parts)


def still_text(rows):
    return " || ".join(format_still(row["style"], row["primary"], row["big"]) for row in rows)


CSV_FIELDS = ["檔案", "輸出", "狀態", "替換樣式數", "替換明細", "仍缺字", "錯誤", "秒數"]
STATUS_TEXT = {
    "SAVED": "已輸出",
    "FAILED": "失敗",
    "SKIPPED": "略過",
    "STOPPED": "已停止",
}


def csv_row(rec):
    return {
        "檔案": rec.get("file", ""),
        "輸出": rec.get("output", ""),
        "狀態": STATUS_TEXT.get(rec.get("status", ""), rec.get("status", "")),
        "替換樣式數": rec.get("styles_replaced", 0),
        "替換明細": rec.get("replacements", ""),
        "仍缺字": rec.get("still_missing", ""),
        "錯誤": rec.get("error", ""),
        "秒數": rec.get("elapsed_seconds", 0),
    }


def is_dwg_file(path):
    """True when the first six bytes are an ACxxxx header. Does not open the file for write."""
    try:
        with open(path, "rb") as fh:
            raw = fh.read(6)
    except OSError:
        return False
    if len(raw) < 6 or not raw.startswith(b"AC"):
        return False
    tail = raw[2:]
    return len(tail) == 4 and tail.isdigit()

"""Input/output path guards.

The shared ``fontfix.directory_problem`` on this revision refuses the same
folder and an output folder inside the input folder. It does not yet refuse
the opposite nesting. This module calls that check when it can be imported,
and always applies its own both-direction check as well. A publish path that
resolves to a source file is refused even when the two folders are siblings.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


class Cad2RevitError(Exception):
    """Base error for a problem the caller can show to the user."""


class ProtectionError(Cad2RevitError):
    """Raised when a path would read or write a source drawing unsafely."""


class MappingError(Cad2RevitError):
    """Raised when a mapping file does not match the schema."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("\n".join(self.errors))


def _norm_key(path) -> str:
    """Case-folded absolute path, with symlinks resolved."""
    resolved = Path(path).resolve()
    return os.path.normcase(os.path.normpath(str(resolved)))


def _is_inside(child: str, parent: str) -> bool:
    """True when ``child`` is a path strictly inside ``parent``.

    Both arguments are already normalized keys. Comparison uses the platform
    separator, so ``/input`` is not a prefix of ``/input-other``.
    """
    if child == parent:
        return False
    prefix = parent if parent.endswith(os.sep) else parent + os.sep
    return child.startswith(prefix)


def paths_overlap(left, right) -> bool:
    """True when the paths are the same or either one is inside the other."""
    try:
        left_key = _norm_key(left)
        right_key = _norm_key(right)
    except OSError:
        return False
    return (
        left_key == right_key
        or _is_inside(left_key, right_key)
        or _is_inside(right_key, left_key)
    )


def own_directory_problem(input_dir, output_dir):
    """Return a Traditional Chinese error, or None when the folders may be used.

    The same folder, an output inside the input, and an input inside the output
    are all refused. The output folder does not have to exist yet. An existing
    output path that is a file is refused.
    """
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
    if dst.exists() and not dst.is_dir():
        return "輸出路徑必須是資料夾"
    try:
        src_key = _norm_key(src)
        dst_key = _norm_key(dst)
    except OSError as exc:
        return f"無法讀取資料夾：{exc}"
    if src_key == dst_key:
        return "輸出資料夾不可與輸入資料夾相同"
    if _is_inside(dst_key, src_key):
        return "輸出資料夾不可放在輸入資料夾裡面"
    if _is_inside(src_key, dst_key):
        return "輸入路徑不可放在輸出資料夾裡面"
    return None


def shared_directory_problem(input_dir, output_dir):
    """Delegate to ``fontfix.directory_problem`` when that module imports.

    Returns None when the shared helper cannot be loaded, so the local check
    still runs. This does not modify ``fontfix``.
    """
    try:
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        folder = str(scripts)
        if folder not in sys.path:
            sys.path.insert(0, folder)
        import fontfix

        return fontfix.directory_problem(input_dir, output_dir)
    except Exception:
        return None


def directory_problem(input_dir, output_dir):
    """Folder guard used by this package.

    A message from the shared helper is kept when both checks fail, so the
    wording stays aligned with the rest of the tool. A problem that only the
    local check reports — including an input folder inside the output folder
    on revisions whose shared helper does not look that way — is still returned.
    """
    own = own_directory_problem(input_dir, output_dir)
    shared = shared_directory_problem(input_dir, output_dir)
    if shared and own:
        return shared
    return own or shared


def protected_keys(paths) -> set:
    """Normalized identities of every path that must not be overwritten."""
    keys = set()
    for path in paths or ():
        if path is None or str(path).strip() == "":
            continue
        item = Path(path)
        try:
            keys.add(_norm_key(item))
        except OSError:
            keys.add(os.path.normcase(os.path.normpath(str(item.absolute()))))
        try:
            if item.exists() or item.is_symlink():
                keys.add(_norm_key(item.resolve()))
        except OSError:
            pass
    return keys


def output_path_problem(dest, protected: set, input_root=None):
    """Return an error when ``dest`` would land on a source or inside the input.

    ``Path.resolve`` follows a parent symlink, so an output name that does not
    exist yet is still refused when its parent points at a source file or into
    the input tree.
    """
    path = Path(dest)
    try:
        resolved = path.resolve()
        keys = {_norm_key(path), _norm_key(resolved)}
    except OSError as exc:
        return f"無法讀取輸出路徑：{exc}"
    if keys & set(protected):
        return "輸出路徑不可指向原始檔"
    if input_root is not None:
        try:
            root_key = _norm_key(input_root)
        except OSError as exc:
            return f"無法讀取資料夾：{exc}"
        resolved_key = _norm_key(resolved)
        if resolved_key == root_key or _is_inside(resolved_key, root_key):
            return "輸出路徑不可放在輸入資料夾裡面"
    return None


def assert_output_safe(dest, protected: set, input_root=None) -> None:
    """Raise ``ProtectionError`` when ``dest`` is not a safe publish path."""
    problem = output_path_problem(dest, protected, input_root)
    if problem:
        raise ProtectionError(problem)

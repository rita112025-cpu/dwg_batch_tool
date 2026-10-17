"""Read DWG/DWT file format versions from the six-byte ACxxxx header.

This module is intentionally dependency-free and read-only:
- no AutoCAD required
- no registry writes
- no administrator rights
- never modifies drawing/template files
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Iterable

SUPPORTED_EXTENSIONS = {".dwg", ".dwt"}

# DWG format header codes.  The label describes the file format generation,
# not necessarily the AutoCAD release that last saved the file.
# `format` 直接以「ACxxxx→西元年」顯示，讓版本與年份一眼看懂。
DWG_FORMATS = {
    "AC1001": "AC1001→1982",
    "AC1002": "AC1002→1986",
    "AC1003": "AC1003→1987",
    "AC1004": "AC1004→1987",
    "AC1006": "AC1006→1988",
    "AC1009": "AC1009→1990/1992",
    "AC1012": "AC1012→1994",
    "AC1014": "AC1014→1997",
    "AC1015": "AC1015→2000",
    "AC1018": "AC1018→2004",
    "AC1021": "AC1021→2007",
    "AC1024": "AC1024→2010",
    "AC1027": "AC1027→2013",
    "AC1032": "AC1032→2018",
}

CSV_FIELDS = ("file", "extension", "format_code", "format", "status")


def read_version(path: str | Path) -> dict[str, str]:
    """Return version metadata for one .dwg/.dwt file without modifying it."""
    p = Path(path)
    result = {
        "file": str(p),
        "extension": p.suffix.lower(),
        "format_code": "",
        "format": "",
        "status": "",
    }

    if p.suffix.lower() not in SUPPORTED_EXTENSIONS:
        result["status"] = "UNSUPPORTED_EXTENSION"
        return result

    try:
        with p.open("rb") as fh:
            raw = fh.read(6)
    except OSError as exc:
        result["status"] = f"READ_ERROR: {exc}"
        return result

    if len(raw) < 6:
        result["status"] = "TOO_SHORT"
        return result

    try:
        code = raw.decode("ascii")
    except UnicodeDecodeError:
        result["status"] = "INVALID_HEADER"
        return result

    result["format_code"] = code
    if not (code.startswith("AC") and len(code) == 6 and code[2:].isdigit()):
        result["status"] = "INVALID_HEADER"
        return result

    result["format"] = DWG_FORMATS.get(code, "Unknown DWG format")
    result["status"] = "OK" if code in DWG_FORMATS else "UNKNOWN_CODE"
    return result


def iter_files(inputs: Iterable[str | Path], recursive: bool = True) -> list[Path]:
    """Expand files/folders into a stable, de-duplicated DWG/DWT file list."""
    found: dict[str, Path] = {}
    for item in inputs:
        p = Path(item)
        if p.is_file():
            if p.suffix.lower() in SUPPORTED_EXTENSIONS:
                found[str(p.resolve()).lower()] = p
            continue
        if not p.is_dir():
            continue
        pattern = "**/*" if recursive else "*"
        for child in p.glob(pattern):
            if child.is_file() and child.suffix.lower() in SUPPORTED_EXTENSIONS:
                found[str(child.resolve()).lower()] = child
    return sorted(found.values(), key=lambda x: str(x).lower())


def scan_versions(inputs: Iterable[str | Path], recursive: bool = True) -> list[dict[str, str]]:
    """Read version metadata for every supported file below inputs."""
    return [read_version(p) for p in iter_files(inputs, recursive=recursive)]


def write_version_csv(
    inputs: Iterable[str | Path],
    csv_path: str | Path,
    recursive: bool = True,
) -> list[dict[str, str]]:
    """Scan files and write UTF-8-SIG CSV suitable for Excel on Windows."""
    rows = scan_versions(inputs, recursive=recursive)
    out = Path(csv_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows

"""Run a folder (or one drawing) through classify and write the outputs."""

from __future__ import annotations

import os
from pathlib import Path

from cad2revit.classify import classify
from cad2revit.const import DRAWING_SUFFIXES
from cad2revit.convert import convert_dwg
from cad2revit.dxf_read import read_dxf
from cad2revit.export import (
    ELEMENT_COLUMNS,
    MANIFEST_COLUMNS,
    UNMATCHED_COLUMNS,
    build_batch,
    build_document,
    element_row,
    manifest_row,
    unmatched_rows,
    write_csv,
    write_json,
)
from cad2revit.guard import (
    ProtectionError,
    directory_problem,
    output_path_problem,
    protected_keys,
)
from cad2revit.hashing import file_sha256
from cad2revit.mapping import load_mapping


def run(
    input_path,
    output_dir,
    mapping_path,
    *,
    accore="",
    recursive=True,
    timeout=300,
    convert_func=None,
):
    """Classify drawings and write JSON/CSV under ``output_dir``.

    Returns a dict with ``fatal``, ``ok``, ``failed``, and ``files``. ``fatal``
    is set when the folders or the mapping cannot be used; in that case nothing
    is written. Per-file problems are listed in ``files`` and still produce an
    error document so the source hash is on record.
    """
    mapping = load_mapping(mapping_path)
    source_root, sources, problem = _collect(input_path, recursive)
    if problem:
        return _fatal(problem, mapping)
    output = Path(output_dir)
    folder_problem = directory_problem(source_root, output)
    if folder_problem:
        return _fatal(folder_problem, mapping)
    protected = protected_keys([*sources, source_root, mapping_path])
    planned = _shared_outputs(output)
    documents = []
    produced_dxf = []
    for source in sources:
        relative = _relative_name(source, source_root)
        is_dwg = source.suffix.lower() == ".dwg"
        planned.extend(_drawing_outputs(output, relative, is_dwg))
    problem = _first_path_problem(planned, protected, source_root)
    if problem:
        return _fatal(problem, mapping)
    hidden = [source_root, output, mapping_path, accore]
    for source in sources:
        document, dxf_path = _process_one(
            source,
            source_root,
            output,
            mapping,
            accore=accore,
            timeout=timeout,
            convert_func=convert_func,
            protected=protected,
            hidden=hidden,
        )
        if dxf_path is not None:
            produced_dxf.append(dxf_path)
        documents.append(document)
    problem = _first_path_problem(
        planned + produced_dxf, protected, source_root
    )
    if problem:
        for dxf_path in produced_dxf:
            Path(dxf_path).unlink(missing_ok=True)
        return _fatal(problem, mapping)
    return _publish(output, mapping, documents, protected, source_root)


def _collect(input_path, recursive):
    path = Path(input_path)
    if path.is_dir():
        return path, _iter_drawings(path, recursive), None
    if path.is_file():
        if path.suffix.lower() not in DRAWING_SUFFIXES:
            return path.parent, [], "輸入檔必須是 DWG 或 DXF"
        return path.parent, [path], None
    return path, [], "輸入資料夾不存在"


def _iter_drawings(folder, recursive):
    allowed = {suffix.lower() for suffix in DRAWING_SUFFIXES}
    walker = folder.rglob("*") if recursive else folder.glob("*")
    found = [
        item
        for item in walker
        if item.is_file() and item.suffix.lower() in allowed
    ]
    return sorted(found, key=lambda item: os.path.normcase(str(item)))


def _relative_name(path, root) -> str:
    relative = Path(os.path.relpath(path, root))
    if relative.is_absolute() or any(part == ".." for part in relative.parts):
        raise ProtectionError("輸入檔解析後不在輸入資料夾內")
    return relative.as_posix()


def _shared_outputs(output: Path):
    return [
        output / "batch_summary.json",
        output / "csv" / "elements.csv",
        output / "csv" / "manifest.csv",
        output / "csv" / "unmatched.csv",
    ]


def _drawing_outputs(output: Path, relative: str, is_dwg: bool):
    rel = Path(relative)
    paths = [
        output / "json" / rel.with_suffix(".cad2revit.json"),
        output / "csv" / rel.with_suffix(".elements.csv"),
    ]
    if is_dwg:
        paths.append(output / "dxf" / rel.with_suffix(".dxf"))
    return paths


def _first_path_problem(paths, protected, source_root):
    for path in paths:
        problem = output_path_problem(path, protected, source_root)
        if problem:
            return problem
    return None


def _process_one(
    source,
    source_root,
    output,
    mapping,
    *,
    accore,
    timeout,
    convert_func,
    protected,
    hidden,
):
    relative = _relative_name(source, source_root)
    hidden = [*hidden, source, source.resolve()]
    try:
        before = file_sha256(source)
    except OSError as exc:
        source_info = {
            "relative_path": relative,
            "sha256": "",
            "sha256_after": "",
            "unchanged": False,
        }
        document = build_document(
            source_info,
            None,
            None,
            mapping,
            {"method": "unread", "status": "ERROR"},
            status="ERROR",
            error=_scrub(exc.__class__.__name__, hidden),
        )
        return document, None
    is_dwg = source.suffix.lower() == ".dwg"
    dxf_path = source
    produced = None
    if is_dwg:
        produced = output / "dxf" / Path(relative).with_suffix(".dxf")
        rec = convert_dwg(
            source,
            produced,
            accore,
            output,
            timeout=timeout,
            convert_func=convert_func,
        )
        conversion = {
            "method": rec["method"],
            "status": rec["status"],
            "real_accoreconsole_verified": False,
        }
        if rec["status"] != "DXF_VALID" or not rec["source_unchanged"]:
            status = "SOURCE_MODIFIED" if not rec["source_unchanged"] else "CONVERSION_FAILED"
            document = build_document(
                _source_info(relative, rec["source_sha256"], rec["source_sha256_after"], rec["source_unchanged"]),
                None,
                None,
                mapping,
                conversion,
                status=status,
                error=_scrub(rec["error"], hidden),
            )
            return document, None
        dxf_path = produced
        conversion = {
            "method": rec["method"],
            "status": "DXF_VALID",
            "real_accoreconsole_verified": False,
        }
    else:
        conversion = {"method": "direct_dxf", "status": "not_required"}
    try:
        extracted = read_dxf(dxf_path)
        classified = classify(extracted, mapping, before)
    except Exception as exc:
        after = _hash_or(source, before)
        if produced is not None:
            produced.unlink(missing_ok=True)
        document = build_document(
            _source_info(relative, before, after, after == before),
            None,
            None,
            mapping,
            conversion,
            status="SOURCE_MODIFIED" if after != before else "ERROR",
            error=_scrub(f"{type(exc).__name__}: {exc}", hidden),
        )
        return document, None
    after = _hash_or(source, before)
    if after != before:
        if produced is not None:
            produced.unlink(missing_ok=True)
        document = build_document(
            _source_info(relative, before, after, False),
            None,
            None,
            mapping,
            conversion,
            status="SOURCE_MODIFIED",
            error="原始檔雜湊已改變",
        )
        return document, None
    document = build_document(
        _source_info(relative, before, after, True),
        extracted,
        classified,
        mapping,
        conversion,
        status="OK",
        error="",
    )
    return document, produced


def _source_info(relative, before, after, unchanged):
    return {
        "relative_path": relative,
        "sha256": before,
        "sha256_after": after,
        "unchanged": unchanged,
    }


def _hash_or(path, fallback) -> str:
    try:
        return file_sha256(path)
    except OSError:
        return fallback


def _publish(output, mapping, documents, protected, source_root):
    element_rows = []
    unmatched = []
    manifest = []
    summaries = []
    for document in documents:
        relative = document["source"]["relative_path"]
        json_path = output / "json" / Path(relative).with_suffix(".cad2revit.json")
        csv_path = output / "csv" / Path(relative).with_suffix(".elements.csv")
        write_json(json_path, document, protected, source_root)
        rows = [element_row(document, element) for element in document["elements"]]
        write_csv(csv_path, rows, ELEMENT_COLUMNS, protected, source_root)
        element_rows.extend(rows)
        unmatched.extend(unmatched_rows(document))
        manifest.append(manifest_row(document))
        summaries.append(
            {
                "source_relative_path": relative,
                "status": document["status"],
                "error": document.get("error") or "",
                "element_count": document["counts"]["elements"],
                "source_sha256": document["source"]["sha256"],
                "source_unchanged": document["source"]["unchanged"],
            }
        )
    write_csv(output / "csv" / "elements.csv", element_rows, ELEMENT_COLUMNS, protected, source_root)
    write_csv(output / "csv" / "manifest.csv", manifest, MANIFEST_COLUMNS, protected, source_root)
    write_csv(output / "csv" / "unmatched.csv", unmatched, UNMATCHED_COLUMNS, protected, source_root)
    batch = build_batch(mapping, summaries, fatal=None)
    write_json(output / "batch_summary.json", batch, protected, source_root)
    failed = sum(1 for item in summaries if item["status"] != "OK")
    return {
        "fatal": None,
        "ok": len(summaries) - failed,
        "failed": failed,
        "files": summaries,
        "unmatched": unmatched,
    }


def _fatal(message, mapping=None):
    return {
        "fatal": message,
        "ok": 0,
        "failed": 0,
        "files": [],
        "unmatched": [],
        "mapping": mapping,
    }


def _scrub(text, hidden) -> str:
    """Drop absolute paths from a message before it is written to disk."""
    secrets = set()
    for item in hidden:
        if not item:
            continue
        path = Path(item)
        secrets.add(str(path))
        try:
            resolved = path.resolve()
        except OSError:
            continue
        secrets.add(str(resolved))
        for parent in resolved.parents:
            if parent.parent == parent:
                break
            secrets.add(str(parent))
    cleaned = str(text or "")
    for secret in sorted(secrets, key=len, reverse=True):
        if len(secret) > 1 and secret in cleaned:
            cleaned = cleaned.replace(secret, Path(secret).name)
    return cleaned.strip()

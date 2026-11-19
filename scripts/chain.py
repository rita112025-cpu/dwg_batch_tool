"""Chain DWG-writing tasks in one batch: each step continues from the previous step's outputs.

Without chaining every step reads the original input folder, so the second step either
skips (its output already exists) or overwrites the first step's work with a copy made
from the untouched original.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

# Tasks that read DWGs from the input folder and write edited DWGs to the same relative
# path under the output folder. A dry run writes no DWG and is not part of the chain.
CHAIN_KEYS = frozenset({"fontfix", "proxygraphics", "layerclass", "textreplace"})


def _key(path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def is_chain_step(key: str, params: dict) -> bool:
    return key in CHAIN_KEYS and not params.get("dry_run")


def update_carry(carry: dict, results, dst) -> dict:
    """Record which file each output should continue from after a step.

    ``carry`` maps a normalised output path to ``(output Path, file to continue from)``.
    SAVED points at the new output; UNCHANGED keeps the earlier file (or the step's own
    source when the file is new to the chain); FAILED/STOPPED drop the file so later
    steps do not build on an incomplete result.
    """
    dst_root = Path(dst).resolve()
    for rec in results or ():
        out = rec.get("output") or ""
        if not out:
            continue
        out_path = Path(out)
        try:
            out_path.resolve().relative_to(dst_root)
        except ValueError:
            continue
        key = _key(out_path)
        status = rec.get("status")
        if status == "SAVED" and out_path.is_file():
            carry[key] = (out_path, out_path)
        elif status == "UNCHANGED":
            if key not in carry and Path(rec.get("file", "")).is_file():
                carry[key] = (out_path, Path(rec["file"]))
        elif status in ("FAILED", "STOPPED"):
            carry.pop(key, None)
    return carry


def build_stage(carry: dict, dst) -> Path:
    """Copy the files to continue from into ``<dst>/logs/chain_<id>`` at their output paths."""
    dst = Path(dst)
    stage = dst / "logs" / f"chain_{uuid.uuid4().hex[:8]}"
    stage.mkdir(parents=True)
    for out_path, source in carry.values():
        target = stage / out_path.resolve().relative_to(dst.resolve())
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return stage


def remove_stage(stage) -> str:
    """Delete a stage folder. Returns an error message, or ``""``."""
    try:
        shutil.rmtree(stage)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return ""

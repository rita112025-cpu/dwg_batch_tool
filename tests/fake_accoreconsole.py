#!/usr/bin/env python3
"""Stand-in for accoreconsole used by font-fix plumbing tests.

It does not run AutoLISP. It reads the generated script, prints the same UTF-16
markers a real console would, and writes the opened DWG through (QSAVE in place,
or a copy for SAVEAS). A drawing whose bytes contain FONTFIX_TEST_CRASH makes it
emit a blocking MessageBox and exit, so the batch runner's restart path can be
tested without AutoCAD.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CRASH = b"FONTFIX_TEST_CRASH"
EMPTY = b"LAYER_TEST_EMPTY"

_LAYER_DUMP = [
    "LAYERROW|=|PL|=|1|=|LWPOLYLINE|=|4|=|0|=|1",
    "LAYERROW|=|TST|=|2|=|TEXT|=|2|=|0|=|1",
    "LAYERROW|=|H|=|3|=|HATCH|=|1|=|0|=|1",
    "LAYERROW|=|TITLE|=|4|=|INSERT|=|1|=|0|=|1",
    "LAYERLOCK|=|DEFPOINTS|=|skipped",
    "LAYERSUMMARY|=|8|=|4|=|1",
    "LAYERCLASS_READY",
]


def _messages_for(script_text: str) -> list[str]:
    lines = script_text.splitlines()
    layer_mode = "lc-run" in script_text
    messages = [] if layer_mode else ["FONTFIX_READY"]
    pending = ""
    index = 0
    while index < len(lines):
        raw = lines[index].strip()
        upper = raw.upper()
        if upper in {"_.OPEN", "_OPEN"} and index + 1 < len(lines):
            pending = lines[index + 1].strip().strip('"')
            index += 2
            continue
        if "ATCH_OPEN_" in raw:
            found = re.search(r"ATCH_OPEN_(\d+)", raw)
            number = found.group(1) if found else "0"
            name = Path(pending).name if pending else ""
            payload = Path(pending).read_bytes() if pending and Path(pending).is_file() else b""
            messages.append(f"DWGBATCH_OPEN_{number}|{name}|")
            if CRASH in payload:
                stars = "*" * 24
                messages.append(f"{stars}MessageBox{'*' * 20}")
                messages.append("invalid drawing")
                messages.append("*" * 30)
                break
            if layer_mode:
                if EMPTY in payload:
                    messages.append("LAYERSUMMARY|=|0|=|0|=|0")
                    messages.append("LAYERCLASS_READY")
                else:
                    messages.extend(_LAYER_DUMP)
            else:
                sep = "|=|"
                messages.append(f"FONTFIX_REPLACED{sep}Standard{sep}missing.shx{sep}romans.shx{sep}{sep}")
                messages.append(f"FONTFIX_SUMMARY{sep}1{sep}0")
        elif upper in {"_.QSAVE", "_QSAVE"} and pending and Path(pending).is_file():
            path = Path(pending)
            data = path.read_bytes()
            if layer_mode and b"\nLAYERAPPLIED" not in data:
                data += b"\nLAYERAPPLIED"
            path.write_bytes(data)
        elif upper in {"_.SAVEAS", "_SAVEAS"} and index + 2 < len(lines):
            target = lines[index + 2].strip().strip('"')
            if pending and Path(pending).is_file():
                data = Path(pending).read_bytes() + b"\nSAVEAS"
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_bytes(data)
            index += 3
            continue
        elif "ATCH_DONE_" in raw:
            found = re.search(r"ATCH_DONE_(\d+)", raw)
            messages.append(f"DWGBATCH_DONE_{found.group(1) if found else '0'}_")
        index += 1
    return messages


def main():
    args = sys.argv[1:]
    try:
        script = Path(args[args.index("/s") + 1])
    except (ValueError, IndexError):
        sys.stderr.write("missing /s script\n")
        return 2
    text = script.read_text(encoding="utf-8", errors="replace")
    blob = ("\n".join(_messages_for(text)) + "\n").encode("utf-16-le")
    sys.stdout.buffer.write(blob)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())

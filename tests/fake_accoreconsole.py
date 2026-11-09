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

# Fixed dump the text-mode tests match against. The MTEXT body is the wire
# form of {\H2.5;W12 400x200} (one backslash stored as two).
_TEXT_DUMP = [
    "TEXTDUMP|=|Model|=|S-BEAM|=|TEXT|=||=|W12 400x200|=|10.5|=|20.25|=|AB1|=|0",
    "TEXTDUMP|=|Model|=|S-BEAM|=|MTEXT|=||=|{\\\\H2.5;W12 400x200}|=|1.0|=|2.0|=|AB2|=|1",
    "TEXTDUMP|=|配置|=|A-NOTE|=|TEXT|=||=|備註|=|3|=|4|=|AB3|=|0",
    "TEXTDUMP|=|圖塊定義|=|0|=|TEXT|=|TITLE|=|W99 300x100|=|0|=|0|=|BB1|=|0",
    "TEXTDUMP_SUMMARY|=|4",
    "TEXTDUMP_READY",
]


def _is_qsave(raw: str) -> bool:
    folded = raw.upper()
    return folded in {"_.QSAVE", "_QSAVE"} or '(COMMAND "_.QSAVE")' in folded


def _messages_for(script_text: str) -> list[str]:
    lines = script_text.splitlines()
    text_dump = "tx-dump-current" in script_text
    text_apply = "tx-apply-" in script_text
    layer_mode = "lc-run" in script_text
    proxy_mode = "pg-run" in script_text
    messages = [] if text_dump or text_apply or layer_mode or proxy_mode else ["FONTFIX_READY"]
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
            if text_dump:
                messages.extend(_TEXT_DUMP)
            elif text_apply:
                messages.append("TEXTAPPLY|=|AB1|=|ok")
                messages.append("TEXTAPPLY_SUMMARY|=|1|=|0")
            elif layer_mode:
                if EMPTY in payload:
                    messages.append("LAYERSUMMARY|=|0|=|0|=|0")
                    messages.append("LAYERCLASS_READY")
                else:
                    messages.extend(_LAYER_DUMP)
            elif proxy_mode:
                pass  # Proxy markers are emitted by the load/run commands below.
            else:
                sep = "|=|"
                if b"FONTFIX_TEST_CLEAN" in payload:
                    messages.append(f"FONTFIX_SUMMARY{sep}0{sep}0")
                elif b"FONTFIX_TEST_SHAPE" in payload:
                    messages.append(f"FONTFIX_SHAPE{sep}missing_shape_file.shx{sep}AB12{sep}")
                    messages.append(f"FONTFIX_SUMMARY{sep}0{sep}0")
                else:
                    messages.append(f"FONTFIX_REPLACED{sep}Standard{sep}missing.shx{sep}romans.shx{sep}{sep}")
                    messages.append(f"FONTFIX_SUMMARY{sep}1{sep}0")
        elif proxy_mode and raw.startswith('(load '):
            messages.append("PROXYGRAPHICS_READY")
        elif proxy_mode and "(pg-run)" in raw:
            current = Path(pending).read_bytes() if pending and Path(pending).is_file() else b""
            if b"\nPROXYGRAPHICS=1" in current:
                messages.append("PROXYGRAPHICS_RESULT|=|1|=|1")
            else:
                messages.append("PROXYGRAPHICS_RESULT|=|0|=|1")
        elif _is_qsave(raw) and pending and Path(pending).is_file():
            path = Path(pending)
            data = path.read_bytes()
            folded = raw.upper()
            unchanged_font = "*FF-CHANGED*" in folded and (
                b"FONTFIX_TEST_CLEAN" in data or b"FONTFIX_TEST_SHAPE" in data
            )
            already_proxy = "*PG-BEFORE*" in folded and b"\nPROXYGRAPHICS=1" in data
            if not unchanged_font and not already_proxy:
                if text_apply and b"\nTEXTAPPLIED" not in data:
                    data += b"\nTEXTAPPLIED"
                if layer_mode and b"\nLAYERAPPLIED" not in data:
                    data += b"\nLAYERAPPLIED"
                if proxy_mode and b"\nPROXYGRAPHICS=1" not in data:
                    data += b"\nPROXYGRAPHICS=1"
                elif proxy_mode:
                    data += b"\nFORCEDSAVE"
                if not (text_dump or text_apply or layer_mode or proxy_mode):
                    data += b"\nQSAVED"
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

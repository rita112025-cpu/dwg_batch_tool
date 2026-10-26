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


def _messages_for(script_text: str) -> list[str]:
    lines = script_text.splitlines()
    text_dump = "tx-dump-current" in script_text
    text_apply = "tx-apply-" in script_text
    messages = [] if text_dump or text_apply else ["FONTFIX_READY"]
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
            else:
                sep = "|=|"
                messages.append(f"FONTFIX_REPLACED{sep}Standard{sep}missing.shx{sep}romans.shx{sep}{sep}")
                messages.append(f"FONTFIX_SUMMARY{sep}1{sep}0")
        elif upper in {"_.QSAVE", "_QSAVE"} and pending and Path(pending).is_file():
            path = Path(pending)
            data = path.read_bytes()
            if text_apply and b"\nTEXTAPPLIED" not in data:
                data += b"\nTEXTAPPLIED"
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

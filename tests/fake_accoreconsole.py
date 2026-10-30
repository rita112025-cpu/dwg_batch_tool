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
TB_NOREADY = b"TB_TEST_NOREADY"

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


# Fixed dump the title-block tests read: a matching TITLE-A1, a dynamic block
# whose effective name is 圖框A3 (one multi-line attribute), and a DOOR.
_TB_DUMP = [
    "TBINSERT|=|Layout1|=|TITLE-A1|=|TITLE-A1|=|1A0",
    "TBATTR|=|1A0|=|DWG_NO|=|001|=|1A1|=|0",
    "TBATTR|=|1A0|=|TITLE|=|一樓平面圖|=|1A2|=|0",
    "TBATTR|=|1A0|=|DATE|=|2026/01/05|=|1A3|=|0",
    "TBINSERT|=|Model|=|*U12|=|圖框A3|=|1E5",
    "TBATTR|=|1E5|=|DWG_NO|=|A-002|=|2B1|=|0",
    "TBATTR|=|1E5|=|NOTE|=|多行|=|2B2|=|1",
    "TBINSERT|=|Model|=|DOOR|=|DOOR|=|3C0",
    "TBATTR|=|3C0|=|D1|=|門|=|3C1|=|0",
    "TBDUMP_SUMMARY|=|3",
]


def _tb_apply_rows(script_text: str, number: str) -> list[str]:
    """Echo ``ok`` for each handle in ``tb-apply-<number>``; handle DEAD reports missing."""
    found = re.search(r'\(load "([^"]+)"\)', script_text)
    if not found or not Path(found.group(1)).is_file():
        return []
    lisp = Path(found.group(1)).read_text(encoding="utf-8-sig")
    start = lisp.find(f"(defun tb-apply-{number} ()")
    end = lisp.find("\n(defun ", start + 1)
    text = "" if start < 0 else lisp[start:end if end > 0 else len(lisp)]
    rows, ok, fail = [], 0, 0
    for handle in re.findall(r'\(list "([^"]*)" ', text):
        status = "missing" if handle == "DEAD" else "ok"
        ok, fail = (ok + 1, fail) if status == "ok" else (ok, fail + 1)
        rows.append(f"TBAPPLY|=|{handle}|=|{status}")
    rows.append(f"TBAPPLY_SUMMARY|=|{ok}|=|{fail}")
    return rows


def _messages_for(script_text: str) -> list[str]:
    lines = script_text.splitlines()
    text_dump = "tx-dump-current" in script_text
    text_apply = "tx-apply-" in script_text
    layer_mode = "lc-run" in script_text
    tb_dump = "tb-dump-current" in script_text
    tb_apply = "tb-apply-" in script_text
    messages = [] if text_dump or text_apply or layer_mode or tb_dump or tb_apply else ["FONTFIX_READY"]
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
            if tb_dump:
                messages.extend(_TB_DUMP)
                if TB_NOREADY not in payload:
                    messages.append("TBDUMP_READY")
            elif tb_apply:
                # The function called on the next script line, as AutoCAD would run it.
                called = re.search(r"tb-apply-(\d+)", lines[index + 1]) if index + 1 < len(lines) else None
                messages.extend(_tb_apply_rows(script_text, called.group(1) if called else number))
            elif text_dump:
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
            else:
                sep = "|=|"
                messages.append(f"FONTFIX_REPLACED{sep}Standard{sep}missing.shx{sep}romans.shx{sep}{sep}")
                messages.append(f"FONTFIX_SUMMARY{sep}1{sep}0")
        elif upper in {"_.QSAVE", "_QSAVE"} and pending and Path(pending).is_file():
            path = Path(pending)
            data = path.read_bytes()
            if text_apply and b"\nTEXTAPPLIED" not in data:
                data += b"\nTEXTAPPLIED"
            if tb_apply and b"\nTBAPPLIED" not in data:
                data += b"\nTBAPPLIED"
            if layer_mode and b"\nLAYERAPPLIED" not in data:
                data += b"\nLAYERAPPLIED"
            path.write_bytes(data)
        elif upper in {"_.SAVEAS", "_SAVEAS"} and index + 3 < len(lines) and lines[index + 1].strip().upper() == "DXF":
            # convert_batch form: _SAVEAS / DXF / <Enter> / "target". Test "DWGs" hold DXF text,
            # so the bytes are written through unchanged.
            target = lines[index + 3].strip().strip('"')
            if pending and Path(pending).is_file():
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_bytes(Path(pending).read_bytes())
            index += 4
            continue
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

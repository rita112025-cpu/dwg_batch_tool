"""Command line for the Phase 1 classifier.

Examples use placeholder folder names. Pass ``--accore`` only when a DWG must
be converted; DXF files are read directly and do not need AutoCAD.
"""

from __future__ import annotations

import argparse
import sys

from cad2revit import __version__
from cad2revit.guard import Cad2RevitError, MappingError, ProtectionError
from cad2revit.mapping import load_mapping
from cad2revit.run import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cad2revit",
        description="把 DWG/DXF 分類成日後可交給 Revit 放置步驟的 JSON 與 CSV。這個階段不放置元件。",
    )
    parser.add_argument("--version", action="version", version=f"cad2revit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-mapping", help="檢查對照表是否符合 schema")
    validate.add_argument("mapping", help="對照表 JSON")
    validate.set_defaults(func=_validate)

    convert = sub.add_parser("run", help="讀圖並寫出分類結果")
    convert.add_argument("--input", required=True, help="DWG/DXF 檔或資料夾")
    convert.add_argument("--output", required=True, help="輸出資料夾（不可與輸入互相包含）")
    convert.add_argument("--mapping", required=True, help="對照表 JSON")
    convert.add_argument("--accore", default="", help="accoreconsole.exe；只有 DWG 需要")
    convert.add_argument("--timeout", type=int, default=300, help="單張 DWG 轉換秒數")
    convert.add_argument(
        "--recursive",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="包含子資料夾（預設開啟）",
    )
    convert.set_defaults(func=_run)
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except MappingError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except ProtectionError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except Cad2RevitError as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _validate(args) -> int:
    mapping = load_mapping(args.mapping)
    print(f"對照表有效：{mapping['file_name']}（{len(mapping['layer_rules'])} 條圖層、{len(mapping['block_rules'])} 條圖塊）")
    return 0


def _run(args) -> int:
    result = run(
        args.input,
        args.output,
        args.mapping,
        accore=args.accore,
        recursive=args.recursive,
        timeout=args.timeout,
    )
    if result["fatal"]:
        print(result["fatal"], file=sys.stderr)
        return 2
    layers = sorted({row["name"] for row in result["unmatched"] if row["kind"] == "layer"})
    blocks = sorted({row["name"] for row in result["unmatched"] if row["kind"] == "block"})
    print(
        f"完成：成功 {result['ok']}、失敗 {result['failed']}、"
        f"未對上圖層 {len(layers)}、未對上圖塊 {len(blocks)}"
    )
    if layers:
        print("未對上圖層：" + "、".join(layers))
    if blocks:
        print("未對上圖塊：" + "、".join(blocks))
    for item in result["files"]:
        if item["status"] != "OK":
            print(f"{item['source_relative_path']}: {item['status']} {item['error']}", file=sys.stderr)
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Command-line entry point for mdlinkcheck."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .core import check_markdown_file, collect_markdown_files, format_issue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mdlinkcheck",
        description="Check local relative links in Markdown files.",
    )
    parser.add_argument("paths", nargs="+", metavar="PATH", help="Markdown file or directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = [Path(value) for value in args.paths]

    missing = [path for path in paths if not path.exists()]
    if missing:
        for path in missing:
            print(f"mdlinkcheck: path does not exist: {path}", file=sys.stderr)
        return 2

    try:
        markdown_files = collect_markdown_files(paths)
        issues = [
            issue
            for markdown_file in markdown_files
            for issue in check_markdown_file(markdown_file)
        ]
    except (OSError, UnicodeError) as error:
        print(f"mdlinkcheck: {error}", file=sys.stderr)
        return 2

    for issue in issues:
        print(format_issue(issue))
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Core Markdown link checking logic."""

from __future__ import annotations

import re
import string
import unicodedata
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, Literal, Sequence
from urllib.parse import unquote

ErrorType = Literal["missing-file", "missing-anchor"]

_LINK_RE = re.compile(r"!?\[[^\]\n]*\]\(([^)\n]+)\)")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_HEADING_RE = re.compile(r"^ {0,3}#{1,6}[ \t]+(.+?)[ \t]*$")
_EXTERNAL_PREFIXES = ("http://", "https://", "mailto:")


@dataclass(frozen=True)
class Issue:
    source: Path
    line: int
    column: int
    error_type: ErrorType
    target: str


def collect_markdown_files(paths: Sequence[Path]) -> list[Path]:
    """Return unique Markdown files in deterministic order."""
    found: dict[str, Path] = {}
    for path in paths:
        candidates = path.rglob("*.md") if path.is_dir() else (path,)
        for candidate in candidates:
            if candidate.is_file() and candidate.suffix.lower() == ".md":
                key = str(candidate.resolve()).casefold()
                found.setdefault(key, candidate)
    return sorted(found.values(), key=lambda item: str(item).casefold())


def github_slug(heading: str) -> str:
    """Convert heading text to the GitHub-style slug required by this tool."""
    lowered = heading.strip().lower()
    without_punctuation = "".join(
        character
        for character in lowered
        if character in "-_"
        or (
            character not in string.punctuation
            and not unicodedata.category(character).startswith("P")
        )
    )
    return re.sub(r"\s", "-", without_punctuation)


def _mask_inline_code(line: str) -> str:
    characters = list(line)
    index = 0
    while index < len(line):
        if line[index] != "`":
            index += 1
            continue
        run_end = index
        while run_end < len(line) and line[run_end] == "`":
            run_end += 1
        marker = line[index:run_end]
        close = line.find(marker, run_end)
        if close == -1:
            index = run_end
            continue
        for position in range(index, close + len(marker)):
            characters[position] = " "
        index = close + len(marker)
    return "".join(characters)


def _content_lines(text: str) -> Iterator[tuple[int, str]]:
    fence_character: str | None = None
    fence_length = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if fence_character is not None:
            closing = re.match(
                rf"^ {{0,3}}{re.escape(fence_character)}{{{fence_length},}}[ \t]*$", line
            )
            if closing:
                fence_character = None
                fence_length = 0
            continue

        opening = _FENCE_RE.match(line)
        if opening:
            marker = opening.group(1)
            fence_character = marker[0]
            fence_length = len(marker)
            continue
        yield line_number, _mask_inline_code(line)


def _heading_anchors(markdown_file: Path) -> set[str]:
    text = markdown_file.read_text(encoding="utf-8")
    occurrences: Counter[str] = Counter()
    anchors: set[str] = set()
    for _, line in _content_lines(text):
        match = _HEADING_RE.match(line)
        if not match:
            continue
        heading = re.sub(r"[ \t]+#+[ \t]*$", "", match.group(1))
        base = github_slug(heading)
        duplicate_index = occurrences[base]
        occurrences[base] += 1
        anchors.add(base if duplicate_index == 0 else f"{base}-{duplicate_index}")
    return anchors


def _split_target(raw_target: str) -> tuple[str, str | None]:
    target = raw_target.strip()
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1]
    path_part, separator, anchor = target.partition("#")
    decoded_path = unquote(path_part)
    decoded_anchor = unquote(anchor) if separator else None
    return decoded_path, decoded_anchor


def check_markdown_file(markdown_file: Path) -> list[Issue]:
    """Check one Markdown file and return issues in source order."""
    text = markdown_file.read_text(encoding="utf-8")
    issues: list[Issue] = []
    anchor_cache: dict[Path, set[str]] = {}

    for line_number, line in _content_lines(text):
        for match in _LINK_RE.finditer(line):
            raw_target = match.group(1).strip()
            if raw_target.lower().startswith(_EXTERNAL_PREFIXES):
                continue

            path_part, anchor = _split_target(raw_target)
            destination = markdown_file if not path_part else markdown_file.parent / path_part
            if not destination.exists():
                issues.append(
                    Issue(markdown_file, line_number, match.start() + 1, "missing-file", raw_target)
                )
                continue

            if anchor is not None:
                cache_key = destination.resolve()
                if cache_key not in anchor_cache:
                    anchor_cache[cache_key] = _heading_anchors(destination)
                if anchor not in anchor_cache[cache_key]:
                    issues.append(
                        Issue(
                            markdown_file,
                            line_number,
                            match.start() + 1,
                            "missing-anchor",
                            raw_target,
                        )
                    )
    return issues


def format_issue(issue: Issue) -> str:
    return (
        f"{issue.source}:{issue.line}:{issue.column}: "
        f"{issue.error_type} {issue.target}"
    )

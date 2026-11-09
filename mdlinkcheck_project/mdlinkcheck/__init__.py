"""Check local links in Markdown files."""

from .core import Issue, check_markdown_file, collect_markdown_files, github_slug

__all__ = ["Issue", "check_markdown_file", "collect_markdown_files", "github_slug"]
__version__ = "0.1.0"

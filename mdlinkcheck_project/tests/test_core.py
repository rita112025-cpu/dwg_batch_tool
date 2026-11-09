from __future__ import annotations

import unittest
from pathlib import Path

from mdlinkcheck.core import check_markdown_file, collect_markdown_files, github_slug


FIXTURES = Path(__file__).parent / "fixtures"


class CoreTests(unittest.TestCase):
    def test_fixture_errors_match_exactly(self) -> None:
        source = FIXTURES / "check.md"
        actual = [
            (issue.line, issue.column, issue.error_type, issue.target)
            for issue in check_markdown_file(source)
        ]
        self.assertEqual(
            actual,
            [
                (3, 1, "missing-file", "targets/missing.md"),
                (7, 1, "missing-anchor", "targets/headings.md#repeat-2"),
                (9, 1, "missing-anchor", "#absent"),
            ],
        )

    def test_external_and_code_links_are_ignored(self) -> None:
        issues = check_markdown_file(FIXTURES / "check.md")
        targets = {issue.target for issue in issues}
        self.assertNotIn("ignored-inline.md", targets)
        self.assertNotIn("ignored-fence.md", targets)
        self.assertNotIn("ignored-tilde.md", targets)
        self.assertFalse(any(target.startswith("http") for target in targets))
        self.assertFalse(any(target.startswith("mailto:") for target in targets))

    def test_spaces_percent_encoding_images_and_same_file_anchor(self) -> None:
        issues = check_markdown_file(FIXTURES / "check.md")
        targets = {issue.target for issue in issues}
        self.assertNotIn("targets/with space.md", targets)
        self.assertNotIn("targets/with%20space.md", targets)
        self.assertNotIn("assets/pixel.png", targets)
        self.assertNotIn("#local-section", targets)

    def test_slug_rules(self) -> None:
        self.assertEqual(github_slug("Hello, World!"), "hello-world")
        self.assertEqual(github_slug("Keep_it - Fine"), "keep_it---fine")
        self.assertEqual(github_slug("C++ Guide"), "c-guide")

    def test_collects_directories_recursively_and_deduplicates(self) -> None:
        files = collect_markdown_files([FIXTURES, FIXTURES / "check.md"])
        self.assertEqual(len(files), len({path.resolve() for path in files}))
        self.assertIn(FIXTURES / "targets" / "headings.md", files)

    def test_tilde_fence_and_variable_inline_backticks(self) -> None:
        source = Path(__file__).parent / "cases" / "code.md"
        issues = check_markdown_file(source)
        self.assertEqual([issue.target for issue in issues], ["missing-c.md"])


if __name__ == "__main__":
    unittest.main()

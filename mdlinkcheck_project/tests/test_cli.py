from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "mdlinkcheck", *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class CliTests(unittest.TestCase):
    def test_exit_code_zero(self) -> None:
        source = ROOT / "tests" / "cases" / "valid.md"
        result = run_cli(str(source))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")

    def test_exit_code_one_and_exact_fixture_output(self) -> None:
        result = run_cli(str(FIXTURES))
        source = FIXTURES / "check.md"
        expected = "\n".join(
            [
                f"{source}:3:1: missing-file targets/missing.md",
                f"{source}:7:1: missing-anchor targets/headings.md#repeat-2",
                f"{source}:9:1: missing-anchor #absent",
                "",
            ]
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(result.stdout, expected)

    def test_exit_code_two_for_missing_path(self) -> None:
        missing = FIXTURES / "does-not-exist"
        result = run_cli(str(missing))
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("path does not exist", result.stderr)

    def test_exit_code_two_for_missing_arguments(self) -> None:
        result = run_cli()
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()

"""Failure paths shared by the accoreconsole batch runners (fake console, no AutoCAD)."""
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import fontfix_batch  # noqa: E402
import layerbatch  # noqa: E402
import textbatch  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"
HEADER = b"AC1032" + b"\x00" * 32

RUNNERS = {
    "fontfix": lambda src, dst, accore, **kw: fontfix_batch.run(src, dst, accore, **kw),
    "layer": lambda src, dst, accore, **kw: layerbatch.run(
        src, dst, accore, type_map="TEXT=TXT\nLWPOLYLINE=PL", **kw),
    "layer_dry": lambda src, dst, accore, **kw: layerbatch.run(
        src, dst, accore, type_map="TEXT=TXT\nLWPOLYLINE=PL", dry_run=True, **kw),
    "extract": lambda src, dst, accore, **kw: textbatch.run_extract(
        src, dst, accore, pattern="W12", mode="contains", **kw),
    "replace": lambda src, dst, accore, **kw: textbatch.run_replace(
        src, dst, accore, find=r"^(W\d+)\s.*$", repl=r"\1", mode="regex", **kw),
    "replace_dry": lambda src, dst, accore, **kw: textbatch.run_replace(
        src, dst, accore, find=r"^(W\d+)\s.*$", repl=r"\1", mode="regex", dry_run=True, **kw),
}
WRITERS = ("fontfix", "layer", "replace")


def _dwg(path, extra=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEADER + extra)


def _console(tmp_path, body):
    """A stand-in console that misbehaves the way ``body`` says."""
    script = tmp_path / "bad_console.py"
    script.write_text("import sys, time\n" + body + "\n", encoding="utf-8")
    return script


def _failed_or_fatal(summary):
    return summary["fatal"] or summary["failed"] or summary.get("stopped")


@pytest.fixture
def two_drawings(tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    _dwg(src / "子" / "b.dwg")
    return src


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_missing_console_is_fatal(name, two_drawings, tmp_path):
    summary = RUNNERS[name](two_drawings, tmp_path / "out", tmp_path / "nope.exe", timeout=5)
    assert summary["fatal"]


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_output_inside_input_is_fatal(name, two_drawings):
    summary = RUNNERS[name](two_drawings, two_drawings / "out", FAKE, timeout=5)
    assert summary["fatal"]


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_output_folder_that_is_a_file_is_fatal(name, two_drawings, tmp_path):
    blocker = tmp_path / "out"
    blocker.write_text("x")
    summary = RUNNERS[name](two_drawings, blocker, FAKE, timeout=5)
    assert summary["fatal"]


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_empty_input_folder(name, tmp_path):
    (tmp_path / "in").mkdir()
    summary = RUNNERS[name](tmp_path / "in", tmp_path / "out", FAKE, timeout=5)
    assert summary["fatal"] == "" and summary["dwg"] == 0


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_stop_before_start(name, two_drawings, tmp_path):
    stop = threading.Event()
    stop.set()
    summary = RUNNERS[name](two_drawings, tmp_path / "out", FAKE, timeout=5, stop_event=stop)
    assert summary["stopped"]
    assert summary["ok"] == 0


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_crashing_drawing_fails_alone(name, workers, tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg", extra=b"FONTFIX_TEST_CRASH")
    _dwg(src / "b.dwg")
    _dwg(src / "c.dwg")
    summary = RUNNERS[name](src, tmp_path / "out", FAKE, timeout=30, workers=workers)
    assert summary["fatal"] == ""
    assert summary["failed"] == 1
    assert summary["ok"] == 2


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_console_exiting_without_output_fails_files(name, two_drawings, tmp_path):
    console = _console(tmp_path, "sys.exit(3)")
    summary = RUNNERS[name](two_drawings, tmp_path / "out", console, timeout=30)
    assert _failed_or_fatal(summary)
    assert summary["ok"] == 0


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_silent_console_times_out(name, tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    console = _console(tmp_path, "time.sleep(30)")
    summary = RUNNERS[name](src, tmp_path / "out", console, timeout=1)
    assert _failed_or_fatal(summary)
    assert summary["ok"] == 0


@pytest.mark.parametrize("name", WRITERS)
def test_existing_output_is_skipped_unless_overwrite(name, tmp_path):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    dst = tmp_path / "out"
    _dwg(dst / "a.dwg", extra=b"OLD")
    summary = RUNNERS[name](src, dst, FAKE, timeout=30)
    assert summary["skipped"] == 1
    assert (dst / "a.dwg").read_bytes().endswith(b"OLD")
    again = RUNNERS[name](src, dst, FAKE, timeout=30, overwrite=True)
    assert again["skipped"] == 0 and again["ok"] == 1


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_flat_output_without_subfolders(name, two_drawings, tmp_path):
    summary = RUNNERS[name](two_drawings, tmp_path / "out", FAKE, timeout=30, recursive=False)
    assert summary["fatal"] == ""
    assert summary["dwg"] == 1


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_log_and_progress_callbacks(name, two_drawings, tmp_path):
    lines, steps = [], []
    summary = RUNNERS[name](
        two_drawings, tmp_path / "out", FAKE, timeout=30,
        log=lambda level, message: lines.append(level),
        progress=lambda i, n, path: steps.append((i, n)),
    )
    assert summary["fatal"] == ""
    assert lines and steps and steps[-1][1] == 2

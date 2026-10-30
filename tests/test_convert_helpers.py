"""convert_single helpers and the synthetic multi-format demo (no AutoCAD)."""
import errno
import json
import os
import sys
import threading
from pathlib import Path

import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import convert_single  # noqa: E402
import demo_multiformat  # noqa: E402


def test_validate_dxf_states(tmp_path):
    assert convert_single.validate_dxf(tmp_path / "none.dxf") == ("DXF_INVALID", "file missing")
    empty = tmp_path / "empty.dxf"
    empty.write_bytes(b"")
    assert convert_single.validate_dxf(empty) == ("DXF_INVALID", "0 bytes")
    broken = tmp_path / "broken.dxf"
    broken.write_text("not a dxf")
    assert convert_single.validate_dxf(broken)[0] == "DXF_INVALID"
    good = tmp_path / "good.dxf"
    ezdxf.new().saveas(good)
    assert convert_single.validate_dxf(good) == ("DXF_VALID", "")


def test_publish_dxf_falls_back_to_copy_across_volumes(tmp_path, monkeypatch):
    source = tmp_path / "a.tmp"
    source.write_text("data")
    target = tmp_path / "b.dxf"
    real_replace = os.replace
    calls = []

    def replace(src, dst):
        calls.append(src)
        if len(calls) == 1:
            raise OSError(errno.EXDEV, "cross-device")
        return real_replace(src, dst)

    monkeypatch.setattr(convert_single.os, "replace", replace)
    convert_single.publish_dxf(source, target)
    assert target.read_text() == "data" and not source.exists()


def test_publish_dxf_reraises_other_errors(tmp_path, monkeypatch):
    def replace(src, dst):
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(convert_single.os, "replace", replace)
    try:
        convert_single.publish_dxf(tmp_path / "a", tmp_path / "b")
    except OSError as exc:
        assert exc.errno == errno.EACCES
    else:
        raise AssertionError("expected OSError")


def test_run_accore_finishes_times_out_and_stops():
    quick = [sys.executable, "-c", "print('hi')"]
    rc, out, _, why = convert_single.run_accore(quick, timeout=30)
    assert rc == 0 and b"hi" in out and why == ""
    slow = [sys.executable, "-c", "import time; time.sleep(30)"]
    assert convert_single.run_accore(slow, timeout=1)[3] == "timeout"
    stop = threading.Event()
    stop.set()
    assert convert_single.run_accore(slow, timeout=30, stop_event=stop)[3] == "stopped"


def test_terminate_falls_back_to_kill():
    class Stubborn:
        pid = 999999

        def terminate(self):
            raise OSError("no")

        def kill(self):
            self.killed = True

    proc = Stubborn()
    convert_single._terminate(proc)
    assert proc.killed


def test_safe_tmpdir_is_script_safe(tmp_path):
    folder = convert_single.safe_tmpdir(tmp_path / "logs 中文")
    assert convert_single._script_safe(folder)


def test_demo_multiformat_runs_end_to_end(tmp_path, monkeypatch):
    # No capsys here: pymupdf keeps the stdout it sees on first import.
    out = tmp_path / "demo"
    monkeypatch.setattr(sys, "argv", ["demo_multiformat.py", "--output", str(out)])
    demo_multiformat.main()
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["source_hashes_unchanged"] is True
    assert (out / "output").is_dir()

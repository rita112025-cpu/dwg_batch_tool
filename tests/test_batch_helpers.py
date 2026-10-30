"""Per-file result helpers of the batch runners, fed hand-made job dicts."""
import runpy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import common  # noqa: E402
import convert_single  # noqa: E402
import fontfix_batch  # noqa: E402
import layerbatch  # noqa: E402
import textbatch  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"
HEADER = b"AC1032" + b"\x00" * 32


def _job(tmp_path, **extra):
    src = tmp_path / "src.dwg"
    src.write_bytes(HEADER)
    produced = tmp_path / "tmp.dwg"
    job = {
        "rec": {"status": "", "error": ""},
        "src": src,
        "spath": str(produced),
        "produced": produced,
        "tmp_in": produced,
        "dest": tmp_path / "out" / "dest.dwg",
        "sha_before": convert_single.sha256(src),
    }
    job.update(extra)
    return job


# ---- publish (identical in the three runners) ----

@pytest.mark.parametrize("publish", [fontfix_batch._publish_job, layerbatch._publish, textbatch._publish])
def test_publish_rejects_missing_and_non_dwg_output(publish, tmp_path):
    job = _job(tmp_path)
    assert publish(job) == (False, "沒有產生 DWG")
    job["produced"].write_bytes(b"NOT A DWG AT ALL")
    ok, err = publish(job)
    assert not ok and "ACxxxx" in err


@pytest.mark.parametrize("publish", [fontfix_batch._publish_job, layerbatch._publish, textbatch._publish])
def test_publish_moves_the_drawing(publish, tmp_path):
    job = _job(tmp_path)
    job["produced"].write_bytes(HEADER)
    assert publish(job) == (True, "")
    assert job["dest"].read_bytes() == HEADER


def test_copy_unchanged(tmp_path):
    job = _job(tmp_path)
    job["tmp_in"].write_bytes(HEADER)
    assert textbatch._copy_unchanged(job) == (True, "")
    job["tmp_in"].write_bytes(b"garbage")
    assert textbatch._copy_unchanged(job)[0] is False


# ---- fontfix ----

def test_fontfix_finish_job_failures(tmp_path):
    wrong = fontfix_batch._finish_job(_job(tmp_path, opened_name="other.dwg"), True)
    assert wrong["status"] == "FAILED" and "other.dwg" in wrong["error"]
    no_lisp = fontfix_batch._finish_job(_job(tmp_path), False)
    assert "LISP" in no_lisp["error"]
    nothing = fontfix_batch._finish_job(_job(tmp_path, console="noise"), True)
    assert nothing["error"] == "沒有收到修復結果"
    errors = fontfix_batch._finish_job(_job(tmp_path, console="FONTFIX_ERROR|=|bad style\n"), True)
    assert errors["status"] == "FAILED"


def test_fontfix_finish_job_detects_changed_source(tmp_path):
    console = "FONTFIX_REPLACED|=|S|=|a.shx|=|romans.shx|=||=|\nFONTFIX_SUMMARY|=|1|=|0\n"
    job = _job(tmp_path, console=console)
    job["produced"].write_bytes(HEADER)
    job["src"].write_bytes(HEADER + b"edited")
    rec = fontfix_batch._finish_job(job, True)
    assert rec["status"] == "FAILED" and "改變" in rec["error"]
    job = _job(tmp_path, console=console)
    job["src"].unlink()
    rec = fontfix_batch._finish_job(job, True)
    assert "無法再讀取來源" in rec["error"]


def test_fontfix_cli(tmp_path, capsys):
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.dwg").write_bytes(HEADER)
    mapping = tmp_path / "map.txt"
    mapping.write_text("old.shx=new.shx\n", encoding="utf-8")
    code = fontfix_batch.main([str(src), str(tmp_path / "out"), "--accore", str(FAKE),
                               "--map", str(mapping), "--no-recursive"])
    assert code == 0
    assert "成功 1" in capsys.readouterr().out


# ---- layer classification ----

def test_layer_accept_failures(tmp_path):
    cases = [
        ({"launch_error": "boom"}, True, "boom"),
        ({"stopped_here": True}, True, "已停止"),
        ({}, False, "沒有收到分圖層結果"),
        ({"console": "x", "opened_name": "other.dwg"}, True, "other.dwg"),
        ({"console": "x"}, False, "LISP"),
        ({"console": "noise"}, True, "沒有收到分圖層結果"),
    ]
    for extra, ready, expect in cases:
        job = _job(tmp_path, **extra)
        assert layerbatch._accept(job, ready) is None
        assert expect in job["rec"]["error"]


def test_layer_accept_reports_lisp_errors(tmp_path):
    console = "LAYER_ERROR|=|cannot move\nLAYERSUMMARY|=|3|=|1|=|0\n"
    job = _job(tmp_path, console=console)
    parsed = layerbatch._accept(job, True)
    assert parsed is not None and job["rec"]["status"] == "FAILED"


def test_layer_per_file_csv_outside_root(tmp_path):
    path = layerbatch._per_file_csv(tmp_path / "logs", tmp_path / "x" / "a.dwg", tmp_path / "other")
    assert path.name == "a.csv"


# ---- text ----

def test_text_accept_dump_failures(tmp_path):
    cases = [
        ({"launch_error": "boom"}, True, "boom"),
        ({"stopped_here": True}, True, "已停止"),
        ({}, False, "沒有收到文字清單"),
        ({"console": "x", "opened_name": "other.dwg"}, True, "other.dwg"),
        ({"console": "x"}, False, "LISP"),
        ({"console": "noise"}, True, "沒有收到文字清單"),
    ]
    for extra, ready, expect in cases:
        job = _job(tmp_path, **extra)
        assert textbatch._accept_dump(job, ready) is False
        assert expect in job["rec"]["error"]


def test_text_accept_dump_keeps_rows_with_errors(tmp_path):
    console = ("TEXT_ERROR|=|odd entity\n"
               "TEXTDUMP|=|Model|=|0|=|TEXT|=||=|W12 1|=|0|=|0|=|A1|=|0\nTEXTDUMP_SUMMARY|=|1\n")
    job = _job(tmp_path, console=console)
    assert textbatch._accept_dump(job, True) is True
    assert job["rows"] and "odd entity" in job["rec"]["error"]


def test_text_plan_changes_reports_bad_rows():
    rows = [{"kind": "TEXT", "raw": "W12 1", "layer": "0", "handle": "A1"}]
    changes, notes = textbatch._plan_changes(rows, "W12", "W13", "literal", False, None)
    assert len(changes) == 1 and changes[0]["new"]
    changes, _ = textbatch._plan_changes(rows, "ZZZ", "W13", "literal", False, 1)
    assert changes == []


# ---- summary script ----

def test_summary_script_prints_counts(tmp_path, monkeypatch, capsys):
    out = tmp_path / "output"
    (out / "csv").mkdir(parents=True)
    (out / "csv" / "file_index.csv").write_text("a\n1\n2\n", encoding="utf-8-sig")
    monkeypatch.setattr(common, "load_config", lambda path=None: {"output_dir": str(out)})
    runpy.run_path(str(Path(common.__file__).with_name("summary.py")), run_name="__main__")
    (out / "logs").mkdir()
    (out / "logs" / "errors.log").write_text("x\ny\n", encoding="utf-8")
    runpy.run_path(str(Path(common.__file__).with_name("summary.py")), run_name="__main__")
    text = capsys.readouterr().out
    assert "file_index.csv: 2 rows" in text
    assert "errors.log lines: 0" in text and "errors.log lines: 2" in text

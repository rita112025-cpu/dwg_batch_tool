"""Overlap, stop reports, incremental console reads, and temp cleanup. No AutoCAD."""
import csv
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import batch_convert  # noqa: E402
import convert_batch  # noqa: E402
import convert_single  # noqa: E402
import dwgdiff  # noqa: E402
import fontfix  # noqa: E402
import fontfix_batch  # noqa: E402
import layerbatch  # noqa: E402
import layerstd  # noqa: E402
import pipeline  # noqa: E402
import proxybatch  # noqa: E402
import textbatch  # noqa: E402
import titleblock_batch  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"
HEADER = b"AC1032" + b"\x00" * 32
STANDARD = ROOT / "samples" / "layer_standard.csv"

RUNNERS = {
    "fontfix": lambda src, dst, **kw: fontfix_batch.run(src, dst, FAKE, **kw),
    "layer": lambda src, dst, **kw: layerbatch.run(
        src, dst, FAKE, type_map="TEXT=TXT\nLWPOLYLINE=PL", **kw),
    "extract": lambda src, dst, **kw: textbatch.run_extract(
        src, dst, FAKE, pattern="W12", mode="contains",
        **{key: value for key, value in kw.items() if key != "overwrite"}),
    "replace": lambda src, dst, **kw: textbatch.run_replace(
        src, dst, FAKE, find="W", repl="X", mode="literal", **kw),
    "proxy": lambda src, dst, **kw: proxybatch.run(src, dst, FAKE, **kw),
    "titleblock": lambda src, dst, **kw: titleblock_batch.run_read(
        src, dst, FAKE, patterns="TITLE",
        **{key: value for key, value in kw.items() if key != "overwrite"}),
}


def _dwg(path, extra=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEADER + extra)


def _nested_sources(tmp_path):
    """Input ``o/X`` sits inside output ``o``. ``o/X/X/a.dwg`` would publish onto ``o/X/a.dwg``."""
    outer = tmp_path / "o"
    inner = outer / "X"
    top = inner / "a.dwg"
    nested = inner / "X" / "a.dwg"
    _dwg(top, b"TOP")
    _dwg(nested, b"NESTED")
    return outer, inner, top, nested


def test_output_for_maps_nested_input_onto_another_source(tmp_path):
    _outer, inner, top, nested = _nested_sources(tmp_path)
    dest = fontfix.output_for(nested, inner, inner.parent, True)
    assert dest == top
    assert fontfix.output_hits_source(dest, [top, nested])
    assert "輸入" in fontfix.directory_problem(inner, inner.parent)


@pytest.mark.parametrize("name", sorted(RUNNERS))
def test_input_inside_output_is_refused_and_sources_stay(name, tmp_path):
    _outer, inner, top, nested = _nested_sources(tmp_path)
    before = {top: top.read_bytes(), nested: nested.read_bytes()}
    summary = RUNNERS[name](inner, inner.parent, timeout=5, overwrite=True, recursive=True)
    assert summary["fatal"]
    assert "輸入" in summary["fatal"]
    assert top.read_bytes() == before[top]
    assert nested.read_bytes() == before[nested]


def test_layerstd_and_diff_and_convert_refuse_nested_folders(tmp_path):
    outer, inner, top, nested = _nested_sources(tmp_path)
    before = top.read_bytes()
    checked = layerstd.run(inner, outer, STANDARD, str(FAKE), recursive=True, overwrite=True)
    assert checked["fatal"] and "輸入" in checked["fatal"]
    assert top.read_bytes() == before

    old = inner / "old"
    new = inner / "new"
    old.mkdir()
    new.mkdir()
    diff = dwgdiff.run(old, new, outer)
    assert diff["fatal"] and "輸入" in diff["fatal"]
    assert not (outer / "dwgdiff").exists()

    cfg = {
        "input_dir": str(inner),
        "output_dir": str(outer),
        "accoreconsole": str(FAKE),
        "recursive": True,
        "overwrite": True,
    }
    stats = batch_convert.run(cfg)
    assert stats["fatal"] and "輸入" in stats["fatal"]
    assert not (outer / "dxf").exists()
    assert nested.read_bytes().endswith(b"NESTED")

    piped = pipeline.run_pipeline(dict(cfg), {"convert": False, "analyze": False, "scada": False})
    assert "輸入" in piped["fatal"]
    contained = pipeline.run_pipeline(
        dict(cfg, input_dir=str(outer), output_dir=str(inner / "out")),
        {"convert": False, "analyze": False, "scada": False},
    )
    assert "輸出" in contained["fatal"]


def test_multiformat_refuses_both_directions(tmp_path, engineering_cfg):
    outer = tmp_path / "o"
    inner = outer / "X"
    inner.mkdir(parents=True)
    with pytest.raises(ValueError):
        pipeline.run_multiformat(dict(engineering_cfg, input_dir=str(inner), output_dir=str(outer)))
    with pytest.raises(ValueError):
        pipeline.run_multiformat(dict(engineering_cfg, input_dir=str(outer), output_dir=str(inner / "out")))


def test_titleblock_prepare_refuses_a_destination_that_is_a_source(tmp_path):
    src = tmp_path / "a.dwg"
    src.write_bytes(HEADER)
    logs = tmp_path / "logs"
    logs.mkdir()
    jobs, temps = titleblock_batch._prepare([src], logs, [src], overwrite=True)
    assert jobs[0]["skip"] is True
    assert jobs[0]["rec"]["status"] == "FAILED"
    assert "原始" in jobs[0]["rec"]["error"]
    assert src.read_bytes() == HEADER
    assert temps == []


def test_convert_refuses_a_destination_that_is_a_source(tmp_path):
    src = tmp_path / "a.dwg"
    src.write_bytes(HEADER + b"KEEP")
    other = tmp_path / "b.dwg"
    other.write_bytes(HEADER + b"OTHER")
    out = tmp_path / "out"
    stop = threading.Event()
    stop.set()
    results = convert_batch.convert_many(
        [(src, src), (other, out / "b.dxf")],
        FAKE,
        {"output_dir": str(out)},
        overwrite=True,
        stop_event=stop,
    )
    by_name = {}
    for rec in results:
        by_name.setdefault(Path(rec["source"]).name, rec["status"])
    assert by_name["a.dwg"] == "ERROR"
    assert src.read_bytes() == HEADER + b"KEEP"
    assert "原始" in next(rec["error"] for rec in results if Path(rec["source"]).name == "a.dwg")


def test_stop_report_includes_skips_and_precheck_errors(tmp_path):
    kept = tmp_path / "a.dwg"
    missing = tmp_path / "gone.dwg"
    pending = tmp_path / "b.dwg"
    kept.write_bytes(HEADER)
    pending.write_bytes(HEADER)
    out = tmp_path / "out"
    existing = out / "dxf" / "a.dxf"
    existing.parent.mkdir(parents=True)
    existing.write_text("SENTINEL", encoding="utf-8")
    stop = threading.Event()
    stop.set()
    seen = []
    results = convert_batch.convert_many(
        [(kept, existing), (missing, out / "dxf" / "gone.dxf"), (pending, out / "dxf" / "b.dxf")],
        FAKE,
        {"output_dir": str(out)},
        overwrite=False,
        stop_event=stop,
        on_result=seen.append,
    )
    reported = {Path(rec["source"]).name: rec["status"] for rec in seen}
    assert reported["a.dwg"] == "SKIPPED_EXISTS"
    assert reported["gone.dwg"] == "ERROR"
    assert "b.dwg" not in reported
    assert [rec["status"] for rec in results if rec["status"] == "STOPPED"] == ["STOPPED"]
    assert existing.read_text(encoding="utf-8") == "SENTINEL"
    assert not (out / "dxf" / "b.dxf").exists()

    src = tmp_path / "batch"
    src.mkdir()
    (src / "a.dwg").write_bytes(HEADER)
    (src / "b.dwg").write_bytes(HEADER)
    batch_out = tmp_path / "batch-out"
    ready = batch_out / "dxf" / "a.dxf"
    ready.parent.mkdir(parents=True)
    ready.write_text("SENTINEL", encoding="utf-8")
    stats = batch_convert.run({
        "input_dir": str(src),
        "output_dir": str(batch_out),
        "accoreconsole": str(FAKE),
        "recursive": False,
        "timeout_seconds": 30,
        "overwrite": False,
    }, stop_event=stop)
    assert stats["stopped"] is True
    assert stats["skipped"] == 1
    csv_path = batch_out / "logs" / "batch_results.csv"
    jsonl_path = batch_out / "logs" / "batch_results.jsonl"
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8-sig")))
    lines = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines()]
    assert [row["status"] for row in rows] == ["SKIPPED_EXISTS"]
    assert [line["status"] for line in lines] == ["SKIPPED_EXISTS"]
    assert len(rows) == len(lines) == stats["skipped"]
    assert ready.read_text(encoding="utf-8") == "SENTINEL"


def test_fontfix_stop_still_writes_skipped_rows(tmp_path, monkeypatch):
    src = tmp_path / "in"
    _dwg(src / "a.dwg")
    _dwg(src / "b.dwg")
    dst = tmp_path / "out"
    _dwg(dst / "a.dwg", b"KEEP")
    monkeypatch.setattr(fontfix, "read_installed_typefaces", lambda: [])
    monkeypatch.setattr(fontfix_batch, "_run_launch", lambda *args, **kwargs: (0, True, "", ""))
    summary = fontfix_batch.run(src, dst, FAKE, timeout=5, recursive=False)
    assert summary["stopped"] is True
    assert summary["skipped"] == 1
    text = (dst / "logs" / "fontfix_results.csv").read_text(encoding="utf-8-sig")
    assert "略過" in text
    assert "已停止" in text
    payload = [
        json.loads(line)
        for line in (dst / "logs" / "fontfix_results.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert {row["status"] for row in payload} == {"SKIPPED", "STOPPED"}
    assert (dst / "a.dwg").read_bytes().endswith(b"KEEP")


def test_incremental_decode_matches_full_decode():
    text = ("noise " * 80) + "DWGBATCH_OPEN_2|圖.dwg|\n" + "\U0001F4D0" + "DWGBATCH_DONE_2_"
    raw = text.encode("utf-16-le")
    live = convert_batch._LiveOutput()
    buf = bytearray()
    for index in range(0, len(raw), 3):
        buf.extend(raw[index:index + 3])
        assert live.pull(buf) == convert_batch._decode(buf)
    assert live.opens[-1][2:] == (2, "圖.dwg")
    assert live.dones[-1][2] == 2
    assert live._dec.decoded_bytes == len(raw)
    assert live._resume >= len(live.text) - len("DWGBATCH_")


def test_marker_split_across_reads_is_found_once():
    raw = "prefix DWGBATCH_OPEN_1|ab|\nDWGBATCH_DONE_1_".encode("utf-16-le")
    live = convert_batch._LiveOutput()
    buf = bytearray(raw[:5])
    assert live.pull(buf) == convert_batch._decode(buf)
    assert live.opens == [] and live.dones == []
    buf.extend(raw[5:])
    live.pull(buf)
    assert [item[2] for item in live.opens] == [1]
    assert [item[2] for item in live.dones] == [1]
    live.pull(buf)
    assert len(live.opens) == 1 and len(live.dones) == 1


def test_safe_tmpdir_never_uses_a_public_folder(tmp_path, monkeypatch):
    public = tmp_path / "public"
    public.mkdir()
    spaced = tmp_path / "user temp"
    spaced.mkdir()
    monkeypatch.setenv("PUBLIC", str(public))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(convert_single.tempfile, "gettempdir", lambda: str(spaced))
    monkeypatch.setattr(convert_single, "_short_path", lambda path: str(path))
    with pytest.raises(OSError):
        convert_single.safe_tmpdir(tmp_path / "logs 中文")
    assert list(public.rglob("*")) == []


def test_safe_tmpdir_uses_the_per_user_temp(tmp_path, monkeypatch):
    public = tmp_path / "public"
    public.mkdir()
    user = tmp_path / "usertemp"
    monkeypatch.setenv("PUBLIC", str(public))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(convert_single.tempfile, "gettempdir", lambda: str(user))
    folder = convert_single.safe_tmpdir(tmp_path / "logs 中文")
    assert folder.is_relative_to(user)
    assert convert_single._script_safe(folder)
    assert list(public.rglob("*")) == []


def test_stale_per_user_temp_is_removed(tmp_path, monkeypatch):
    user = tmp_path / "usertemp"
    root = user / "dwg_batch_tool"
    old = root / "old"
    old.mkdir(parents=True)
    (old / "copy.dwg").write_bytes(b"stale")
    fresh = root / "fresh"
    fresh.mkdir()
    stale = time.time() - (convert_single._STALE_TEMP_SECONDS + 120)
    os.utime(old, (stale, stale))
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(convert_single.tempfile, "gettempdir", lambda: str(user))
    convert_single.safe_tmpdir(tmp_path / "logs 中文")
    assert not old.exists()
    assert fresh.exists()


def test_cleanup_runtime_stops_children_and_deletes_copies(tmp_path):
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    copy = tmp_path / "copy.dwg"
    copy.write_bytes(b"copy")
    convert_single.track_process(proc)
    convert_single.track_temp(copy)
    try:
        convert_single.cleanup_runtime()
        assert proc.poll() is not None
        assert not copy.exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

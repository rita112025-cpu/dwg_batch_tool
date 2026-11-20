"""A failed reconvert must keep the last good DXF, say so, and keep it out of the follow-on analysis."""
import sys
from pathlib import Path

import ezdxf

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import analyze_dxf  # noqa: E402
import batch_convert  # noqa: E402
import convert_batch  # noqa: E402
import convert_single  # noqa: E402
import find_scada  # noqa: E402
import pipeline  # noqa: E402
from common import load_config  # noqa: E402

HEADER = b"AC1032" + b"\x00" * 32


def _cfg(tmp_path):
    inp, out = tmp_path / "in", tmp_path / "out"
    inp.mkdir(parents=True, exist_ok=True)
    return dict(load_config(), input_dir=str(inp), output_dir=str(out), overwrite=True)


def _old_dxf(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = ezdxf.new()
    doc.modelspace().add_text("OLD_RESULT")
    doc.saveas(path)
    return path.read_bytes()


def test_install_dxf_keeps_existing_output_when_new_one_is_invalid(tmp_path):
    dxf = tmp_path / "a.dxf"
    old = _old_dxf(dxf)
    tmp = tmp_path / "new.dxf"
    tmp.write_text("not a dxf")
    status, _, _ = convert_single.install_dxf(tmp, dxf)
    assert status == "DXF_INVALID"
    assert dxf.read_bytes() == old
    assert not tmp.exists()
    assert not [p for p in tmp_path.iterdir() if p.name not in ("a.dxf",)]


def test_install_dxf_replaces_output_when_new_one_is_valid(tmp_path):
    dxf = tmp_path / "a.dxf"
    old = _old_dxf(dxf)
    tmp = tmp_path / "new.dxf"
    ezdxf.new().saveas(tmp)
    status, detail, size = convert_single.install_dxf(tmp, dxf)
    assert (status, detail) == ("DXF_VALID", "")
    assert dxf.read_bytes() != old and size == dxf.stat().st_size
    assert not tmp.exists()


def test_batch_timeout_on_reconvert_keeps_old_dxf_and_says_so(tmp_path, monkeypatch):
    """Smoke case D: convert once, then overwrite with a forced timeout."""
    cfg = _cfg(tmp_path)
    dwg = Path(cfg["input_dir"]) / "t_large.dwg"
    dwg.write_bytes(HEADER)
    dxf = Path(cfg["output_dir"]) / "dxf" / "t_large.dxf"
    old = _old_dxf(dxf)
    monkeypatch.setattr(convert_batch, "_run_launch",
                        lambda jobs, *a, **k: (0, False, "timeout after 0.3s", -1))
    [rec] = convert_batch.convert_many([(dwg, dxf)], __file__, cfg, timeout=0.3, overwrite=True)
    assert rec["status"] == "DXF_INVALID"
    assert "timeout after 0.3s" in rec["error"]
    assert convert_single.RETAINED_NOTE in rec["error"]
    assert dxf.read_bytes() == old


def test_batch_failure_without_previous_dxf_has_no_retained_note(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    dwg = Path(cfg["input_dir"]) / "new.dwg"
    dwg.write_bytes(HEADER)
    dxf = Path(cfg["output_dir"]) / "dxf" / "new.dxf"
    monkeypatch.setattr(convert_batch, "_run_launch",
                        lambda jobs, *a, **k: (0, False, "timeout after 0.3s", -1))
    [rec] = convert_batch.convert_many([(dwg, dxf)], __file__, cfg, timeout=0.3, overwrite=True)
    assert rec["status"] == "DXF_INVALID"
    assert convert_single.RETAINED_NOTE not in rec["error"]
    assert not dxf.exists()


def test_single_timeout_on_reconvert_keeps_old_dxf_and_says_so(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    dwg = Path(cfg["input_dir"]) / "t_large.dwg"
    dwg.write_bytes(HEADER)
    dxf = Path(cfg["output_dir"]) / "dxf" / "t_large.dxf"
    old = _old_dxf(dxf)
    monkeypatch.setattr(convert_single, "run_accore", lambda cmd, timeout, stop_event=None: (-1, b"", b"", "timeout"))
    rec = convert_single.convert(dwg, dxf, sys.executable, cfg, timeout=2, overwrite=True)
    assert rec["status"] == "DXF_INVALID"
    assert "timeout after 2s" in rec["error"] and convert_single.RETAINED_NOTE in rec["error"]
    assert dxf.read_bytes() == old


def _pipeline_env(monkeypatch, results):
    """Stub out AutoCAD: env check passes, batch_convert.run returns ``results``."""
    monkeypatch.setattr(pipeline.env_check, "check", lambda cfg: dict(ok=True, lines=[], autocad="x", ezdxf="y"))

    def fake_run(cfg, log=None, progress=None, stop_event=None):
        ok = sum(r["status"] in ("DXF_VALID", "SKIPPED_EXISTS") for r in results)
        return dict(total=len(results), ok=ok, failed=len(results) - ok, skipped=0, stopped=False,
                    fatal="", results=results, failed_files=[])

    monkeypatch.setattr(pipeline.batch_convert, "run", fake_run)
    seen = {}
    real_analyze, real_search = analyze_dxf.run, find_scada.run

    def spy_analyze(cfg, *a, files=None, **k):
        seen["analyze"] = files
        return real_analyze(cfg, *a, files=files, **k)

    def spy_search(cfg, *a, files=None, **k):
        seen["search"] = files
        return real_search(cfg, *a, files=files, **k)

    monkeypatch.setattr(pipeline.analyze_dxf, "run", spy_analyze)
    monkeypatch.setattr(pipeline.find_scada, "run", spy_search)
    return seen


def _rec(dwg, dxf, status, error=""):
    return dict(source=str(dwg), output=str(dxf), status=status, return_code=0, file_size=0,
                elapsed_seconds=0.0, error=error)


def test_pipeline_analyzes_only_this_runs_successful_dxf(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    dxf_root = Path(cfg["output_dir"]) / "dxf"
    good, stale = dxf_root / "good.dxf", dxf_root / "stale.dxf"
    _old_dxf(good)
    _old_dxf(stale)
    seen = _pipeline_env(monkeypatch, [
        _rec("good.dwg", good, "DXF_VALID"),
        _rec("stale.dwg", stale, "DXF_INVALID", "timeout after 0.3s; " + convert_single.RETAINED_NOTE),
    ])
    logs = []
    summary = pipeline.run_pipeline(cfg, dict(convert=True, analyze=True, scada=True),
                                    log=lambda lv, m: logs.append(m))
    assert seen["analyze"] == [good] and seen["search"] == [good]
    index = (Path(cfg["output_dir"]) / "csv" / "file_index.csv").read_text(encoding="utf-8-sig")
    assert "good.dxf" in index and "stale.dxf" not in index
    assert summary["failed"] == 1


def test_pipeline_skips_analysis_when_nothing_converted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    stale = Path(cfg["output_dir"]) / "dxf" / "stale.dxf"
    _old_dxf(stale)
    seen = _pipeline_env(monkeypatch, [
        _rec("stale.dwg", stale, "DXF_INVALID", "timeout after 0.3s; " + convert_single.RETAINED_NOTE),
    ])
    logs = []
    pipeline.run_pipeline(cfg, dict(convert=True, analyze=True, scada=True),
                          log=lambda lv, m: logs.append(m))
    assert "analyze" not in seen and "search" not in seen
    assert any("略過分析" in m for m in logs)


def _cli_convert(monkeypatch, cfg, results):
    """Run batch_convert.main with a stubbed conversion that returns ``results``."""
    ok = sum(r["status"] in batch_convert.OK_STATUS for r in results)
    monkeypatch.setattr(batch_convert, "run", lambda c, *a, **k: dict(
        total=len(results), ok=ok, failed=len(results) - ok, skipped=0, stopped=False, fatal="",
        results=results, failed_files=[]))
    return batch_convert.main(cfg)


def test_cli_analysis_reads_only_this_runs_successful_dxf(tmp_path, monkeypatch):
    """run.bat: batch_convert.py then analyze_dxf.py / find_scada.py --this-run."""
    cfg = dict(_cfg(tmp_path), accoreconsole=str(tmp_path / "accore.exe"))
    (Path(cfg["input_dir"]) / "good.dwg").write_bytes(HEADER)
    (Path(cfg["input_dir"]) / "stale.dwg").write_bytes(HEADER)
    dxf_root = Path(cfg["output_dir"]) / "dxf"
    good, stale = dxf_root / "good.dxf", dxf_root / "stale.dxf"
    _old_dxf(good)
    _old_dxf(stale)
    _cli_convert(monkeypatch, cfg, [_rec("good.dwg", good, "DXF_VALID"),
                                    _rec("stale.dwg", stale, "DXF_INVALID", convert_single.RETAINED_NOTE)])
    assert analyze_dxf.main(cfg, ["--this-run"]) == 0
    index = (Path(cfg["output_dir"]) / "csv" / "file_index.csv").read_text(encoding="utf-8-sig")
    assert "good.dxf" in index and "stale.dxf" not in index
    seen = []
    real = find_scada.run
    monkeypatch.setattr(find_scada, "run", lambda c, *a, files=None, **k: seen.append(files) or real(c, *a, files=files, **k))
    assert find_scada.main(cfg, ["--this-run"]) == 0
    assert seen == [[good]]


def test_cli_analysis_skips_when_this_run_converted_nothing(tmp_path, monkeypatch, capsys):
    cfg = dict(_cfg(tmp_path), accoreconsole=str(tmp_path / "accore.exe"))
    (Path(cfg["input_dir"]) / "stale.dwg").write_bytes(HEADER)
    stale = Path(cfg["output_dir"]) / "dxf" / "stale.dxf"
    _old_dxf(stale)
    _cli_convert(monkeypatch, cfg, [_rec("stale.dwg", stale, "DXF_INVALID", convert_single.RETAINED_NOTE)])
    assert analyze_dxf.main(cfg, ["--this-run"]) == 0
    assert find_scada.main(cfg, ["--this-run"]) == 0
    assert "略過分析" in capsys.readouterr().out
    assert not (Path(cfg["output_dir"]) / "csv" / "file_index.csv").exists()


def test_cli_this_run_without_a_conversion_record_fails(tmp_path):
    cfg = _cfg(tmp_path)
    assert analyze_dxf.main(cfg, ["--this-run"]) == 3
    assert find_scada.main(cfg, ["--this-run"]) == 3


def test_cli_stale_record_is_cleared_when_a_new_conversion_starts(tmp_path, monkeypatch):
    """A stopped / crashed run must not leave the previous run's list for --this-run to pick up."""
    cfg = dict(_cfg(tmp_path), accoreconsole=str(tmp_path / "accore.exe"))
    from common import read_this_run, write_this_run
    write_this_run(cfg, [str(Path(cfg["output_dir"]) / "dxf" / "old.dxf")])

    def boom(*a, **k):
        assert read_this_run(cfg) is None
        raise KeyboardInterrupt

    monkeypatch.setattr(batch_convert, "run", boom)
    assert batch_convert.main(cfg) == 130
    assert read_this_run(cfg) is None


def test_analyze_only_still_scans_the_whole_folder(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    dxf_root = Path(cfg["output_dir"]) / "dxf"
    _old_dxf(dxf_root / "a.dxf")
    _old_dxf(dxf_root / "sub" / "b.dxf")
    seen = _pipeline_env(monkeypatch, [])
    pipeline.run_pipeline(cfg, dict(convert=False, analyze=True, scada=False))
    assert seen["analyze"] is None
    index = (Path(cfg["output_dir"]) / "csv" / "file_index.csv").read_text(encoding="utf-8-sig")
    assert "a.dxf" in index and "b.dxf" in index

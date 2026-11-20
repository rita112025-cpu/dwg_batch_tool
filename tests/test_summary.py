"""summary.py --this-run must not present the previous run's CSV counts as this run's result."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import common  # noqa: E402
import summary  # noqa: E402


def _cfg(tmp_path):
    return dict(common.load_config(), output_dir=str(tmp_path / "out"))


def _old_csv(cfg):
    csv_dir = Path(cfg["output_dir"]) / "csv"
    csv_dir.mkdir(parents=True)
    (csv_dir / "file_index.csv").write_text("a\n1\n2\n", encoding="utf-8-sig")


def test_without_flag_prints_counts_as_before(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _old_csv(cfg)
    assert summary.main(cfg, []) == 0
    out = capsys.readouterr().out
    assert "file_index.csv: 2 rows" in out and "本次未執行分析" not in out


def test_this_run_with_converted_dxf_prints_counts_normally(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _old_csv(cfg)
    common.write_this_run(cfg, [Path(cfg["output_dir"]) / "dxf" / "a.dxf"])
    summary.main(cfg, ["--this-run"])
    out = capsys.readouterr().out
    assert "file_index.csv: 2 rows" in out and "本次未執行分析" not in out and "上次" not in out


def test_this_run_with_nothing_converted_labels_counts_as_previous(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _old_csv(cfg)
    common.write_this_run(cfg, [])
    summary.main(cfg, ["--this-run"])
    lines = capsys.readouterr().out.splitlines()
    assert "本次未執行分析" in lines[0]
    assert any("file_index.csv: 2 rows" in ln and "上次結果" in ln for ln in lines)


def test_this_run_without_conversion_record_says_so(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    _old_csv(cfg)
    summary.main(cfg, ["--this-run"])
    out = capsys.readouterr().out
    assert "本次未執行分析" in out and "上次結果" in out

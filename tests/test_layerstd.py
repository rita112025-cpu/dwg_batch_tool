"""Layer standard check: standard loading, checks on live ezdxf drawings, reports and run()."""
import csv
import sys
import threading
from pathlib import Path

import ezdxf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import layerstd  # noqa: E402

SAMPLES = ROOT / "samples"


def _std(text, tmp_path):
    path = tmp_path / "std.csv"
    path.write_text(text, encoding="utf-8")
    return path


def _drawing(path):
    doc = ezdxf.new()
    doc.layers.add("A-WALL", color=2, linetype="Continuous", lineweight=13)
    doc.layers.add("a-door-x", color=3)
    doc.layers.add("TEMP1", color=1)
    doc.layers.add("EMPTY_ONE", color=1)
    noplot = doc.layers.add("X-NOPLOT", color=8)
    noplot.dxf.plot = 1
    msp = doc.modelspace()
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "A-WALL"})
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "0"})
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "a-door-x"})
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "TEMP1"})
    msp.add_line((0, 0), (1, 1), dxfattribs={"layer": "X-NOPLOT"})
    block = doc.blocks.new("B1")
    block.add_line((0, 0), (1, 1), dxfattribs={"layer": "0"})
    doc.saveas(path)
    return path


def test_sample_files_match_and_load(tmp_path):
    assert (SAMPLES / "layer_standard.csv").read_text(encoding="utf-8-sig") == layerstd.SAMPLE_STANDARD
    std = layerstd.load_standard(SAMPLES / "layer_standard.csv")
    assert std.forbidden == ["TEMP*"] and len(std.rules) == 9
    std_json = layerstd.load_standard(SAMPLES / "layer_standard.json")
    wall = std_json.rules[2]
    assert wall.colors == {1, 7} and wall.lineweights == {35, 50} and wall.plot is True and wall.required
    assert std_json.rules[1].plot is False
    assert std_json.rules[4].colors == set(range(1, 10)) and std_json.rules[4].lineweights == {-3}
    out = layerstd.write_sample(tmp_path / "s.csv")
    assert out.read_text(encoding="utf-8-sig") == layerstd.SAMPLE_STANDARD


def test_value_parsers():
    assert layerstd.parse_bool("") is None and layerstd.parse_bool("是") is True
    assert layerstd.parse_bool("off") is False
    with pytest.raises(ValueError):
        layerstd.parse_bool("maybe")
    assert layerstd.parse_colors("1|3-4;7") == {1, 3, 4, 7}
    assert layerstd.parse_colors("") is None
    with pytest.raises(ValueError):
        layerstd.parse_colors("300")
    assert [layerstd.parse_lineweight(t) for t in ("0.25", "25", "default", "bylayer", "byblock")] == \
        [25, 25, -3, -1, -2]
    assert [layerstd.format_lineweight(v) for v in (25, -3, -2, -1)] == ["0.25", "default", "byblock", "bylayer"]


@pytest.mark.parametrize("text, needle", [
    ("kind,pattern\nbogus,A\n", "kind"),
    ("kind,pattern\nwildcard,\n", "pattern"),
    ("kind,pattern\nforbidden,\n", "pattern"),
    ("kind,pattern,value\nsetting,nope,1\n", "設定"),
    ("kind,pattern,value\nsetting,empty_layers,loud\n", "error"),
    ("kind,pattern\nregex,([\n", "第 2 列"),
    ("name,color\nA,1\n", "缺少欄位"),
])
def test_bad_standard_rows(tmp_path, text, needle):
    with pytest.raises(ValueError, match=needle):
        layerstd.load_standard(_std(text, tmp_path))


def test_bad_json_and_comment_rows(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("[]", encoding="utf-8")
    with pytest.raises(ValueError):
        layerstd.load_standard(bad)
    std = layerstd.load_standard(_std("kind,pattern\n#note,x\n,\nexact,A\n", tmp_path))
    assert len(std.rules) == 1
    j = tmp_path / "j.json"
    j.write_text('{"rules": [{"pattern": "A*", "plot": null}]}', encoding="utf-8")
    assert layerstd.load_standard(j).rules[0].kind == "wildcard"


def test_check_drawing_reports_every_rule_type(tmp_path):
    std = layerstd.load_standard(SAMPLES / "layer_standard.csv")
    dxf = _drawing(tmp_path / "d.dxf")
    row, details = layerstd.check_file(dxf, std)
    found = {(d["layer"], d["rule"]) for d in details}
    assert ("A-WALL", "COLOR_MISMATCH") in found
    assert ("A-WALL", "LINEWEIGHT_MISMATCH") in found
    assert ("a-door-x", "NAME_NONSTANDARD") in found          # lowercase fails case-sensitive regex
    assert ("TEMP1", "LAYER_FORBIDDEN") in found
    assert ("TEMP1", "NAME_NONSTANDARD") in found              # unlisted -> warning
    assert ("EMPTY_ONE", "EMPTY_LAYER") in found
    assert ("0", "ENTITIES_ON_LAYER_0") in found
    assert ("X-NOPLOT", "PLOT_MISMATCH") in found
    assert not any(d["layer"] == "Defpoints" for d in details)
    wall = next(d for d in details if d["rule"] == "LINEWEIGHT_MISMATCH")
    assert wall["expected"] == "0.35 | 0.50" and wall["actual"] == "0.13"
    zero = next(d for d in details if d["rule"] == "ENTITIES_ON_LAYER_0")
    assert zero["actual"] == "1"                               # block content on 0 is not counted
    assert row["status"] == "OK" and row["errors"] > 0 and 0 < row["compliance_rate"] < 100


def test_missing_required_linetype_and_off_settings(tmp_path):
    text = ("kind,pattern,value,color,linetype,required\n"
            "setting,layer0_entities,off,,,\nsetting,empty_layers,off,,,\nsetting,unlisted_layers,off,,,\n"
            "exact,A-WALL,,,DASHED,\nwildcard,MUST*,,,,yes\n")
    std = layerstd.load_standard(_std(text, tmp_path))
    _row, details = layerstd.check_file(_drawing(tmp_path / "d.dxf"), std)
    rules = sorted(d["rule"] for d in details)
    assert rules == ["LINETYPE_MISMATCH", "MISSING_REQUIRED_LAYER"]
    summary = layerstd.summarize("x", [], [d for d in details if d["rule"] == "MISSING_REQUIRED_LAYER"])
    assert summary["compliance_rate"] == 0.0
    assert layerstd.summarize("x", [], [])["compliance_rate"] == 0.0


def test_check_file_bad_dxf(tmp_path):
    bad = tmp_path / "bad.dxf"
    bad.write_text("not a dxf", encoding="utf-8")
    row, details = layerstd.check_file(bad, layerstd.Standard())
    assert row["status"] == "FAILED" and details == [] and row["compliance_rate"] == ""


def test_run_writes_reports_and_handles_dwg(tmp_path, monkeypatch):
    src = tmp_path / "in"
    (src / "sub").mkdir(parents=True)
    _drawing(src / "a.dxf")
    (src / "sub" / "b.dwg").write_bytes(b"AC1032")
    (src / "sub" / "c.dwg").write_bytes(b"AC1032")
    accore = tmp_path / "accoreconsole.exe"
    accore.write_bytes(b"")
    import convert_batch

    def fake_convert(items, accore, cfg, **kw):
        recs = []
        for dwg, dxf in items:
            if dwg.name == "b.dwg":
                Path(dxf).parent.mkdir(parents=True, exist_ok=True)
                ezdxf.new().saveas(dxf)
                recs.append(dict(source=str(dwg), status="DXF_VALID", error=""))
            else:
                recs.append(dict(source=str(dwg), status="DXF_INVALID", error="corrupt"))
        return recs

    monkeypatch.setattr(convert_batch, "convert_many", fake_convert)
    seen = []
    result = layerstd.run(src, tmp_path / "out", SAMPLES / "layer_standard.csv", str(accore),
                          progress=lambda i, n, p: seen.append((i, n)), log=lambda *a: None)
    assert result["fatal"] == "" and result["ok"] == 2 and result["failed"] == 1
    assert seen == [(1, 3), (2, 3), (3, 3)]
    reports = {k: Path(v) for k, v in result["reports"].items()}
    with reports["summary"].open(encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [Path(r["file"]).name for r in rows] == ["a.dxf", "b.dwg", "c.dwg"]
    assert rows[2]["status"] == "FAILED"
    with reports["details"].open(encoding="utf-8-sig") as fh:
        assert list(csv.DictReader(fh))[0].keys() == set(layerstd.DETAIL_FIELDS) | set()
    page = reports["html"].read_text(encoding="utf-8")
    assert "<style>" in page and "http" not in page and "ENTITIES_ON_LAYER_0" in page


def test_run_without_accore_fatal_and_stop(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    (src / "a.dwg").write_bytes(b"AC1032")
    result = layerstd.run(src, tmp_path / "out", SAMPLES / "layer_standard.csv", "")
    assert result["failed"] == 1 and "accoreconsole" in result["failed_files"][0]
    assert layerstd.run(src, tmp_path / "o", tmp_path / "missing.csv")["fatal"]
    assert layerstd.run(tmp_path / "nope", tmp_path / "o", SAMPLES / "layer_standard.csv")["fatal"]
    stop = threading.Event()
    stop.set()
    _drawing(src / "b.dxf")
    stopped = layerstd.run(src, tmp_path / "o2", SAMPLES / "layer_standard.csv", "", stop_event=stop)
    assert stopped["stopped"] and stopped["ok"] == 0


def test_render_html_clean_report():
    page = layerstd.render_html([layerstd.summarize("f.dxf", [], [])], [], "std.csv", generated="now")
    assert "沒有違規" in page and "now" in page

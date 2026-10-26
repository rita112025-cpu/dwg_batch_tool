"""Pipeline tests. Run: .venv\\Scripts\\python -m pytest tests -v"""
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import ezdxf
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from common import load_config  # noqa: E402
from convert_single import convert, validate_dxf  # noqa: E402
import batch_convert  # noqa: E402
import analyze_dxf  # noqa: E402
import find_scada  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample.dwg"
CFG = load_config()
ACCORE = CFG["accoreconsole"]


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture
def cfg(tmp_path):
    c = dict(CFG)
    c["input_dir"] = str(tmp_path / "input")
    c["output_dir"] = str(tmp_path / "output")
    (tmp_path / "input").mkdir()
    return c


@pytest.fixture(scope="module")
def sample_dxf(tmp_path_factory):
    out = tmp_path_factory.mktemp("dxf") / "sample.dxf"
    r = convert(FIXTURE, out, ACCORE, dict(CFG, output_dir=str(out.parent / "o")))
    assert r["status"] == "DXF_VALID", r
    return out


@pytest.mark.accore
def test_01_accoreconsole_found():
    assert Path(ACCORE).is_file()


@pytest.mark.accore
def test_02_accoreconsole_runs(tmp_path):
    scr = tmp_path / "h.scr"
    scr.write_text('(princ "\\nOK_MARK\\n")\nQUIT\nY\n')
    cp = subprocess.run([ACCORE, "/s", str(scr), "/l", "en-US"], capture_output=True,
                        timeout=120, stdin=subprocess.DEVNULL)
    assert cp.returncode == 0


@pytest.mark.accore
def test_03_04_convert_and_ezdxf_read(sample_dxf):
    assert validate_dxf(sample_dxf) == ("DXF_VALID", "")
    assert ezdxf.readfile(str(sample_dxf)).dxfversion == "AC1032"


@pytest.mark.accore
def test_05_06_08_chinese_and_space_path_source_untouched(tmp_path, cfg):
    d = tmp_path / "中文 路徑"
    d.mkdir()
    src = d / "圖面 x.dwg"
    shutil.copy(FIXTURE, src)
    before = sha(src)
    out = tmp_path / "輸出 dir" / "圖面 x.dxf"
    r = convert(src, out, ACCORE, cfg)
    assert r["status"] == "DXF_VALID", r
    assert sha(src) == before


@pytest.mark.accore
def test_07_duplicate_names_in_batch(cfg):
    inp = Path(cfg["input_dir"])
    for sub in ("", "sub"):
        (inp / sub).mkdir(exist_ok=True)
        shutil.copy(FIXTURE, inp / sub / "a.dwg")
    batch_convert.main(cfg)
    out = Path(cfg["output_dir"]) / "dxf"
    assert (out / "a.dxf").exists() and (out / "sub" / "a.dxf").exists()


@pytest.mark.accore
def test_09_no_overwrite_existing_dxf(tmp_path, cfg):
    out = tmp_path / "x.dxf"
    out.write_text("SENTINEL")
    r = convert(FIXTURE, out, ACCORE, cfg, overwrite=False)
    assert r["status"] == "SKIPPED_EXISTS"
    assert out.read_text() == "SENTINEL"


@pytest.mark.accore
def test_10_to_13_parse(sample_dxf, cfg):
    res, info = analyze_dxf.analyze(sample_dxf, "sample.dxf")
    types = {t["entity_type"] for t in res["texts"]}
    assert {"TEXT", "MTEXT", "ATTRIB"} <= types
    assert any("RTU-01" in t["text"] for t in res["texts"])
    assert any("中文測試" in t["text"] for t in res["texts"])
    assert [b["block_name"] for b in res["blocks"]] == ["BLK_PLC"]
    assert res["attribs"][0]["tag"] == "TAG1" and res["attribs"][0]["text"] == "PLC-A1"
    assert res["dimensions"] and res["dimensions"][0]["measurement"] == 100.0
    assert any(l["layer"] == "SCADA_TEST" and l["entity_count"] > 0 for l in res["layers"])


@pytest.mark.accore
def test_14_keyword_search(sample_dxf):
    rules = find_scada.scada_rules.build_rules(CFG)
    rows, _ = find_scada.search_doc(ezdxf.readfile(str(sample_dxf)), "s.dxf", rules)
    kinds = {(r["keyword"], r["source_type"]) for r in rows}
    assert ("RTU", "TEXT") in kinds and ("UPS", "MTEXT") in kinds
    assert ("PLC", "BLOCK_NAME") in kinds and ("PLC", "ATTRIB") in kinds
    assert ("SCADA", "LAYER_NAME") in kinds
    # case-insensitive
    assert find_scada.scada_rules.match_keyword("rtu panel", "RTU")


def test_loaded_doc_can_be_reused_for_analysis_and_search(monkeypatch):
    doc = ezdxf.new()
    doc.layers.add("SCADA")
    doc.modelspace().add_text("RTU-01", dxfattribs={"layer": "SCADA"})
    rules = find_scada.scada_rules.build_rules(CFG)

    monkeypatch.setattr(analyze_dxf.ezdxf, "readfile", lambda *_args, **_kwargs: pytest.fail(
        "analyze_doc must not reload an already parsed DXF"))
    rows, info = analyze_dxf.analyze_doc(doc, "memory.dxf")
    hits, excluded = find_scada.search_doc(doc, "memory.dxf", rules)

    assert info["text_count"] == 1
    assert rows["texts"][0]["text"] == "RTU-01"
    assert any(row["keyword"] == "RTU" for row in hits)
    assert not excluded


@pytest.mark.accore
def test_15_batch_continues_after_failure(cfg):
    inp = Path(cfg["input_dir"])
    (inp / "a_bad.dwg").write_text("not a dwg")
    shutil.copy(FIXTURE, inp / "b_good.dwg")
    rc = batch_convert.main(cfg)
    assert rc == 1
    out = Path(cfg["output_dir"])
    assert not (out / "dxf" / "a_bad.dxf").exists()
    assert (out / "dxf" / "b_good.dxf").exists()
    assert "a_bad.dwg" in (out / "logs" / "errors.log").read_text(encoding="utf-8")


@pytest.mark.accore
def test_16_batch_uses_one_accoreconsole_launch(cfg):
    inp = Path(cfg["input_dir"])
    for n in range(5):
        shutil.copy(FIXTURE, inp / f"f{n}.dwg")
    s = batch_convert.run(cfg)
    assert s["ok"] == 5 and s["failed"] == 0
    logs = list((Path(cfg["output_dir"]) / "logs").glob("batch_*.stdout.log"))
    assert len(logs) == 1          # whole batch inside a single accoreconsole process


@pytest.mark.accore
def test_17_corrupt_dwg_mid_batch_restarts_and_continues(cfg):
    inp = Path(cfg["input_dir"])
    shutil.copy(FIXTURE, inp / "a.dwg")
    (inp / "b_bad.dwg").write_text("garbage")
    shutil.copy(FIXTURE, inp / "c.dwg")
    s = batch_convert.run(cfg)
    assert (s["ok"], s["failed"]) == (2, 1)
    out = Path(cfg["output_dir"]) / "dxf"
    assert (out / "a.dxf").exists() and (out / "c.dxf").exists() and not (out / "b_bad.dxf").exists()


@pytest.mark.accore
def test_18_non_ansi_filename_uses_copy_and_source_untouched(cfg):
    inp = Path(cfg["input_dir"])
    src = inp / f"emoji_{chr(0x1F4D0)}.dwg"
    shutil.copy(FIXTURE, src)
    before = sha(src)
    s = batch_convert.run(cfg)
    assert s["ok"] == 1 and sha(src) == before


@pytest.mark.accore
def test_19_read_only_dwg_is_converted_not_failed(cfg):
    """A DWG with the read-only attribute makes AutoCAD ask 'open read-only?'; that must not be reported as corrupt."""
    import os
    import stat
    inp = Path(cfg["input_dir"])
    src = inp / "ro.dwg"
    shutil.copy(FIXTURE, src)
    os.chmod(src, stat.S_IREAD)
    before = sha(src)
    try:
        s = batch_convert.run(cfg)
        assert (s["ok"], s["failed"]) == (1, 0), s["failed_files"]
        assert sha(src) == before
    finally:
        os.chmod(src, stat.S_IWRITE | stat.S_IREAD)


def test_20_messagebox_classifier():
    from convert_batch import _has_blocking_messagebox as f
    stars = "*" * 24
    ro = f"{stars}MessageBox{'*' * 20}\nAutoCAD 警告\nx.dwg 目前使用中或是唯讀。\n>>>Responding: Yes.\n{'*' * 30}\n"
    bad = f"{stars}MessageBox{'*' * 20}\nAutoCAD 訊息\n圖檔無效。\n>>>Responding: OK.\n{'*' * 30}\n"
    assert not f(ro) and f(bad) and f(ro + bad) and not f("no box here")

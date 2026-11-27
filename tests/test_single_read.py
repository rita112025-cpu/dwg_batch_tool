"""The CAD pipeline parses each DXF once and shares the document between steps."""
import sys
from pathlib import Path

import ezdxf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import analyze_dxf  # noqa: E402
import convert_single  # noqa: E402
import pipeline  # noqa: E402
from common import load_config  # noqa: E402
from engineering_data import Store  # noqa: E402


def _make_dxf(path):
    doc = ezdxf.new()
    msp = doc.modelspace()
    msp.add_text("SCADA PANEL", dxfattribs={"layer": "E-SCADA", "insert": (1, 2)})
    blk = doc.blocks.new("RTU")
    blk.add_attdef("TAG", (0, 0))
    msp.add_blockref("RTU", (5, 5), dxfattribs={"layer": "E-SCADA"}).add_auto_attribs({"TAG": "RTU-01"})
    doc.saveas(path)
    return path


def _count_reads(monkeypatch):
    calls = []
    real = ezdxf.readfile

    def counting(*args, **kwargs):
        calls.append(args[0])
        return real(*args, **kwargs)

    monkeypatch.setattr(ezdxf, "readfile", counting)
    return calls


def test_analyze_with_doc_matches_path_read(tmp_path):
    dxf = _make_dxf(tmp_path / "a.dxf")
    by_path = analyze_dxf.analyze(dxf, "a.dxf")
    by_doc = analyze_dxf.analyze(dxf, "a.dxf", doc=ezdxf.readfile(dxf))
    assert by_doc == by_path


def test_read_valid_dxf_returns_doc(tmp_path):
    dxf = _make_dxf(tmp_path / "a.dxf")
    status, detail, doc = convert_single.read_valid_dxf(dxf)
    assert (status, detail) == ("DXF_VALID", "")
    assert doc is not None and doc.dxfversion
    assert convert_single.read_valid_dxf(tmp_path / "none.dxf") == ("DXF_INVALID", "file missing", None)


def test_parse_cad_dxf_input_reads_once(tmp_path, monkeypatch):
    dxf = _make_dxf(tmp_path / "in.dxf")
    calls = _count_reads(monkeypatch)
    store = Store(tmp_path / "p.db")
    assert pipeline._parse_cad(dxf, store, load_config(), tmp_path / "out", None) == "OK"
    assert len(calls) == 1


def test_parse_cad_dwg_input_reuses_validation_doc(tmp_path, monkeypatch):
    dwg = tmp_path / "in.dwg"
    dwg.write_bytes(b"not a real dwg")
    source = _make_dxf(tmp_path / "source.dxf")
    calls = _count_reads(monkeypatch)

    def fake_convert(src, dest, accore, cfg, overwrite=False, stop_event=None, keep_doc=False):
        Path(dest).parent.mkdir(parents=True, exist_ok=True)
        Path(dest).write_bytes(source.read_bytes())
        status, detail, doc = convert_single.read_valid_dxf(dest)
        rec = dict(status=status, error=detail)
        if keep_doc:
            rec["doc"] = doc
        return rec

    monkeypatch.setattr(convert_single, "convert", fake_convert)
    store = Store(tmp_path / "p.db")
    assert pipeline._parse_cad(dwg, store, load_config(), tmp_path / "out", None) == "OK"
    assert len(calls) == 1

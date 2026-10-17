from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import dwg_version


def _fake(path: Path, header: bytes):
    path.write_bytes(header + b"payload")
    return path


def test_read_known_dwg_version(tmp_path):
    p = _fake(tmp_path / "a.dwg", b"AC1032")
    r = dwg_version.read_version(p)
    assert r["format_code"] == "AC1032"
    assert r["format"] == "AC1032→2018"
    assert r["status"] == "OK"


def test_dwt_uses_same_header_logic(tmp_path):
    p = _fake(tmp_path / "template.dwt", b"AC1027")
    r = dwg_version.read_version(p)
    assert r["extension"] == ".dwt"
    assert r["format_code"] == "AC1027"
    assert r["format"] == "AC1027→2013"
    assert r["status"] == "OK"


def test_known_format_labels_are_chinese_and_keep_family_meaning():
    expected = {
        "AC1032": "AC1032→2018",
        "AC1027": "AC1027→2013",
        "AC1024": "AC1024→2010",
        "AC1021": "AC1021→2007",
        "AC1018": "AC1018→2004",
        "AC1015": "AC1015→2000",
        "AC1014": "AC1014→1997",
        "AC1012": "AC1012→1994",
        "AC1009": "AC1009→1990/1992",
    }
    assert {code: dwg_version.DWG_FORMATS[code] for code in expected} == expected


def test_invalid_header_is_reported(tmp_path):
    p = _fake(tmp_path / "bad.dwg", b"NOTDWG")
    r = dwg_version.read_version(p)
    assert r["status"] == "INVALID_HEADER"


def test_unknown_ac_code_is_preserved(tmp_path):
    p = _fake(tmp_path / "future.dwg", b"AC9999")
    r = dwg_version.read_version(p)
    assert r["format_code"] == "AC9999"
    assert r["format"] == "Unknown DWG format"
    assert r["status"] == "UNKNOWN_CODE"


def test_scan_and_csv_include_dwg_and_dwt(tmp_path):
    _fake(tmp_path / "a.dwg", b"AC1032")
    sub = tmp_path / "sub"
    sub.mkdir()
    _fake(sub / "b.dwt", b"AC1024")
    (sub / "ignore.txt").write_text("x", encoding="utf-8")

    out = tmp_path / "out" / "versions.csv"
    rows = dwg_version.write_version_csv([tmp_path], out, recursive=True)

    assert len(rows) == 2
    assert {r["extension"] for r in rows} == {".dwg", ".dwt"}
    text = out.read_text(encoding="utf-8-sig")
    assert "format_code" in text
    assert "AC1032" in text
    assert "AC1024" in text
    assert "AC1032→2018" in text
    assert "AC1024→2010" in text

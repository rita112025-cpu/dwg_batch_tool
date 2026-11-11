"""JSON/CSV output, source protection, and the DWG copy path."""

import csv
import hashlib
import json
from pathlib import Path

import ezdxf
import pytest

from cad2revit import __version__
from cad2revit.cli import main
from cad2revit.hashing import file_sha256
from cad2revit.run import run
from cad2revit_synth import sample_drawing

EXAMPLE = Path(__file__).resolve().parents[1] / "cad2revit" / "examples" / "mapping.example.json"


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _read_csv(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _outputs_text(folder):
    chunks = []
    for path in folder.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8"))
    return "\n".join(chunks)


def test_hash_helper_matches_the_repo_reader(tmp_path, monkeypatch):
    import convert_single

    seen = []
    real = convert_single.sha256

    def spy(path, n=1 << 20):
        seen.append(Path(path).name)
        return real(path, n)

    monkeypatch.setattr(convert_single, "sha256", spy)
    target = tmp_path / "a.bin"
    target.write_bytes(b"abc")
    assert file_sha256(target) == hashlib.sha256(b"abc").hexdigest()
    assert seen == ["a.bin"]


def test_json_and_csv_carry_manifest_fields_and_leave_the_source(tmp_path, monkeypatch):
    src = tmp_path / "in"
    out = tmp_path / "out"
    drawing = sample_drawing(src / "sub" / "plan.dxf")
    before = drawing.read_bytes()
    real_open = open

    def guarded(file, mode="r", *args, **kwargs):
        if Path(file).resolve() == drawing.resolve() and any(flag in mode for flag in ("w", "a", "x", "+")):
            raise AssertionError(mode)
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr("builtins.open", guarded)
    result = run(src, out, EXAMPLE)
    assert result["fatal"] is None
    assert result["ok"] == 1 and result["failed"] == 0
    assert drawing.read_bytes() == before
    document = json.loads((out / "json" / "sub" / "plan.cad2revit.json").read_text(encoding="utf-8"))
    assert document["schema"] == "cad2revit.elements.v1"
    assert document["tool_version"] == __version__
    assert document["status"] == "OK"
    assert document["source"]["relative_path"] == "sub/plan.dxf"
    assert document["source"]["sha256"] == _digest(drawing)
    assert document["source"]["sha256_after"] == document["source"]["sha256"]
    assert document["source"]["unchanged"] is True
    assert document["drawing"]["insunits"] == 4
    assert document["drawing"]["insbase"] == [100, 200, 300]
    assert document["mapping"]["file_name"] == "mapping.example.json"
    assert document["conversion"]["method"] == "direct_dxf"
    assert "E-RANDOM" in {item["name"] for item in document["unmatched"]["layers"]}
    assert {item["name"] for item in document["unmatched"]["blocks"]} == {"WIDGET-1"}
    assert str(tmp_path) not in _outputs_text(out)

    rows = _read_csv(out / "csv" / "elements.csv")
    per_file = _read_csv(out / "csv" / "sub" / "plan.elements.csv")
    assert rows == per_file
    assert rows
    assert {row["source_sha256"] for row in rows} == {document["source"]["sha256"]}
    assert {row["tool_version"] for row in rows} == {__version__}
    assert {row["units_code"] for row in rows} == {"4"}
    assert {row["units_name"] for row in rows} == {"Millimeters"}
    line = next(row for row in rows if row["entity_type"] == "LINE" and row["z1"] == row["z2"])
    assert line["revit_category"] == "Cable Tray"
    assert line["x1"] == "0.0" or float(line["x1"]) == 0
    assert float(line["z1"]) == 1
    panel = next(row for row in rows if row["entity_type"] == "INSERT")
    assert panel["revit_family"] == "Generic Panel"
    assert float(panel["rotation_deg"]) == 45
    assert json.loads(panel["attributes"])[0]["tag"] == "ID"
    manifest = _read_csv(out / "csv" / "manifest.csv")
    assert manifest[0]["source_sha256"] == document["source"]["sha256"]
    assert manifest[0]["tool_version"] == __version__
    assert manifest[0]["insunits_name"] == "Millimeters"
    unmatched = _read_csv(out / "csv" / "unmatched.csv")
    assert {row["name"] for row in unmatched} == {"E-RANDOM", "WIDGET-1"}
    summary = json.loads((out / "batch_summary.json").read_text(encoding="utf-8"))
    assert summary["schema"] == "cad2revit.batch.v1"
    assert summary["files"][0]["status"] == "OK"
    assert summary["fatal"] is None


def test_cli_reports_unmatched_and_rejects_a_bad_mapping(tmp_path, capsys):
    src = tmp_path / "in"
    out = tmp_path / "out"
    sample_drawing(src / "plan.dxf")
    assert main(["run", "--input", str(src), "--output", str(out), "--mapping", str(EXAMPLE)]) == 0
    printed = capsys.readouterr().out
    assert "未對上圖層" in printed and "E-RANDOM" in printed
    assert "WIDGET-1" in printed
    bad = tmp_path / "bad.json"
    bad.write_text("{", encoding="utf-8")
    assert main(["validate-mapping", str(bad)]) == 2
    assert main(["validate-mapping", str(EXAMPLE)]) == 0


def test_no_recursive_skips_subfolders(tmp_path):
    src = tmp_path / "in"
    sample_drawing(src / "top.dxf")
    sample_drawing(src / "sub" / "nested.dxf")
    (src / "note.txt").write_text("ignore", encoding="utf-8")
    out = tmp_path / "out"
    result = run(src, out, EXAMPLE, recursive=False)
    assert result["ok"] == 1
    assert (out / "json" / "top.cad2revit.json").is_file()
    assert not (out / "json" / "sub").exists()


def test_empty_folder_still_writes_headers(tmp_path):
    src = tmp_path / "in"
    out = tmp_path / "out"
    src.mkdir()
    result = run(src, out, EXAMPLE)
    assert result["fatal"] is None and result["ok"] == 0
    rows = _read_csv(out / "csv" / "elements.csv")
    assert rows == []
    assert _read_csv(out / "csv" / "manifest.csv") == []


def test_invalid_dxf_is_an_error_and_the_bytes_stay(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    drawing = src / "bad.dxf"
    drawing.write_bytes(b"this is not a dxf")
    out = tmp_path / "out"
    result = run(src, out, EXAMPLE)
    assert result["failed"] == 1
    assert drawing.read_bytes() == b"this is not a dxf"
    document = json.loads((out / "json" / "bad.cad2revit.json").read_text(encoding="utf-8"))
    assert document["status"] == "ERROR"
    assert document["elements"] == []
    assert document["source"]["unchanged"] is True
    assert str(tmp_path) not in document["error"]
    assert str(tmp_path) not in _outputs_text(out)


def test_output_inside_input_and_input_inside_output_write_nothing(tmp_path):
    src = tmp_path / "in"
    drawing = sample_drawing(src / "plan.dxf")
    before = drawing.read_bytes()
    nested_out = src / "out"
    refused = run(src, nested_out, EXAMPLE)
    assert refused["fatal"] and "裡面" in refused["fatal"]
    assert not nested_out.exists()
    assert drawing.read_bytes() == before

    outer = tmp_path / "o"
    inner = outer / "X"
    inner_drawing = sample_drawing(inner / "plan.dxf")
    inner_before = inner_drawing.read_bytes()
    reverse = run(inner, outer, EXAMPLE)
    assert reverse["fatal"] and "輸入" in reverse["fatal"]
    assert not (outer / "batch_summary.json").exists()
    assert not (outer / "json").exists()
    assert inner_drawing.read_bytes() == inner_before

    same = run(src, src, EXAMPLE)
    assert same["fatal"] and "相同" in same["fatal"]
    assert drawing.read_bytes() == before


def test_symlink_output_cannot_replace_a_source(tmp_path):
    src = tmp_path / "in"
    out = tmp_path / "out"
    drawing = sample_drawing(src / "plan.dxf")
    before = drawing.read_bytes()
    link = out / "json" / "plan.cad2revit.json"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(drawing)
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")
    result = run(src, out, EXAMPLE)
    assert result["fatal"]
    assert "原始" in result["fatal"]
    assert drawing.read_bytes() == before
    assert not (out / "batch_summary.json").exists()


def test_single_file_parent_is_the_input_folder(tmp_path):
    src = tmp_path / "in"
    out = tmp_path / "out"
    drawing = sample_drawing(src / "plan.dxf")
    result = run(drawing, out, EXAMPLE)
    assert result["ok"] == 1
    blocked = run(drawing, src / "side", EXAMPLE)
    assert blocked["fatal"] and "裡面" in blocked["fatal"]


def test_missing_accore_does_not_touch_the_dwg_or_call_the_converter(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    drawing = src / "plan.dwg"
    drawing.write_bytes(b"AC1032-synthetic-not-a-real-drawing")
    before = drawing.read_bytes()
    out = tmp_path / "out"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("converter should not run")

    result = run(src, out, EXAMPLE, accore=str(tmp_path / "missing.exe"), convert_func=forbidden)
    assert result["failed"] == 1
    assert drawing.read_bytes() == before
    document = json.loads((out / "json" / "plan.cad2revit.json").read_text(encoding="utf-8"))
    assert document["status"] == "CONVERSION_FAILED"
    assert "accoreconsole" in document["error"]
    assert document["conversion"]["real_accoreconsole_verified"] is False
    assert document["source"]["unchanged"] is True
    assert not list(out.rglob("*.dxf"))
    assert not list(src.rglob("*.dxf"))
    assert str(tmp_path) not in _outputs_text(out)


def _write_dxf(path):
    doc = ezdxf.new("R2018")
    doc.header["$INSUNITS"] = 4
    doc.header["$INSBASE"] = (0, 0, 0)
    doc.modelspace().add_line((0, 0, 1), (5, 0, 1), dxfattribs={"layer": "E-TRAY-MAIN"})
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.saveas(path)


def test_dwg_conversion_runs_on_a_copy_and_publishes_under_output(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    drawing = src / "plan.dwg"
    drawing.write_bytes(b"AC1032-synthetic-not-a-real-drawing")
    before = drawing.read_bytes()
    out = tmp_path / "out"
    accore = tmp_path / "accoreconsole.exe"
    accore.write_bytes(b"fake")
    seen = []

    def fake(dwg, dxf, accore_path, cfg=None, timeout=300, overwrite=False, stop_event=None):
        copy = Path(dwg)
        seen.append(copy.resolve())
        assert copy.resolve() != drawing.resolve()
        assert copy.read_bytes() == before
        assert copy.is_file()
        _write_dxf(Path(dxf))
        return {"status": "DXF_VALID", "error": ""}

    result = run(src, out, EXAMPLE, accore=str(accore), convert_func=fake)
    # The spy is installed by patching the module the wrapper looks up.
    assert result["ok"] == 1, result
    assert seen and seen[0] != drawing.resolve()
    assert not seen[0].exists()
    assert drawing.read_bytes() == before
    assert (out / "dxf" / "plan.dxf").is_file()
    assert not list(src.rglob("*.dxf"))
    document = json.loads((out / "json" / "plan.cad2revit.json").read_text(encoding="utf-8"))
    assert document["conversion"]["method"] == "accoreconsole_temp_copy"
    assert document["conversion"]["real_accoreconsole_verified"] is False
    assert document["source"]["sha256"] == _digest(drawing)
    assert document["elements"]
    assert document["elements"][0]["revit_category"] == "Cable Tray"
    assert str(tmp_path) not in _outputs_text(out)


def test_copy_hashed_is_the_repo_helper(tmp_path, monkeypatch):
    import convert_single

    src = tmp_path / "in"
    src.mkdir()
    drawing = src / "plan.dwg"
    drawing.write_bytes(b"AC1032-synthetic-not-a-real-drawing")
    accore = tmp_path / "accoreconsole.exe"
    accore.write_bytes(b"fake")
    seen = []
    real_copy = convert_single.copy_hashed

    def spy(source, dest, n=1 << 20):
        seen.append(Path(source).resolve())
        return real_copy(source, dest, n)

    monkeypatch.setattr(convert_single, "copy_hashed", spy)

    def fake(dwg, dxf, accore_path, cfg=None, timeout=300, overwrite=False, stop_event=None):
        _write_dxf(Path(dxf))
        return {"status": "DXF_VALID", "error": ""}

    result = run(src, tmp_path / "out", EXAMPLE, accore=str(accore), convert_func=fake)
    assert result["ok"] == 1
    assert seen == [drawing.resolve()]


def test_converter_source_modified_flag_is_about_the_copy_when_the_original_holds(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    drawing = src / "plan.dwg"
    drawing.write_bytes(b"AC1032-synthetic-not-a-real-drawing")
    before = drawing.read_bytes()
    accore = tmp_path / "accoreconsole.exe"
    accore.write_bytes(b"fake")

    def fake(dwg, dxf, accore_path, cfg=None, timeout=300, overwrite=False, stop_event=None):
        _write_dxf(Path(dxf))
        return {"status": "SOURCE_MODIFIED", "error": "source DWG hash changed!"}

    result = run(src, tmp_path / "out", EXAMPLE, accore=str(accore), convert_func=fake)
    assert result["ok"] == 1
    assert drawing.read_bytes() == before


def test_a_changed_source_hash_is_not_published(tmp_path):
    src = tmp_path / "in"
    src.mkdir()
    drawing = src / "plan.dwg"
    drawing.write_bytes(b"AC1032-synthetic-not-a-real-drawing")
    accore = tmp_path / "accoreconsole.exe"
    accore.write_bytes(b"fake")
    out = tmp_path / "out"

    def fake(dwg, dxf, accore_path, cfg=None, timeout=300, overwrite=False, stop_event=None):
        drawing.write_bytes(drawing.read_bytes() + b"TAMPER")
        _write_dxf(Path(dxf))
        return {"status": "DXF_VALID", "error": ""}

    result = run(src, out, EXAMPLE, accore=str(accore), convert_func=fake)
    assert result["failed"] == 1
    assert result["files"][0]["status"] == "SOURCE_MODIFIED"
    assert result["files"][0]["source_unchanged"] is False
    assert not (out / "dxf" / "plan.dxf").exists()
    document = json.loads((out / "json" / "plan.cad2revit.json").read_text(encoding="utf-8"))
    assert document["elements"] == []
    assert document["source"]["unchanged"] is False

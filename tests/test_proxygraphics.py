"""PROXYGRAPHICS script generation and batch plumbing tests."""
import csv
import hashlib
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import proxybatch  # noqa: E402
import proxygraphics  # noqa: E402
from common import decode_console, load_config  # noqa: E402

FAKE = ROOT / "tests" / "fake_accoreconsole.py"
HEADER = b"AC1032" + b"\x00" * 32


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _csv_rows(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _run_accore_script(accore: str, script: Path, timeout: int = 240) -> tuple[str, int]:
    command = [str(accore), "/s", str(script), "/l", "en-US"]
    if str(accore).lower().endswith(".py"):
        command.insert(0, sys.executable)
    proc = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL, timeout=timeout,
    )
    return decode_console(proc.stdout) + decode_console(proc.stderr), proc.returncode


def _read_proxy_value(accore: str, drawing: Path, workdir: Path) -> int:
    """Reopen a copy of *drawing* and return PROXYGRAPHICS without modifying it."""
    workdir.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex[:8]
    copy = workdir / f"read_{token}.dwg"
    script = workdir / f"read_{token}.scr"
    shutil.copy2(drawing, copy)
    try:
        script.write_bytes(proxygraphics.build_verify_script(copy).encode("ascii"))
        text, rc = _run_accore_script(accore, script)
        parsed = proxygraphics.parse_verify_output(text)
        assert rc == 0 and parsed["value"] is not None, text
        return parsed["value"]
    finally:
        for path in (copy, script):
            path.unlink(missing_ok=True)


def _set_proxy_value(accore: str, drawing: Path, value: int, workdir: Path) -> None:
    """Set and QSAVE PROXYGRAPHICS on a temp fixture copy (never on a project drawing)."""
    workdir.mkdir(parents=True, exist_ok=True)
    script = workdir / f"set_{uuid.uuid4().hex[:8]}.scr"
    text = "\n".join([
        "FILEDIA", "0", "CMDDIA", "0", "EXPERT", "5",
        "_.OPEN", f'"{str(drawing).replace(chr(92), "/")}"',
        f'(setvar "PROXYGRAPHICS" {value})',
        '(command "_.QSAVE")',
        "_.QUIT",
    ]) + "\n"
    script.write_bytes(text.encode("ascii"))
    try:
        output, rc = _run_accore_script(accore, script)
        assert rc == 0, output
    finally:
        script.unlink(missing_ok=True)


def test_script_sets_verifies_saves_and_reports():
    lisp = proxygraphics.build_lisp()
    assert '(setvar "PROXYGRAPHICS" 1)' in lisp
    assert '(getvar "PROXYGRAPHICS")' in lisp
    save = proxygraphics.build_save_commands()
    assert '(command "_.QSAVE")' in save
    assert "PROXYGRAPHICS_SAVE|=|OK" in save
    assert "PROXYGRAPHICS_SAVE|=|FAILED" in save
    assert "PROXYGRAPHICS_SAVE|=|NOT_REQUIRED" in save
    script = proxygraphics.build_script(
        [{"spath": "C:/temp/in_abc.dwg"}], "C:/temp/proxygraphics.lsp"
    )
    assert "(pg-run)" in script
    assert "PROXYGRAPHICS_SAVE" in script
    assert script.index("(pg-run)") < script.index("PROXYGRAPHICS_SAVE")
    assert '(if (> (getvar "DBMOD") 0) (command "_.CLOSE" "N"))' in script
    assert "_.SAVEAS" not in script
    assert script.isascii()
    verify = proxygraphics.build_verify_script("C:/temp/out_abc.dwg")
    assert "PROXYGRAPHICS_VERIFY" in verify
    assert '(getvar "PROXYGRAPHICS")' in verify
    assert "QSAVE" not in verify
    assert verify.isascii()


def test_parse_output_reports_save_and_values():
    parsed = proxygraphics.parse_output(
        "PROXYGRAPHICS_READY\n"
        "PROXYGRAPHICS_RESULT|=|0|=|1\n"
        "PROXYGRAPHICS_SAVE|=|OK\n"
    )
    assert parsed == {"ready": True, "before": 0, "after": 1,
                      "save": "OK", "save_note": "", "errors": []}
    failed = proxygraphics.parse_output(
        "PROXYGRAPHICS_READY\nPROXYGRAPHICS_RESULT|=|0|=|1\n"
        "PROXYGRAPHICS_SAVE|=|FAILED|=|DBMOD=1\n"
    )
    assert failed["save"] == "FAILED" and failed["save_note"] == "DBMOD=1"
    assert proxygraphics.parse_output("PROXYGRAPHICS_ERROR|=|read only\n")["errors"] == ["read only"]


def test_parse_verify_output_requires_a_value():
    assert proxygraphics.parse_verify_output("PROXYGRAPHICS_VERIFY|=|1\n") == {
        "value": 1, "errors": []}
    zero = proxygraphics.parse_verify_output("PROXYGRAPHICS_VERIFY|=|0\n")
    assert zero["value"] == 0 and zero["errors"] == []
    missing = proxygraphics.parse_verify_output("something else\n")
    assert missing["value"] is None and missing["errors"]


def test_batch_zero_to_one_is_verified_and_source_is_untouched(tmp_path):
    src = tmp_path / "input"
    out = tmp_path / "output"
    src.mkdir()
    drawing = src / "tray.dwg"
    drawing.write_bytes(HEADER + b"CABLE_TRAY")
    before = _sha(drawing)

    result = proxybatch.run(src, out, FAKE, recursive=True)

    produced = out / "tray.dwg"
    assert not result["fatal"]
    assert (result["dwg"], result["ok"], result["failed"], result["skipped"]) == (1, 1, 0, 0)
    assert _sha(drawing) == before
    rec = result["results"][0]
    assert (rec["before"], rec["after"]) == (0, 1)
    assert (rec["save"], rec["verify"], rec["output_value"]) == ("OK", "PASS", 1)
    assert produced.is_file() and b"PROXYGRAPHICS=1" in produced.read_bytes()

    rows = _csv_rows(out / "logs" / "proxygraphics_results.csv")
    assert list(rows[0]) == proxygraphics.CSV_FIELDS
    assert rows[0]["狀態"] == "已輸出"
    assert rows[0]["原設定"] == "0" and rows[0]["新設定"] == "1"
    assert rows[0]["存檔"] == "已存檔"
    assert rows[0]["輸出驗證"] == "通過" and rows[0]["輸出值"] == "1"

    snapshot = produced.read_bytes()
    skipped = proxybatch.run(src, out, FAKE, recursive=True)
    assert (skipped["ok"], skipped["skipped"], skipped["failed"]) == (0, 1, 0)
    assert produced.read_bytes() == snapshot
    skip_rec = skipped["results"][0]
    assert (skip_rec["save"], skip_rec["verify"]) == ("NOT_RUN", "NOT_RUN")


def test_already_enabled_proxygraphics_is_not_resaved(tmp_path):
    src = tmp_path / "input"
    src.mkdir()
    drawing = src / "tray.dwg"
    drawing.write_bytes(HEADER + b"\nPROXYGRAPHICS=1")
    original = drawing.read_bytes()
    out = tmp_path / "output"

    result = proxybatch.run(src, out, FAKE, recursive=False)

    assert not result["fatal"]
    assert (result["ok"], result["failed"]) == (1, 0)
    rec = result["results"][0]
    assert (rec["before"], rec["after"]) == (1, 1)
    assert (rec["save"], rec["verify"], rec["output_value"]) == ("NOT_REQUIRED", "PASS", 1)
    assert (out / "tray.dwg").read_bytes() == original


def test_batch_loads_lisp_for_each_drawing_and_preserves_sources(tmp_path):
    src = tmp_path / "input"
    out = tmp_path / "output"
    src.mkdir()
    originals = {}
    for name in ("first.dwg", "second.dwg", "third.dwg"):
        drawing = src / name
        drawing.write_bytes(HEADER + name.encode("ascii"))
        originals[name] = _sha(drawing)

    result = proxybatch.run(src, out, FAKE)

    assert not result["fatal"]
    assert (result["dwg"], result["ok"], result["failed"]) == (3, 3, 0)
    for name, digest in originals.items():
        assert _sha(src / name) == digest
        assert b"PROXYGRAPHICS=1" in (out / name).read_bytes()
    assert all(rec["verify"] == "PASS" and rec["output_value"] == 1 for rec in result["results"])


def test_script_loads_lisp_inside_each_opened_drawing():
    script = proxygraphics.build_script(
        [{"spath": "C:/temp/first.dwg"}, {"spath": "C:/temp/second.dwg"}],
        "C:/temp/proxygraphics.lsp",
    )
    sections = script.split("_.OPEN\n")
    assert '(load ' not in sections[0]
    assert len(sections) == 3
    for section in sections[1:]:
        assert section.index("ATCH_OPEN_") < section.index('(load ')
        assert section.index('(load ') < section.index("(pg-run)")
        assert section.index("(pg-run)") < section.index("PROXYGRAPHICS_SAVE")


def test_qsave_failure_does_not_publish(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_FAKE_QSAVE_FAIL", "1")
    src = tmp_path / "input"
    src.mkdir()
    drawing = src / "tray.dwg"
    drawing.write_bytes(HEADER)
    before = _sha(drawing)
    out = tmp_path / "output"

    result = proxybatch.run(src, out, FAKE, recursive=False)

    assert (result["ok"], result["failed"]) == (0, 1)
    rec = result["results"][0]
    assert rec["status"] == "FAILED" and rec["save"] == "FAILED"
    assert (rec["verify"], rec["output_value"]) == ("NOT_RUN", "")
    assert not (out / "tray.dwg").exists()
    assert _sha(drawing) == before


def test_verify_open_failure_removes_a_new_output(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_FAKE_VERIFY_OPEN_FAIL", "1")
    src = tmp_path / "input"
    src.mkdir()
    (src / "tray.dwg").write_bytes(HEADER)
    out = tmp_path / "output"

    result = proxybatch.run(src, out, FAKE, recursive=False)

    assert (result["ok"], result["failed"]) == (0, 1)
    rec = result["results"][0]
    assert rec["verify"] == "FAIL" and rec["output_value"] == ""
    assert "驗證" in rec["error"] and "已刪除未驗證輸出" in rec["error"]
    assert not (out / "tray.dwg").exists()


def test_verify_value_not_one_removes_a_new_output(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_FAKE_VERIFY_VALUE", "0")
    src = tmp_path / "input"
    src.mkdir()
    (src / "tray.dwg").write_bytes(HEADER)
    out = tmp_path / "output"

    result = proxybatch.run(src, out, FAKE, recursive=False)

    assert (result["ok"], result["failed"]) == (0, 1)
    rec = result["results"][0]
    assert (rec["verify"], rec["output_value"]) == ("FAIL", 0)
    assert "不是 1" in rec["error"]
    assert not (out / "tray.dwg").exists()


def test_existing_output_is_restored_when_verification_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_FAKE_VERIFY_VALUE", "0")
    src = tmp_path / "input"
    src.mkdir()
    drawing = src / "tray.dwg"
    drawing.write_bytes(HEADER + b"SOURCE")
    source_before = _sha(drawing)
    out = tmp_path / "output"
    out.mkdir()
    existing = out / "tray.dwg"
    original = b"ORIGINAL OUTPUT BYTES"
    existing.write_bytes(original)

    result = proxybatch.run(src, out, FAKE, recursive=False, overwrite=True)

    assert (result["ok"], result["failed"]) == (0, 1)
    rec = result["results"][0]
    assert rec["verify"] == "FAIL" and "已還原原輸出" in rec["error"]
    assert existing.read_bytes() == original
    assert _sha(drawing) == source_before
    backup_dirs = list((out / "logs" / "tmp").glob("proxygraphics_backup_*"))
    assert backup_dirs == [] or all(not list(p.iterdir()) for p in backup_dirs)


def test_existing_output_backup_is_removed_after_verified_success(tmp_path):
    src = tmp_path / "input"
    src.mkdir()
    (src / "tray.dwg").write_bytes(HEADER + b"SOURCE")
    out = tmp_path / "output"
    out.mkdir()
    existing = out / "tray.dwg"
    existing.write_bytes(b"ORIGINAL OUTPUT BYTES")

    result = proxybatch.run(src, out, FAKE, recursive=False, overwrite=True)

    assert (result["ok"], result["failed"]) == (1, 0)
    assert existing.read_bytes() != b"ORIGINAL OUTPUT BYTES"
    assert b"PROXYGRAPHICS=1" in existing.read_bytes()
    assert list((out / "logs" / "tmp").glob("proxygraphics_backup_*")) == []


def test_restore_failure_keeps_and_reports_the_backup(monkeypatch, tmp_path):
    monkeypatch.setenv("PG_FAKE_VERIFY_VALUE", "0")
    monkeypatch.setattr(proxybatch, "_restore_backup",
                        lambda job: (False, "simulated restore failure"))
    src = tmp_path / "input"
    src.mkdir()
    (src / "tray.dwg").write_bytes(HEADER)
    out = tmp_path / "output"
    out.mkdir()
    (out / "tray.dwg").write_bytes(b"ORIGINAL")

    result = proxybatch.run(src, out, FAKE, recursive=False, overwrite=True)

    assert (result["ok"], result["failed"]) == (0, 1)
    error = result["results"][0]["error"]
    assert "還原失敗，備份保留於" in error and "simulated restore failure" in error
    backups = list((out / "logs" / "tmp").glob("proxygraphics_backup_*/*"))
    assert backups, error
    shutil.rmtree(backups[0].parent.parent, ignore_errors=True)


@pytest.mark.accore
def test_real_accore_zero_to_one_and_verifies_output(tmp_path):
    accore = load_config().get("accoreconsole", "")
    src = tmp_path / "input"
    src.mkdir()
    drawing = src / "zero.dwg"
    drawing.write_bytes((ROOT / "tests" / "fixtures" / "sample.dwg").read_bytes())
    _set_proxy_value(accore, drawing, 0, tmp_path / "helpers")
    assert _read_proxy_value(accore, drawing, tmp_path / "helpers") == 0
    source_before = _sha(drawing)
    out = tmp_path / "output"

    result = proxybatch.run(src, out, accore, recursive=False)

    assert not result["fatal"]
    assert (result["ok"], result["failed"]) == (1, 0)
    rec = result["results"][0]
    assert (rec["before"], rec["after"]) == (0, 1)
    assert (rec["save"], rec["verify"], rec["output_value"]) == ("OK", "PASS", 1)
    assert _sha(drawing) == source_before
    produced = out / "zero.dwg"
    assert _read_proxy_value(accore, produced, tmp_path / "helpers") == 1
    rows = _csv_rows(out / "logs" / "proxygraphics_results.csv")
    assert rows[0]["原設定"] == "0" and rows[0]["新設定"] == "1"
    assert rows[0]["存檔"] == "已存檔"
    assert rows[0]["輸出驗證"] == "通過" and rows[0]["輸出值"] == "1"


@pytest.mark.accore
def test_real_accore_processes_multiple_drawings(tmp_path):
    source = ROOT / "tests" / "fixtures" / "sample.dwg"
    src = tmp_path / "input"
    out = tmp_path / "output"
    src.mkdir()
    for name in ("first.dwg", "second.dwg", "third.dwg"):
        (src / name).write_bytes(source.read_bytes())

    result = proxybatch.run(src, out, load_config().get("accoreconsole", ""))

    assert not result["fatal"]
    assert (result["ok"], result["failed"]) == (3, 0), result["results"]
    for rec in result["results"]:
        assert rec["save"] == "NOT_REQUIRED"
        assert rec["verify"] == "PASS" and rec["output_value"] == 1
        assert _sha(Path(rec["file"])) == _sha(source)
        assert Path(rec["output"]).is_file()


@pytest.mark.accore
def test_real_accore_does_not_resave_when_proxygraphics_is_already_on(tmp_path):
    source = ROOT / "tests" / "fixtures" / "sample.dwg"
    src = tmp_path / "input"
    out = tmp_path / "output"
    src.mkdir()
    (src / "sample.dwg").write_bytes(source.read_bytes())
    accore = load_config().get("accoreconsole", "")
    first = proxybatch.run(src, out, accore)
    assert (first["ok"], first["failed"]) == (1, 0), first["results"]
    assert first["results"][0]["save"] == "NOT_REQUIRED"
    assert first["results"][0]["verify"] == "PASS"
    saved = (out / "sample.dwg").read_bytes()

    again_src = tmp_path / "again"
    again_out = tmp_path / "again-out"
    again_src.mkdir()
    (again_src / "sample.dwg").write_bytes(saved)
    second = proxybatch.run(again_src, again_out, accore)
    assert not second["fatal"]
    assert (second["ok"], second["failed"]) == (1, 0), second["results"]
    rec = second["results"][0]
    assert (rec["before"], rec["after"]) == (1, 1)
    assert (rec["save"], rec["verify"], rec["output_value"]) == ("NOT_REQUIRED", "PASS", 1)
    assert (again_out / "sample.dwg").read_bytes() == saved


@pytest.mark.accore
def test_real_accore_sets_and_saves_proxygraphics(tmp_path):
    source = ROOT / "tests" / "fixtures" / "sample.dwg"
    src = tmp_path / "input"
    out = tmp_path / "output"
    src.mkdir()
    drawing = src / "sample.dwg"
    drawing.write_bytes(source.read_bytes())
    before = _sha(drawing)

    result = proxybatch.run(src, out, load_config().get("accoreconsole", ""))

    assert not result["fatal"]
    assert (result["ok"], result["failed"]) == (1, 0)
    rec = result["results"][0]
    assert rec["after"] == 1
    assert rec["verify"] == "PASS" and rec["output_value"] == 1
    assert _sha(drawing) == before
    assert (out / "sample.dwg").is_file()

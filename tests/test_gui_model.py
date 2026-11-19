"""gui_model is tkinter-free, so these run on every platform (test_gui_pipeline needs tkinter)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import gui_model as m  # noqa: E402


def test_task_keys_are_unique_and_labelled():
    keys = [k for k, _ in m.TASKS]
    assert len(keys) == len(set(keys))
    assert set(m.TASK_LABELS) == set(keys)
    assert m.NEEDS_ACCORE <= set(keys)


def test_every_task_has_a_primary_result_entry():
    assert {k for k, _ in m.TASKS if k != "scan"} <= set(m.PRIMARY_RESULTS)


def test_classify_outcome_states():
    assert m.classify_outcome({"ok": 3})[0] == "success"
    assert m.classify_outcome({"ok": 0, "skipped": 2})[0] == "skipped"
    assert m.classify_outcome({"ok": 1, "failed": 1})[0] == "warning"
    assert m.classify_outcome({"ok": 0, "failed": 2})[0] == "failed"
    assert m.classify_outcome({"stopped": True})[0] == "warning"
    assert m.classify_outcome({"fatal": True})[0] == "failed"


def test_friendly_failure_and_job_number(tmp_path):
    assert m.friendly_failure("C:/in/bad.dwg: DXF_INVALID timeout after 300s").endswith("轉檔逾時；圖面可能過大或 AutoCAD 沒有回應")
    assert m.friendly_failure("plain") == "plain"
    assert m.job_number(1) == "①" and m.job_number(10) == "10"


def test_get_primary_result(tmp_path):
    assert m.get_primary_result("version", tmp_path) is None
    (tmp_path / "csv").mkdir()
    f = tmp_path / "csv" / "dwg_versions.csv"
    f.write_text("x")
    assert m.get_primary_result("version", tmp_path) == f

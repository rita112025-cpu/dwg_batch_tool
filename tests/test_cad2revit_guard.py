"""Both-direction folder guard, including when the shared helper allows the reverse."""

from pathlib import Path

import pytest

from cad2revit import guard


def test_prefix_does_not_treat_a_longer_name_as_nested():
    assert guard._is_inside("/data/out", "/data")
    assert not guard._is_inside("/data/output", "/data/out")
    assert not guard._is_inside("/data/in", "/data/in")


def test_same_folder_output_inside_input_and_input_inside_output(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "in"
    dst = tmp_path / "out"
    src.mkdir()
    assert guard.directory_problem(src, dst) is None
    assert guard.directory_problem("in", "out") is None
    assert "相同" in guard.directory_problem(src, src)
    assert "相同" in guard.directory_problem(src, src / ".." / src.name)
    nested = src / "nested"
    assert "輸出資料夾不可放在輸入資料夾裡面" in guard.directory_problem(src, nested)
    assert "輸出資料夾不可放在輸入資料夾裡面" in guard.directory_problem(
        src, src / "nested" / ".." / "nested"
    )
    outer = tmp_path / "o"
    inner = outer / "X"
    inner.mkdir(parents=True)
    assert "輸入路徑不可放在輸出資料夾裡面" in guard.directory_problem(inner, outer)
    assert "輸入路徑不可放在輸出資料夾裡面" in guard.directory_problem(inner, inner / "..")
    assert "不存在" in guard.directory_problem(tmp_path / "missing", dst)
    assert guard.directory_problem("", dst)
    assert guard.directory_problem(src, "  ")
    taken = tmp_path / "file.txt"
    taken.write_text("x", encoding="utf-8")
    assert "資料夾" in guard.directory_problem(src, taken)


def test_reverse_nesting_does_not_depend_on_the_shared_helper(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "shared_directory_problem", lambda *_args, **_kwargs: None)
    outer = tmp_path / "o"
    inner = outer / "X"
    inner.mkdir(parents=True)
    message = guard.directory_problem(inner, outer)
    assert message == "輸入路徑不可放在輸出資料夾裡面"
    assert guard.own_directory_problem(inner, outer) == message
    nested = inner / "out"
    assert guard.directory_problem(inner, nested) == "輸出資料夾不可放在輸入資料夾裡面"


def test_shared_message_is_kept_when_both_checks_fail(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "shared_directory_problem", lambda *_args, **_kwargs: "來自共用檢查")
    src = tmp_path / "in"
    src.mkdir()
    assert guard.directory_problem(src, src) == "來自共用檢查"


def test_shared_helper_is_consulted(tmp_path, monkeypatch):
    calls = []

    def spy(src, dst):
        calls.append((Path(src), Path(dst)))
        return None

    monkeypatch.setattr(guard, "shared_directory_problem", spy)
    src = tmp_path / "in"
    dst = tmp_path / "out"
    src.mkdir()
    assert guard.directory_problem(src, dst) is None
    assert calls


def _symlink(link, target):
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks are not available: {exc}")


def test_output_symlink_that_resolves_to_a_source_is_refused(tmp_path):
    src = tmp_path / "in" / "plan.dxf"
    src.parent.mkdir()
    src.write_bytes(b"source-bytes")
    out = tmp_path / "out"
    link = out / "json" / "plan.cad2revit.json"
    link.parent.mkdir(parents=True)
    _symlink(link, src)
    protected = guard.protected_keys([src, src.parent])
    assert guard.output_path_problem(link, protected, src.parent) == "輸出路徑不可指向原始檔"
    with pytest.raises(guard.ProtectionError):
        guard.assert_output_safe(link, protected, src.parent)


def test_output_parent_symlink_into_the_input_tree_is_refused(tmp_path):
    src = tmp_path / "in"
    nested = src / "nested"
    nested.mkdir(parents=True)
    drawing = nested / "plan.dxf"
    drawing.write_bytes(b"keep")
    out = tmp_path / "out"
    out.mkdir()
    _symlink(out / "json", nested)
    dest = out / "json" / "plan.cad2revit.json"
    protected = guard.protected_keys([drawing, src])
    assert "輸入" in guard.output_path_problem(dest, protected, src)
    assert drawing.read_bytes() == b"keep"

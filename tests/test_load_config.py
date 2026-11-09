"""load_config: config.json first, config.example.json fallback, clear error when neither exists."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import common  # noqa: E402


@pytest.fixture
def fake_root(tmp_path, monkeypatch):
    monkeypatch.setattr(common, "ROOT", tmp_path)
    return tmp_path


def _write(path: Path, value: str) -> None:
    path.write_text(json.dumps({"source": value}), encoding="utf-8")


def test_uses_config_json_when_present(fake_root):
    _write(fake_root / "config.json", "local")
    _write(fake_root / "config.example.json", "example")
    assert common.load_config() == {"source": "local"}


def test_falls_back_to_example_when_config_json_missing(fake_root):
    _write(fake_root / "config.example.json", "example")
    assert common.load_config() == {"source": "example"}


def test_error_when_neither_file_exists(fake_root):
    with pytest.raises(FileNotFoundError) as exc:
        common.load_config()
    message = str(exc.value)
    assert "config.json" in message
    assert "config.example.json" in message
    assert "請複製 config.example.json 為 config.json" in message


def test_explicit_missing_path_still_raises(fake_root):
    _write(fake_root / "config.example.json", "example")
    with pytest.raises(FileNotFoundError):
        common.load_config(fake_root / "nope.json")


def test_explicit_path_is_used_even_when_config_json_exists(fake_root):
    _write(fake_root / "config.json", "local")
    other = fake_root / "other.json"
    _write(other, "explicit")
    assert common.load_config(other) == {"source": "explicit"}

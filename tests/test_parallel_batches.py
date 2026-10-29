"""Batch runners with ``workers`` > 1, driven by the fake console (no AutoCAD)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import dwgtext  # noqa: E402
import fontfix_batch  # noqa: E402
import layerbatch  # noqa: E402
import textbatch  # noqa: E402

FAKE = Path(__file__).resolve().parent / "fake_accoreconsole.py"
HEADER = b"AC1032" + b"\x00" * 32


def _dwg(path, extra=b""):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(HEADER + extra)


def _drawings(root, count):
    for index in range(count):
        _dwg(root / f"圖面 {index:02d}.dwg")


def test_fontfix_parallel_isolates_a_crashing_drawing(tmp_path):
    src = tmp_path / "in"
    _drawings(src, 5)
    _dwg(src / "壞圖.dwg", extra=b"FONTFIX_TEST_CRASH")
    seen = []
    summary = fontfix_batch.run(src, tmp_path / "out", FAKE, timeout=30, workers=3,
                                progress=lambda i, n, path: seen.append(i))
    assert summary["fatal"] == ""
    assert (summary["dwg"], summary["ok"], summary["failed"]) == (6, 5, 1)
    assert len(summary["results"]) == 6
    assert max(seen) == 6
    assert len(list((tmp_path / "out").glob("*.dwg"))) == 5


def test_text_replace_parallel_saves_every_drawing(tmp_path):
    src = tmp_path / "in"
    _drawings(src, 4)
    summary = textbatch.run_replace(src, tmp_path / "out", FAKE, timeout=30, workers=2,
                                    find=r"^(W\d+)\s.*$", repl=r"\1", mode="regex")
    assert summary["fatal"] == ""
    assert summary["ok"] == 4 and summary["failed"] == 0
    assert len(list((tmp_path / "out").glob("*.dwg"))) == 4


def test_layerclass_parallel_matches_sequential(tmp_path):
    src = tmp_path / "in"
    _drawings(src, 3)
    kwargs = dict(type_map="TEXT=TXT\nLWPOLYLINE=PL", dry_run=True, timeout=30)
    one = layerbatch.run(src, tmp_path / "seq", FAKE, workers=1, **kwargs)
    many = layerbatch.run(src, tmp_path / "par", FAKE, workers=3, **kwargs)
    assert one["fatal"] == "" and many["fatal"] == ""
    pick = ("dwg", "ok", "failed", "moved", "created")
    assert {key: one[key] for key in pick} == {key: many[key] for key in pick}


def test_apply_script_uses_global_apply_index():
    jobs = [{"spath": "C:/t/a.dwg", "apply_index": 5}, {"spath": "C:/t/b.dwg"}]
    text = dwgtext.build_apply_script(jobs, Path("C:/t/textapply.lsp"))
    if isinstance(text, bytes):
        text = text.decode("mbcs", errors="replace")
    assert "(tx-apply-5)" in text and "ATCH_OPEN_0|" in text
    assert "(tx-apply-1)" in text and "ATCH_OPEN_1|" in text

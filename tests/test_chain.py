"""A batch of DWG-writing tasks continues each step from the previous step's outputs."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import chain  # noqa: E402


def _rec(src, out, status):
    return {"file": str(src), "output": str(out), "status": status}


def test_update_carry_tracks_saved_unchanged_and_drops_failures(tmp_path):
    src, dst = tmp_path / "in", tmp_path / "out"
    (dst / "sub").mkdir(parents=True)
    src.mkdir()
    for name in ("a.dwg", "b.dwg", "c.dwg"):
        (src / name).write_bytes(b"orig")
    (dst / "a.dwg").write_bytes(b"fixed")
    (dst / "sub" / "d.dwg").write_bytes(b"fixed")
    carry = chain.update_carry({}, [
        _rec(src / "a.dwg", dst / "a.dwg", "SAVED"),
        _rec(src / "b.dwg", dst / "b.dwg", "UNCHANGED"),
        _rec(src / "c.dwg", dst / "c.dwg", "SKIPPED"),
        _rec(src / "d.dwg", dst / "sub" / "d.dwg", "SAVED"),
        _rec(src / "x.dwg", tmp_path / "elsewhere" / "x.dwg", "SAVED"),
    ], dst)
    assert sorted(out.name for out, _ in carry.values()) == ["a.dwg", "b.dwg", "d.dwg"]
    assert {out.name: source for out, source in carry.values()}["b.dwg"] == src / "b.dwg"

    chain.update_carry(carry, [_rec("stage/a.dwg", dst / "a.dwg", "FAILED")], dst)
    assert sorted(out.name for out, _ in carry.values()) == ["b.dwg", "d.dwg"]


def test_build_stage_mirrors_output_paths_and_is_removed(tmp_path):
    src, dst = tmp_path / "in", tmp_path / "out"
    (dst / "sub").mkdir(parents=True)
    src.mkdir()
    (src / "b.dwg").write_bytes(b"orig")
    (dst / "sub" / "d.dwg").write_bytes(b"fixed")
    carry = chain.update_carry({}, [
        _rec(src / "b.dwg", dst / "b.dwg", "UNCHANGED"),
        _rec(src / "d.dwg", dst / "sub" / "d.dwg", "SAVED"),
    ], dst)
    stage = chain.build_stage(carry, dst)
    assert stage.parent == dst / "logs"
    assert (stage / "b.dwg").read_bytes() == b"orig"
    assert (stage / "sub" / "d.dwg").read_bytes() == b"fixed"
    assert chain.remove_stage(stage) == ""
    assert not stage.exists()


def test_dry_run_is_not_a_chain_step():
    assert chain.is_chain_step("proxygraphics", {})
    assert not chain.is_chain_step("textreplace", {"dry_run": True})
    assert not chain.is_chain_step("textextract", {})


def _finish_task(app):
    if app.worker is not None:
        app.worker.join(timeout=30)
    app._poll()


def test_gui_batch_runs_proxygraphics_on_fontfix_output(tmp_path, monkeypatch, tk_root):
    import app as appmod
    monkeypatch.setattr(appmod.App, "_check_env", lambda self: None)
    for child in tk_root.winfo_children():
        child.destroy()
    app = appmod.App(tk_root)
    src, dst = tmp_path / "in", tmp_path / "out"
    src.mkdir()
    (src / "a.dwg").write_bytes(b"orig")
    base = dict(src=str(src), dst=str(dst), accore="x", recursive=False, overwrite=False, timeout=1)
    app._preparers["fontfix"] = lambda: dict(base)
    app._preparers["proxygraphics"] = lambda: dict(base)
    seen = {}

    def fake_fontfix(params, log, progress):
        dst.mkdir(exist_ok=True)
        (dst / "a.dwg").write_bytes(b"fonts fixed")
        return dict(dwg=1, ok=1, failed=0, skipped=0, fatal="", failed_files=[], results=[
            _rec(src / "a.dwg", dst / "a.dwg", "SAVED")])

    def fake_proxy(params, log, progress):
        stage = Path(params["src"])
        seen.update(params, data=(stage / "a.dwg").read_bytes())
        return dict(dwg=1, ok=1, failed=0, skipped=0, fatal="", failed_files=[], results=[
            _rec(stage / "a.dwg", dst / "a.dwg", "SAVED")])

    app._runners["fontfix"] = fake_fontfix
    app._runners["proxygraphics"] = fake_proxy
    app._start_tasks(["fontfix", "proxygraphics"])
    _finish_task(app)
    assert seen["data"] == b"fonts fixed"
    assert seen["overwrite"] is True and seen["dst"] == str(dst)
    assert Path(seen["src"]).parent == dst / "logs"
    assert not Path(seen["src"]).exists()

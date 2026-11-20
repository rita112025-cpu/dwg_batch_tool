"""Windows smoke test with real accoreconsole: normal / zh path / ja path / timeouts.

Usage:  .venv\\Scripts\\python -I docs\\smoke\\smoke_windows.py <work_dir> [case ...]
Each case gets a fresh <work_dir>\\<case> folder (deleted first). Source DWGs are copied from
SRC_DIR (default D:\\drawings\\input, override with --src). Exit code 0 = every check passed,
1 = a check failed, 2 = the process query failed (leftover state unknown).
"""
import argparse
import contextlib
import hashlib
import io
import json
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))
from common import load_config  # noqa: E402
import analyze_dxf  # noqa: E402
import batch_convert  # noqa: E402
import convert_single  # noqa: E402
import find_scada  # noqa: E402
import pipeline  # noqa: E402
import summary  # noqa: E402

SMALL_NAME = "DN-AR-SB12-0000_v29-圖紙 - AR 2021 - 夾層(2F)-1150903-20261119.dwg"
LARGE_NAME = "DN-AR-SB12-0000_v29-圖紙 - AR 2031 - 穿堂-月台層(3F)-1150903-20261119.dwg"


class ProcQueryError(RuntimeError):
    pass


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def procs():
    """All processes as {pid: (name, ppid)}. Raises ProcQueryError when PowerShell fails."""
    ps = "Get-CimInstance Win32_Process | ForEach-Object { \"$($_.ProcessId)|$($_.ParentProcessId)|$($_.Name)\" }"
    cp = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    res = {}
    for line in cp.stdout.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit():
            res[int(parts[0])] = (parts[2], int(parts[1]))
    if cp.returncode != 0 or not res:
        raise ProcQueryError(f"process query failed rc={cp.returncode}: {cp.stderr.strip()[:200]}")
    return res


def accore_trees(snap):
    """PIDs of every accoreconsole.exe and all of its descendants in ``snap``."""
    tree = {p for p, (n, _) in snap.items() if n.lower() == "accoreconsole.exe"}
    grew = True
    while grew:
        grew = False
        for p, (_, pp) in snap.items():
            if pp in tree and p not in tree:
                tree.add(p)
                grew = True
    return tree


class Watcher(threading.Thread):
    """Record accoreconsole trees that appear while a case runs, ignoring ones already running."""

    def __init__(self, preexisting):
        super().__init__(daemon=True)
        self.stop, self.seen, self.error, self.pre = threading.Event(), {}, "", preexisting

    def run(self):
        while not self.stop.is_set():
            try:
                snap = procs()
            except ProcQueryError as e:
                self.error = str(e)
                return
            for p in accore_trees(snap) - set(self.pre):
                self.seen[p] = snap[p][0]
            time.sleep(0.5)


class Spy:
    """Record which DXF list the pipeline hands to analysis and SCADA search."""

    def __init__(self):
        self.calls = {}
        self._real = (analyze_dxf.run, find_scada.run)

    def __enter__(self):
        real_a, real_s = self._real

        def a(cfg, *args, files=None, **kw):
            self.calls["analyze"] = None if files is None else [str(p) for p in files]
            return real_a(cfg, *args, files=files, **kw)

        def s(cfg, *args, files=None, **kw):
            self.calls["search"] = None if files is None else [str(p) for p in files]
            return real_s(cfg, *args, files=files, **kw)

        pipeline.analyze_dxf.run, pipeline.find_scada.run = a, s
        return self

    def __exit__(self, *exc):
        pipeline.analyze_dxf.run, pipeline.find_scada.run = self._real


def _cfg(inp, out, timeout, overwrite=True):
    cfg = load_config()
    cfg.update(input_dir=str(inp), output_dir=str(out), overwrite=overwrite, timeout_seconds=timeout,
               workers=1)
    return cfg


def _run_watched(fn):
    """Run ``fn`` while watching processes. Returns (value, process report)."""
    pre_snap = procs()
    pre = {p: pre_snap[p][0] for p in accore_trees(pre_snap)}
    w = Watcher(pre)
    w.start()
    t0 = time.time()
    value = fn()
    wall = round(time.time() - t0, 1)
    time.sleep(2.0)  # let Windows reap the console and its children
    w.stop.set()
    w.join()
    if w.error:
        raise ProcQueryError(w.error)
    after = procs()
    left = {p: n for p, n in w.seen.items() if p in after and after[p][0] == n}
    return value, dict(wall_seconds=wall, preexisting=pre, seen=w.seen, left=left)


def _temp_left(out):
    logs = out / "logs"
    return [str(p) for p in logs.rglob("*") if p.suffix.lower() in (".dxf", ".dwg", ".scr")] if logs.exists() else []


def _fresh(case_dir, files):
    if case_dir.exists():
        shutil.rmtree(case_dir)
    inp = case_dir / "in"
    for rel, src in files:
        dst = inp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
    return inp, case_dir / "out", {rel: sha(inp / rel) for rel, _ in files}


def _recs(results):
    return [{k: r.get(k) for k in ("source", "output", "status", "error", "file_size")} for r in results]


def case_convert(name, case_dir, files):
    """Batch convert; every file must become a valid DXF."""
    inp, out, hashes = _fresh(case_dir, files)
    stats, proc = _run_watched(lambda: batch_convert.run(_cfg(inp, out, 300), log=lambda lv, m: None))
    dxfs = sorted((out / "dxf").rglob("*.dxf"))
    checks = {
        "all DXF_VALID": all(r["status"] == "DXF_VALID" for r in stats["results"]) and len(stats["results"]) == len(files),
        "one DXF per DWG": len(dxfs) == len(files),
        "source hash unchanged": all(sha(inp / rel) == h for rel, h in hashes.items()),
        "no leftover process": not proc["left"],
        "no temp files left": not _temp_left(out),
    }
    return dict(case=name, results=_recs(stats["results"]),
                dxf_files=[str(p.relative_to(out)) for p in dxfs], process=proc, checks=checks)


def case_reconvert_timeout(name, case_dir, files):
    """Phase 1 converts normally; phase 2 reconverts into the same output with a forced timeout.
    The previous DXF must stay unchanged and the follow-on analysis must receive nothing."""
    inp, out, hashes = _fresh(case_dir, files)
    opts = dict(convert=True, analyze=True, scada=True, overwrite=True, csv=True, json=True)
    with Spy() as spy1:
        s1, proc1 = _run_watched(lambda: pipeline.run_pipeline(_cfg(inp, out, 300), opts))
    dxfs = sorted((out / "dxf").rglob("*.dxf"))
    dxf_hash = {str(p): sha(p) for p in dxfs}
    with Spy() as spy2:
        logs = []
        s2, proc2 = _run_watched(lambda: pipeline.run_pipeline(
            _cfg(inp, out, 0.3), opts, log=lambda lv, m: logs.append(f"{lv} {m}")))
    phase2 = [json.loads(line) for line in
              (out / "logs" / "batch_results.jsonl").read_text(encoding="utf-8").splitlines()][-len(files):]
    checks = {
        "phase 1 all converted": s1["ok"] == len(files) and s1["failed"] == 0,
        "phase 1 analysis got this run's DXFs": sorted(spy1.calls.get("analyze") or []) == sorted(dxf_hash),
        "phase 2 all reported failed": s2["failed"] == len(files) and all(r["status"] == "DXF_INVALID" for r in phase2),
        "phase 2 failure says previous DXF kept": all(convert_single.RETAINED_NOTE in r["error"] for r in phase2),
        "phase 2 previous DXF unchanged": all(Path(p).exists() and sha(p) == h for p, h in dxf_hash.items()),
        "phase 2 analysis not run": "analyze" not in spy2.calls and "search" not in spy2.calls,
        "phase 2 skip reason logged": any("略過分析" in m for m in logs),
        "source hash unchanged": all(sha(inp / rel) == h for rel, h in hashes.items()),
        "no leftover process": not proc1["left"] and not proc2["left"],
        "no temp files left": not _temp_left(out),
    }
    return dict(case=name, phase1_analysis_input=spy1.calls.get("analyze"),
                phase2_results=_recs(phase2), phase2_analysis_input=spy2.calls.get("analyze", "not called"),
                process=dict(phase1=proc1, phase2=proc2), checks=checks)


def case_cli_reconvert_timeout(name, case_dir, files):
    """run.bat order: batch_convert.main -> analyze_dxf.main --this-run -> find_scada.main --this-run.
    Phase 2 reconverts with a forced timeout; analysis must skip and leave the old DXF alone."""
    inp, out, hashes = _fresh(case_dir, files)
    cfg1, cfg2 = _cfg(inp, out, 300), _cfg(inp, out, 0.3)

    summaries = []

    def cli(cfg):
        rc = [batch_convert.main(cfg), analyze_dxf.main(cfg, ["--this-run"]),
              find_scada.main(cfg, ["--this-run"])]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            summary.main(cfg, ["--this-run"])
        summaries.append(buf.getvalue())
        return rc

    def results_hash():
        return {str(p): sha(p) for d in ("csv", "json") for p in sorted((out / d).rglob("*")) if p.is_file()}

    with Spy() as spy1:
        rc1, proc1 = _run_watched(lambda: cli(cfg1))
    dxfs = sorted((out / "dxf").rglob("*.dxf"))
    dxf_hash = {str(p): sha(p) for p in dxfs}
    index1 = (out / "csv" / "file_index.csv").read_bytes()
    results1 = results_hash()
    with Spy() as spy2:
        rc2, proc2 = _run_watched(lambda: cli(cfg2))
    phase2 = [json.loads(line) for line in
              (out / "logs" / "batch_results.jsonl").read_text(encoding="utf-8").splitlines()][-len(files):]
    checks = {
        "phase 1 exit codes 0/0/0": rc1 == [0, 0, 0],
        "phase 1 analysis got this run's DXFs": len(dxfs) == len(files) and all(
            Path(p).name.encode() in index1 for p in dxf_hash),
        "phase 2 convert exit 1, analysis exit 0": rc2 == [1, 0, 0],
        "phase 2 failure says previous DXF kept": all(convert_single.RETAINED_NOTE in r["error"] for r in phase2),
        "phase 2 previous DXF unchanged": all(Path(p).exists() and sha(p) == h for p, h in dxf_hash.items()),
        "phase 2 analysis not run": not spy2.calls,
        "phase 1 summary lists this run's rows": f"file_index.csv: {len(files)} rows" in summaries[0],
        "phase 2 summary says analysis not run": "本次未執行分析" in summaries[1] and "file_index.csv:" not in summaries[1],
        "phase 2 previous csv/json kept": results_hash() == results1 and bool(results1),
        "source hash unchanged": all(sha(inp / rel) == h for rel, h in hashes.items()),
        "no leftover process": not proc1["left"] and not proc2["left"],
        "no temp files left": not _temp_left(out),
    }
    return dict(case=name, exit_codes=dict(phase1=rc1, phase2=rc2), summaries=summaries, phase1_calls=spy1.calls,
                phase2_results=_recs(phase2), process=dict(phase1=proc1, phase2=proc2), checks=checks)


def case_gui_stop(name, case_dir, files):
    """Real App window: reconvert into a folder that already has DXFs, press 停止 on the first
    DWG->DXF progress. Analysis and SCADA search must not start; every DXF this run did not
    newly convert must keep its hash. Buttons are pressed with invoke(), not the mouse."""
    import tkinter as tk
    sys.path.insert(0, str(REPO))
    import app as appmod

    inp, out, hashes = _fresh(case_dir, files)
    first = batch_convert.run(_cfg(inp, out, 300), log=lambda lv, m: None)
    dxf_hash = {r["output"]: sha(r["output"]) for r in first["results"] if r["status"] == "DXF_VALID"}
    dialogs, events = [], []
    for fn in ("showinfo", "showwarning", "showerror"):
        setattr(appmod.messagebox, fn, lambda title, msg="", _fn=fn, **k: dialogs.append((_fn, title, msg)))
    for fn in ("askyesno", "askokcancel", "askyesnocancel"):
        setattr(appmod.messagebox, fn, lambda title, msg="", _fn=fn, **k: dialogs.append((_fn, title, msg)) or False)

    root = tk.Tk()
    a = appmod.App(root)
    a.v_in.set(str(inp))
    a.v_out.set(str(out))
    for key, value in dict(convert=True, analyze=True, scada=True, overwrite=True, recursive=True).items():
        a.opts[key].set(value)
    real_progress = a._on_task_progress

    def on_progress(key, stage, i, n, cur):
        events.append((stage, i, n))
        if stage == "DWG→DXF" and str(a.btn_stop["state"]) == "normal" and not a.stop_event.is_set():
            a.btn_stop.invoke()
            events.append(("STOP_PRESSED", i, n))
        return real_progress(key, stage, i, n, cur)

    a._on_task_progress = on_progress
    state = dict(started=False, t0=time.time())

    def tick():
        if not state["started"]:
            if a.env_ok and str(a.btn_start["state"]) == "normal":
                a.btn_start.invoke()
                state["started"] = True
            elif time.time() - state["t0"] > 60:
                state["error"] = "environment check did not enable 開始處理 DWG within 60s"
                root.quit()
                return
        elif not a._busy():
            root.after(500, root.quit)  # let _poll render the result
            return
        root.after(100, tick)

    def drive():
        with Spy() as spy:
            root.after(100, tick)
            root.mainloop()
        return spy

    try:
        spy, proc = _run_watched(drive)
        result_text = a.v_result.get()
    finally:
        root.destroy()
    this_run = {}
    jl = out / "logs" / "batch_results.jsonl"
    for line in jl.read_text(encoding="utf-8").splitlines()[len(first["results"]):]:
        r = json.loads(line)
        this_run[r["output"]] = r["status"]
    kept = {p: h for p, h in dxf_hash.items() if this_run.get(p) != "DXF_VALID"}
    checks = {
        "started from the GUI": state["started"] and "error" not in state,
        "stop pressed during DWG->DXF": any(e[0] == "STOP_PRESSED" for e in events),
        "conversion stopped before the last file": len([s for s in this_run.values() if s == "DXF_VALID"]) < len(files),
        "analysis not started": "analyze" not in spy.calls,
        "SCADA search not started": "search" not in spy.calls,
        "DXFs not reconverted keep their hash": bool(kept) and all(sha(p) == h for p, h in kept.items()),
        "no error dialog": not [d for d in dialogs if d[0] == "showerror"],
        "source hash unchanged": all(sha(inp / rel) == h for rel, h in hashes.items()),
        "no leftover process": not proc["left"],
        "no temp files left": not _temp_left(out),
    }
    return dict(case=name, error=state.get("error", ""), events=events[:20], dialogs=dialogs,
                this_run_status=this_run, kept_unchanged=len(kept), result_text=result_text,
                process=proc, checks=checks)


def case_single_timeout(name, case_dir, files):
    inp, out, hashes = _fresh(case_dir, files)
    rel = files[0][0]
    cfg = _cfg(inp, out, 2)
    rec, proc = _run_watched(lambda: convert_single.convert(
        inp / rel, out / "dxf" / Path(rel).with_suffix(".dxf"), cfg["accoreconsole"], cfg, 2, True))
    checks = {
        "reported timeout": rec["status"] == "DXF_INVALID" and "timeout" in rec["error"],
        "no DXF written": not (out / "dxf").exists() or not list((out / "dxf").rglob("*.dxf")),
        "source hash unchanged": sha(inp / rel) == hashes[rel],
        "no leftover process": not proc["left"],
        "no temp files left": not _temp_left(out),
    }
    return dict(case=name, results=_recs([rec]), process=proc, checks=checks)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("work_dir")
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--src", default=r"D:\drawings\input")
    a = ap.parse_args()
    base, src = Path(a.work_dir), Path(a.src)
    small, large = src / SMALL_NAME, src / LARGE_NAME
    cases = [
        ("A_ascii", case_convert, [("a_small.dwg", small), ("a_large.dwg", large)]),
        ("B_zh_繁中路徑", case_convert, [("車站圖面/" + small.name, small), ("車站圖面/" + large.name, large)]),
        ("C_ja_日本語パス", case_convert, [("図面フォルダ/図面_テスト_夾層.dwg", small), ("図面フォルダ/ずめん_大.dwg", large)]),
        ("D_reconvert_timeout", case_reconvert_timeout, [("t_large.dwg", large), ("t_small.dwg", small)]),
        ("E_timeout_single", case_single_timeout, [("t_large.dwg", large)]),
        ("F_runbat_reconvert_timeout", case_cli_reconvert_timeout, [("t_large.dwg", large), ("t_small.dwg", small)]),
        ("G_gui_stop", case_gui_stop, [(f"g_{n:02d}.dwg", large) for n in range(8)]),
    ]
    base.mkdir(parents=True, exist_ok=True)
    report, code = [], 0
    for name, fn, files in cases:
        if a.cases and name not in a.cases:
            continue
        try:
            r = fn(name, base / name, files)
        except ProcQueryError as e:
            r, code = dict(case=name, error=str(e), checks={"process query": False}), max(code, 2)
        r["passed"] = all(r["checks"].values())
        if not r["passed"]:
            code = max(code, 1)
        report.append(r)
        failed = [k for k, v in r["checks"].items() if not v]
        print(f"{'PASS' if r['passed'] else 'FAIL'} {name}" + (f"  failed: {failed}" if failed else ""), flush=True)
    (base / "smoke_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"report: {base / 'smoke_report.json'}  exit={code}")
    return code


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())

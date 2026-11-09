"""Recursively convert input/**/*.dwg -> output/dxf/**/*.dxf inside ONE accoreconsole window.
Per-file failure never stops the batch."""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, resolve, log_error
from convert_batch import convert_many
from parallel import clamp_workers

FIELDS = ["source", "output", "status", "return_code", "file_size", "elapsed_seconds", "error"]
OK_STATUS = ("DXF_VALID", "SKIPPED_EXISTS")


def list_dwg(cfg):
    inp = resolve(cfg, "input_dir")
    pattern = "**/*" if cfg.get("recursive", True) else "*"
    return sorted(p for p in inp.glob(pattern) if p.is_file() and p.suffix.lower() == ".dwg")


def run(cfg, log=None, progress=None, stop_event=None):
    """Batch convert. log(level, msg); progress(index, total, current_path).
    Returns dict(total, ok, failed, stopped, fatal, results)."""
    log = log or (lambda level, msg: print(f"[{level}] {msg}"))
    progress = progress or (lambda i, n, cur: None)
    stats = dict(total=0, ok=0, failed=0, skipped=0, stopped=False, fatal="", results=[],
                 failed_files=[])
    accore = cfg["accoreconsole"]
    if not Path(accore).exists():
        log_error(cfg, f"accoreconsole not found: {accore}")
        stats["fatal"] = f"accoreconsole not found: {accore}"
        return stats
    inp, out = resolve(cfg, "input_dir"), resolve(cfg, "output_dir")
    files = list_dwg(cfg)
    stats["total"] = len(files)
    logs = out / "logs"
    try:
        logs.mkdir(parents=True, exist_ok=True)
        (out / "dxf").mkdir(parents=True, exist_ok=True)
        probe = logs / ".write_test"
        probe.write_text("x")
        probe.unlink()
    except OSError as e:
        stats["fatal"] = f"output folder not writable: {e}"
        return stats
    csv_path = logs / "batch_results.csv"
    new = not csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8-sig") as cf, \
            open(logs / "batch_results.jsonl", "a", encoding="utf-8") as jf:
        w = csv.DictWriter(cf, FIELDS)
        if new:
            w.writeheader()
        items = [(f, out / "dxf" / f.relative_to(inp).with_suffix(".dxf")) for f in files]

        def on_result(rec):
            if rec["status"] == "STOPPED":
                return
            name = Path(rec["source"]).name
            w.writerow(rec)
            cf.flush()  # log immediately
            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            jf.flush()
            stats["results"].append(rec)
            if rec["status"] == "SKIPPED_EXISTS":
                stats["skipped"] += 1
                log("INFO", f"SKIP {name} (DXF already exists)")
            elif rec["status"] == "DXF_VALID":
                stats["ok"] += 1
                log("OK", f"{name} ({rec['status']}, {rec['elapsed_seconds']}s)")
            else:
                stats["failed"] += 1
                stats["failed_files"].append(f"{name}: {rec['status']} {rec['error']}".strip())
                log("ERROR", f"FAIL {name}: {rec['status']} {rec['error']}")
                log("INFO", "continue with next file")

        res = convert_many(items, accore, cfg, cfg.get("timeout_seconds", 300),
                           cfg.get("overwrite", False), stop_event, progress, on_result,
                           workers=clamp_workers(cfg.get("workers", 1)))
        if any(r["status"] == "STOPPED" for r in res):
            stats["stopped"] = True
            log("WARN", "stopped while converting")
    return stats


def main(cfg=None):
    cfg = cfg or load_config()
    try:
        s = run(cfg)
    except KeyboardInterrupt:
        print("Interrupted (Ctrl+C); results so far are saved.")
        return 130
    if s["fatal"]:
        print("FATAL:", s["fatal"])
        return 3
    print(f"done: ok={s['ok']} failed={s['failed']} skipped={s['skipped']} total={s['total']}")
    return 0 if s["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

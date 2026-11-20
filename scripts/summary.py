"""Print a summary of the last run.

``--this-run``: when the latest conversion produced no DXF (or no finished conversion is recorded),
analysis did not run, so the CSV counts below are the previous run's and are labelled that way.
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, resolve, read_this_run


def _analysis_skipped(cfg, argv):
    """Reason analysis did not run this time, or "" when the CSVs reflect this run."""
    if "--this-run" not in argv:
        return ""
    files = read_this_run(cfg)
    if files is None:
        return "沒有已完成的轉檔紀錄"
    return "" if files else "本次沒有成功轉出的 DXF"


def main(cfg=None, argv=None):
    cfg = cfg or load_config()
    argv = sys.argv[1:] if argv is None else argv
    out = resolve(cfg, "output_dir")
    reason = _analysis_skipped(cfg, argv)
    suffix = ""
    if reason:
        print(f"本次未執行分析（{reason}）；以下筆數是上次分析的結果")
        suffix = "（上次結果）"
    for name in ("file_index", "scada_hits", "object_hits", "block_summary"):
        p = out / "csv" / f"{name}.csv"
        if p.exists():
            with open(p, encoding="utf-8-sig") as f:
                print(f"{name}.csv: {sum(1 for _ in csv.reader(f)) - 1} rows{suffix}")
    err = out / "logs" / "errors.log"
    if err.exists():
        with open(err, encoding="utf-8") as f:
            print("errors.log lines:", sum(1 for _ in f))
    else:
        print("errors.log lines: 0")
    return 0


if __name__ == "__main__":
    main()

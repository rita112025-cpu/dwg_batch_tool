"""Print a summary of the last run.

``--this-run`` (used by run.bat): when the latest batch_convert.py run produced no DXF, analysis
was skipped and csv/ still holds an earlier run's results, so say so instead of printing those
row counts as if they were this run's."""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import load_config, resolve, read_this_run


def main(cfg=None, argv=None):
    cfg = cfg or load_config()
    argv = sys.argv[1:] if argv is None else argv
    out = resolve(cfg, "output_dir")
    analysis_ran = True
    if "--this-run" in argv:
        files = read_this_run(cfg)
        if not files:
            why = "轉檔未完成" if files is None else "本次沒有成功轉出的 DXF"
            print(f"本次未執行分析（{why}）；csv/、json/ 仍是上次的結果，不列入本次摘要")
            analysis_ran = False
    if analysis_ran:
        for name in ("file_index", "scada_hits", "object_hits", "block_summary"):
            p = out / "csv" / f"{name}.csv"
            if p.exists():
                with open(p, encoding="utf-8-sig") as f:
                    print(f"{name}.csv: {sum(1 for _ in csv.reader(f)) - 1} rows")
    err = out / "logs" / "errors.log"
    if err.exists():
        with open(err, encoding="utf-8") as f:
            print("errors.log lines:", sum(1 for _ in f))
    else:
        print("errors.log lines: 0")
    return 0


if __name__ == "__main__":
    main()  # always exit 0: the summary is informational

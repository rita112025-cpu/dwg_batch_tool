"""Shared helpers: config, error log, text decoding."""
import json
import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_config(path=None):
    """Load config. With no explicit path: config.json, else config.example.json.

    An explicit path that does not exist raises FileNotFoundError as usual.
    """
    if path:
        p = Path(path)
    else:
        p = ROOT / "config.json"
        if not p.exists():
            example = ROOT / "config.example.json"
            if not example.exists():
                raise FileNotFoundError(
                    f"找不到設定檔：{p}（也找不到 {example}）。"
                    "請複製 config.example.json 為 config.json 後再修改路徑。")
            p = example
    with open(p, encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg


def resolve(cfg, key):
    p = Path(cfg[key])
    return p if p.is_absolute() else ROOT / p


THIS_RUN_FILE = "converted_this_run.json"


def _this_run_path(cfg):
    return resolve(cfg, "output_dir") / "logs" / THIS_RUN_FILE


def write_this_run(cfg, paths):
    """Record the DXFs the latest conversion produced (or kept on purpose) for --this-run."""
    p = _this_run_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps([str(x) for x in paths], ensure_ascii=False), encoding="utf-8")


def clear_this_run(cfg):
    _this_run_path(cfg).unlink(missing_ok=True)


def read_this_run(cfg):
    """DXF paths from the latest conversion, or None when no finished conversion is recorded."""
    try:
        return [Path(x) for x in json.loads(_this_run_path(cfg).read_text(encoding="utf-8"))]
    except (OSError, ValueError):
        return None


def this_run_targets(cfg, argv):
    """CLI helper for analyze_dxf.py / find_scada.py. Returns (files, exit_code):
    files None = scan the whole folder; exit_code set = stop with that code."""
    if "--this-run" not in argv:
        return None, None
    files = read_this_run(cfg)
    if files is None:
        print(f"FATAL: no finished conversion recorded ({THIS_RUN_FILE}); run batch_convert.py first")
        return None, 3
    if not files:
        print("本次沒有成功轉出的 DXF，略過分析")
        return None, 0
    return files, None


def log_error(cfg_or_dir, msg):
    """Append to output/logs/errors.log (never just print)."""
    if isinstance(cfg_or_dir, dict):
        d = resolve(cfg_or_dir, "output_dir") / "logs"
    else:
        d = Path(cfg_or_dir)
    d.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    with open(d / "errors.log", "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def decode_console(data: bytes) -> str:
    """accoreconsole writes UTF-16 (with BOM) when redirected; fall back to utf-8/mbcs."""
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return data.decode("utf-16", errors="replace")
    if b"\x00" in data[:200]:
        return data.decode("utf-16-le", errors="replace")
    for enc in ("utf-8", "mbcs"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")

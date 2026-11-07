import argparse
import json
import sys
from pathlib import Path

from . import COLLECTORS, normalize_run, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="Normalize both Phase 0 collector runs")
    parser.add_argument("--raw-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("data/normalized"))
    args = parser.parse_args(argv)
    try:
        runs = [normalize_run(json.loads((args.raw_dir / (name + ".json")).read_text(encoding="utf-8")), name) for name in COLLECTORS]
        for run in runs:
            write_json(args.output_dir / (run["collector"] + ".json"), run)
            print(f"[normalize] {run['collector']} posts={len(run['posts'])} errors={len(run['errors'])}", file=sys.stderr)
        return 0
    except (OSError, ValueError, TypeError):
        print("[normalize] Invalid or unreadable collector input; check run schema and paths", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

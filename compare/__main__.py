import argparse
import json
import sys
from pathlib import Path

from normalizer import write_json
from . import build_report, markdown_report, write_markdown


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare a paired Phase 0 run")
    parser.add_argument("--input-dir", type=Path, default=Path("data/normalized"))
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)
    try:
        runs = [json.loads((args.input_dir / (name + ".json")).read_text(encoding="utf-8")) for name in ("facebook-go", "fbgql")]
        report = build_report(*runs)
        markdown = markdown_report(report)
        write_json(args.output_dir / "comparison.json", report)
        write_markdown(args.output_dir / "comparison.md", markdown)
        print(f"[compare] matched={report['comparison']['matched_posts']} assessment={report['assessment']['status']}", file=sys.stderr)
        return 0
    except (OSError, ValueError, TypeError, KeyError):
        print("[compare] Invalid or inconsistent inputs; check matching group_id/run_id, normalized schema and paths", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

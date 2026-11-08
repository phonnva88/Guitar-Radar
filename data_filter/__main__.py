"""Offline replay: python -m data_filter --input RAW --output NORMALIZED."""
import argparse
import json
from pathlib import Path
import sys

from normalizer import write_json
from data_filter import FilterError, filter_collection, process_collection, process_normalized


def main(argv=None):
    parser = argparse.ArgumentParser(description="Offline normalization/filtering; no Facebook session or database needed")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--normalized-input", action="store_true", help="Input already uses the normalized schema")
    parser.add_argument("--q", default="", help="Explicit case-insensitive text selection")
    parser.add_argument("--from", dest="start", help="Inclusive timezone-aware published time")
    parser.add_argument("--to", dest="end", help="Inclusive timezone-aware published time")
    parser.add_argument("--has-images", choices=("true", "false"))
    args = parser.parse_args(argv)
    try:
        if args.input.resolve() == args.output.resolve():
            raise FilterError("Input and output must be different files")
        if args.input.stat().st_size > 64 * 1024 * 1024:
            raise FilterError("Input exceeds 64 MiB")
        raw = json.loads(args.input.read_text(encoding="utf-8"))
        process = process_normalized if args.normalized_input else process_collection
        normalized = process(raw)
        result = filter_collection(normalized, q=args.q, start=args.start, end=args.end,
                                   has_images=None if args.has_images is None else args.has_images == "true")
        write_json(args.output, result)
    except (OSError, UnicodeError, ValueError, TypeError):
        print("[data-filter] Invalid input or output; no file contents were logged.", file=sys.stderr)
        return 1
    print(f"[data-filter] Offline processing complete: {len(result['posts'])} posts. No Facebook requests.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

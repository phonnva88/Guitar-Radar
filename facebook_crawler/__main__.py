"""Export a bounded sanitized Facebook collector result for offline processing."""
import argparse
from pathlib import Path
import sys

from facebook_crawler import CollectionFailure, FacebookCrawler, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="Facebook acquisition only; writes sanitized raw JSON")
    parser.add_argument("--group", required=True, help="Facebook numeric group ID or /groups/<id-or-alias> URL")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--lookback-hours", default=72, type=float)
    parser.add_argument("--timeout", default=300, type=float)
    parser.add_argument("--from", dest="start", help="Inclusive timezone-aware lower bound; requires --to")
    parser.add_argument("--to", dest="end", help="Inclusive timezone-aware upper bound; requires --from")
    args = parser.parse_args(argv)
    if (args.start is None) != (args.end is None):
        parser.error("--from and --to must be supplied together")
    try:
        raw = FacebookCrawler(lookback_hours=args.lookback_hours, timeout=args.timeout)(
            {"group_url": args.group, "requested_from": args.start, "requested_to": args.end})
        write_json(args.output, raw)
    except CollectionFailure as exc:
        print(f"[facebook-crawler] {exc.code}: no new result was exported.", file=sys.stderr)
        return 1
    except (ValueError, OSError):
        print("[facebook-crawler] configuration_error: check arguments and output permissions.", file=sys.stderr)
        return 1
    print(f"[facebook-crawler] status={raw['status']} posts={len(raw['posts'])}; sanitized raw export written.")
    return int(raw["status"] != "success" or not raw["window"]["complete"])


if __name__ == "__main__":
    raise SystemExit(main())

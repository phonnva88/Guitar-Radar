"""Run bounded, sequential collectors against one resolved group and one session."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
COLLECTORS = ("facebook-go", "fbgql")
HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com"}


class RunError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".phase0-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2, ensure_ascii=False)
            f.write("\n")
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def load_local_env() -> None:
    """Read only supported keys, without shell evaluation or value logging."""
    path = ROOT / ".env"
    allowed = {"FACEBOOK_COOKIE", "FACEBOOK_COOKIE_FILE", "FACEBOOK_USER_ID", "FACEBOOK_GROUP_ID"}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:]
            key, sep, value = line.partition("=")
            key = key.strip()
            if key not in allowed or not sep:
                continue
            try:
                parts = shlex.split(value, comments=True)
            except ValueError:
                raise RunError("authentication_failed", "Invalid .env quoting; no values were logged.") from None
            # Unquoted header values are accepted; use quotes for cookies containing #.
            parsed = " ".join(parts)
            os.environ.setdefault(key, parsed)
    if not os.environ.get("FACEBOOK_COOKIE") and os.environ.get("FACEBOOK_COOKIE_FILE"):
        try:
            os.environ["FACEBOOK_COOKIE"] = Path(os.environ["FACEBOOK_COOKIE_FILE"]).expanduser().read_text(encoding="utf-8").strip()
        except OSError:
            raise RunError("authentication_failed", "Cannot read FACEBOOK_COOKIE_FILE; check its path and permissions.") from None


def group_handle(value: str) -> str:
    if re.fullmatch(r"[0-9]+", value) and int(value) > 0:
        return value
    try:
        u = urlsplit(value)
        if u.scheme not in {"http", "https"} or u.hostname not in HOSTS or u.username or u.password or u.port not in {None, 80, 443}:
            raise ValueError
        m = re.fullmatch(r"/groups/([A-Za-z0-9._-]+)/?", u.path)
        if not m:
            raise ValueError
        if m[1].isdigit() and int(m[1]) == 0:
            raise ValueError
        return m[1]
    except ValueError:
        raise RunError("group_not_found", "Expected a positive group ID or Facebook /groups/<id-or-alias>/ URL.") from None


def resolve_group(value: str, timeout: float) -> str:
    handle = group_handle(value)
    if handle.isdigit():
        return handle
    # Reuse the wrapper's checked transport: no redirects, retries or credential logs.
    spec = importlib.util.spec_from_file_location("phase0_fbgql", ROOT / "collectors/fbgql/main.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        return module.resolve_group_alias(handle, timeout=timeout)
    except module.CollectorError as exc:
        raise RunError(exc.code, exc.message) from None


def safe_group_input(value: str) -> str:
    try:
        handle = group_handle(value)
        return handle if handle.isdigit() else f"https://www.facebook.com/groups/{handle}"
    except RunError:
        return "invalid_group_input"


def failure(collector: str, group: str | None, group_input: str, run_id: str, started_at: str,
            duration_ms: int, error: RunError, args: argparse.Namespace) -> dict:
    return {
        "schema_version": 1, "collector": collector, "group_id": group,
        "group_input": safe_group_input(group_input), "run_id": run_id, "source": "live",
        "started_at": started_at, "collected_at": now(), "duration_ms": duration_ms,
        "requested_pages": args.pages, "page_size": args.page_size, "sort": "CHRONOLOGICAL",
        "pages_fetched": 0, "status": "failed", "posts": [],
        "errors": [{"code": error.code, "stage": "orchestrator", "message": error.message}],
    }


def postprocess(data_dir: Path, report_dir: Path, expected_run_id: str | None = None) -> int:
    # Preserve previous reports, but never leave them at the current-run paths if
    # normalization/comparison fails. Users can inspect archived run IDs later.
    history = report_dir / "history" / uuid.uuid4().hex
    for name in ("comparison.json", "comparison.md"):
        path = report_dir / name
        if path.exists():
            history.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.rename(history / name)
    commands = [
        [sys.executable, "-m", "normalizer", "--raw-dir", str(data_dir / "raw"), "--output-dir", str(data_dir / "normalized")],
        [sys.executable, "-m", "compare", "--input-dir", str(data_dir / "normalized"), "--output-dir", str(report_dir)],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=ROOT, check=False)
        if result.returncode:
            print("[phase0] Postprocessing failed; previous reports are archived under history/.", file=sys.stderr)
            return result.returncode
    try:
        report = json.loads((report_dir / "comparison.json").read_text(encoding="utf-8"))
        if expected_run_id and report.get("run_id") != expected_run_id:
            raise ValueError
    except (OSError, ValueError, AttributeError):
        print("[phase0] Report is missing or has a different run_id; do not use it as this run's result.", file=sys.stderr)
        return 2
    return 0


def demo() -> int:
    run_id = "synthetic-" + uuid.uuid4().hex
    for collector in COLLECTORS:
        value = json.loads((ROOT / "tests/fixtures" / f"{collector}.json").read_text(encoding="utf-8"))
        value.update(run_id=run_id, source="synthetic")
        write_json(ROOT / "data/demo/raw" / f"{collector}.json", value)
    print("[phase0] Synthetic fixture demonstration; no Facebook requests or measured benchmark.", file=sys.stderr)
    return postprocess(ROOT / "data/demo", ROOT / "reports/demo", run_id)


def run(args: argparse.Namespace) -> int:
    if args.demo:
        return demo()
    run_id, started_at, start = uuid.uuid4().hex, now(), time.monotonic()
    selected = COLLECTORS if args.collector == "both" else (args.collector,)
    data_dir, report_dir = ROOT / args.data_dir, ROOT / args.report_dir
    group, error = None, None
    group_input = args.group or os.environ.get("FACEBOOK_GROUP_ID", "")
    try:
        load_local_env()
        group_input = args.group or os.environ.get("FACEBOOK_GROUP_ID", "")
        if not 1 <= args.pages <= 20 or not 1 <= args.page_size <= 20 or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 600:
            raise RunError("unknown_error", "Bounds: pages 1..20, page-size 1..20, timeout 1..600 seconds.")
        # Validate input even before authentication; group is never inferred from fixture data.
        handle = group_handle(group_input)
        group = handle if handle.isdigit() else None
        if not os.environ.get("FACEBOOK_COOKIE"):
            raise RunError("authentication_failed", "FACEBOOK_COOKIE is missing. Supply it locally via ignored .env or FACEBOOK_COOKIE_FILE; never send it in chat.")
        group = resolve_group(group_input, args.timeout)
    except RunError as exc:
        error = exc
    failed = False
    for collector in selected:
        output = data_dir / "raw" / f"{collector}.json"
        collector_error = error
        if collector_error is None:
            cmd = [str(ROOT / ".bin/facebook-go")] if collector == "facebook-go" else [sys.executable, str(ROOT / "collectors/fbgql/main.py")]
            cmd += ["--group", group, "--pages", str(args.pages), "--page-size", str(args.page_size),
                    "--timeout", str(args.timeout), "--output", str(output), "--run-id", run_id]
            try:
                result = subprocess.run(cmd, cwd=ROOT, check=False, timeout=args.timeout + 10)
                # Never consume a previous run's files after process/startup failure.
                try:
                    value = json.loads(output.read_text(encoding="utf-8"))
                    fresh = value.get("run_id") == run_id and value.get("collector") == collector and value.get("group_id") == group
                except (ValueError, OSError, AttributeError):
                    fresh = False
                if not fresh:
                    collector_error = RunError("unknown_error", "Collector did not produce a valid output for this run.")
                else:
                    value["group_input"] = safe_group_input(group_input)
                    write_json(output, value)
                failed |= result.returncode != 0 or (fresh and value.get("status") != "success")
            except subprocess.TimeoutExpired:
                collector_error = RunError("network_error", "Collector exceeded its time budget; output discarded to avoid stale or incomplete results.")
            except OSError:
                collector_error = RunError("unknown_error", "Cannot start collector. Run make setup/build and check the Python environment.")
        if collector_error:
            failed = True
            write_json(output, failure(collector, group, group_input, run_id, started_at,
                                      round((time.monotonic() - start) * 1000), collector_error, args))
            print(f"[{collector}] {collector_error.code}: {collector_error.message}", file=sys.stderr)
    if len(selected) == 2:
        failed |= postprocess(data_dir, report_dir, run_id) != 0
    else:
        print("[phase0] Single collector output saved. Run both in one phase0 invocation for a paired report.", file=sys.stderr)
    return int(failed)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--group")
    p.add_argument("--pages", type=int, default=2)
    p.add_argument("--page-size", type=int, default=20)
    p.add_argument("--timeout", type=float, default=120)
    p.add_argument("--collector", choices=("both",) + COLLECTORS, default="both")
    p.add_argument("--data-dir", default="data")
    p.add_argument("--report-dir", default="reports")
    p.add_argument("--demo", action="store_true")
    args = p.parse_args()
    try:
        return run(args)
    except (OSError, ValueError, ImportError):
        print("[phase0] Cannot complete pipeline; check dependencies, input files and output permissions. No credential values logged.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

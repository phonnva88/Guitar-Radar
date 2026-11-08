from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from compare import build_report, markdown_report, write_markdown
from normalizer import canonical_permalink, normalize_run, timestamp, write_json
from scripts import phase0
from validation.ground_truth import RESOLUTION_ERRORS, assess_fixture, numeric_group_from_url

ROOT = phase0.ROOT
COLLECTORS = phase0.COLLECTORS
STOP_CODES = {"checkpoint_required", "rate_limited", "session_expired", "authentication_failed", "account_restricted"}
STOP_HINTS = {
    "session_expired": "Facebook returned a logged-out session. Log in manually, confirm group access, then update FACEBOOK_COOKIE in .env or FACEBOOK_COOKIE_FILE. Exported values override .env; unset FACEBOOK_COOKIE FACEBOOK_COOKIE_FILE FACEBOOK_USER_ID to use .env.",
    "authentication_failed": "Configure a valid FACEBOOK_COOKIE with c_user and xs locally in .env or FACEBOOK_COOKIE_FILE; FACEBOOK_USER_ID must match c_user. Exported values override .env. Never paste cookies into chat or logs.",
    "checkpoint_required": "Complete the security checkpoint manually on Facebook before running validation again.",
    "rate_limited": "Facebook throttled or restricted requests. Wait for the restriction to clear before running validation again.",
    "account_restricted": "Resolve the account restriction manually on Facebook before running validation again.",
}
VERSIONS = {"facebook-go": "0c762f3eea9082ed7a4694d6c474c3622546b3b6", "fbgql": "28e03ae84012be44d37a67014fcfcfcc420c9ee9"}


def utc(value: str) -> str:
    try:
        d = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None:
            raise ValueError
        return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, AttributeError):
        raise ValueError("FROM/TO must be ISO 8601 timestamps with a timezone") from None


def window(start: str, end: str) -> tuple[str, str]:
    if end == "now":
        end = phase0.now()
    start, end = utc(start), utc(end)
    if datetime.fromisoformat(start.replace("Z", "+00:00")) > datetime.fromisoformat(end.replace("Z", "+00:00")):
        raise ValueError("FROM must not be after TO")
    return start, end


def fixture_url(value) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or parsed.hostname not in phase0.HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None
        if canonical_permalink(value) or re.fullmatch(r"/share/p/[A-Za-z0-9]+/?", parsed.path):
            # Canonical story.php IDs live in the query; keep only those keys.
            query = parsed.query if parsed.path in {"/story.php", "/permalink.php"} else ""
            if query:
                from urllib.parse import parse_qs, urlencode
                values = parse_qs(query)
                query = urlencode({k: values[k][0] for k in ("story_fbid", "id") if k in values})
            return urlunsplit(("https", "www.facebook.com", parsed.path, query, ""))
    except ValueError:
        pass
    return None


def known_posts(path: Path | None, csv: str = "") -> list[dict]:
    values = json.loads(path.read_text(encoding="utf-8")) if path and path.exists() else []
    if path and not path.exists():
        raise ValueError("Known-post fixture file does not exist")
    if not isinstance(values, list):
        raise ValueError("Known-post fixtures must be an array")
    values += [{"url" if value.strip().startswith("https://") else "post_id": value.strip(), "expected_has_images": True} for value in csv.split(",") if value.strip()]
    result = {}
    for entry in values:
        if not isinstance(entry, dict) or type(entry.get("expected_has_images")) is not bool:
            raise ValueError("Each known post requires expected_has_images true or false")
        post_id, url = entry.get("post_id"), fixture_url(entry.get("url"))
        url_key = canonical_permalink(url) if url else None
        url_post_id = url_key.removeprefix("post:") if url_key else None
        if post_id and url_post_id and str(post_id) != url_post_id:
            raise ValueError("Known-post ID does not match its canonical URL")
        if not post_id and url:
            post_id = url_post_id
        if not post_id and url:
            label = url
        elif isinstance(post_id, str) and re.fullmatch(r"[A-Za-z0-9_]+", post_id):
            label = post_id
        else:
            raise ValueError("Known posts need a valid post ID or canonical Facebook post URL")
        if label in result:
            existing = result[label]
            if existing["expected_has_images"] != entry["expected_has_images"]:
                raise ValueError("Conflicting known-post expectations")
            if existing.get("url") != url or existing.get("verified_posted_at") != timestamp(entry.get("verified_posted_at")):
                raise ValueError("Conflicting known-post URLs or verified timestamps")
        verified_at = timestamp(entry.get("verified_posted_at"))
        if "verified_posted_at" in entry and (not isinstance(entry.get("verified_posted_at"), str) or not verified_at):
            raise ValueError("verified_posted_at must be a timezone-aware ISO 8601 timestamp")
        record = {"post_id": post_id, "url": url, "unresolved": post_id is None,
                  "expected_has_images": entry["expected_has_images"]}
        if entry.get("source_url"):
            source_url = fixture_url(entry["source_url"])
            if not source_url:
                raise ValueError("Known-post source URL must be a Facebook post URL")
            record["source_url"] = source_url
        error = entry.get("resolution_error")
        if isinstance(error, str) and error in RESOLUTION_ERRORS:
            record["resolution_error"] = error
        if verified_at:
            record["verified_posted_at"] = verified_at
        result[label] = record
    return list(result.values())


class CanonicalLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "link" and "canonical" in attrs.get("rel", "").split():
            self.urls.append(attrs.get("href"))
        if tag == "meta" and attrs.get("property") == "og:url":
            self.urls.append(attrs.get("content"))


def resolve_known(known, timeout=120):
    if not any(item.get("unresolved") for item in known) or not os.environ.get("FACEBOOK_COOKIE"):
        return known, None
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("validation_fbgql", ROOT / "collectors/fbgql/main.py")
    adapter = module_from_spec(spec)
    spec.loader.exec_module(adapter)
    transport, result, stop = None, [], None
    try:
        cookies = adapter.cookies_from_env(os.environ)
        transport = adapter.SafeTransport(timeout)
        from fbgql.auth import BROWSER_HEADERS
        for item in known:
            entry = dict(item)
            if entry.get("unresolved"):
                try:
                    body = transport.get(entry["url"], cookies, headers=BROWSER_HEADERS)
                    parser = CanonicalLinks()
                    parser.feed(body)
                    urls = parser.urls + [getattr(transport, "last_url", None)]
                    candidates = {}
                    for url in urls:
                        safe = fixture_url(url)
                        key = canonical_permalink(safe) if safe else None
                        if key:
                            candidates.setdefault(key.removeprefix("post:"), []).append(safe)
                    if len(candidates) == 1:
                        post_id, references = next(iter(candidates.items()))
                        numeric = [url for url in references if numeric_group_from_url(url)]
                        groups = {numeric_group_from_url(url) for url in numeric}
                        if len(groups) > 1:
                            entry["resolution_error"] = "ambiguous_canonical_group"
                        else:
                            resolved_url = (numeric or references)[0]
                            entry.update(post_id=post_id, source_url=entry["url"], url=resolved_url, unresolved=False)
                            entry.pop("resolution_error", None)
                    else:
                        entry["resolution_error"] = "ambiguous_or_missing_canonical_post"
                except adapter.CollectionError as exc:
                    entry["resolution_error"] = exc.code
                    if exc.code in STOP_CODES:
                        stop = phase0.RunError(exc.code, "Known-post link resolution stopped after a session/security/rate-limit response.")
                if not stop:
                    time.sleep(1)
            result.append(entry)
            if stop:
                result.extend(dict(value) for value in known[len(result):])
                break
    except adapter.CollectionError as exc:
        result = [dict(item) for item in known]
        stop = phase0.RunError(exc.code, "Known-post session validation failed; no collection started.")
    finally:
        if transport is not None:
            transport.close()
    return result, stop


def post_key(post: dict, group: str | None) -> str | None:
    return "post:" + str(post["post_id"]) if post.get("post_id") else canonical_permalink(post.get("url"), group)


def image_validation(run: dict, known: list[dict]) -> dict:
    index = {}
    for post in run["posts"]:
        key = post_key(post, run["group_id"])
        if key:
            index.setdefault(key.removeprefix("post:"), []).append(post)
    records = []
    for item in known:
        key = item.get("post_id") or canonical_permalink(item.get("url"), run.get("group_id"))
        observations = index.get(str(key).removeprefix("post:"), []) if key else []
        counts = [len(p["images"]) for p in observations]
        inconsistent = len(set(counts)) > 1
        detected = bool(counts) and all(count > 0 for count in counts)
        scope = assess_fixture(item, run.get("group_id"), run.get("window"), observed=bool(observations))
        if scope["group_status"] == "wrong_group":
            status = "wrong_group"
        elif scope["group_status"] == "group_unverified":
            status = "unresolved_alias" if scope["group_reason"] == "share_alias_unresolved" else "group_unverified"
        elif scope["window_status"] == "outside_window_verified":
            status = "outside_window_verified"
        elif scope["window_status"] == "timestamp_unknown":
            status = "timestamp_unknown"
        elif not observations:
            status = "not_observed"
        elif inconsistent:
            status = "ambiguous_duplicates"
        elif item["expected_has_images"]:
            status = "detected" if detected else "extraction_failure"
        else:
            status = "unexpected_images" if detected else "expected_text_only"
        records.append({**item, **scope, "observed": bool(observations), "images_detected": detected,
                        "status": status, "image_counts": counts,
                        "evidence": [p.get("validation", {}) for p in observations]})
    positive = [r for r in records if r["expected_has_images"]]
    eligible_positive = [r for r in positive if r["eligible_for_window"]]
    observed = [r for r in eligible_positive if r["observed"]]
    detected = [r for r in eligible_positive if r["status"] == "detected"]
    count = lambda status: sum(r["status"] == status for r in records)
    return {"ground_truth_supplied": bool(positive), "known_image_posts": len(positive),
            "eligible_known_image_posts": len(eligible_positive),
            "observed_known_image_posts": len(observed), "detected_image_posts": len(detected),
            "missing_known_image_posts": [r.get("post_id") or r.get("url") for r in eligible_positive if r["status"] == "not_observed"],
            "extraction_failures": [r.get("post_id") for r in eligible_positive if r["status"] == "extraction_failure"],
            "ambiguous_known_posts": [r.get("post_id") for r in records if r["status"] == "ambiguous_duplicates"],
            "expected_text_only_posts": len(records) - len(positive),
            "eligible_text_only_posts": sum(not r["expected_has_images"] and r["eligible_for_window"] for r in records),
            "unexpected_image_posts": [r.get("post_id") for r in records if r["status"] == "unexpected_images"],
            "fixture_scope_counts": {status: count(status) for status in (
                "wrong_group", "unresolved_alias", "group_unverified", "outside_window_verified", "timestamp_unknown",
                "not_observed", "detected", "extraction_failure", "expected_text_only", "unexpected_images",
                "ambiguous_duplicates")},
            "wrong_group_posts": [r.get("post_id") or r.get("url") for r in records if r["status"] == "wrong_group"],
            "unverified_group_posts": [r.get("post_id") or r.get("url") for r in records if r["status"] == "group_unverified"],
            "unresolved_posts": [r.get("post_id") or r.get("url") for r in records if r["status"] == "unresolved_alias"],
            "outside_window_verified_posts": [r.get("post_id") or r.get("url") for r in records if r["status"] == "outside_window_verified"],
            "timestamp_unknown_posts": [r.get("post_id") or r.get("url") for r in records if r["status"] == "timestamp_unknown"],
            "observed_without_images": [r.get("post_id") for r in eligible_positive if r["status"] == "extraction_failure"],
            "image_detection_recall": len(detected) / len(eligible_positive) if eligible_positive else None,
            "conditional_image_detection_recall": len(detected) / len(observed) if observed else None,
            "observation_recall": len(observed) / len(eligible_positive) if eligible_positive else None,
            "records": records,
            "note": "Wrong-group, group-unverified, outside-window and unknown-time fixtures are excluded from recall and reported separately; missing eligible posts are collection misses, not proven extraction failures."}


def diagnostics(report: dict, runs: dict) -> dict:
    output = {}
    for name, field in (("facebook-go", "only_facebook_go_posts"), ("fbgql", "only_fbgql_posts")):
        keys = {post_key(p, runs[name]["group_id"]) for p in report["comparison"][field]}
        records = []
        for post in runs[name]["posts"]:
            if post_key(post, runs[name]["group_id"]) not in keys:
                continue
            evidence = post.get("validation", {})
            debug = evidence.get("debug_file")
            saved = False
            if isinstance(debug, str):
                path = Path(debug)
                path = path if path.is_absolute() else ROOT / path
                saved = path.is_file() and path.resolve().is_relative_to((ROOT / "data/debug").resolve())
            records.append({"post_id": post["post_id"], "collector": name, "url": post["url"],
                            "posted_at": post["posted_at"], "text_length": len(post["text"] or ""),
                            "author_present": bool(post["author_id"] or post["author_name"]),
                            "images_count": len(post["images"]), "possible_type": evidence.get("possible_type", "unknown"),
                            "type_evidence": evidence.get("type_evidence", []), "raw_response_saved": saved,
                            "debug_file": debug if saved else None, "reason_status": "unresolved",
                            "note": "Only observed in this collector sample; absence alone does not establish the cause."})
        output[name] = records
    return output


def git_commit() -> str | None:
    try:
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True,
                              text=True, check=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def failed_run(collector, group, group_input, run_id, start, end, args, error, skipped=False):
    base_args = argparse.Namespace(pages=args.max_pages, page_size=args.page_size)
    value = phase0.failure(collector, group, group_input, run_id, phase0.now(), 0, error, base_args)
    value["library_commit"] = VERSIONS[collector]
    value["window"] = {"from": start, "to": end, "complete": False, "stop_reason": "error",
                       "scanned_posts": 0, "unknown_timestamp_posts": 0, "ordering_violations": 0}
    value["errors"][0]["skipped"] = skipped
    return value


def collect_pair(args, start, end, known, *, execute=subprocess.run) -> dict:
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex
    directory = ROOT / "data/runs" / run_id
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    group_input = args.group or os.environ.get("FACEBOOK_GROUP_ID", "")
    group, error = None, getattr(args, "preflight_error", None)
    try:
        phase0.load_local_env()
        group_input = args.group or os.environ.get("FACEBOOK_GROUP_ID", "")
        handle = phase0.group_handle(group_input)
        group = handle if handle.isdigit() else None
        if not os.environ.get("FACEBOOK_COOKIE"):
            raise phase0.RunError("authentication_failed", "FACEBOOK_COOKIE is missing; configure it locally, never in chat.")
        if error is None:
            group = phase0.resolve_group(group_input, args.timeout)
    except phase0.RunError as exc:
        error = exc
    metadata = {"run_id": run_id, "group_id": group, "group_input": phase0.safe_group_input(group_input),
                "started_at": phase0.now(), "from": start, "to": end, "source": "live", "git_commit": git_commit(),
                "facebook_go_version": VERSIONS["facebook-go"], "fbgql_version": VERSIONS["fbgql"], "known_posts": known}
    write_json(directory / "metadata.json", metadata)
    runs, halted = {}, None
    for collector in COLLECTORS:
        output = directory / f"{collector}.raw.json"
        problem = error or halted
        if problem is None:
            command = [str(ROOT / ".bin/facebook-go")] if collector == "facebook-go" else [sys.executable, str(ROOT / "collectors/fbgql/main.py")]
            command += ["--group", group, "--pages", str(args.max_pages), "--page-size", str(args.page_size),
                        "--timeout", str(args.timeout), "--from", start, "--to", end,
                        "--run-id", run_id, "--output", str(output),
                        "--debug-dir", str(ROOT / "data/debug" / run_id / collector)]
            try:
                result = execute(command, cwd=ROOT, check=False, timeout=args.timeout + 10)
                raw = json.loads(output.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or raw.get("run_id") != run_id or raw.get("group_id") != group or raw.get("collector") != collector:
                    raise ValueError
                # Nonzero status must not be disguised by a successful envelope.
                if result.returncode and raw.get("status") == "success":
                    raise ValueError
            except subprocess.TimeoutExpired:
                problem = phase0.RunError("network_error", "Collector exceeded its time budget; no further requests were made.")
            except (OSError, ValueError):
                problem = phase0.RunError("unknown_error", "Collector failed to produce a current run output; check setup/build and dependencies.")
        if problem:
            raw = failed_run(collector, group, group_input, run_id, start, end, args, problem, halted is not None)
            if output.exists():
                # Keep any timed-out partial file for investigation, never reuse as current.
                output.rename(directory / f"{collector}.interrupted.json")
            write_json(output, raw)
        if not output.exists():
            write_json(output, raw)
        normalized = normalize_run(raw, collector)
        write_json(directory / f"{collector}.normalized.json", normalized)
        runs[collector] = normalized
        codes = {e.get("code") for e in raw["errors"]}
        stop = sorted(codes & STOP_CODES)
        if stop:
            halted = phase0.RunError(stop[0], "Collection stopped after an authentication/security/rate-limit response; retry only after manual resolution.")
        # Log only recognized codes, never upstream messages or credential values.
        stop_codes = ",".join(stop)
        reason = f" reason={stop_codes}" if stop_codes else ""
        print(f"[{collector}] run={run_id} status={raw['status']} window_complete={normalized.get('window', {}).get('complete', False)}{reason}", file=sys.stderr)
    return finish_run(metadata, runs, known, directory)


def finish_run(metadata: dict, runs: dict, known: list[dict], directory: Path | None = None) -> dict:
    comparison = build_report(runs["facebook-go"], runs["fbgql"])
    value = {"metadata": metadata, "collectors": runs, "comparison": comparison,
             "analysis_known_posts": known,
             "diagnostics": diagnostics(comparison, runs),
             "images": {name: image_validation(run, known) for name, run in runs.items()}}
    write_json(ROOT / "reports/runs" / (metadata["run_id"] + ".json"), value)
    if directory:
        write_json(directory / "comparison.json", comparison)
        write_markdown(directory / "comparison.md", markdown_report(comparison))
    return value


def persist_suite(runs: list[dict], known: list[dict], output_dir: Path):
    from validation.reports import build_stability, write_reports
    report = build_stability(runs, known)
    write_reports(report, output_dir)
    investigation = {"scope": "typed media in preserved story responses; no full-gallery claim",
                     "runs": [{"run_id": r["metadata"]["run_id"], "source": r["metadata"]["source"], "images": r["images"]} for r in runs]}
    write_json(output_dir / "image-investigation.json", investigation)
    lines = ["# Image extraction investigation", "", "Raw debug records preserve sanitized story-level responses, not authentication pages.", ""]
    for run in runs:
        lines.append(f"Run: {run['metadata']['run_id']} (source: {run['metadata']['source']})")
        for collector in COLLECTORS:
            v = run["images"][collector]
            lines += [f"- {collector}: known image posts={v['known_image_posts']}, observed={v['observed_known_image_posts']}, detected={v['detected_image_posts']}",
                      f"  Eligible in-window image fixtures: {v['eligible_known_image_posts']}; recall: {v['image_detection_recall']}",
                      f"  Wrong group: {', '.join(map(str, v['wrong_group_posts'])) or 'none'}; unresolved aliases: {', '.join(map(str, v['unresolved_posts'])) or 'none'}; other group unverified: {', '.join(map(str, v['unverified_group_posts'])) or 'none'}",
                      f"  Outside window (verified): {', '.join(map(str, v['outside_window_verified_posts'])) or 'none'}; timestamp unknown: {', '.join(map(str, v['timestamp_unknown_posts'])) or 'none'}",
                      f"  Extraction failures: {', '.join(map(str, v['extraction_failures'])) or 'none established'}; not observed in window: {', '.join(map(str, v['missing_known_image_posts'])) or 'none'}"]
    lines += ["", "Known fixtures must be user-confirmed. Synthetic tests do not establish live image extraction or complete galleries."]
    write_markdown(output_dir / "image-investigation.md", "\n".join(lines) + "\n")


def archive_summaries(directory: Path):
    history = directory / "history" / ("validation-" + uuid.uuid4().hex)
    names = ("stability.json", "stability.md", "consistency.json", "consistency.md", "collector-health.md",
             "final-report.md", "image-investigation.json", "image-investigation.md")
    for name in names:
        path = directory / name
        if path.exists():
            history.mkdir(parents=True, exist_ok=True, mode=0o700)
            path.rename(history / name)
    write_json(directory / "validation-status.json", {"status": "running", "started_at": phase0.now()})


def demo(args):
    """Exercise the full report path using explicitly synthetic enriched fixtures."""
    known = [{"post_id": "1001", "expected_has_images": True}, {"post_id": "1002", "expected_has_images": False}]
    archive_summaries(ROOT / "reports/validation-demo")
    runs = []
    for i in range(3):
        run_id = "synthetic-validation-" + uuid.uuid4().hex
        pair = {}
        for collector in COLLECTORS:
            raw = json.loads((ROOT / "tests/fixtures" / f"{collector}.json").read_text())
            raw.update(source="synthetic", run_id=run_id,
                       window={"from": "2026-11-07T09:00:00Z", "to": "2026-11-07T12:00:00Z", "complete": True,
                               "stop_reason": "feed_exhausted", "scanned_posts": len(raw["posts"]),
                               "unknown_timestamp_posts": 0, "ordering_violations": 0})
            for post in raw["posts"]:
                post_id = post.get("id", post.get("post_id"))
                if post_id == "1001":
                    post["image_candidates"] = [{"url": "https://example.invalid/synthetic-photo", "width": 100, "height": 100,
                                                  "source": "attachment", "path": "$.attachments[0].media.image"}]
                    post["possible_type"], post["type_evidence"] = "photo_post", ["synthetic Photo attachment"]
            directory = ROOT / "data/runs" / run_id
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            write_json(directory / f"{collector}.raw.json", raw)
            pair[collector] = normalize_run(raw, collector)
            write_json(directory / f"{collector}.normalized.json", pair[collector])
        metadata = {"run_id": run_id, "group_id": "123456789", "started_at": phase0.now(),
                    "from": "2026-11-07T09:00:00Z", "to": "2026-11-07T12:00:00Z", "source": "synthetic",
                    "git_commit": git_commit(), "facebook_go_version": VERSIONS["facebook-go"], "fbgql_version": VERSIONS["fbgql"]}
        write_json(directory / "metadata.json", metadata)
        runs.append(finish_run(metadata, pair, known, directory))
    persist_suite(runs, known, ROOT / "reports/validation-demo")
    write_json(ROOT / "reports/validation-demo/validation-status.json", {"status": "completed", "source": "synthetic", "run_ids": [r["metadata"]["run_id"] for r in runs]})
    print("[validation] Synthetic demo complete; NOT a live benchmark. reports/validation-demo", file=sys.stderr)
    return 0


def resolve_fixtures(args) -> int:
    """Resolve only fixture URLs; never collect feed posts or archive suite reports."""
    fixture = args.known_posts or (ROOT / "fixtures/known-image-posts.json")
    known = known_posts(fixture)
    if not known or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 3600:
        raise ValueError("Fixture resolution needs fixtures and a valid timeout")
    output = args.output_dir / "known-posts-resolved.json"
    status_path = args.output_dir / "fixture-resolution-status.json"
    write_json(output, known)
    write_json(status_path, {"status": "running", "started_at": phase0.now()})
    stop = None
    try:
        phase0.load_local_env()
        if any(item["unresolved"] for item in known) and not os.environ.get("FACEBOOK_COOKIE"):
            raise phase0.RunError("authentication_failed", "Fixture resolution requires a local session.")
        known, stop = resolve_known(known, args.timeout)
    except phase0.RunError as exc:
        stop = exc
    write_json(output, known)
    resolved = sum(not item.get("unresolved") and numeric_group_from_url(item.get("url")) is not None for item in known)
    failed = bool(stop or resolved != len(known))
    write_json(status_path, {"status": "incomplete" if failed else "completed", "finished_at": phase0.now(),
                             "fixtures": len(known), "resolved_with_numeric_group": resolved,
                             "stop_code": stop.code if stop else None, "feed_collection_started": False})
    if stop:
        code = stop.code if stop.code in STOP_HINTS else "authentication_failed"
        print(f"[validation] STOP: {code}. {STOP_HINTS[code]}", file=sys.stderr)
    print(f"[validation] Fixture links with numeric group: {resolved}/{len(known)}. No feed collection. See {output}.", file=sys.stderr)
    return int(failed)


def selected_run_ids(args) -> set[str] | None:
    """Read a selection before its source report can be archived."""
    if args.run_ids_from:
        value = json.loads(args.run_ids_from.read_text(encoding="utf-8"))
        values = value.get("run_ids") if isinstance(value, dict) else None
    elif args.run_ids:
        values = [value.strip() for value in args.run_ids.split(",")]
    else:
        return None
    if (not isinstance(values, list) or not values
            or any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value) for value in values)
            or len(values) != len(set(values))):
        raise ValueError("Run selection needs nonempty, unique run IDs")
    return set(values)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Phase 0.5 window/repeated validation; no Phase 1 services")
    parser.add_argument("command", choices=("window", "repeat", "analyze", "resolve-known", "demo"))
    parser.add_argument("--group")
    parser.add_argument("--from", dest="start", default="2026-11-06T00:00:00+07:00", help="Inclusive lower bound; default matches the requested vegmpremium validation")
    parser.add_argument("--to", dest="end", default="now", help="Inclusive upper bound, or now (frozen once for all repeats)")
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--page-size", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--known-posts", type=Path)
    parser.add_argument("--runs-dir", type=Path, default=Path("data/runs"))
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--run-ids", help="Comma-separated saved run IDs for analyze; do not mix groups/windows")
    selection.add_argument("--run-ids-from", type=Path, help="Read run_ids from an existing suite JSON before archiving it; analyze only")
    parser.add_argument("--output-dir", type=Path, default=Path("reports"))
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            return demo(args)
        if args.command != "analyze" and (args.run_ids or args.run_ids_from):
            raise ValueError("Saved run selections are only valid for analyze")
        if args.command == "resolve-known":
            return resolve_fixtures(args)
        requested = selected_run_ids(args) if args.command == "analyze" else None
        archive_summaries(args.output_dir)
        phase0.load_local_env()
        fixture = args.known_posts or (ROOT / "fixtures/known-image-posts.json")
        known = known_posts(fixture, "" if args.known_posts else os.environ.get("KNOWN_IMAGE_POSTS", ""))
        if args.command == "analyze":
            runs = []
            saved_known = None
            for directory in sorted(args.runs_dir.iterdir()):
                if directory.is_dir() and (directory / "metadata.json").is_file():
                    metadata = json.loads((directory / "metadata.json").read_text())
                    if requested is not None and metadata.get("run_id") not in requested:
                        continue
                    per_known = known if args.known_posts else metadata.get("known_posts", known)
                    signature = lambda values: sorted((str(item.get("post_id")), str(item.get("url")), item.get("verified_posted_at") or "", item["expected_has_images"]) for item in values)
                    if saved_known is not None and signature(saved_known) != signature(per_known):
                        raise ValueError("Saved known-post resolutions differ; select compatible runs or provide an explicit fixture")
                    saved_known = per_known
                    pair = {c: normalize_run(json.loads((directory / f"{c}.raw.json").read_text()), c) for c in COLLECTORS}
                    runs.append(finish_run(metadata, pair, per_known))
            if not runs:
                raise ValueError("No saved Phase 0.5 run metadata found")
            if requested is not None and requested != {r["metadata"]["run_id"] for r in runs}:
                raise ValueError("Some requested run IDs were not found")
            known = saved_known
        else:
            start, end = window(args.start, args.end)
            if not 1 <= args.runs <= 100 or not 1 <= args.max_pages <= 200 or not 1 <= args.page_size <= 20:
                raise ValueError("Invalid run/page limits")
            if not math.isfinite(args.interval) or not 0 <= args.interval <= 3600 or not math.isfinite(args.timeout) or not 1 <= args.timeout <= 3600:
                raise ValueError("Invalid interval or timeout")
            known, args.preflight_error = resolve_known(known, args.timeout)
            write_json(args.output_dir / "known-posts-resolved.json", known)
            runs = []
            for index in range(args.runs if args.command == "repeat" else 1):
                run = collect_pair(args, start, end, known)
                runs.append(run)
                # Save progress after each complete pair, even if later interrupted.
                persist_suite(runs, known, args.output_dir)
                codes = {e.get("code") for c in COLLECTORS for e in run["collectors"][c]["errors"]}
                if codes & STOP_CODES:
                    for code in sorted(codes & STOP_CODES):
                        print(f"[validation] STOP: {code}. {STOP_HINTS[code]}", file=sys.stderr)
                    break
                if index + 1 < args.runs and args.command == "repeat":
                    time.sleep(args.interval)
        persist_suite(runs, known, args.output_dir)
        result = int(any(r["collectors"][c]["status"] != "success" or not (r["collectors"][c].get("window") or {}).get("complete") for r in runs for c in COLLECTORS))
        write_json(args.output_dir / "validation-status.json", {"status": "completed_with_collection_errors" if result else "completed", "finished_at": phase0.now(), "run_ids": [r["metadata"]["run_id"] for r in runs]})
        if args.command == "analyze":
            print(f"[validation] Offline analysis finished: {len(runs)} saved pairs. Reports: {args.output_dir / 'final-report.md'}.", file=sys.stderr)
            if result:
                print("[validation] Saved collection failures/incomplete windows are retained; no new feed requests were made.", file=sys.stderr)
        return result
    except (OSError, ValueError, TypeError, KeyError, ImportError, phase0.RunError):
        try:
            name = "fixture-resolution-status.json" if args.command == "resolve-known" else "validation-status.json"
            write_json(args.output_dir / name, {"status": "failed", "finished_at": phase0.now(), "message": "Check input files and local configuration; no credentials logged."})
        except OSError:
            pass
        print("[validation] Cannot complete: check setup, timezone window, matching groups/windows and known-post fixture schema. No credentials logged.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Evidence-based Phase 0.5 stability and readiness reports.

Coverage below is coverage of the union observed by these collectors, never a
claim about every Facebook post. Missing/ambiguous evidence blocks readiness.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
import math
from pathlib import Path
from statistics import fmean, median
from urllib.parse import urlsplit, urlunsplit

from compare import field_presence, match_posts, safe_md, validate_normalized, write_markdown
from normalizer import FACEBOOK_HOSTS, canonical_permalink, identity, timestamp, web_url, write_json
from validation.ground_truth import RESOLUTION_ERRORS, assess_fixture, safe_source_url

COLLECTORS = ("facebook-go", "fbgql")
NAMES = {"facebook-go": "facebook_go", "fbgql": "fbgql"}
MIN_READY_RUNS = 3
CRITERIA = ("success_rate", "observed_union_coverage", "url_presence", "author_presence",
            "posted_at_presence", "image_detection_recall", "median_duration_ms")
FIELDS = ("text", "author", "url", "posted_at", "images_count")


def _mean(values):
    return fmean(values) if values else None


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _bounds(value):
    if not isinstance(value, dict):
        raise ValueError("A fixed time window is required")
    start, end = timestamp(value.get("from")), timestamp(value.get("to"))
    if not start or not end or _time_key(start) > _time_key(end):
        raise ValueError("Invalid or missing time window")
    return {"from": start, "to": end}


def _time_key(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _values(post):
    return {"text": post.get("text"),
            "author": (post.get("author_id"), post.get("author_name")),
            "url": canonical_permalink(post.get("url")) or post.get("url"),
            "posted_at": timestamp(post.get("posted_at")),
            "images_count": len(post.get("images", []))}


def _ref(post):
    return {"post_id": post.get("post_id"), "url": post.get("url")}


def _indexes(envelopes):
    """Use explicit IDs first, unique URL fallback, and exclude conflicting rows.

    A URL observed with two explicit IDs is not a safe alias. Identical duplicate
    observations are counted; conflicting duplicate observations stay ambiguous.
    """
    aliases = defaultdict(set)
    for envelope in envelopes:
        for post in envelope["posts"]:
            pid, url = identity(post.get("post_id")), canonical_permalink(post.get("url"))
            if pid and url:
                aliases[url].add("id:" + pid)
    results = []
    for envelope in envelopes:
        grouped, unidentifiable = defaultdict(list), []
        for post in envelope["posts"]:
            pid, url = identity(post.get("post_id")), canonical_permalink(post.get("url"))
            if pid:
                key = "id:" + pid
            elif url:
                key = next(iter(aliases[url])) if len(aliases[url]) == 1 else "url:" + url
            else:
                unidentifiable.append(_ref(post))
                continue
            grouped[key].append(post)
        reliable, ambiguous = {}, []
        duplicate_rows = 0
        for key, rows in grouped.items():
            duplicate_rows += len(rows) - 1
            url_collision = key.startswith("url:") and len(aliases[key[4:]]) > 1
            conflict = any(_values(post) != _values(rows[0]) for post in rows[1:])
            if conflict or url_collision:
                ambiguous.append({"identity": key, "rows": len(rows), "post": _ref(rows[0]),
                                  "reason": "conflicting_duplicate_fields" if conflict else "conflicting_explicit_ids_for_permalink"})
            else:
                reliable[key] = rows[0]
        results.append({"posts": reliable, "duplicate_rows": duplicate_rows,
                        "unique_posts": len(grouped) + len(unidentifiable),
                        "unidentifiable": unidentifiable, "ambiguous": ambiguous})
    return results


def _known_posts(known_posts):
    if not isinstance(known_posts, list):
        raise ValueError("Known posts must be a list")
    result, seen = [], {}
    for post in known_posts:
        if not isinstance(post, dict) or type(post.get("expected_has_images")) is not bool:
            raise ValueError("Known posts require explicit expected_has_images")
        pid, url = identity(post.get("post_id")), canonical_permalink(post.get("url"))
        original_url = post.get("url")
        safe_url = None
        if web_url(original_url):
            parsed = urlsplit(original_url)
            if parsed.hostname in FACEBOOK_HOSTS:
                safe_url = urlunsplit(("https", parsed.hostname, parsed.path, "", ""))
        unresolved_share = bool(safe_url and post.get("unresolved") is True and urlsplit(safe_url).path.startswith("/share/p/"))
        if not pid and not url and not unresolved_share:
            raise ValueError("Known posts require a post ID or Facebook permalink")
        url_pid = url.removeprefix("post:") if url else None
        if pid and url_pid and pid != url_pid:
            raise ValueError("Known-post ID does not match its canonical URL")
        verified_at = timestamp(post.get("verified_posted_at"))
        if "verified_posted_at" in post and (not isinstance(post.get("verified_posted_at"), str) or not verified_at):
            raise ValueError("verified_posted_at must be a timezone-aware ISO 8601 timestamp")
        key = "id:" + pid if pid else "url:" + (url or safe_url)
        expected = post["expected_has_images"]
        if key in seen:
            if seen[key][0] != expected:
                raise ValueError("Conflicting image ground truth")
            if seen[key][1:] != (safe_url, verified_at):
                raise ValueError("Conflicting known-post URLs or verified timestamps")
            continue
        seen[key] = (expected, safe_url, verified_at)
        result.append({"post_id": pid, "url": safe_url, "key": key, "unresolved": unresolved_share and not pid,
                       "canonical": url or ("post:" + pid if pid and pid.isdigit() else None),
                       "verified_posted_at": verified_at, "expected_has_images": expected})
        if source_url := safe_source_url(post.get("source_url")):
            result[-1]["source_url"] = source_url
        error = post.get("resolution_error")
        if isinstance(error, str) and error in RESOLUTION_ERRORS:
            result[-1]["resolution_error"] = error
    return result


def _known_matches(expected, post):
    left, right = expected["post_id"], identity(post.get("post_id"))
    if left and right:
        return left == right
    url = canonical_permalink(post.get("url")) or ("post:" + right if right and right.isdigit() else None)
    return bool(expected["canonical"] and expected["canonical"] == url)


def _images(known, indexed, group_id, bounds):
    image_posts = [post for post in known if post["expected_has_images"]]
    records, text_only_observed = [], 0
    for expected in known:
        matches = [post for post in indexed["posts"].values() if _known_matches(expected, post)]
        ambiguous_matches = [item["post"] for item in indexed["ambiguous"]
                             if _known_matches(expected, item.get("post", {}))]
        has_observation = bool(matches or ambiguous_matches)
        scope = assess_fixture(expected, group_id, bounds, observed=has_observation)
        if scope["group_status"] == "wrong_group":
            status = "wrong_group"
        elif scope["group_status"] == "group_unverified":
            status = "unresolved_alias" if scope["group_reason"] == "share_alias_unresolved" else "group_unverified"
        elif scope["window_status"] == "outside_window_verified":
            status = "outside_window_verified"
        elif scope["window_status"] == "timestamp_unknown":
            status = "timestamp_unknown"
        elif ambiguous_matches or len(matches) > 1:
            status = "ambiguous_duplicates"
        elif not matches:
            status = "not_observed"
        elif expected["expected_has_images"]:
            status = "detected" if matches[0]["images"] else "extraction_failure"
        else:
            status = "unexpected_images" if matches[0]["images"] else "expected_text_only"
        if status in {"expected_text_only", "unexpected_images"}:
            text_only_observed += 1
        records.append({**expected, **scope, "observed": has_observation, "status": status,
                        "image_count": len(matches[0]["images"]) if len(matches) == 1 else None})

    eligible_images = [r for r in records if r["expected_has_images"] and r["eligible_for_window"]]
    observed = sum(r["observed"] for r in eligible_images)
    detected = sum(r["status"] == "detected" for r in eligible_images)
    scope_statuses = ("wrong_group", "unresolved_alias", "group_unverified", "outside_window_verified", "timestamp_unknown",
                      "not_observed", "detected", "extraction_failure", "expected_text_only",
                      "unexpected_images", "ambiguous_duplicates")
    scope_counts = {status: sum(r["status"] == status for r in records) for status in scope_statuses}
    failures = [r["post_id"] or r["url"] for r in records if r["eligible_for_window"] and r["status"] == "extraction_failure"]
    missing = [r["post_id"] or r["url"] for r in records if r["eligible_for_window"] and r["expected_has_images"] and r["status"] == "not_observed"]
    unexpected = [r["post_id"] or r["url"] for r in records if r["eligible_for_window"] and r["status"] == "unexpected_images"]
    ambiguous = [r["post_id"] or r["url"] for r in records if r["eligible_for_window"] and r["status"] == "ambiguous_duplicates"]
    text_total = sum(not post["expected_has_images"] for post in known)
    eligible_text = sum(not r["expected_has_images"] and r["eligible_for_window"] for r in records)
    return {"ground_truth_supplied": bool(image_posts), "known_image_posts": len(image_posts),
            "eligible_known_image_posts": len(eligible_images),
            "observed_known_image_posts": observed, "detected_image_posts": detected,
            "missing_known_image_posts": missing, "extraction_failures": failures,
            "expected_text_only_posts": text_total, "eligible_text_only_posts": eligible_text,
            "observed_text_only_posts": text_only_observed, "unexpected_image_posts": unexpected,
            "ambiguous_known_posts": ambiguous, "fixture_scope_counts": scope_counts,
            "wrong_group_posts": [r["post_id"] or r["url"] for r in records if r["status"] == "wrong_group"],
            "unverified_group_posts": [r["post_id"] or r["url"] for r in records if r["status"] == "group_unverified"],
            "unresolved_posts": [r["post_id"] or r["url"] for r in records if r["status"] == "unresolved_alias"],
            "outside_window_verified_posts": [r["post_id"] or r["url"] for r in records if r["status"] == "outside_window_verified"],
            "timestamp_unknown_posts": [r["post_id"] or r["url"] for r in records if r["status"] == "timestamp_unknown"],
            "observed_without_images": failures, "records": records,
            "image_detection_recall": detected / len(eligible_images) if eligible_images else None,
            "conditional_image_detection_recall": detected / observed if observed else None,
            "observation_recall": observed / len(eligible_images) if eligible_images else None,
            "note": "Wrong-group, group-unverified, outside-window and unknown-time fixtures are excluded from recall and reported separately; missing eligible posts are collection omissions, not established extraction failures."}


def _per_run(envelope, indexed, known):
    successful = envelope["status"] == "success" and not envelope["errors"]
    window = envelope["window"]
    complete = successful and window.get("complete") is True and window.get("stop_reason") in {"older_than_from", "feed_exhausted"} and window.get("unknown_timestamp_posts", 0) == 0 and window.get("ordering_violations", 0) == 0
    posts = list(indexed["posts"].values())
    input_rows = envelope.get("normalization", {}).get("input_posts", len(envelope["posts"]))
    if not isinstance(input_rows, int) or isinstance(input_rows, bool) or input_rows < len(envelope["posts"]):
        raise ValueError("Invalid normalization input row count")
    return {"status": envelope["status"], "success": successful,
            "skipped": any(isinstance(e, dict) and e.get("skipped") is True for e in envelope["errors"]),
            "duration_ms": envelope["duration_ms"], "pages_fetched": envelope["pages_fetched"],
            "rows_collected": input_rows, "in_window_posts": len(envelope["posts"]), "rows_scanned": window.get("scanned_posts"),
            "unique_posts": indexed["unique_posts"], "identifiable_posts": len(posts),
            "duplicate_rows": indexed["duplicate_rows"], "unidentifiable_posts": len(indexed["unidentifiable"]),
            "ambiguous_posts": len(indexed["ambiguous"]), "errors": len(envelope["errors"]),
            "error_codes": [e.get("code", "unknown_error") if isinstance(e, dict) else "unknown_error" for e in envelope["errors"]],
            "window_complete": complete, "window": window,
            "field_presence": field_presence(posts), "field_presence_denominator": len(posts),
            "post_id_presence": sum(bool(identity(post.get("post_id"))) for post in posts) / len(posts) if posts else None,
            "image_validation": _images(known, indexed, envelope.get("group_id"), window),
            "library_commit": envelope.get("library_commit")}


def _consistency(observations, indexes, run_ids, source):
    transitions = []
    for i in range(1, len(observations)):
        previous, current = indexes[i - 1]["posts"], indexes[i]["posts"]
        before, after = set(previous), set(current)
        changes = []
        field_changes = Counter()
        for key in sorted(before & after):
            old, new = _values(previous[key]), _values(current[key])
            fields = [field for field in FIELDS if old[field] != new[field]]
            if fields:
                field_changes.update(fields)
                changes.append({"identity": key, "post": _ref(current[key]), "changed_fields": fields,
                                "text_lengths": [len(old["text"]) if old["text"] is not None else None,
                                                 len(new["text"]) if new["text"] is not None else None],
                                "image_counts": [old["images_count"], new["images_count"]]})
        comparable = source == "live" and observations[i - 1]["window_complete"] and observations[i]["window_complete"]
        transitions.append({"from_run_id": run_ids[i - 1], "to_run_id": run_ids[i],
                            "comparable": comparable, "added": sorted(after - before), "removed": sorted(before - after),
                            "common_posts": len(before & after), "changed_posts": changes,
                            "field_change_counts": {field: field_changes[field] for field in FIELDS},
                            "post_set_jaccard": len(before & after) / len(before | after) if before | after else None})
    issues = any(t["added"] or t["removed"] or t["changed_posts"] for t in transitions)
    ambiguities = any(index["ambiguous"] or index["unidentifiable"] for index in indexes)
    verified = bool(transitions) and all(t["comparable"] for t in transitions) and not ambiguities
    return {"status": "stable_in_observed_window" if verified and not issues else "changes_observed" if issues else "not_established",
            "transitions": transitions, "comparable_transitions": sum(t["comparable"] for t in transitions),
            "post_sets_consistent": not any(t["added"] or t["removed"] for t in transitions) if verified else None,
            "fields_consistent": not any(t["changed_posts"] for t in transitions) if verified else None,
            "ambiguities": [{"run_id": rid, "posts": index["ambiguous"], "unidentifiable": index["unidentifiable"]}
                            for rid, index in zip(run_ids, indexes) if index["ambiguous"] or index["unidentifiable"]],
            "interpretation": "Changes can reflect feed changes, edited/deleted posts or collection behavior; they are not automatically collector bugs."}


def _aggregate(observations, indexes, source, coverage):
    successful = [item for item in observations if item["success"]]
    durations = [item["duration_ms"] for item in successful if source == "live"]
    weighted_posts = [post for index in indexes for post in index["posts"].values()]
    images = [item["image_validation"] for item in observations]
    expected = sum(item["eligible_known_image_posts"] for item in images)
    observed = sum(item["observed_known_image_posts"] for item in images)
    detected = sum(item["detected_image_posts"] for item in images)
    return {"runs": len(observations), "success_runs": len(successful),
            "success_rate": len(successful) / len(observations) if observations else None,
            "complete_window_runs": sum(item["window_complete"] for item in observations),
            "avg_duration_ms": _mean(durations), "median_duration_ms": median(durations) if durations else None,
            "duration_samples": len(durations), "duration_population": "successful live collector runs only",
            "avg_posts": _mean([item["identifiable_posts"] for item in successful]),
            "avg_posts_all_runs": _mean([item["identifiable_posts"] for item in observations]),
            "avg_observed_union_coverage": _mean(coverage), "coverage_samples": len(coverage),
            "field_presence": field_presence(weighted_posts), "field_presence_denominator": len(weighted_posts),
            "post_id_presence": sum(bool(identity(post.get("post_id"))) for post in weighted_posts) / len(weighted_posts) if weighted_posts else None,
            "duplicate_rows": sum(item["duplicate_rows"] for item in observations),
            "ambiguous_posts": sum(item["ambiguous_posts"] for item in observations),
            "unidentifiable_posts": sum(item["unidentifiable_posts"] for item in observations),
            "errors": sum(item["errors"] for item in observations),
            "images": {"expected_image_observations": expected,
                       "supplied_expected_image_observations": sum(item["known_image_posts"] for item in images),
                       "eligible_known_image_posts": expected,
                       "observed_image_observations": observed,
                       "detected_image_observations": detected,
                       "image_detection_recall": detected / expected if expected else None,
                       "conditional_image_detection_recall": detected / observed if observed else None,
                       "observation_recall": observed / expected if expected else None,
                       "extraction_failures": sum(len(item["extraction_failures"]) for item in images),
                       "missing_known_image_observations": sum(len(item["missing_known_image_posts"]) for item in images),
                       "unexpected_image_observations": sum(len(item["unexpected_image_posts"]) for item in images),
                       "eligible_text_only_observations": sum(item["eligible_text_only_posts"] for item in images),
                       "fixture_scope_counts": {key: sum(item["fixture_scope_counts"][key] for item in images)
                                                for key in images[0]["fixture_scope_counts"]} if images else {},
                       "wrong_group_observations": sum(item["fixture_scope_counts"]["wrong_group"] for item in images),
                       "unresolved_alias_observations": sum(item["fixture_scope_counts"]["unresolved_alias"] for item in images),
                       "group_unverified_observations": sum(item["fixture_scope_counts"]["group_unverified"] for item in images),
                       "outside_window_verified_observations": sum(item["fixture_scope_counts"]["outside_window_verified"] for item in images),
                       "timestamp_unknown_observations": sum(item["fixture_scope_counts"]["timestamp_unknown"] for item in images)},
            "per_run": observations}


def _omissions(runs, paired_ok, only_samples):
    """Report evidence from the opposite collector without guessing a cause."""
    result = {}
    for omitted in COLLECTORS:
        other = COLLECTORS[1] if omitted == COLLECTORS[0] else COLLECTORS[0]
        cases, by_identity, by_type = [], defaultdict(set), Counter()
        for i, (run, controlled) in enumerate(zip(runs, paired_ok)):
            supplied = run.get("diagnostics", {}).get(other, [])
            if not isinstance(supplied, list) or any(not isinstance(item, dict) for item in supplied):
                raise ValueError("Collector diagnostics must be lists")
            diagnostics = []
            for post in only_samples[i][other]:
                matches = [item for item in supplied if (item.get("post_id") and item.get("post_id") == post.get("post_id")) or
                           (not item.get("post_id") and canonical_permalink(item.get("url")) and canonical_permalink(item.get("url")) == canonical_permalink(post.get("url")))]
                diagnostic = dict(matches[0]) if len(matches) == 1 else {"post_id": post.get("post_id"), "url": post.get("url")}
                diagnostic.setdefault("possible_type", post.get("validation", {}).get("possible_type", "unknown"))
                diagnostics.append(diagnostic)
            for diagnostic in diagnostics:
                if not isinstance(diagnostic, dict):
                    raise ValueError("Malformed collector-only diagnostic")
                key = identity(diagnostic.get("post_id")) or canonical_permalink(diagnostic.get("url"))
                kind = diagnostic.get("possible_type") if isinstance(diagnostic.get("possible_type"), str) else "unknown"
                record = {"run_id": run["metadata"]["run_id"], "post_id": diagnostic.get("post_id"),
                          "url": diagnostic.get("url"), "possible_type": kind,
                          "controlled_window_pair": controlled, "reason": "not_established",
                          "reviewed_explanation_supplied": diagnostic.get("reason_status") == "explained" and bool(diagnostic.get("explanation"))}
                cases.append(record)
                if controlled:
                    by_type[kind] += 1
                    if key and not record["reviewed_explanation_supplied"]:
                        by_identity[key].add(run["metadata"]["run_id"])
        result[NAMES[omitted]] = {"collector": omitted, "observed_in": other, "cases": cases,
                                 "unresolved_cases": sum(case["controlled_window_pair"] and not case["reviewed_explanation_supplied"] for case in cases),
                                 "post_type_counts": dict(by_type),
                                 "repeated_missing_identities": [{"identity": key, "runs": sorted(ids)}
                                                                 for key, ids in sorted(by_identity.items()) if len(ids) >= 2],
                                 "interpretation": "These posts were observed only by the other collector. Layout/pagination/feed causes require raw evidence or manual review; no cause is inferred."}
    return result


def _ready_reasons(collector, metrics, consistency, source, group, paired_count, omissions):
    reasons = []
    if source != "live":
        reasons.append("Live evidence is required; synthetic/unknown data cannot establish readiness.")
    if group is None:
        reasons.append("The target group was not resolved.")
    if metrics["runs"] < MIN_READY_RUNS or metrics["complete_window_runs"] < MIN_READY_RUNS:
        reasons.append(f"At least {MIN_READY_RUNS} successful, complete fixed-window runs are required.")
    if metrics["success_rate"] != 1.0:
        reasons.append("Collection failures remain in the repeated-run sample.")
    if metrics["complete_window_runs"] != metrics["runs"]:
        reasons.append("Some windows are incomplete or contain unknown timestamps/ordering violations.")
    if paired_count < MIN_READY_RUNS:
        reasons.append(f"At least {MIN_READY_RUNS} complete nonempty paired live runs are needed for observed coverage.")
    required = metrics["field_presence"]
    for field in ("text", "author", "url", "posted_at"):
        if required[field] != 1.0:
            reasons.append(f"Required field {field} is missing on some observations, or no usable observations exist.")
    if any(not item["identifiable_posts"] for item in metrics["per_run"]):
        reasons.append("Every repeat needs a nonempty identifiable sample.")
    if metrics["ambiguous_posts"] or metrics["unidentifiable_posts"]:
        reasons.append("Missing or ambiguous post identities need investigation.")
    if metrics["post_id_presence"] != 1.0:
        reasons.append("Explicit post IDs are missing on some observations, or no usable observations exist.")
    if metrics["images"]["expected_image_observations"] == 0:
        reasons.append("No user-confirmed known-image posts were verified in the target group and fixed time window; media extraction is unverified.")
    elif metrics["images"]["image_detection_recall"] != 1.0:
        reasons.append("Some known-image posts are missing or have no extracted images.")
    scope = metrics["images"]["fixture_scope_counts"]
    if scope.get("wrong_group", 0):
        reasons.append("Some supplied fixtures resolve to a different numeric group ID.")
    if scope.get("unresolved_alias", 0):
        reasons.append("Some supplied share aliases remain unresolved and cannot verify their group.")
    if scope.get("group_unverified", 0):
        reasons.append("Some supplied fixtures have no verified numeric group ID, such as post-ID-only entries.")
    if scope.get("timestamp_unknown", 0):
        reasons.append("Some same-group fixtures have unknown timestamps and cannot be assigned to the fixed window.")
    if metrics["images"]["supplied_expected_image_observations"] and metrics["images"]["eligible_text_only_observations"] == 0:
        reasons.append("No supplied text-only fixture was verified in the target group and fixed time window.")
    if metrics["images"]["unexpected_image_observations"]:
        reasons.append("Images were detected on known text-only posts; possible false positives require review.")
    if consistency["status"] != "stable_in_observed_window":
        reasons.append("Repeat identity/field consistency is not established or changes require review.")
    if omissions["unresolved_cases"]:
        reasons.append("Collector-only observations from the other collector still need a documented explanation.")
    return reasons


def build_stability(runs: list[dict], known_posts: list[dict]) -> dict:
    """Aggregate a compatible suite; reject mixed targets/windows or stale runs."""
    if not isinstance(runs, list):
        raise ValueError("Runs must be a list")
    known = _known_posts(known_posts)
    run_ids, bounds, group, source, group_input = [], None, None, None, None
    envelopes = {collector: [] for collector in COLLECTORS}
    for run in runs:
        if not isinstance(run, dict) or not isinstance(run.get("metadata"), dict):
            raise ValueError("Run metadata is required")
        metadata = run["metadata"]
        rid = metadata.get("run_id")
        if not isinstance(rid, str) or not rid or rid in run_ids:
            raise ValueError("Run IDs must be nonempty and unique")
        current_bounds = _bounds(metadata)
        current_group = identity(metadata.get("group_id"))
        current_source = metadata.get("source", "unknown")
        if run_ids and (bounds != current_bounds or group != current_group):
            raise ValueError("Stability runs must use the same group and fixed time window")
        if run_ids and source != current_source:
            raise ValueError("Do not mix live, synthetic and unknown-source runs")
        if run_ids and group is None and group_input != metadata.get("group_input"):
            raise ValueError("Unresolved runs must have the same group input")
        bounds, group, source = current_bounds, current_group, current_source
        group_input = metadata.get("group_input")
        run_ids.append(rid)
        for collector in COLLECTORS:
            envelope = validate_normalized(run.get("collectors", {}).get(collector), collector)
            if envelope["run_id"] != rid or identity(envelope["group_id"]) != group:
                raise ValueError("Collector metadata does not match its suite run")
            if envelope.get("source", "unknown") != current_source:
                raise ValueError("Collector data source does not match run metadata")
            if _bounds(envelope.get("window")) != bounds:
                raise ValueError("Collector window does not match suite metadata")
            for post in envelope["posts"]:
                observed_time = timestamp(post.get("posted_at"))
                if not observed_time or not (_time_key(bounds["from"]) <= _time_key(observed_time) <= _time_key(bounds["to"])):
                    raise ValueError("Normalized post has no valid timestamp in the fixed time window")
            envelopes[collector].append(envelope)
    source = source or "unknown"
    indexes = {collector: _indexes(envelopes[collector]) for collector in COLLECTORS}
    observations = {collector: [_per_run(envelope, indexed, known) for envelope, indexed in zip(envelopes[collector], indexes[collector])]
                    for collector in COLLECTORS}
    coverage, paired_ok, matching, only_samples = {collector: [] for collector in COLLECTORS}, [], [], []
    for i, run in enumerate(runs):
        left, right = indexes["facebook-go"][i], indexes["fbgql"][i]
        match = match_posts(list(left["posts"].values()), list(right["posts"].values()), group)
        union = len(left["posts"]) + len(right["posts"]) - len(match["matches"])
        controlled = source == "live" and all(observations[c][i]["window_complete"] and indexes[c][i]["posts"]
                                                  and not indexes[c][i]["ambiguous"] and not indexes[c][i]["unidentifiable"] for c in COLLECTORS)
        controlled = bool(controlled and not match["ambiguous_left"] and not match["ambiguous_right"])
        paired_ok.append(controlled)
        only_samples.append({"facebook-go": match["only_left"], "fbgql": match["only_right"]})
        matching.append({"run_id": run_ids[i], "controlled_live_pair": controlled, "observed_union_posts": union,
                         "matched": len(match["matches"]), "only_facebook_go": len(match["only_left"]),
                         "only_fbgql": len(match["only_right"]),
                         "ambiguous_facebook_go": len(match["ambiguous_left"]), "ambiguous_fbgql": len(match["ambiguous_right"])})
        if controlled and union:
            for collector in COLLECTORS:
                coverage[collector].append(len(indexes[collector][i]["posts"]) / union)
    metrics = {NAMES[c]: _aggregate(observations[c], indexes[c], source, coverage[c]) for c in COLLECTORS}
    consistency = {NAMES[c]: _consistency(observations[c], indexes[c], run_ids, source) for c in COLLECTORS}
    omissions = _omissions(runs, paired_ok, only_samples)
    health = {}
    for collector in COLLECTORS:
        name = NAMES[collector]
        reasons = _ready_reasons(collector, metrics[name], consistency[name], source, group, sum(paired_ok), omissions[name])
        health[name] = {"collector": collector, "status": "READY FOR PHASE 1" if not reasons else "NOT READY FOR PHASE 1", "blockers": reasons}

    def score(collector):
        item = metrics[NAMES[collector]]
        missing = lambda value: value if value is not None else -1
        return (missing(item["success_rate"]), missing(item["avg_observed_union_coverage"]),
                missing(item["field_presence"]["url"]), missing(item["field_presence"]["author"]),
                missing(item["field_presence"]["posted_at"]), missing(item["images"]["image_detection_recall"]),
                -item["median_duration_ms"] if item["median_duration_ms"] is not None else -math.inf)

    candidate = None
    if source == "live" and sum(paired_ok) >= MIN_READY_RUNS:
        ranking = sorted(COLLECTORS, key=score, reverse=True)
        if score(ranking[0]) != score(ranking[1]):
            candidate = ranking[0]
    primary = candidate if candidate and health[NAMES[candidate]]["status"] == "READY FOR PHASE 1" else None
    fallback = next((c for c in COLLECTORS if c != primary and metrics[NAMES[c]]["success_runs"] >= 1), None) if primary else None
    blockers = health[NAMES[candidate or "facebook-go"]]["blockers"]
    if candidate is None and source == "live" and sum(paired_ok) >= MIN_READY_RUNS:
        blockers = [*blockers, "Collector ranking is tied; no primary collector was selected."]
    recommendation = {"primary": primary, "fallback": fallback, "primary_candidate": candidate,
                      "phase1_status": "READY FOR PHASE 1" if primary else "NOT READY FOR PHASE 1",
                      "criteria_order": list(CRITERIA), "minimum_repeated_runs": MIN_READY_RUNS,
                      "decision_rule": "Lexicographic criteria in the documented order; a tie selects nobody. Candidate ranking is distinct from readiness.",
                      "blockers": blockers if not primary else [],
                      "reason": "A collector met all observed-sample readiness gates." if primary else "The available evidence does not satisfy every Phase 0.5 exit criterion."}
    go = metrics["facebook_go"]
    go_missing = omissions["facebook_go"]
    fbgql_only_total = sum(item["only_fbgql"] for item in matching)
    if fbgql_only_total == 0 and metrics["fbgql"]["field_presence"]["author"] == 0:
        fbgql_role = "No additional posts were observed in this sample; author data was absent from all fbgql observations, so its current value is limited to a comparison/audit signal."
    elif fbgql_only_total == 0:
        fbgql_role = "No additional posts were observed in this sample; its current value is a comparison/audit signal."
    else:
        fbgql_role = "Some posts appeared only in fbgql; review their saved evidence before assigning fallback value."
    fbgql_metrics = metrics["fbgql"]
    error_word = "error" if fbgql_metrics["errors"] == 1 else "errors"
    fbgql_run_summary = (f"Collection succeeded in {fbgql_metrics['success_runs']}/{fbgql_metrics['runs']} runs; "
                         f"{fbgql_metrics['complete_window_runs']}/{fbgql_metrics['runs']} windows completed; "
                         f"{fbgql_metrics['errors']} collection {error_word}.") if fbgql_metrics["runs"] else "No fbgql runs were available."
    answers = {
        "A": {"question": "Does facebook-go have enough data for Guitar Radar?", "answer": health["facebook_go"]["status"], "evidence": health["facebook_go"]["blockers"]},
        "B": {"question": "Does image extraction work?", "answer": "Verified on all eligible, user-confirmed known-image observations" if go["images"]["expected_image_observations"] > 0 and go["images"]["image_detection_recall"] == 1.0 and source == "live" else "Not fully verified on live known-image posts",
              "evidence": {"facebook_go": go["images"], "fbgql": metrics["fbgql"]["images"]}},
        "C": {"question": "Is facebook-go stable across repeated runs?", "answer": consistency["facebook_go"]["status"], "evidence": {"runs": go["runs"], "success_rate": go["success_rate"], "complete_window_runs": go["complete_window_runs"]}},
        "D": {"question": "What value does fbgql provide as fallback?", "answer": f"{fbgql_role} {fbgql_run_summary}",
              "evidence": {"success_rate": metrics["fbgql"]["success_rate"], "only_fbgql_per_run": [item["only_fbgql"] for item in matching],
                           "additional_posts_in_sample": fbgql_only_total, "field_presence": metrics["fbgql"]["field_presence"]}},
        "E": {"question": "Does facebook-go systematically miss a post type?", "answer": "Repeated collector-only evidence requires investigation" if go_missing["repeated_missing_identities"] else "No systematic omission established by this sample; absence of evidence is not proof of complete coverage",
              "evidence": {"post_type_counts": go_missing["post_type_counts"], "repeated_missing_identities": go_missing["repeated_missing_identities"]}},
        "F": {"question": "Are there blockers before Phase 1?", "answer": recommendation["phase1_status"], "evidence": recommendation["blockers"]},
    }
    return {"schema_version": 1, "phase": "0.5", "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": source, "group_id": group, "group_input": group_input, "window": bounds, "run_ids": run_ids,
            "metadata": [run["metadata"] for run in runs], "known_post_count": len(known),
            "evaluated_known_posts": known,
            "facebook_go": metrics["facebook_go"], "fbgql": metrics["fbgql"],
            "matching": matching, "controlled_paired_runs": sum(paired_ok), "consistency": consistency,
            "collector_only": omissions, "health": health, "recommendation": recommendation, "answers": answers,
            "limitations": ["Coverage uses only the union observed in complete paired samples; Facebook ground truth is unavailable.",
                            "Field presence does not establish complete captions or complete image galleries.",
            "Image detection recall uses only user-supplied image fixtures verified for this numeric group and fixed window; invalid-scope fixtures are reported separately.",
            "Conditional image recall excludes eligible fixtures not observed; missing in-window posts and observed extraction failures are reported separately.",
                            "Duration summaries include only successful live runs, not failed startup or synthetic timings.",
                            "Unexpected set/field changes require review; Facebook can edit, delete or reorder posts.",
                            "At least three complete repetitions is a minimum POC gate, not a guarantee of production reliability."]}


def _percent(value):
    return f"{value:.1%}" if value is not None else "N/A"


def _display(value):
    return "N/A" if value is None else safe_md(value)


def _stability_md(report):
    lines = ["# Collector Stability", "", f"Source: {safe_md(report['source'])}", "",
             f"Group: {_display(report['group_id'])}", "",
             f"Window: {_display(report['window'])}", "",
             "| Metric | facebook-go | fbgql |", "|---|---:|---:|"]
    for title, key in (("Runs", "runs"), ("Successes", "success_runs"), ("Complete window runs", "complete_window_runs"),
                       ("Average duration ms (successful live only)", "avg_duration_ms"), ("Median duration ms", "median_duration_ms"),
                       ("Average identifiable posts (successful runs)", "avg_posts"), ("Errors", "errors"),
                       ("Duplicate rows", "duplicate_rows"), ("Ambiguous identities", "ambiguous_posts"), ("Unidentifiable posts", "unidentifiable_posts")):
        lines.append(f"| {title} | {_display(report['facebook_go'][key])} | {_display(report['fbgql'][key])} |")
    for title, values in (("Success rate", [report[c]["success_rate"] for c in ("facebook_go", "fbgql")]),
                          ("Observed union coverage", [report[c]["avg_observed_union_coverage"] for c in ("facebook_go", "fbgql")]),
                          ("Known-image detection recall", [report[c]["images"]["image_detection_recall"] for c in ("facebook_go", "fbgql")])):
        lines.append(f"| {title} | {_percent(values[0])} | {_percent(values[1])} |")
    lines += ["", "## Ground-truth fixture scope", "",
              "Counts are fixture observations across runs. Invalid group/time fixtures do not enter image recall.", "",
              "| Status | facebook-go | fbgql |", "|---|---:|---:|"]
    for title, key in (("Wrong numeric group", "wrong_group_observations"),
                       ("Share alias unresolved", "unresolved_alias_observations"),
                       ("Other group unverified", "group_unverified_observations"),
                       ("Outside window with verified time", "outside_window_verified_observations"),
                       ("Timestamp unknown", "timestamp_unknown_observations"),
                       ("Eligible known image posts observed without images", "extraction_failures")):
        lines.append(f"| {title} | {report['facebook_go']['images'][key]} | {report['fbgql']['images'][key]} |")
    lines += ["", "## Field presence", "", "| Field | facebook-go | fbgql |", "|---|---:|---:|"]
    for field in ("text", "author", "url", "images", "posted_at"):
        lines.append(f"| {field} | {_percent(report['facebook_go']['field_presence'][field])} | {_percent(report['fbgql']['field_presence'][field])} |")
    lines += ["", "## Per run", "", "| Run | Collector | Status | Window complete | Rows | Identifiable | Duplicates | Errors | Duration ms |", "|---|---|---|---|---:|---:|---:|---:|---:|"]
    for i, rid in enumerate(report["run_ids"]):
        for collector in COLLECTORS:
            item = report[NAMES[collector]]["per_run"][i]
            lines.append(f"| {safe_md(rid)} | {collector} | {item['status']} | {item['window_complete']} | {item['rows_collected']} | {item['identifiable_posts']} | {item['duplicate_rows']} | {item['errors']} | {item['duration_ms']} |")
    lines += ["", "## Limits", ""] + ["- " + item for item in report["limitations"]]
    return "\n".join(lines) + "\n"


def _consistency_md(report):
    lines = ["# Fixed-window Consistency", ""]
    for collector in COLLECTORS:
        data = report["consistency"][NAMES[collector]]
        lines += ["## " + collector, "", "Status: " + data["status"], "", data["interpretation"], ""]
        for transition in data["transitions"]:
            lines += [f"### {safe_md(transition['from_run_id'])} → {safe_md(transition['to_run_id'])}", "",
                      f"Comparable live windows: {transition['comparable']}", "",
                      "Added: " + (", ".join(safe_md(v) for v in transition["added"]) or "none"), "",
                      "Removed: " + (", ".join(safe_md(v) for v in transition["removed"]) or "none"), ""]
            for change in transition["changed_posts"]:
                lines.append(f"- {safe_md(change['identity'])}: changed {', '.join(change['changed_fields'])}; text lengths {change['text_lengths']}; image counts {change['image_counts']}")
            if not transition["changed_posts"]:
                lines.append("No field changes among reliable matched identities.")
            lines.append("")
        if data["ambiguities"]:
            lines += ["Ambiguous or unidentified observations were excluded from arbitrary pairing; see consistency.json.", ""]
    return "\n".join(lines) + "\n"


def _health_md(report):
    lines = ["# Collector Health", "", "Evidence source: " + safe_md(report["source"]), ""]
    for collector in COLLECTORS:
        name = NAMES[collector]
        health, metrics = report["health"][name], report[name]
        lines += ["## " + collector, "", "Status: " + health["status"], "",
                  "Success rate: " + _percent(metrics["success_rate"]), "",
                  "Observed union coverage: " + _percent(metrics["avg_observed_union_coverage"]), "",
                  "Known-image detection recall: " + _percent(metrics["images"]["image_detection_recall"]), "",
                  "Fixture scope counts: " + safe_md(metrics["images"]["fixture_scope_counts"]), ""]
        lines += ["- " + item for item in health["blockers"]] or ["All declared POC evidence gates passed in this sample."]
        lines.append("")
    decision = report["recommendation"]
    lines += ["## Recommendation", "", "Primary: " + (decision["primary"] or "not established"), "",
              "Fallback/audit: " + (decision["fallback"] or "not established"), "",
              "Metric candidate: " + (decision["primary_candidate"] or "not established"), "",
              "Phase 1: " + decision["phase1_status"], "", decision["reason"], "",
              "Criteria order: " + ", ".join(decision["criteria_order"]), "", decision["decision_rule"]]
    return "\n".join(lines) + "\n"


def _final_md(report):
    lines = ["# Phase 0.5 Validation", "", report["recommendation"]["phase1_status"], ""]
    for key, answer in report["answers"].items():
        lines += [f"## {key}. {answer['question']}", "", answer["answer"], ""]
        if isinstance(answer["evidence"], list):
            lines += ["- " + str(item) for item in answer["evidence"]] or ["No unresolved readiness blockers in the observed sample."]
        else:
            lines.append("See stability.json for exact per-run and aggregate evidence.")
        lines.append("")
    lines += ["## Collector-only evidence", ""]
    for collector in COLLECTORS:
        info = report["collector_only"][NAMES[collector]]
        lines += ["### Potential omissions by " + collector, "", info["interpretation"], ""]
        for case in info["cases"]:
            lines.append(f"- Run {safe_md(case['run_id'])}: ID {_display(case['post_id'])}, type {safe_md(case['possible_type'])}, controlled window pair {case['controlled_window_pair']}, cause not established.")
        if not info["cases"]:
            lines.append("No collector-only diagnostic records were supplied for this collector.")
        lines.append("")
    lines += ["## Interpretation limits", ""] + ["- " + value for value in report["limitations"]]
    lines += ["", "Phase 1 is not started by this tool."]
    return "\n".join(lines) + "\n"


def write_reports(report: dict, output_dir: Path) -> None:
    """Persist private aggregate artifacts; caller owns per-run preservation."""
    output_dir = Path(output_dir)
    write_json(output_dir / "stability.json", report)
    write_markdown(output_dir / "stability.md", _stability_md(report))
    consistency = {"schema_version": 1, "group_id": report["group_id"], "window": report["window"],
                   "run_ids": report["run_ids"], "source": report["source"], **report["consistency"]}
    write_json(output_dir / "consistency.json", consistency)
    write_markdown(output_dir / "consistency.md", _consistency_md(report))
    write_markdown(output_dir / "collector-health.md", _health_md(report))
    write_markdown(output_dir / "final-report.md", _final_md(report))

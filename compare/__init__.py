"""Exact post identity comparison, with uncertainty kept visible."""

from __future__ import annotations

import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from normalizer import canonical_permalink, identity, text, validate_envelope, web_url

FIELDS = ("text", "author", "url", "images", "posted_at")
SETTINGS = ("requested_pages", "page_size", "sort")


def reference(post: dict, index: int | None = None) -> dict:
    result = {"post_id": post.get("post_id"), "url": post.get("url")}
    if index is not None:
        result["index"] = index
    return result


def validate_normalized(run: object, collector: str) -> dict:
    run = validate_envelope(run, collector)
    for post in run["posts"]:
        if not isinstance(post, dict) or post.get("collector") != collector or identity(post.get("group_id")) != identity(run["group_id"]):
            raise ValueError("Normalized post has inconsistent collector or group")
        for key in ("post_id", "group_name", "author_id", "author_name", "text", "url", "posted_at", "collected_at"):
            if key not in post or (post[key] is not None and not isinstance(post[key], str)):
                raise ValueError("Invalid normalized scalar field")
        if not isinstance(post.get("images"), list) or any(not web_url(image) for image in post["images"]):
            raise ValueError("Invalid normalized images")
    return run


def deduplicate(posts: list[dict], group: str) -> tuple[list[dict], list[dict], list[dict]]:
    """Keep the first observation for each identity; never mix field values."""
    unique, duplicates, unidentifiable = [], [], []
    by_id, url_only = {}, {}
    explicit_urls = defaultdict(list)
    # ID-bearing rows are considered first so URL-only copies can be associated
    # with a unique known ID regardless of original feed order.
    for index, post in enumerate(posts):
        post_id = identity(post.get("post_id"))
        if not post_id:
            continue
        if post_id in by_id:
            duplicates.append({"duplicate": reference(post, index), "kept": reference(by_id[post_id])})
            continue
        by_id[post_id] = post
        unique.append(post)
        url = canonical_permalink(post.get("url"), group)
        if url:
            explicit_urls[url].append(post)
    for index, post in enumerate(posts):
        if identity(post.get("post_id")):
            continue
        url = canonical_permalink(post.get("url"), group)
        if not url:
            unidentifiable.append(reference(post, index))
            unique.append(post)
            continue
        known = explicit_urls.get(url, [])
        kept = known[0] if len(known) == 1 else url_only.get(url)
        if kept is not None:
            duplicates.append({"duplicate": reference(post, index), "kept": reference(kept)})
        else:
            url_only[url] = post
            unique.append(post)
    return unique, duplicates, unidentifiable


def match_posts(left: list[dict], right: list[dict], group: str) -> dict:
    left_ids = {post["post_id"]: index for index, post in enumerate(left) if post.get("post_id")}
    right_ids = {post["post_id"]: index for index, post in enumerate(right) if post.get("post_id")}
    used_left, used_right, matches = set(), set(), []
    for post_id in left_ids:
        if post_id in right_ids:
            i, j = left_ids[post_id], right_ids[post_id]
            used_left.add(i)
            used_right.add(j)
            matches.append((i, j, "post_id"))
    candidates_left, candidates_right = defaultdict(set), defaultdict(set)
    for i, first in enumerate(left):
        if i in used_left:
            continue
        url = canonical_permalink(first.get("url"), group)
        if not url:
            continue
        for j, second in enumerate(right):
            if j in used_right or canonical_permalink(second.get("url"), group) != url:
                continue
            # Two populated, unequal post IDs are evidence of different posts,
            # even if the library happened to attach an identical permalink.
            if first.get("post_id") and second.get("post_id"):
                continue
            candidates_left[i].add(j)
            candidates_right[j].add(i)
    for i, candidates in candidates_left.items():
        if len(candidates) == 1:
            j = next(iter(candidates))
            if len(candidates_right[j]) == 1:
                matches.append((i, j, "canonical_permalink"))
                used_left.add(i)
                used_right.add(j)
    ambiguous_left = set(candidates_left) - used_left
    ambiguous_right = set(candidates_right) - used_right
    comparable = lambda post: bool(post.get("post_id") or canonical_permalink(post.get("url"), group))
    only_left = [post for i, post in enumerate(left) if i not in used_left and i not in ambiguous_left and comparable(post)]
    only_right = [post for j, post in enumerate(right) if j not in used_right and j not in ambiguous_right and comparable(post)]
    return {"matches": matches, "only_left": only_left, "only_right": only_right,
            "ambiguous_left": [reference(left[i]) for i in sorted(ambiguous_left)],
            "ambiguous_right": [reference(right[j]) for j in sorted(ambiguous_right)]}


def field_presence(posts: list[dict]) -> dict:
    checks = {"text": lambda p: bool(text(p.get("text"))),
              "author": lambda p: bool(text(p.get("author_id")) or text(p.get("author_name"))),
              "url": lambda p: bool(web_url(p.get("url"))),
              "images": lambda p: bool(p.get("images")),
              "posted_at": lambda p: bool(p.get("posted_at"))}
    return {field: sum(check(post) for post in posts) / len(posts) if posts else None for field, check in checks.items()}


def pair_evidence(first: dict, second: dict, match_by: str) -> dict:
    first_text, second_text = first.get("text"), second.get("text")
    prefix = None
    if first_text and second_text and first_text != second_text:
        if second_text.startswith(first_text):
            prefix = "facebook-go"
        elif first_text.startswith(second_text):
            prefix = "fbgql"
    both_images = bool(first.get("images")) and bool(second.get("images"))
    return {"facebook_go": reference(first), "fbgql": reference(second), "match_by": match_by,
            "text_lengths": {"facebook_go": len(first_text) if first_text is not None else None,
                             "fbgql": len(second_text) if second_text is not None else None},
            "text_equal": first_text == second_text if first_text is not None and second_text is not None else None,
            "possible_truncation_in": prefix,
            "author_name_equal": first["author_name"] == second["author_name"] if first.get("author_name") and second.get("author_name") else None,
            "posted_at_equal": first["posted_at"] == second["posted_at"] if first.get("posted_at") and second.get("posted_at") else None,
            "image_counts": {"facebook_go": len(first["images"]), "fbgql": len(second["images"])},
            "image_url_sets_equal": set(first["images"]) == set(second["images"]) if both_images else None}


def summary(run: dict, unique: list[dict], duplicates: list[dict], unidentifiable: list[dict]) -> dict:
    return {"total_posts": len(run["posts"]), "unique_posts": len(unique),
            "unique_identifiable_posts": len(unique) - len(unidentifiable), "duplicate_rows": len(duplicates),
            "unidentifiable_posts": len(unidentifiable), "duration_ms": run["duration_ms"],
            "errors": len(run["errors"]), "error_details": run["errors"], "status": run["status"],
            "source": run.get("source", "unknown"), "pages_fetched": run["pages_fetched"],
            "settings": {field: run.get(field) for field in SETTINGS}, "library_commit": run.get("library_commit"),
            "duplicates": duplicates, "unidentifiable": unidentifiable}


def build_report(first: object, second: object) -> dict:
    first = validate_normalized(first, "facebook-go")
    second = validate_normalized(second, "fbgql")
    group = identity(first["group_id"])
    if group != identity(second["group_id"]):
        raise ValueError("Collectors must target the same group")
    if group is None and first.get("group_input") != second.get("group_input"):
        raise ValueError("Unresolved failed runs must describe the same group input")
    if first["run_id"] != second["run_id"]:
        raise ValueError("Collectors have different run_id values; refusing stale comparison")
    if first.get("source", "unknown") != second.get("source", "unknown"):
        raise ValueError("Collectors have different data sources; refusing mixed-source comparison")
    left, duplicates_left, unidentifiable_left = deduplicate(first["posts"], group)
    right, duplicates_right, unidentifiable_right = deduplicate(second["posts"], group)
    matched = match_posts(left, right, group)
    pairs = [pair_evidence(left[i], right[j], method) for i, j, method in matched["matches"]]
    settings_differ = [field for field in SETTINGS if first.get(field) != second.get(field)]
    settings_unknown = [field for field in SETTINGS if first.get(field) is None or second.get(field) is None]
    notes = ["Counts describe these bounded samples, not all posts in the group.",
             "Field completeness is nonempty field presence, not verified caption/gallery completeness.",
             "An empty images array can represent a text-only post or a collector limitation.",
             "Text-prefix and image-URL differences are evidence to inspect, not proof of truncation or missing images.",
             "CDN URLs may differ for the same image; URL-set equality does not establish visual identity.",
             "Repeated runs are needed to evaluate stability; this report contains one paired run.",
             "Only-in-sample lists exclude unidentifiable and ambiguous posts; they do not prove a global omission."]
    if settings_differ:
        notes.append("Collector settings differ: " + ", ".join(settings_differ))
    if settings_unknown:
        notes.append("Collector settings were not recorded: " + ", ".join(settings_unknown))
    reasons = []
    if any(run.get("source") != "live" for run in (first, second)):
        reasons.append("Input includes synthetic or unknown-source data; this is not a live benchmark.")
    if any(run["status"] != "success" or run["errors"] for run in (first, second)):
        reasons.append("At least one collection or normalization run has errors or incomplete status.")
    if not left or not right:
        reasons.append("Both collectors need a nonempty sample before a useful comparison.")
    if unidentifiable_left or unidentifiable_right or matched["ambiguous_left"] or matched["ambiguous_right"]:
        reasons.append("Some post identities are missing or ambiguous.")
    if settings_differ or settings_unknown:
        reasons.append("Matching page limits, page sizes and sort settings are required for a controlled sample.")
    observation = {
        "more_unique_identifiable_posts": "facebook-go" if len(left) - len(unidentifiable_left) > len(right) - len(unidentifiable_right) else "fbgql" if len(right) - len(unidentifiable_right) > len(left) - len(unidentifiable_left) else "tie",
        "shorter_duration": "facebook-go" if first["duration_ms"] < second["duration_ms"] else "fbgql" if second["duration_ms"] < first["duration_ms"] else "tie",
    }
    if reasons:
        observation = {key: None for key in observation}
    return {"schema_version": 1, "group_id": group, "run_id": first["run_id"],
            "group_input": first.get("group_input"),
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "facebook_go": summary(first, left, duplicates_left, unidentifiable_left),
            "fbgql": summary(second, right, duplicates_right, unidentifiable_right),
            "comparison": {"matched_posts": len(pairs), "only_facebook_go": len(matched["only_left"]),
                "only_fbgql": len(matched["only_right"]), "matched": pairs,
                "only_facebook_go_posts": [reference(post) for post in matched["only_left"]],
                "only_fbgql_posts": [reference(post) for post in matched["only_right"]],
                "ambiguous_facebook_go": matched["ambiguous_left"], "ambiguous_fbgql": matched["ambiguous_right"]},
            "field_completeness": {"facebook_go": field_presence(left), "fbgql": field_presence(right)},
            "field_completeness_denominator": {"facebook_go": len(left), "fbgql": len(right)},
            "assessment": {"status": "inconclusive" if reasons else "observed_single_run", "recommended_collector": None,
                "reasons": reasons or ["A single bounded run cannot establish long-term reliability or a best collector."],
                "observed_sample_metrics": observation, "stability": "not_evaluated"},
            "notes": notes}


def safe_md(value: object) -> str:
    return str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|").replace("`", "\\`").replace("<", "&lt;")


def markdown_report(report: dict) -> str:
    first, second, match = report["facebook_go"], report["fbgql"], report["comparison"]
    lines = ["# Facebook Collector Comparison", "", f"Group: {safe_md(report['group_id'] or report.get('group_input') or 'unresolved')}", "",
             f"Run: {safe_md(report['run_id'])}", "", "## Collection", "",
             "| Metric | facebook-go | fbgql |", "|---|---:|---:|"]
    for title, key in (("Source", "source"), ("Status", "status"), ("Rows collected", "total_posts"), ("Unique identifiable posts", "unique_identifiable_posts"),
                       ("Duplicate rows", "duplicate_rows"), ("Unidentifiable posts", "unidentifiable_posts"), ("Duration (ms)", "duration_ms"), ("Pages fetched", "pages_fetched"), ("Errors", "errors")):
        lines.append(f"| {title} | {safe_md(first[key])} | {safe_md(second[key])} |")
    lines += ["", "## Matching", "", f"Posts found by both: {match['matched_posts']}", "",
              f"Only facebook-go (identifiable sample): {match['only_facebook_go']}", "",
              f"Only fbgql (identifiable sample): {match['only_fbgql']}", "",
              f"Ambiguous identities: facebook-go={len(match['ambiguous_facebook_go'])}, fbgql={len(match['ambiguous_fbgql'])}", "",
              "## Field completeness (presence only)", "", "| Field | facebook-go | fbgql |", "|---|---:|---:|"]
    for field in FIELDS:
        value = lambda collector: "N/A" if report["field_completeness"][collector][field] is None else f"{report['field_completeness'][collector][field]:.1%}"
        lines.append(f"| {field} | {value('facebook_go')} | {value('fbgql')} |")
    for key, title in (("only_facebook_go_posts", "Posts only in facebook-go sample"), ("only_fbgql_posts", "Posts only in fbgql sample")):
        lines += ["", "## " + title, ""]
        lines += [f"- ID: {safe_md(post['post_id'] or 'unknown')}; URL: {safe_md(post['url'] or 'unknown')}" for post in match[key]] or ["None identified."]
    lines += ["", "## Matched-post evidence", "", "| facebook-go ID | fbgql ID | Match by | Text lengths (Go / Python) | Possible prefix truncation | Image counts (Go / Python) |", "|---|---|---|---:|---|---:|"]
    for pair in match["matched"]:
        lengths, images = pair["text_lengths"], pair["image_counts"]
        lines.append(f"| {safe_md(pair['facebook_go']['post_id'] or 'unknown')} | {safe_md(pair['fbgql']['post_id'] or 'unknown')} | {pair['match_by']} | {lengths['facebook_go']} / {lengths['fbgql']} | {pair['possible_truncation_in'] or 'not established'} | {images['facebook_go']} / {images['fbgql']} |")
    lines += ["", "## Errors", ""]
    for collector, run in (("facebook-go", first), ("fbgql", second)):
        for error in run["error_details"]:
            lines.append(f"- {collector}: {safe_md(error.get('code', 'unknown_error'))} ({safe_md(error.get('stage', 'unknown'))}): {safe_md(error.get('message', 'No detail'))}")
    if not first["error_details"] and not second["error_details"]:
        lines.append("No errors recorded.")
    lines += ["", "## Initial conclusion", "", "Assessment: " + report["assessment"]["status"], ""]
    lines += ["- " + reason for reason in report["assessment"]["reasons"]]
    lines += ["", "No best collector is selected automatically. Stability is not evaluated by this single paired run.", "", "## Interpretation limits", ""]
    lines += ["- " + note for note in report["notes"]]
    return "\n".join(lines) + "\n"


def write_markdown(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".phase0-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)

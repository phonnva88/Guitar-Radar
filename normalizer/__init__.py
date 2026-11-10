"""Loss-aware conversion of collector envelopes to the Phase 0 schema."""

from __future__ import annotations

import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .media import deduplicate_images, photo_identity

COLLECTORS = ("facebook-go", "fbgql")
FACEBOOK_HOSTS = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "mbasic.facebook.com", "fb.com", "www.fb.com"}
POST_TYPES = {"normal_post", "shared_post", "anonymous_post", "announcement", "reel", "video", "photo_post", "marketplace_style_post", "suggested_post", "unknown"}


def write_json(path: Path, value: object) -> None:
    """Replace an output atomically without making personal data world readable."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=".phase0-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def text(value: object) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def identity(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value) if value >= 0 else None
    return text(value)


def timestamp(value: object) -> str | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, (int, float)):
            if not math.isfinite(value) or value <= 0:
                return None
            dt = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.year <= 1 or dt.tzinfo is None:
                return None
        else:
            return None
        return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError, OSError):
        return None


def window_metadata(value: object) -> dict | None:
    """Validate an inclusive, timezone-aware interval and copy safe diagnostics."""
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("Run window must be an object or null")
    start, end = value.get("from"), value.get("to")
    if not isinstance(start, str) or not isinstance(end, str):
        raise ValueError("Window needs timezone-aware from and to timestamps")
    start, end = timestamp(start), timestamp(end)
    if start is None or end is None or datetime.fromisoformat(start.replace("Z", "+00:00")) > datetime.fromisoformat(end.replace("Z", "+00:00")):
        raise ValueError("Window timestamps must be valid and ordered")
    complete = value.get("complete", False)
    if type(complete) is not bool:
        raise ValueError("Window complete must be a boolean")
    result = {"from": start, "to": end, "complete": complete,
              "stop_reason": text(value.get("stop_reason")) or "unknown"}
    for key in ("scanned_posts", "unknown_timestamp_posts", "ordering_violations"):
        count = value.get(key, 0)
        if type(count) is not int or count < 0:
            raise ValueError("Window counters must be nonnegative integers")
        result[key] = count
    return result


def window_datetimes(value: dict) -> tuple[datetime, datetime]:
    return tuple(datetime.fromisoformat(value[key].replace("Z", "+00:00")) for key in ("from", "to"))


def web_url(value: object) -> str | None:
    if not isinstance(value, str) or not value or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            return None
        if parsed.port not in {None, 80, 443}:
            return None
        return value
    except ValueError:
        return None


def canonical_permalink(value: object, group_id: str | None = None) -> str | None:
    """Canonical post identity used for comparison, never invented post data."""
    if not web_url(value):
        return None
    parsed = urlsplit(value)
    if parsed.hostname.lower() not in FACEBOOK_HOSTS:
        return None
    path = parsed.path.rstrip("/")
    match = re.fullmatch(r"/groups/([^/]+)/(?:posts|permalink)/([^/]+)", path)
    if match:
        group, post = match.groups()
        if group_id and group != group_id:
            return None
        return "post:" + post
    match = re.fullmatch(r"/[^/]+/posts/([^/]+)", path)
    if match:
        return "post:" + match.group(1)
    if path in {"/permalink.php", "/story.php"}:
        post = parse_qs(parsed.query).get("story_fbid", [])
        if len(post) == 1 and post[0]:
            return "post:" + post[0]
    # Facebook also emits canonical numeric single-post links.
    if re.fullmatch(r"/[0-9]+", path):
        return "post:" + path[1:]
    return None


def validate_envelope(raw: object, expected_collector: str | None = None) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Run envelope must be an object")
    if type(raw.get("schema_version")) is not int or raw.get("schema_version") != 1:
        raise ValueError("Unsupported run schema_version")
    if raw.get("collector") not in COLLECTORS or (expected_collector and raw.get("collector") != expected_collector):
        raise ValueError("Unexpected collector")
    if not text(raw.get("run_id")):
        raise ValueError("Run needs a nonempty run_id")
    if not identity(raw.get("group_id")):
        group_input = raw.get("group_input")
        failed_resolution = (raw.get("status") == "failed" and raw.get("posts") == [] and
                             isinstance(group_input, str) and bool(group_input.strip()))
        if not failed_resolution:
            raise ValueError("Run needs a group_id; only failed empty resolution runs may omit it")
    if raw.get("status") not in {"success", "partial", "failed"}:
        raise ValueError("Run status must be success, partial or failed")
    if not isinstance(raw.get("posts"), list) or not isinstance(raw.get("errors"), list):
        raise ValueError("Run posts and errors must be arrays")
    duration = raw.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
        raise ValueError("Run duration_ms must be a nonnegative finite number")
    pages = raw.get("pages_fetched")
    if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0:
        raise ValueError("Run pages_fetched must be a nonnegative integer")
    window_metadata(raw.get("window"))
    return raw


def run_metadata(raw: dict) -> dict:
    result = {key: raw.get(key) for key in ("schema_version", "collector", "group_id", "run_id", "started_at", "collected_at", "duration_ms", "pages_fetched", "status", "group_input")}
    result["group_id"] = identity(raw["group_id"])
    result["source"] = raw.get("source") if raw.get("source") in {"live", "synthetic"} else "unknown"
    for key in ("requested_pages", "page_size", "sort", "library_commit", "git_commit"):
        value = raw.get(key)
        if value is None or isinstance(value, (str, int, float, bool)):
            result[key] = value
    if "window" in raw:
        result["window"] = window_metadata(raw.get("window"))
    # Keep only the documented error context, not arbitrary upstream payloads.
    result["errors"] = [{k: error[k] for k in ("code", "stage", "message", "page", "post_index", "skipped") if k in error}
                        if isinstance(error, dict) else {"code": "unknown_error", "stage": "collector", "message": "Malformed collector error"}
                        for error in raw["errors"]]
    return result


def post_validation(post: dict) -> dict | None:
    """Keep vetted media evidence separate from the stable normalized schema."""
    keys = ("image_candidates", "possible_type", "type_evidence", "debug_file", "debug_response_saved")
    if not any(key in post for key in keys):
        return None
    candidates = post.get("image_candidates", [])
    if not isinstance(candidates, list):
        raise ValueError("Image candidates must be an array")
    images = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Image candidate must be an object")
        url, source = web_url(candidate.get("url")), candidate.get("source")
        if not url or source not in {"attachment", "media"}:
            continue
        image = {"url": url, "source": source, "path": text(candidate.get("path"))}
        photo_id = photo_identity(candidate.get("photo_id"))
        if photo_id is not None:
            image["photo_id"] = photo_id
        for dimension in ("width", "height"):
            size = candidate.get(dimension)
            image[dimension] = size if type(size) is int and size > 0 else None
        images.append(image)
    evidence = post.get("type_evidence", [])
    if not isinstance(evidence, list):
        raise ValueError("Post type evidence must be an array")
    return {"image_candidates": deduplicate_images(images),
            "possible_type": post.get("possible_type") if post.get("possible_type") in POST_TYPES else "unknown",
            "type_evidence": [item for item in evidence if text(item)],
            "debug_file": text(post.get("debug_file")),
            "debug_response_saved": post.get("debug_response_saved") is True}


def normalize_post(post: object, raw: dict) -> dict:
    if not isinstance(post, dict):
        raise ValueError("Post is not an object")
    collector, group = raw["collector"], identity(raw["group_id"])
    if collector == "facebook-go":
        own_group = identity(post.get("groupId"))
        if own_group and own_group != group:
            raise ValueError("Post belongs to another group")
        author = post.get("author") if isinstance(post.get("author"), dict) else {}
        attachments = post.get("attachments") or []
        if not isinstance(attachments, list):
            raise ValueError("Post attachments is not an array")
        images = []
        for url in attachments:
            if web_url(url) and re.search(r"\.(?:jpe?g|png|gif|webp|avif)$", urlsplit(url).path, re.IGNORECASE) and url not in images:
                images.append(url)
        values = (post.get("id"), post.get("message"), post.get("permalink"), post.get("createdAt"))
        author_id = identity(post.get("authorId")) or identity(author.get("id"))
        author_name = text(post.get("authorName")) or text(author.get("name"))
    else:
        # Upstream's group Post model has neither author nor media. page_name is
        # discovery context, not evidence of a group author's identity.
        values = (post.get("post_id"), post.get("text"), post.get("permalink"), post.get("created_time"))
        author_id, author_name, images = None, None, []
    post_id, caption, url, posted_at = values
    validation = post_validation(post)
    # An explicit candidate array was examined against the actual media layout.
    # Do not re-add rejected video thumbnails via the legacy URL fallback.
    if "image_candidates" in post:
        images = [candidate["url"] for candidate in validation["image_candidates"]]
    result = {"collector": collector, "post_id": identity(post_id), "group_id": group,
            "group_name": None, "author_id": author_id, "author_name": author_name,
            "text": text(caption), "url": web_url(url), "images": images,
            "posted_at": timestamp(posted_at), "collected_at": timestamp(raw.get("collected_at"))}
    if validation is not None:
        result["validation"] = validation
    return result


def normalize_run(raw: object, expected_collector: str | None = None) -> dict:
    raw = validate_envelope(raw, expected_collector)
    normalized = run_metadata(raw)
    normalized["posts"] = []
    window = normalized.get("window")
    bounds = window_datetimes(window) if window else None
    counts = {"input_posts": len(raw["posts"]), "in_window_posts": 0,
              "before_window_posts": 0, "after_window_posts": 0,
              "unknown_timestamp_posts": 0, "invalid_posts": 0,
              "unknown_timestamp_references": []}
    for index, post in enumerate(raw["posts"]):
        try:
            parsed = normalize_post(post, raw)
            if bounds is not None:
                if parsed["posted_at"] is None:
                    counts["unknown_timestamp_posts"] += 1
                    counts["unknown_timestamp_references"].append({"index": index, "post_id": parsed["post_id"], "url": parsed["url"]})
                    continue
                posted = datetime.fromisoformat(parsed["posted_at"].replace("Z", "+00:00"))
                if posted < bounds[0]:
                    counts["before_window_posts"] += 1
                    continue
                if posted > bounds[1]:
                    counts["after_window_posts"] += 1
                    continue
                counts["in_window_posts"] += 1
            normalized["posts"].append(parsed)
        except (ValueError, TypeError, KeyError):
            counts["invalid_posts"] += 1
            normalized["errors"].append({"code": "parse_error", "stage": "normalization", "post_index": index,
                                         "message": "Post could not be normalized; other records were preserved"})
    if window is not None:
        normalized["window_filter"] = counts
        window["scanned_posts"] = max(window["scanned_posts"], len(raw["posts"]))
        window["unknown_timestamp_posts"] = max(window["unknown_timestamp_posts"], counts["unknown_timestamp_posts"])
    if len(normalized["errors"]) > len(raw["errors"]):
        normalized["status"] = "partial" if normalized["posts"] else "failed"
    normalized["normalization"] = {"input_posts": len(raw["posts"]), "output_posts": len(normalized["posts"]),
        "notes": ["Missing values are null or []; timestamps are UTC.",
                  "Field presence does not prove complete captions or galleries.",
                  "Legacy facebook-go attachments use image file extensions; explicit image candidates use raw GraphQL media evidence.",
                  "Pinned fbgql Post model has no author; media evidence is captured separately from raw GraphQL.",
                  "Window filters are inclusive; undated posts remain in raw data and are excluded with diagnostics, not assumed outside the window."]}
    return normalized

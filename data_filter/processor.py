"""Deterministic offline processing for saved collector envelopes."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime

from normalizer import COLLECTORS, normalize_run, run_metadata, timestamp
from compare import validate_normalized
from data_filter.posts import FilterError

ERROR_CODES = {
    "authentication_failed", "session_expired", "checkpoint_required", "rate_limited", "account_restricted",
    "group_not_accessible", "group_not_found", "network_error", "parse_error", "configuration_error",
    "dependency_missing", "unknown_error", "pagination_stalled", "invalid_cursor", "media_parse_error", "interrupted",
}


def _safe_errors(errors):
    result = []
    for error in errors:
        code = error.get("code") if isinstance(error, dict) else None
        result.append({"code": code if isinstance(code, str) and code in ERROR_CODES else "unknown_error"})
    return result


def process_collection(raw):
    """Normalize a raw collector envelope without network, database or env IO.

    All input posts remain eligible unless the existing explicit collection
    window excludes them. Classification and selection rules are not invented.
    """
    if not isinstance(raw, dict) or raw.get("collector") not in COLLECTORS:
        raise FilterError("Unsupported collector envelope")
    try:
        result = normalize_run(deepcopy(raw), raw["collector"])
        result["errors"] = _safe_errors(result["errors"])
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        raise FilterError("Invalid collector envelope") from None


def process_normalized(envelope):
    """Replay an explicitly declared normalized input without renormalizing it."""
    if not isinstance(envelope, dict) or envelope.get("collector") not in COLLECTORS:
        raise FilterError("Unsupported normalized envelope")
    try:
        value = validate_normalized(deepcopy(envelope), envelope["collector"])
        result = run_metadata(value)
        result["posts"] = value["posts"]
        result["errors"] = _safe_errors(result["errors"])
        for key in ("normalization", "window_filter", "filtering"):
            if key in value:
                result[key] = value[key]
        return result
    except (ValueError, TypeError, KeyError, AttributeError):
        raise FilterError("Invalid normalized envelope") from None


def normalize_filters(q="", group_id=None, start=None, end=None, has_images=None, page=1, page_size=20):
    """Validate reusable feed-selection arguments independently of SQL/HTTP."""
    if not isinstance(q, str) or len(q) > 300:
        raise FilterError("Search must contain at most 300 characters")
    if group_id is not None and (type(group_id) is not int or group_id < 1):
        raise FilterError("Expected a positive group ID")
    if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
        raise FilterError("Invalid pagination")
    if has_images is not None and type(has_images) is not bool:
        raise FilterError("has_images must be a boolean")
    bounds = []
    for value in (start, end):
        parsed = timestamp(value.isoformat() if isinstance(value, datetime) else value) if value is not None else None
        if value is not None and parsed is None:
            raise FilterError("Date filters require timezone-aware timestamps")
        bounds.append(parsed)
    if all(bounds) and datetime.fromisoformat(bounds[0].replace("Z", "+00:00")) > datetime.fromisoformat(bounds[1].replace("Z", "+00:00")):
        raise FilterError("Start must be before end")
    return {"q": q, "group_id": group_id, "start": bounds[0], "end": bounds[1],
            "has_images": has_images, "page": page, "page_size": page_size}


def filter_collection(envelope, *, q="", start=None, end=None, has_images=None):
    """Apply only explicit criteria, preserving status and collection-window truth."""
    filters = normalize_filters(q=q, start=start, end=end, has_images=has_images)
    result = deepcopy(envelope)
    selected = []
    for post in result["posts"]:
        if q and q.casefold() not in (post.get("text") or "").casefold():
            continue
        if has_images is not None and bool(post.get("images")) != has_images:
            continue
        when = timestamp(post.get("posted_at"))
        if filters["start"] or filters["end"]:
            if when is None:
                continue
            instant = datetime.fromisoformat(when.replace("Z", "+00:00"))
            if filters["start"] and instant < datetime.fromisoformat(filters["start"].replace("Z", "+00:00")):
                continue
            if filters["end"] and instant > datetime.fromisoformat(filters["end"].replace("Z", "+00:00")):
                continue
        selected.append(post)
    result["posts"] = selected
    result["filtering"] = {"input_posts": len(envelope["posts"]), "output_posts": len(selected),
                           "parameters": {key: filters[key] for key in ("q", "start", "end", "has_images")}}
    return result

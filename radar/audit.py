"""Immutable collector evidence and conservative offline coverage checks.

Field presence and repeat consistency are observations, never proof that Facebook
returned every post or that every gallery/caption is complete.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
import json
import re
import unicodedata
from urllib.parse import urlsplit

from psycopg.types.json import Jsonb

from normalizer import FACEBOOK_HOSTS, canonical_permalink, timestamp
from validation.media import sanitize

FIELDS = ("text", "author", "url", "images", "posted_at")
COMPARE_FIELDS = ("text", "author_id", "author_name", "url", "posted_at", "images")
LIMIT = 64 * 1024 * 1024
COVERAGE_NOTE = "Chỉ bài đối chiếu đã xác nhận mới kiểm tra được việc bỏ sót; chưa có dữ liệu độc lập để chứng minh bao phủ toàn bộ Facebook."
STABILITY_NOTE = "Chỉ so sánh các lượt thành công, cửa sổ hoàn chỉnh, cùng group số, nguồn và FROM/TO cố định. Thay đổi có thể do Facebook sửa, xóa hoặc sắp xếp lại bài."


def _errors():
    from radar.db import InvalidInput, NotFound
    return InvalidInput, NotFound


def _stamp(value):
    return timestamp(value.isoformat() if isinstance(value, datetime) else value)


def _instant(value):
    value = _stamp(value)
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def _post_id(post):
    value = post.get("post_id") or post.get("id")
    return str(value) if isinstance(value, (str, int)) and not isinstance(value, bool) and str(value) else None


def _clean(value):
    # These structures are not part of post evidence and must never be retained.
    blocked = {"html", "bootstrap", "bootstrap_html", "response_body", "request_body"}
    def strip(item):
        if isinstance(item, dict):
            return {str(k): strip(v) for k, v in item.items() if str(k).lower() not in blocked}
        if isinstance(item, list):
            return [strip(v) for v in item]
        return item
    return sanitize(strip(value))


def _safe_envelope(value, *, raw=False):
    if not isinstance(value, dict) or not isinstance(value.get("posts", []), list) or any(not isinstance(p, dict) for p in value.get("posts", [])):
        raise _errors()[0]("Invalid collector snapshot")
    allowed = ("schema_version", "collector", "source", "run_id", "group_id", "started_at", "collected_at",
               "duration_ms", "pages_fetched", "requested_pages", "page_size", "sort", "status", "window",
               "posts", "normalization")
    safe = {key: value[key] for key in allowed if key in value}
    from radar.db import safe_code
    safe["errors"] = [{"code": safe_code(x.get("code"))} for x in value.get("errors", []) if isinstance(x, dict)]
    safe = _clean(safe)
    try:
        size = len(json.dumps(safe, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (ValueError, TypeError):
        raise _errors()[0]("Collector snapshot is not valid JSON") from None
    if size > LIMIT:
        raise _errors()[0]("Collector snapshot exceeds 64 MiB")
    return safe


def capture_run(conn, run, envelope, raw=None):
    """Persist one immutable, sanitized snapshot in the ingestion transaction."""
    normalized = _safe_envelope(envelope)
    raw = _safe_envelope(raw, raw=True) if raw is not None else None
    if raw is not None:
        for field in ("collector", "group_id", "source", "run_id"):
            if raw.get(field) != normalized.get(field):
                raise _errors()[0]("Raw and normalized snapshot identities disagree")
    conn.execute("""INSERT INTO collector_run_snapshots(run_id,normalized,raw) VALUES(%s,%s,%s)
                    ON CONFLICT(run_id) DO NOTHING""", (run["id"], Jsonb(normalized), Jsonb(raw) if raw is not None else None))


def capture_failure(conn, run, raw, code):
    """Keep safe acquisition evidence when offline processing rejects a run."""
    if not isinstance(raw, dict):
        return
    envelope = {key: raw[key] for key in ("schema_version", "collector", "source", "run_id", "group_id") if key in raw}
    window = raw.get("window")
    envelope.update(status="failed", posts=[], errors=[{"code": code}],
                    window={**window, "complete": False, "stop_reason": "error"} if isinstance(window, dict) else None)
    capture_run(conn, run, envelope, raw)


def _index(posts):
    ids = [_post_id(post) for post in posts if isinstance(post, dict)]
    counts = Counter(value for value in ids if value)
    return ({_post_id(p): p for p in posts if isinstance(p, dict) and _post_id(p)},
            sorted(key for key, count in counts.items() if count > 1),
            sum(not value for value in ids))


def _window_key(envelope):
    window = envelope.get("window") or {}
    start, end = _stamp(window.get("from")), _stamp(window.get("to"))
    group = envelope.get("group_id")
    if not start or not end or not isinstance(group, str) or not group.isdigit():
        return None
    if envelope.get("status") != "success" or envelope.get("errors") or window.get("complete") is not True:
        return None
    if window.get("stop_reason") not in {"older_than_from", "feed_exhausted"}:
        return None
    if window.get("unknown_timestamp_posts", 0) or window.get("ordering_violations", 0):
        return None
    if envelope.get("source") not in ("live", "synthetic"):
        return None
    return group, start, end, envelope.get("source")


def _stats(row):
    envelope = row.get("normalized")
    raw = row.get("raw")
    result = {key: row.get(key) for key in ("id", "group_id", "group_name", "status", "source", "error_code", "posts_count", "window_complete", "started_at", "finished_at")}
    result.update(snapshot_available=isinstance(envelope, dict), raw_snapshot_available=isinstance(raw, dict),
                  window=envelope.get("window") if isinstance(envelope, dict) else None)
    if not result["window"] and row.get("requested_from") and row.get("requested_to"):
        result["window"] = {"from": _stamp(row["requested_from"]), "to": _stamp(row["requested_to"]),
                            "complete": False, "stop_reason": "not_collected"}
    result["window_complete"] = _window_key(envelope) is not None if isinstance(envelope, dict) else False
    posts = envelope.get("posts", []) if isinstance(envelope, dict) else []
    _, duplicates, missing = _index(posts)
    counts = {"text": sum(bool(p.get("text")) for p in posts),
              "author": sum(bool(p.get("author_name") or p.get("author_id")) for p in posts),
              "url": sum(bool(p.get("url")) for p in posts),
              "images": sum(bool(p.get("images")) for p in posts),
              "posted_at": sum(bool(_stamp(p.get("posted_at"))) for p in posts)}
    raw_rows = raw.get("posts", []) if isinstance(raw, dict) else []
    raw_ids = [_post_id(p) for p in raw_rows]
    ids = [_post_id(p) for p in posts]
    result.update(field_counts=counts, field_presence={key: count / len(posts) if posts else None for key, count in counts.items()},
                  raw_posts=len(raw.get("posts", [])) if isinstance(raw, dict) else None,
                  normalized_posts=len(posts) if isinstance(envelope, dict) else None,
                  duplicates=sum(n - 1 for n in Counter(x for x in ids if x).values()) if isinstance(envelope, dict) else None,
                  missing_ids=missing if isinstance(envelope, dict) else None,
                  raw_duplicates=sum(n - 1 for n in Counter(x for x in raw_ids if x).values()) if isinstance(raw, dict) else None,
                  raw_missing_ids=sum(not x for x in raw_ids) if isinstance(raw, dict) else None,
                  stability={"status": "insufficient_evidence", "previous_run_id": None,
                             "missing_ids": [], "added_ids": [], "changed": [], "reason": "No earlier compatible complete run"})
    return result


def _stability(rows):
    previous = {}
    summaries = {}
    for row in sorted(rows, key=lambda r: r["id"]):
        item = _stats(row)
        envelope = row.get("normalized") or {}
        key = _window_key(envelope) if row.get("status") == "success" else None
        current, duplicates, missing = _index(envelope.get("posts", []))
        if key and not duplicates and not missing and current:
            if key in previous:
                before_row, before = previous[key]
                changed = []
                for post_id in sorted(current.keys() & before.keys()):
                    fields = []
                    for field in COMPARE_FIELDS:
                        left, right = before[post_id].get(field), current[post_id].get(field)
                        if field == "images":
                            left, right = sorted(set(left or [])), sorted(set(right or []))
                        if left != right:
                            fields.append(field)
                    if fields:
                        changed.append({"post_id": post_id, "fields": fields})
                removed, added = sorted(before.keys() - current.keys()), sorted(current.keys() - before.keys())
                item["stability"] = {"status": "changed_in_observed_window" if removed or added or changed else "stable_in_observed_window",
                                     "previous_run_id": before_row["id"], "missing_ids": removed,
                                     "added_ids": added, "changed": changed, "reason": None}
            previous[key] = (row, current)
        elif not key:
            item["stability"]["reason"] = "A complete successful run with the same numeric group, source and fixed window is required"
        else:
            item["stability"]["reason"] = "Duplicate, unidentified or empty post sample cannot establish stability"
        summaries[row["id"]] = item
    return summaries


def _rows(conn, group_id=None, limit=100):
    where, params = (" WHERE r.group_id=%s", [group_id]) if group_id is not None else ("", [])
    return conn.execute("""SELECT r.*,g.name AS group_name,s.normalized,s.raw FROM collection_runs r
                           JOIN monitored_groups g ON g.id=r.group_id
                           LEFT JOIN collector_run_snapshots s ON s.run_id=r.id""" + where +
                        " ORDER BY r.id DESC LIMIT %s", (*params, limit)).fetchall()


def audit_summary(db, group_id=None):
    if group_id is not None and (type(group_id) is not int or group_id < 1):
        raise _errors()[0]("Expected a positive group record ID")
    with db.connect() as conn:
        rows = _rows(conn, group_id)
        summaries = _stability(rows)
        where, params = (" WHERE k.group_id=%s", (group_id,)) if group_id is not None else ("", ())
        known = conn.execute("SELECT k.*,g.name AS group_name FROM collector_known_posts k JOIN monitored_groups g ON g.id=k.group_id" + where + " ORDER BY k.id", params).fetchall()
    return {"runs": [summaries[row["id"]] for row in rows], "known_posts": known,
            "coverage_note": COVERAGE_NOTE, "stability_note": STABILITY_NOTE,
            "history_limit": 100}


def _known_checks(known, envelope):
    posts, _, _ = _index(envelope.get("posts", []))
    window = envelope.get("window") or {}
    start, end = _instant(window.get("from")), _instant(window.get("to"))
    output = []
    for item in known:
        at = _instant(item.get("verified_posted_at"))
        post = posts.get(item["post_id"])
        observed = post is not None
        time_basis = "independently_verified" if at else "unknown"
        if not at and observed:
            at = _instant(post.get("posted_at"))
            if at:
                time_basis = "observed_post"
        images = len(post.get("images") or []) if observed else None
        expected_text = item.get("expected_text")
        def comparable_text(text):
            return " ".join(unicodedata.normalize("NFC", text or "").split())
        text_matches = comparable_text(post.get("text")) == comparable_text(expected_text) if observed and expected_text is not None else None
        image_count_matches = images == item["expected_image_count"] if observed and item.get("expected_image_count") is not None else None
        if not at or not start or not end:
            status = "timestamp_unknown"
        elif not start <= at <= end:
            status = "outside_window"
        elif not observed:
            status = "missing_in_window"
        elif text_matches is False:
            status = "text_mismatch"
        elif image_count_matches is False:
            status = "image_count_mismatch"
        elif item.get("expected_has_images") is None and item.get("expected_image_count") is None:
            status = "image_unverified"
        elif item.get("expected_has_images") is True and not images:
            status = "image_missing"
        elif item.get("expected_has_images") is False and images:
            status = "unexpected_images"
        else:
            status = "detected"
        output.append({**item, "status": status, "observed": observed, "image_count": images,
                       "text_matches": text_matches, "image_count_matches": image_count_matches,
                       "time_basis": time_basis, "effective_posted_at": at.isoformat() if at else None})
    return output


def run_details(db, run_id):
    if type(run_id) is not int or run_id < 1:
        raise _errors()[0]("Expected a positive run record ID")
    with db.connect() as conn:
        row = conn.execute("""SELECT r.*,g.name AS group_name,s.normalized,s.raw FROM collection_runs r
                              JOIN monitored_groups g ON g.id=r.group_id LEFT JOIN collector_run_snapshots s ON s.run_id=r.id
                              WHERE r.id=%s""", (run_id,)).fetchone()
        if not row:
            raise _errors()[1]("Collection run not found")
        history = _rows(conn, row["group_id"], limit=1000)
        known = conn.execute("SELECT * FROM collector_known_posts WHERE group_id=%s ORDER BY id", (row["group_id"],)).fetchall()
    summaries = _stability(history)
    envelope, raw = row.get("normalized") or {}, row.get("raw") or {}
    normalized_index, duplicate_ids, missing = _index(envelope.get("posts", []))
    raw_by_id = {}
    for post in raw.get("posts", []):
        raw_by_id.setdefault(_post_id(post), []).append(post)
    posts = []
    for post in envelope.get("posts", []):
        matching = raw_by_id.get(_post_id(post), [])
        posts.append({**post, "raw": matching[0] if matching else None, "raw_matches_count": len(matching)})
    raw_only = sorted(value for value in raw_by_id if value and value not in normalized_index)
    return {"run": summaries.get(run_id, _stats(row)), "posts": posts,
            "diagnostics": {"duplicate_ids": duplicate_ids, "missing_ids": missing, "raw_only_ids": raw_only,
                            "raw_only_note": "Raw-only rows may be outside the requested time window or undated; they are not automatically classified as lost posts."},
            "window": envelope.get("window"), "known_post_checks": _known_checks(known, envelope),
            "raw_unmatched": [post for value, matches in raw_by_id.items() if value not in normalized_index for post in matches],
            "coverage_note": COVERAGE_NOTE}


def add_known_post(db, group_id, url, expected_has_images=None, verified_posted_at=None,
                   expected_text=None, expected_image_count=None, *, preserve_existing=False):
    InvalidInput, NotFound = _errors()
    if type(group_id) is not int or group_id < 1 or (expected_has_images is not None and type(expected_has_images) is not bool):
        raise InvalidInput("Invalid known-post group or image expectation")
    if expected_text is not None and (not isinstance(expected_text, str) or len(expected_text) > 100000):
        raise InvalidInput("Expected text must contain at most 100000 characters")
    if expected_image_count is not None and (type(expected_image_count) is not int or not 0 <= expected_image_count <= 10000):
        raise InvalidInput("Expected image count must be a nonnegative integer")
    if expected_has_images is not None and expected_image_count is not None and expected_has_images != (expected_image_count > 0):
        raise InvalidInput("Image expectation and image count disagree")
    if not isinstance(url, str):
        raise InvalidInput("Use a canonical Facebook group post permalink")
    try:
        parsed = urlsplit(url)
        match = re.fullmatch(r"/groups/([A-Za-z0-9._-]+)/(?:posts|permalink)/([A-Za-z0-9_-]{1,128})/?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname not in FACEBOOK_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443) or not match:
            raise ValueError
    except ValueError:
        raise InvalidInput("Use a canonical Facebook /groups/<group>/posts/<post> permalink, not a share link") from None
    published = _stamp(verified_posted_at)
    if verified_posted_at is not None and not published:
        raise InvalidInput("Known post time must be a timezone-aware ISO timestamp")
    with db.connect() as conn:
        group = conn.execute("SELECT * FROM monitored_groups WHERE id=%s", (group_id,)).fetchone()
        if not group:
            raise NotFound("Group not found")
        configured = re.fullmatch(r"/groups/([^/]+)/?", urlsplit(group["url"]).path)
        fixture_handle = match[1]
        if fixture_handle.isdigit():
            same = fixture_handle == group.get("facebook_group_id") or (configured and fixture_handle == configured[1] and configured[1].isdigit())
        else:
            same = configured and fixture_handle.casefold() == configured[1].casefold()
        if not same:
            raise InvalidInput("Known post does not belong to the configured group")
        safe_url = f"https://www.facebook.com/groups/{fixture_handle}/posts/{match[2]}/"
        update = ("url=collector_known_posts.url,expected_has_images=COALESCE(collector_known_posts.expected_has_images,EXCLUDED.expected_has_images),"
                  "verified_posted_at=COALESCE(collector_known_posts.verified_posted_at,EXCLUDED.verified_posted_at),"
                  "expected_text=COALESCE(collector_known_posts.expected_text,EXCLUDED.expected_text),"
                  "expected_image_count=COALESCE(collector_known_posts.expected_image_count,EXCLUDED.expected_image_count)"
                  if preserve_existing else
                  "url=EXCLUDED.url,expected_has_images=EXCLUDED.expected_has_images,verified_posted_at=EXCLUDED.verified_posted_at,"
                  "expected_text=EXCLUDED.expected_text,expected_image_count=EXCLUDED.expected_image_count")
        return conn.execute("""INSERT INTO collector_known_posts(group_id,post_id,url,expected_has_images,verified_posted_at,expected_text,expected_image_count)
                               VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(group_id,post_id) DO UPDATE SET
                               """ + update + " RETURNING *",
                            (group_id, match[2], safe_url, expected_has_images, published,
                             _clean(expected_text) if expected_text is not None else None, expected_image_count)).fetchone()

"""Pure post preparation and exact-text comparison rules."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

from normalizer import canonical_permalink, timestamp, web_url


class FilterError(ValueError):
    """A fixed validation message that never includes source payload values."""


def fingerprint(value):
    if not isinstance(value, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest() if len(normalized) >= 40 else None


def prepare_post(item, group_id):
    """Validate one normalized post and prepare values for any storage adapter.

    This function neither reads credentials nor accesses a database. Its return
    value deliberately preserves the Phase 1 persistence boundary.
    """
    if not isinstance(item, dict):
        raise FilterError("Invalid normalized post")
    post_id = item.get("post_id")
    if not isinstance(post_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", post_id):
        raise FilterError("A normalized post needs a valid post ID")
    if item.get("group_id") not in (None, group_id):
        raise FilterError("Post group does not match the collected group")
    url = item.get("url")
    key = canonical_permalink(url) if url else None
    if url and (not key or key != "post:" + post_id):
        raise FilterError("Post permalink disagrees with its post ID")
    if url:
        parsed = urlsplit(url)
        group_match = re.fullmatch(r"/groups/([0-9]+)/(?:posts|permalink)/[^/]+/?", parsed.path)
        if group_match and group_match[1] != group_id:
            raise FilterError("Post permalink belongs to a different group")
        if parsed.path in ("/story.php", "/permalink.php"):
            from urllib.parse import parse_qs, urlencode
            query = parse_qs(parsed.query)
            query = urlencode({k: query[k][0] for k in ("story_fbid", "id") if k in query})
        else:
            query = ""
        url = urlunsplit(("https", "www.facebook.com", parsed.path, query, ""))
    posted_at = timestamp(item.get("posted_at"))
    if item.get("posted_at") is not None and not posted_at:
        raise FilterError("Invalid published timestamp")
    images = item.get("images", [])
    if not isinstance(images, list) or any(not isinstance(x, str) or not web_url(x) or urlsplit(x).scheme != "https" for x in images):
        raise FilterError("Media URLs must be HTTPS")
    evidence = item.get("validation") or {}
    candidates = evidence.get("image_candidates", []) if isinstance(evidence, dict) else []
    candidates = candidates if isinstance(candidates, list) else []
    details = {x.get("url"): x for x in candidates if isinstance(x, dict)}
    media = []
    for position, image in enumerate(dict.fromkeys(images)):
        detail = details.get(image, {})
        media.append((image, position,
                      detail.get("width") if type(detail.get("width")) is int and detail["width"] > 0 else None,
                      detail.get("height") if type(detail.get("height")) is int and detail["height"] > 0 else None,
                      str(detail["source"])[:120] if detail.get("source") else None))
    text = item.get("text") if isinstance(item.get("text"), str) and item["text"].strip() else None
    return {"post_id": post_id, "url": url, "canonical_key": key, "text": text,
            "author_id": str(item["author_id"]) if item.get("author_id") is not None else None,
            "author_name": item.get("author_name") if isinstance(item.get("author_name"), str) else None,
            "posted_at": posted_at, "fingerprint": fingerprint(text), "media": media}

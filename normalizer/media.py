"""Conservative photo identity and rendition selection, without network access."""
from __future__ import annotations

import re
from urllib.parse import urlsplit


_FB_PHOTO_NAME = re.compile(r"^[0-9]+_[0-9]+_[0-9]+_[no]\.(?:jpg|jpeg|png|webp|avif)$", re.I)


def photo_identity(value):
    """Accept an explicit Photo.id, including opaque GraphQL IDs."""
    if type(value) is int and value >= 0:
        return str(value)
    if isinstance(value, str) and value.strip() and not any(ord(c) < 32 for c in value):
        return value
    return None


def _area(candidate):
    width, height = candidate.get("width"), candidate.get("height")
    if type(width) is int and width > 0 and type(height) is int and height > 0:
        return width * height
    return 0


def _url_identity(value):
    """Recognize only the numeric fbcdn photo filename pattern, retaining URLs."""
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        name = parsed.path.rsplit("/", 1)[-1]
        if (host == "fbcdn.net" or host.endswith(".fbcdn.net")) and _FB_PHOTO_NAME.fullmatch(name):
            return ("fbcdn", name.lower())
    except (TypeError, ValueError):
        pass
    return ("url", value)


def deduplicate_images(candidates):
    """Select one full signed URL per known photo, preferring greatest pixel area.

    A private Photo-node scope can connect renditions when an explicit ID is
    absent. IDs take precedence over all URL heuristics: different photo IDs
    remain different attachments, even if their image bytes/URLs match.
    """
    local, positions = [], {}
    for index, candidate in enumerate(candidates):
        candidate = dict(candidate)
        identity = photo_identity(candidate.get("photo_id"))
        if identity is None:
            candidate.pop("photo_id", None)
        else:
            candidate["photo_id"] = identity
        scope = candidate.pop("_photo_scope", None)
        key = (("photo", identity) if identity is not None else
               ("scope", scope) if scope is not None else ("candidate", index))
        if key not in positions:
            positions[key] = len(local)
            local.append(candidate)
        elif _area(candidate) > _area(local[positions[key]]):
            local[positions[key]] = candidate

    output, positions = [], {}
    for candidate in local:
        identity = candidate.get("photo_id")
        key = ("photo", identity) if identity is not None else _url_identity(candidate["url"])
        if key not in positions:
            positions[key] = len(output)
            output.append(candidate)
        elif _area(candidate) > _area(output[positions[key]]):
            output[positions[key]] = candidate
    return output

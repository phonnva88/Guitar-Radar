"""Evidence-based media inspection and credential-safe debug serialization.

Only photo/image objects inside a post's media/attachment subtree are counted.
This is intentionally more conservative than recursively collecting every URL.
"""
from __future__ import annotations

import math
import re
from urllib.parse import quote, quote_plus, unquote_plus, urlsplit, urlunsplit

from normalizer.media import deduplicate_images, photo_identity

_SENSITIVE = re.compile(r"cookie|authorization|password|passwd|access.?token|session|fb_dtsg|jazoest|csrf|credential|encrypt|api[-_]?key|(^|[-_])lsd($|[-_])|(^|_)xs($|_)|(^|_)c_user($|_)|(^|_)datr($|_)|(^|_)sb($|_)|(^|_)fr($|_)|secret|(^|[-_])token($|[-_])", re.I)
_NOISE_KEYS = {"extensions", "tracking", "trackingdata", "tracking_data", "debug_info", "client_mutation_id", "headers", "request", "request_headers", "response_headers", "http_headers"}
_EXCLUDE = {"actors", "actor", "author", "authors", "owner", "profile", "owning_profile", "avatar", "actor_photo", "profile_picture", "profile_photo", "feedback", "comments", "replies", "attached_story", "shared_story", "recommendations", "recommended_stories", "suggested_posts", "related_stories", "related_posts", "sponsored_data", "link_preview", "preview_image", "thumbnail", "thumbnail_image", "cover_photo", "cover_image"}
_MEDIA_KEYS = {"attachments", "attachment", "subattachments", "media", "photos", "photo", "images", "photo_image", "viewer_image"}
_IMAGE_KEYS = {"image", "images", "photo_image", "viewer_image", "preferred_image", "full_image", "large_image", "image_high", "image_low"}
_NON_PHOTO_TYPES = re.compile(r"video|reel|externalurl|externalweb|linkpreview|^link$|linkattachment|profile|user|page$|webpage|externalshare", re.I)


def _sensitive(key: str) -> bool:
    return bool(_SENSITIVE.search(key)) or key.lower() in _NOISE_KEYS or (key.startswith("__") and key != "__typename")


def sanitize(value, secrets=()):
    """Remove credentials recursively, including secret query values and echoes.

    The input must be story data, never bootstrap HTML or request/response headers.
    Signed CDN image query parameters are retained unless they are credential keys.
    """
    variants = set()
    for secret in secrets:
        if isinstance(secret, str) and len(secret) >= 4 and not secret.isdecimal():
            variants.update((secret, quote(secret, safe=""), quote_plus(secret)))
    variants = sorted(variants, key=len, reverse=True)

    def clean_string(text):
        for secret in variants:
            text = text.replace(secret, "[REDACTED]")
        text = re.sub(r"(?i)\bBearer\s+[^\s\"'<>]+", "Bearer [REDACTED]", text)
        text = re.sub(r"(?i)\b(xs|c_user|fb_dtsg|access_token|session_token|authorization|cookie)\s*[:=]\s*[^\s;,\"'<>]+", r"\1=[REDACTED]", text)
        # URLs in scalar fields are sanitized without destroying useful CDN paths.
        try:
            parsed = urlsplit(text)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                host = parsed.hostname
                if parsed.port:
                    host += ":" + str(parsed.port)
                query = "&".join(part for part in parsed.query.split("&") if part and not _sensitive(unquote_plus(part.partition("=")[0])))
                text = urlunsplit((parsed.scheme, host, parsed.path, query, ""))
        except ValueError:
            pass
        return text

    def walk(obj, depth=0):
        if depth > 80:
            return "[DEPTH_LIMIT]"
        if isinstance(obj, dict):
            return {clean_string(str(key)): walk(item, depth + 1) for key, item in obj.items() if not _sensitive(str(key))}
        if isinstance(obj, list):
            return [walk(item, depth + 1) for item in obj]
        if isinstance(obj, str):
            return clean_string(obj)
        if isinstance(obj, float) and not math.isfinite(obj):
            return None
        if obj is None or isinstance(obj, (bool, int, float)):
            return obj
        return None

    return walk(value)


def _url(value):
    if not isinstance(value, str) or any(ord(c) < 32 for c in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme in {"https", "http"} and parts.hostname and not parts.username and not parts.password and parts.port in (None, 80, 443):
            return sanitize(value)
    except ValueError:
        pass
    return None


def _dimension(value):
    if type(value) is int:
        return value if value > 0 else None
    if isinstance(value, float) and math.isfinite(value) and value > 0:
        return int(value)
    return None


def extract_images(story) -> list[dict]:
    """Return URL/dimension/path evidence, excluding avatar/link/video previews."""
    output = []

    def walk(node, path="$", zone=False, image=False, photo=False, dimensions=(None, None), depth=0,
             photo_id=None, photo_scope=None):
        if depth > 70:
            return
        if isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]", zone, image, photo, dimensions, depth + 1, photo_id, photo_scope)
            return
        if not isinstance(node, dict):
            return
        typename = str(node.get("__typename", ""))
        if _NON_PHOTO_TYPES.search(typename):
            return
        target = node.get("target")
        if isinstance(target, dict) and _NON_PHOTO_TYPES.search(str(target.get("__typename", ""))):
            return
        if str(node.get("style_type", "")).lower() in {"share", "link", "external", "share_link"}:
            return
        styles = node.get("style_list")
        if isinstance(styles, list) and any(isinstance(style, str) and style.lower() in {"share", "link", "external", "share_link", "external_url"} for style in styles):
            return
        if typename.lower() == "photo":
            # A nested Photo starts a new identity, including when it has no ID.
            photo_id, photo_scope = photo_identity(node.get("id")), path
            dimensions = (None, None)
        photo = photo or typename.lower() in {"photo", "image", "photomedia", "photoattachment"}
        dimensions = (_dimension(node.get("width")) or dimensions[0], _dimension(node.get("height")) or dimensions[1])
        if zone and (image or photo):
            for key in ("uri", "url", "src"):
                url = _url(node.get(key))
                # A Facebook /photo(s)/ object URL is a permalink, not an image.
                if url and not (urlsplit(url).hostname in {"facebook.com", "www.facebook.com", "m.facebook.com"} and not re.search(r"\.(?:jpe?g|png|webp|gif|avif)$", urlsplit(url).path, re.I)):
                    candidate = {"url": url, "width": dimensions[0], "height": dimensions[1],
                                 "source": "attachment" if "attachment" in path.lower() else "media", "path": f"{path}.{key}"}
                    if photo_id is not None:
                        candidate["photo_id"] = photo_id
                    if photo_scope is not None:
                        candidate["_photo_scope"] = photo_scope
                    output.append(candidate)
        for key, child in node.items():
            low = str(key).lower()
            if low in _EXCLUDE or any(term in low for term in ("avatar", "thumbnail", "profile_picture", "link_preview", "recommend", "related_stor", "suggested")):
                continue
            next_zone = zone or low in _MEDIA_KEYS or photo
            next_image = image or low in _IMAGE_KEYS
            walk(child, f"{path}.{key}", next_zone, next_image, photo, dimensions, depth + 1, photo_id, photo_scope)

    walk(story)
    return deduplicate_images(output)


def classify_post(story) -> tuple[str, list[str]]:
    """Classify only explicit structural markers; captions never imply type."""
    markers = []

    def walk(node, path="$", depth=0):
        if depth > 60:
            return
        if isinstance(node, dict):
            typename = str(node.get("__typename", ""))
            if typename:
                markers.append((path + ".__typename", typename.lower()))
            for key, child in node.items():
                low = str(key).lower()
                if low in {"feedback", "comments", "replies", "recommendations", "recommended_stories", "related_stories"}:
                    continue
                if low in {"is_announcement", "is_suggested", "is_marketplace_post", "is_anonymous", "is_reel"} and child is True:
                    markers.append((path + "." + low, low))
                if low in {"attached_story", "shared_story"} and isinstance(child, dict) and child:
                    markers.append((path + "." + low, "shared_story"))
                walk(child, path + "." + str(key), depth + 1)
        elif isinstance(node, list):
            for i, child in enumerate(node):
                walk(child, f"{path}[{i}]", depth + 1)

    walk(story)
    for kind, terms in (("suggested_post", ("is_suggested",)), ("marketplace_style_post", ("marketplace",)),
                        ("announcement", ("is_announcement",)), ("anonymous_post", ("groupanonauthor", "is_anonymous")),
                        ("shared_post", ("shared_story",)), ("reel", ("reel",)), ("video", ("video",))):
        evidence = [path + "=" + val for path, val in markers if any(term in val for term in terms)]
        if evidence:
            return kind, evidence[:10]
    if extract_images(story):
        return "photo_post", ["photo/image object in post media subtree"]
    # A generic Story typename alone does not establish that a post is text-only.
    return "unknown", []

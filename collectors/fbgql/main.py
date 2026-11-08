#!/usr/bin/env python3
"""Bounded, read-only group collector using the pinned fbgql payloads/parsers.

Use a strict transport because upstream's default transport retries blocks and
follows redirects. Here a checkpoint, login redirect, or rate limit stops the run.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from html.parser import HTMLParser
from http.cookies import SimpleCookie, CookieError
import json
import math
import os
from pathlib import Path
import re
import signal
import sys
import tempfile
import time
from urllib.parse import urljoin, urlsplit
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from validation.media import classify_post, extract_images, sanitize

LIBRARY_COMMIT = "28e03ae84012be44d37a67014fcfcfcc420c9ee9"
MESSAGES = {
    "authentication_failed": "Provide FACEBOOK_COOKIE with c_user and xs; FACEBOOK_USER_ID must match c_user.",
    "session_expired": "Facebook returned a logged-out session; refresh it manually.",
    "checkpoint_required": "Facebook requires a security checkpoint; collection stopped without retry.",
    "rate_limited": "Facebook throttled or restricted requests; collection stopped without retry.",
    "group_not_accessible": "The session cannot access this group, or Facebook returned a null group feed.",
    "group_not_found": "Facebook returned HTTP 404 for the requested resource.",
    "network_error": "The HTTP request failed, exceeded the timeout, or was denied by network policy.",
    "parse_error": "The response or post does not match the pinned fbgql parser schema.",
    "unknown_error": "Unexpected collector failure; inspect configuration and pinned dependency versions.",
    "configuration_error": "Use a numeric group ID, 1..200 pages, 1..50 posts/page, a positive timeout and paired timezone-aware --from/--to bounds.",
    "dependency_missing": "Install collectors/fbgql/requirements.txt in the Python virtual environment.",
    "pagination_stalled": "The group feed repeated a pagination cursor; collection stopped.",
    "invalid_cursor": "The group feed omitted a required pagination cursor; collection stopped.",
    "media_parse_error": "Media inspection or private debug output failed; other post data was preserved.",
}


class CollectionError(Exception):
    def __init__(self, code: str):
        self.code = code
        self.message = MESSAGES[code]
        super().__init__(self.message)


# Shared resolver/orchestration can catch this stable, sanitized error interface.
CollectorError = CollectionError


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def group_id(value: str) -> str:
    value = value.strip()
    if re.fullmatch(r"[0-9]+", value) and int(value) > 0:
        return value
    try:
        u = urlsplit(value)
        allowed = {"facebook.com", "www.facebook.com", "m.facebook.com", "web.facebook.com", "mbasic.facebook.com"}
        match = re.fullmatch(r"/groups/([0-9]+)/?", u.path)
        if u.scheme == "https" and u.hostname in allowed and not u.username and not u.password and u.port in (None, 443) and match and int(match[1]) > 0:
            return match[1]
    except ValueError:
        pass
    raise CollectionError("configuration_error")


def cookies_from_env(environ) -> dict[str, str]:
    value = environ.get("FACEBOOK_COOKIE", "")
    if not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise CollectionError("authentication_failed")
    try:
        jar = SimpleCookie()
        jar.load(value)
        cookies = {key: item.value for key, item in jar.items()}
    except CookieError:
        raise CollectionError("authentication_failed") from None
    if not re.fullmatch(r"[0-9]+", cookies.get("c_user", "")) or not cookies.get("xs"):
        raise CollectionError("authentication_failed")
    expected = environ.get("FACEBOOK_USER_ID", "")
    if expected and expected != cookies["c_user"]:
        raise CollectionError("authentication_failed")
    return cookies


def error_record(code: str, stage: str, **context) -> dict:
    return {"code": code, "stage": stage, "message": MESSAGES[code], **context}


def classify_exception(exc: Exception) -> str:
    if isinstance(exc, CollectionError):
        return exc.code
    # Match only types, never serialize arbitrary dependency/network exceptions.
    return {
        "SessionInvalid": "session_expired", "RateLimited": "rate_limited",
        "RequestRejected": "unknown_error", "DocIdStale": "parse_error",
        "TransportError": "network_error", "Timeout": "network_error",
        "TimeoutError": "network_error", "ConnectionError": "network_error",
        "ProxyError": "network_error", "SSLError": "network_error",
        "JSONDecodeError": "parse_error",
    }.get(type(exc).__name__, "unknown_error")


def check_error_fields(obj: dict) -> None:
    """Classify error envelopes, without treating words in post captions as errors."""
    errors = obj.get("errors") or []
    if not isinstance(errors, list):
        raise CollectionError("parse_error")
    if obj.get("error"):
        errors = errors + [{"code": obj.get("error"), "message": str(obj.get("errorSummary", "")) + " " + str(obj.get("errorDescription", ""))}]
    for err in errors:
        if not isinstance(err, dict):
            raise CollectionError("parse_error")
        if str(err.get("severity", "")).upper() == "WARNING":
            continue
        code = err.get("api_error_code", err.get("code"))
        text = " ".join(str(err.get(k, "")) for k in ("message", "summary", "description")).lower()
        if "checkpoint" in text or "two_step_verification" in text:
            raise CollectionError("checkpoint_required")
        if code in (368, 4, 17, 32, 613, 1675012) or any(t in text for t in ("rate limit", "too many requests", "temporarily blocked", "going too fast", "account restricted", "account disabled")):
            raise CollectionError("rate_limited")
        if code in (1357001, 1357002, 1357004, 190, 102) or any(t in text for t in ("login_required", "not logged in", "please log in", "session expired")):
            raise CollectionError("session_expired")
        if "not found" in text or code == 803:
            raise CollectionError("group_not_found")
        if any(t in text for t in ("permission", "not a member", "not accessible", "access denied")):
            raise CollectionError("group_not_accessible")
        if any(t in text for t in ("doc_id", "persisted query", "missing_required_variable", "unknown operation", "is not defined on")):
            raise CollectionError("parse_error")
        raise CollectionError("unknown_error")


class LoginFormParser(HTMLParser):
    """Inspect actual form elements, not login module names in bootstrap scripts."""

    def __init__(self):
        super().__init__()
        self.found = False
        self.form_fields = None

    def handle_starttag(self, tag, attrs):
        attrs = {key: (value or "").lower() for key, value in attrs}
        if tag == "form":
            self.form_fields = set()
            if (attrs.get("id") in {"login", "login_form", "loginform"}
                    or attrs.get("name") in {"login", "login_form", "loginform"}
                    or re.search(r"/login(?:[/.]|$)", attrs.get("action", ""))):
                self.found = True
        elif tag == "input" and self.form_fields is not None:
            if attrs.get("name") == "email":
                self.form_fields.add("email")
            if attrs.get("name") == "pass" or attrs.get("type") == "password":
                self.form_fields.add("password")
            if self.form_fields == {"email", "password"}:
                self.found = True

    def handle_endtag(self, tag):
        if tag == "form":
            self.form_fields = None


class SafeTransport:
    """fbgql Transport interface, inherited proxy/CA trust, no automatic retries."""
    def __init__(self, timeout: float):
        import requests
        self.requests = requests
        self.session = requests.Session()
        self.deadline = time.monotonic() + timeout
        self.last_url = None

    def remaining(self) -> float:
        left = self.deadline - time.monotonic()
        if left <= 0:
            raise CollectionError("network_error")
        return min(left, 30.0)

    def close(self):
        self.session.close()

    def get(self, url, cookies, proxy=None, headers=None):
        return self.request("GET", url, cookies, headers=headers)

    def post_form(self, headers, data, cookies, proxy=None):
        return self.request("POST", "https://www.facebook.com/api/graphql/", cookies, headers=headers, data=data)

    def request(self, method, url, cookies, *, headers=None, data=None):
        self.last_url = None
        for hop in range(6):
            u = urlsplit(url)
            if u.scheme != "https" or u.hostname not in {"facebook.com", "www.facebook.com"} or u.username or u.password or u.port not in (None, 443):
                raise CollectionError("network_error")
            path = u.path.lower()
            if "/checkpoint" in path or "/two_step_verification" in path or "/security_challenge" in path:
                raise CollectionError("checkpoint_required")
            if "/account/disabled" in path or "/account/restricted" in path:
                raise CollectionError("rate_limited")
            if "/login" in path:
                raise CollectionError("session_expired")
            try:
                response = self.session.request(method, url, cookies=cookies, headers=headers,
                                                data=data, timeout=self.remaining(), allow_redirects=False, stream=True)
                with response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        # POST redirects are never replayed with their CSRF form.
                        if method != "GET":
                            destination = urlsplit(urljoin(url, response.headers.get("Location", "")))
                            if "/checkpoint" in destination.path or "/two_step_verification" in destination.path:
                                raise CollectionError("checkpoint_required")
                            if "/login" in destination.path:
                                raise CollectionError("session_expired")
                            raise CollectionError("network_error")
                        location = response.headers.get("Location")
                        if not location:
                            raise CollectionError("network_error")
                        url = urljoin(url, location)
                        continue
                    if status == 401:
                        raise CollectionError("session_expired")
                    if status == 429:
                        raise CollectionError("rate_limited")
                    if status == 404:
                        raise CollectionError("group_not_found")
                    if status == 403 and ("x-denied-reason" in response.headers or "x-proxy-error" in response.headers):
                        raise CollectionError("network_error")
                    if status not in (200, 403):
                        raise CollectionError("network_error")
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        self.remaining()
                        size += len(chunk)
                        if size > 32 * 1024 * 1024:
                            raise CollectionError("parse_error")
                        chunks.append(chunk)
                    body = b"".join(chunks).decode("utf-8", errors="replace")
                    if body.lstrip().startswith("<"):
                        low = body.lower()
                        if re.search(r"(?:action|href)=[\"'][^\"']*(?:/checkpoint|/two_step_verification|/security_challenge)", low):
                            raise CollectionError("checkpoint_required")
                        if re.search(r"action=[\"'][^\"']*/account/(?:disabled|restricted)", low):
                            raise CollectionError("rate_limited")
                        login = LoginFormParser()
                        login.feed(body)
                        if login.found:
                            raise CollectionError("session_expired")
                        if method == "POST" and status == 200:
                            raise CollectionError("parse_error")
                    if status == 403:
                        low = body.lower()
                        if any(marker in low for marker in ("destination not allowed", "network policy", "proxy denied", "blocked by proxy")):
                            raise CollectionError("network_error")
                        raise CollectionError("group_not_accessible")
                    self.last_url = url
                    return body
            except self.requests.RequestException:
                raise CollectionError("network_error") from None
        raise CollectionError("network_error")


def resolve_group_alias(handle: str, timeout: float = 120) -> str:
    """Resolve one group alias once, so both collectors receive the same numeric ID.

    Only explicit group markers are accepted. Recommendation/sidebar ambiguity is
    a hard parse failure; do not guess which Facebook object ID names the target.
    """
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", handle) or not math.isfinite(timeout) or timeout <= 0:
        raise CollectionError("configuration_error")
    cookies = cookies_from_env(os.environ)
    transport = SafeTransport(timeout)
    try:
        from fbgql.auth import BROWSER_HEADERS
        body = transport.get(f"https://www.facebook.com/groups/{handle}/", cookies, headers=BROWSER_HEADERS)
        import html
        normalized = html.unescape(body).replace(r"\/", "/").replace(r'\"', '"')
        candidates = set(re.findall(r'"groupID"\s*:\s*"([0-9]+)"', normalized))
        candidates.update(re.findall(r'fb://group/\?id=([0-9]+)', normalized))
        candidates.discard("0")
        if len(candidates) != 1:
            raise CollectionError("parse_error")
        return candidates.pop()
    except CollectionError:
        raise
    except ImportError:
        raise CollectionError("dependency_missing") from None
    except Exception as exc:
        raise CollectionError(classify_exception(exc)) from None
    finally:
        transport.close()


def parse_window(args):
    lower, upper = getattr(args, "from_time", None), getattr(args, "to_time", None)
    if not lower and not upper:
        return None
    try:
        if not lower or not upper:
            raise ValueError
        start, end = (datetime.fromisoformat(value.replace("Z", "+00:00")) for value in (lower, upper))
        if start.tzinfo is None or end.tzinfo is None or start > end:
            raise ValueError
        return {"from": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "to": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "complete": False, "stop_reason": "page_limit", "scanned_posts": 0,
                "unknown_timestamp_posts": 0, "ordering_violations": 0}
    except (ValueError, TypeError, AttributeError):
        raise CollectionError("configuration_error") from None


def feed_stories(objects, gid):
    """Select group feed edges, never recursive attached/recommended post IDs.

    Relay may defer page_info or stream edges into subsequent JSON objects.
    Only patches whose path explicitly names this group_feed are accepted.
    """
    stories, info, found = [], None, False
    for obj in objects:
        data = obj.get("data")
        path = obj.get("path")
        if isinstance(path, list) and "group_feed" in path:
            data = data if isinstance(data, dict) else {}
            if isinstance(data.get("page_info"), dict):
                info = data["page_info"]
            if "edges" in path:
                if isinstance(data.get("node"), dict):
                    stories.append(data["node"])
                elif "post_id" in data or data.get("__typename") == "Story":
                    stories.append(data)
                items = obj.get("items") or data.get("items") or []
                if isinstance(items, list):
                    stories.extend(item["node"] for item in items if isinstance(item, dict) and isinstance(item.get("node"), dict))
            found = True
            continue
        if not isinstance(data, dict):
            continue
        for name in ("node", "group"):
            if name in data and data[name] is None:
                raise CollectionError("group_not_accessible")
            group = data.get(name)
            if not isinstance(group, dict):
                continue
            if group.get("id") is not None and str(group["id"]) != gid:
                raise CollectionError("group_not_accessible")
            connection = group.get("group_feed")
            if connection is None and "group_feed" in group:
                raise CollectionError("group_not_accessible")
            if not isinstance(connection, dict):
                continue
            found = True
            edges = connection.get("edges")
            if not isinstance(edges, list):
                raise CollectionError("parse_error")
            for edge in edges:
                if isinstance(edge, dict) and isinstance(edge.get("node"), dict):
                    node = edge["node"]
                    if "post_id" in node or node.get("__typename") == "Story" or "comet_sections" in node:
                        stories.append(node)
            if isinstance(connection.get("page_info"), dict):
                info = connection["page_info"]
    if not found:
        raise CollectionError("parse_error")
    if not isinstance(info, dict) or not isinstance(info.get("has_next_page"), bool):
        raise CollectionError("parse_error")
    return stories, info


def primary_story(story):
    """Avoid a shared original post supplying the outer post's identity/text/time."""
    excluded = {"attached_story", "shared_story", "recommendations", "recommended_stories", "suggested_posts", "related_stories", "related_posts"}
    if isinstance(story, dict):
        return {key: primary_story(value) for key, value in story.items() if key not in excluded}
    if isinstance(story, list):
        return [primary_story(value) for value in story]
    return story


def valid_timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0:
        try:
            datetime.fromtimestamp(value, timezone.utc)
            return True
        except (OverflowError, OSError, ValueError):
            pass
    return False


def save_debug(directory, record, story, *, group, page, secrets):
    if not directory:
        return None
    post_id = str(record.get("post_id") or "unidentified")
    safe_id = post_id if re.fullmatch(r"[A-Za-z0-9_-]{1,160}", post_id) else "unidentified"
    target = Path(directory) / f"{safe_id}.json"
    suffix = 1
    while target.exists():
        suffix += 1
        target = Path(directory) / f"{safe_id}-{suffix}.json"
    value = {"schema_version": 1, "collector": "fbgql", "post_id": record.get("post_id"), "group_id": group,
             "page": page, "payload": story, "image_candidates": record.get("image_candidates", []),
             "possible_type": record.get("possible_type", "unknown"), "type_evidence": record.get("type_evidence", [])}
    atomic_json(str(target), sanitize(value, secrets=secrets))
    return str(target)


def collect(args, environ=None, transport_factory=SafeTransport) -> dict:
    environ = os.environ if environ is None else environ
    started = time.monotonic()
    out = {"schema_version": 1, "collector": "fbgql", "source": "live", "group_id": None,
           "run_id": args.run_id or str(uuid.uuid4()), "started_at": now(), "collected_at": None,
           "duration_ms": 0, "pages_fetched": 0, "requested_pages": None, "page_size": None,
           "sort": "CHRONOLOGICAL", "library_commit": LIBRARY_COMMIT,
           "status": "failed", "errors": [], "posts": [], "window": None, "unparsed_debug_files": []}
    transport, stage, page = None, "configuration", 0
    try:
        gid = group_id(args.group or environ.get("FACEBOOK_GROUP_ID", ""))
        out["group_id"] = gid
        pages, page_size, timeout = int(args.pages), int(args.page_size), float(args.timeout)
        if not (1 <= pages <= 200 and 1 <= page_size <= 50 and math.isfinite(timeout) and timeout > 0):
            raise CollectionError("configuration_error")
        out["window"] = parse_window(args)
        window = out["window"]
        lower = datetime.fromisoformat(window["from"].replace("Z", "+00:00")).timestamp() if window else None
        out["requested_pages"], out["page_size"] = pages, page_size
        stage = "authentication"
        cookies = cookies_from_env(environ)
        stage = "dependencies"
        try:
            from fbgql import auth, config, parsing, payloads
            from fbgql.models import Account
        except ImportError:
            raise CollectionError("dependency_missing") from None
        stage = "authentication"
        transport = transport_factory(timeout)
        print(f"[fbgql] starting group={gid}", file=sys.stderr)
        session = auth.resolve_session(Account(cookies=cookies), transport)
        secrets = [environ.get("FACEBOOK_COOKIE", ""), session.fb_dtsg, *[value for key, value in cookies.items() if key != "c_user"]]
        doc_id = config.DocIdRegistry().get("group_feed")
        cursor, seen_cursors, previous_time = None, set(), None
        headers = dict(config.BASE_HEADERS)
        headers.update({"origin": "https://www.facebook.com", "referer": f"https://www.facebook.com/groups/{gid}",
                        "x-fb-friendly-name": config.FRIENDLY_GROUP_FEED})
        for page in range(1, pages + 1):
            stage = "fetch_page"
            if page > 1:
                # Bounded, conservative interval; never retry blocked responses.
                if hasattr(transport, "remaining"):
                    transport.remaining()
                time.sleep(1)
            form = payloads.group_feed_payload(group_id=gid, cursor=cursor, c_user=session.c_user,
                                              fb_dtsg=session.fb_dtsg, doc_id=doc_id, count=page_size)
            variables = json.loads(form["variables"])
            variables["sortingSetting"] = "CHRONOLOGICAL"
            form["variables"] = json.dumps(variables)
            body = transport.post_form(headers, form, session.cookies, session.proxy)
            stage = "parse_page"
            objects = list(parsing.iter_json_objects(body))
            if not objects:
                raise CollectionError("parse_error")
            clean_body = re.sub(r"^\s*for\s*\(;;\);", "", body)
            nonempty_lines = [line for line in clean_body.splitlines() if line.strip()]
            if len(objects) != len(nonempty_lines):
                out["errors"].append(error_record("parse_error", "parse_page", page=page))
            for obj in objects:
                check_error_fields(obj)
            stories, info = feed_stories(objects, gid)
            out["pages_fetched"] += 1
            count, page_times = 0, []
            # Isolate each upstream story parse so a malformed post cannot kill a page.
            for index, story in enumerate(stories):
                try:
                    own_story = primary_story(story)
                    post = parsing._story_to_post(own_story, None)
                    missing_id = post is None
                    if missing_id:
                        # Run the upstream field parser while preserving an unavailable
                        # identity as null; never emit this internal sentinel.
                        post = parsing._story_to_post({**own_story, "post_id": "__unidentified__"}, None)
                    record = asdict(post)
                    if missing_id:
                        record["post_id"] = None
                    record.update(image_candidates=[], possible_type="unknown", type_evidence=[], debug_file=None, debug_response_saved=False)
                    try:
                        record["image_candidates"] = extract_images(story)
                        record["possible_type"], record["type_evidence"] = classify_post(story)
                        record["debug_file"] = save_debug(getattr(args, "debug_dir", None), record, story, group=gid, page=page, secrets=secrets)
                        record["debug_response_saved"] = record["debug_file"] is not None
                    except Exception:
                        out["errors"].append(error_record("media_parse_error", "media", page=page, post_index=index))
                    record = sanitize(record, secrets=secrets)
                    out["posts"].append(record)
                    count += 1
                    value = record.get("created_time")
                    valid_time = valid_timestamp(value)
                    page_times.append(value if valid_time else None)
                    if window:
                        window["scanned_posts"] += 1
                        if not valid_time:
                            window["unknown_timestamp_posts"] += 1
                        else:
                            if previous_time is not None and value > previous_time:
                                window["ordering_violations"] += 1
                            previous_time = value
                except Exception:
                    page_times.append(None)
                    if window:
                        window["scanned_posts"] += 1
                        window["unknown_timestamp_posts"] += 1
                    out["errors"].append(error_record("parse_error", "parse_post", page=page, post_index=index))
                    # Even a model parse failure must not destroy the GraphQL
                    # evidence needed to investigate a known image post later.
                    try:
                        evidence = {"post_id": story.get("post_id"), "image_candidates": extract_images(story)}
                        evidence["possible_type"], evidence["type_evidence"] = classify_post(story)
                        path = save_debug(getattr(args, "debug_dir", None), evidence, story, group=gid, page=page, secrets=secrets)
                        if path:
                            out["unparsed_debug_files"].append(path)
                    except Exception:
                        out["errors"].append(error_record("media_parse_error", "debug", page=page, post_index=index))
            print(f"[fbgql] page={page} posts={count}", file=sys.stderr)
            if page_times and any(value is not None for value in page_times):
                oldest = datetime.fromtimestamp(min(value for value in page_times if value is not None), timezone.utc).isoformat()
                print(f"[fbgql] oldest_post={oldest}", file=sys.stderr)
            if not info["has_next_page"]:
                if window:
                    window["complete"], window["stop_reason"] = True, "feed_exhausted"
                break
            if window and page_times and all(value is not None and value < lower for value in page_times) and not window["ordering_violations"]:
                window["complete"], window["stop_reason"] = True, "older_than_from"
                break
            cursor = info.get("end_cursor")
            if not isinstance(cursor, str) or not cursor:
                raise CollectionError("invalid_cursor")
            if cursor in seen_cursors:
                raise CollectionError("pagination_stalled")
            seen_cursors.add(cursor)
    except (TypeError, ValueError) as exc:
        out["errors"].append(error_record("configuration_error" if stage == "configuration" else classify_exception(exc), stage, **({"page": page} if page else {})))
    except Exception as exc:
        out["errors"].append(error_record(classify_exception(exc), stage, **({"page": page} if page else {})))
    finally:
        if transport is not None:
            transport.close()
        out["collected_at"] = now()
        out["duration_ms"] = round((time.monotonic() - started) * 1000)
        out["status"] = "success" if not out["errors"] else "partial" if out["posts"] else "failed"
        if out["window"]:
            window = out["window"]
            if out["errors"]:
                window["complete"], window["stop_reason"] = False, "error"
            if window["unknown_timestamp_posts"] or window["ordering_violations"]:
                window["complete"] = False
    for err in out["errors"]:
        print(f"[fbgql] error={err['code']} stage={err['stage']}" + (f" page={err['page']}" if "page" in err else ""), file=sys.stderr)
    print(f"[fbgql] total={len(out['posts'])} duration={out['duration_ms'] / 1000:.3f}s status={out['status']}", file=sys.stderr)
    if out["window"]:
        print(f"[fbgql] window_complete={str(out['window']['complete']).lower()} stop_reason={out['window']['stop_reason']}", file=sys.stderr)
    return out


def atomic_json(path: str, value: dict) -> None:
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=target.parent, delete=False) as handle:
            temporary = handle.name
            json.dump(value, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", default="")
    parser.add_argument("--pages", default="2")
    parser.add_argument("--page-size", default="20")
    parser.add_argument("--timeout", default="120", help="Total collection deadline in seconds")
    parser.add_argument("--output", default="data/raw/fbgql.json")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--from", dest="from_time", help="Inclusive ISO 8601 lower bound with timezone")
    parser.add_argument("--to", dest="to_time", help="Inclusive ISO 8601 upper bound with timezone")
    parser.add_argument("--debug-dir", help="Private per-post sanitized GraphQL evidence directory")
    args = parser.parse_args(argv)
    # POSIX hard deadline also bounds DNS/connect or slow streaming. collect catches
    # CollectionError and writes the partial run. No watchdog process holds secrets.
    previous_handler = None
    try:
        timeout = float(args.timeout)
        if math.isfinite(timeout) and timeout > 0 and hasattr(signal, "setitimer"):
            def expired(_signum, _frame):
                raise CollectionError("network_error")
            previous_handler = signal.signal(signal.SIGALRM, expired)
            signal.setitimer(signal.ITIMER_REAL, timeout)
    except (ValueError, OverflowError):
        pass
    try:
        result = collect(args)
    finally:
        if previous_handler is not None:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous_handler)
    try:
        atomic_json(args.output, result)
    except OSError:
        print("[fbgql] error=output_error Could not write the raw JSON output.", file=sys.stderr)
        return 2
    return 0 if result["status"] == "success" else 1


if __name__ == "__main__":
    raise SystemExit(main())

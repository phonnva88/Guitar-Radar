#!/usr/bin/env python3
"""Bounded, read-only group collector using the pinned fbgql payloads/parsers.

Use a strict transport because upstream's default transport retries blocks and
follows redirects. Here a checkpoint, login redirect, or rate limit stops the run.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
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
    "configuration_error": "Use a numeric group ID or numeric Facebook /groups/<id> URL, 1..20 pages, 1..50 posts/page and a positive timeout.",
    "dependency_missing": "Install collectors/fbgql/requirements.txt in the Python virtual environment.",
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


class SafeTransport:
    """fbgql Transport interface, inherited proxy/CA trust, no automatic retries."""
    def __init__(self, timeout: float):
        import requests
        self.requests = requests
        self.session = requests.Session()
        self.deadline = time.monotonic() + timeout

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
        for hop in range(6):
            u = urlsplit(url)
            if u.scheme != "https" or u.hostname not in {"facebook.com", "www.facebook.com"} or u.username or u.password or u.port not in (None, 443):
                raise CollectionError("network_error")
            path = u.path.lower()
            if "/checkpoint" in path or "/two_step_verification" in path:
                raise CollectionError("checkpoint_required")
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
                        if re.search(r"(?:action|href)=[\"'][^\"']*(?:/checkpoint|/two_step_verification)", low):
                            raise CollectionError("checkpoint_required")
                        if any(marker in low for marker in ('name="email"', "login_form", "loginform", 'name="login"')):
                            raise CollectionError("session_expired")
                        if method == "POST" and status == 200:
                            raise CollectionError("parse_error")
                    if status == 403:
                        low = body.lower()
                        if any(marker in low for marker in ("destination not allowed", "network policy", "proxy denied", "blocked by proxy")):
                            raise CollectionError("network_error")
                        raise CollectionError("group_not_accessible")
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


def collect(args, environ=None, transport_factory=SafeTransport) -> dict:
    environ = os.environ if environ is None else environ
    started = time.monotonic()
    out = {"schema_version": 1, "collector": "fbgql", "source": "live", "group_id": None,
           "run_id": args.run_id or str(uuid.uuid4()), "started_at": now(), "collected_at": None,
           "duration_ms": 0, "pages_fetched": 0, "requested_pages": None, "page_size": None,
           "sort": "CHRONOLOGICAL", "library_commit": LIBRARY_COMMIT,
           "status": "failed", "errors": [], "posts": []}
    transport, stage, page = None, "configuration", 0
    try:
        gid = group_id(args.group or environ.get("FACEBOOK_GROUP_ID", ""))
        out["group_id"] = gid
        pages, page_size, timeout = int(args.pages), int(args.page_size), float(args.timeout)
        if not (1 <= pages <= 20 and 1 <= page_size <= 50 and math.isfinite(timeout) and timeout > 0):
            raise CollectionError("configuration_error")
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
        doc_id = config.DocIdRegistry().get("group_feed")
        cursor, seen_cursors, seen_posts = None, set(), set()
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
            connections = []
            for obj in objects:
                data = obj.get("data")
                if isinstance(data, dict):
                    node = data.get("node", data.get("group"))
                    if node is None and ("node" in data or "group" in data):
                        raise CollectionError("group_not_accessible")
                    connections.extend(parsing.deep_find_all(data, "group_feed"))
            connection = next((c for c in connections if isinstance(c, dict)), None)
            if connection is None:
                raise CollectionError("group_not_accessible" if connections else "parse_error")
            info = connection.get("page_info")
            if not isinstance(info, dict) or not isinstance(info.get("has_next_page"), bool):
                raise CollectionError("parse_error")
            out["pages_fetched"] += 1
            count = 0
            # Isolate each upstream story parse so a malformed post cannot kill a page.
            for obj in objects:
                for index, story in enumerate(parsing._iter_story_dicts(obj)):
                    try:
                        post = parsing._story_to_post(story, None)
                        if post is None:
                            continue
                        if post.post_id in seen_posts:
                            continue
                        seen_posts.add(post.post_id)
                        out["posts"].append(asdict(post))
                        count += 1
                    except Exception:
                        out["errors"].append(error_record("parse_error", "parse_post", page=page, post_index=index))
            print(f"[fbgql] page={page} posts={count}", file=sys.stderr)
            if not info["has_next_page"]:
                break
            cursor = info.get("end_cursor")
            if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
                raise CollectionError("parse_error")
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
    for err in out["errors"]:
        print(f"[fbgql] error={err['code']} stage={err['stage']}" + (f" page={err['page']}" if "page" in err else ""), file=sys.stderr)
    print(f"[fbgql] total={len(out['posts'])} duration={out['duration_ms'] / 1000:.3f}s status={out['status']}", file=sys.stderr)
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

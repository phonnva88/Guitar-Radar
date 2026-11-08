"""Bounded Facebook acquisition without normalization, filtering, or storage."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.cookies import CookieError, SimpleCookie
import json
import math
import os
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
import uuid

from scripts import phase0

ERROR_CODES = {
    "authentication_failed", "session_expired", "checkpoint_required", "rate_limited",
    "account_restricted", "group_not_accessible", "group_not_found", "network_error",
    "parse_error", "configuration_error", "dependency_missing", "unknown_error",
    "pagination_stalled", "invalid_cursor", "media_parse_error", "interrupted",
}
ENVELOPE_KEYS = {
    "schema_version", "collector", "source", "library_commit", "requested_pages",
    "page_size", "sort", "group_id", "run_id", "started_at", "collected_at",
    "duration_ms", "pages_fetched", "status", "errors", "posts", "window",
}


def safe_code(code: object) -> str:
    return code if isinstance(code, str) and code in ERROR_CODES else "unknown_error"


class CollectionFailure(Exception):
    """Only fixed error codes cross the acquisition boundary."""

    def __init__(self, code: object):
        self.code = safe_code(code)
        super().__init__(self.code)


def _instant(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(timezone.utc)


def _session_literals():
    header = os.environ.get("FACEBOOK_COOKIE", "")
    result = [header] if header else []
    try:
        cookies = SimpleCookie()
        cookies.load(header)
        # c_user is a source account ID, not a session secret. Redacting it
        # could incorrectly erase an author ID from the observed post.
        result += [item.value for key, item in cookies.items() if key != "c_user" and len(item.value) >= 8]
    except CookieError:
        pass
    return sorted(set(result), key=len, reverse=True)


def _sanitize(value, secrets):
    if isinstance(value, dict):
        return {key: _sanitize(item, secrets) for key, item in value.items()
                if not re.search(r"cookie|authorization|headers?|session|access.?token|fb_dtsg|jazoest|^lsd$", key, re.I)}
    if isinstance(value, list):
        return [_sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
    return value


def write_json(path: Path, value: dict):
    """Write an explicit export atomically with private file permissions."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".facebook-crawler-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class FacebookCrawler:
    """Invoke the validated Go adapter and return its sanitized raw envelope.

    Post fields such as id, message, createdAt, and image_candidates retain
    their upstream shape. This module does not select, normalize, or deduplicate
    posts. A page cap or timeout remains visible as an incomplete window.
    """

    def __init__(self, lookback_hours=72, timeout=300, *, root=None,
                 execute=None, resolve_group=None, load_env=None, now=None):
        if not math.isfinite(lookback_hours) or not 0 < lookback_hours <= 8760:
            raise ValueError("lookback_hours must be between 0 and 8760")
        if not math.isfinite(timeout) or not 1 <= timeout <= 3600:
            raise ValueError("timeout must be between 1 and 3600 seconds")
        self.lookback_hours, self.timeout = lookback_hours, timeout
        self.root = Path(root) if root is not None else phase0.ROOT
        self.execute = execute
        self.cancel_event = threading.Event()
        self.resolve_group = resolve_group or phase0.resolve_group
        self.load_env = load_env or phase0.load_local_env
        self.now = now or (lambda: datetime.now(timezone.utc))
        self._session_baseline = {key: os.environ[key] for key in
                                  ("FACEBOOK_COOKIE", "FACEBOOK_COOKIE_FILE", "FACEBOOK_USER_ID")
                                  if key in os.environ}

    def _execute(self, command, *, cwd, timeout, **_kwargs):
        """Shutdown and lock loss can terminate and reap an in-flight adapter."""
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.cancel_event.is_set():
                    raise CollectionFailure("interrupted")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                try:
                    return subprocess.CompletedProcess(command, process.wait(timeout=min(0.25, remaining)))
                except subprocess.TimeoutExpired:
                    continue
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)

    def _bounds(self, run):
        lower, upper = run.get("requested_from"), run.get("requested_to")
        try:
            if lower is not None or upper is not None:
                start, end = _instant(lower), _instant(upper)
                if start > end:
                    raise ValueError
                return start, end
            end = self.now()
            if end.tzinfo is None:
                raise ValueError
            end = end.astimezone(timezone.utc)
            return end - timedelta(hours=self.lookback_hours), end
        except (ValueError, TypeError, AttributeError):
            raise CollectionFailure("configuration_error") from None

    def __call__(self, run: dict) -> dict:
        try:
            if self.cancel_event.is_set():
                raise CollectionFailure("interrupted")
            start, end = self._bounds(run)
            for key in ("FACEBOOK_COOKIE", "FACEBOOK_COOKIE_FILE", "FACEBOOK_USER_ID"):
                if key in self._session_baseline:
                    os.environ[key] = self._session_baseline[key]
                else:
                    os.environ.pop(key, None)
            self.load_env()
            if not os.environ.get("FACEBOOK_COOKIE"):
                raise CollectionFailure("authentication_failed")
            group_url = run.get("group_url") or run.get("url")
            group = str(self.resolve_group(group_url, timeout=self.timeout))
            if self.cancel_event.is_set():
                raise CollectionFailure("interrupted")
            if not group.isdigit() or int(group) <= 0:
                raise CollectionFailure("group_not_found")
            binary = self.root / ".bin/facebook-go"
            if not binary.is_file():
                raise CollectionFailure("configuration_error")
            lower, upper = (instant.isoformat().replace("+00:00", "Z") for instant in (start, end))
            collector_run_id = "facebook-crawl-" + uuid.uuid4().hex
            with tempfile.TemporaryDirectory(prefix="guitar-radar-") as temporary:
                output = Path(temporary) / "output.json"
                command = [str(binary), "--group", group, "--from", lower, "--to", upper,
                           "--pages", "50", "--page-size", "20", "--timeout", str(self.timeout),
                           "--run-id", collector_run_id, "--output", str(output)]
                timed_out, returncode = False, 0
                try:
                    result = (self.execute or self._execute)(command, cwd=self.root, check=False,
                                                           timeout=self.timeout + 10,
                                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    returncode = result.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                if not output.is_file():
                    raise CollectionFailure("network_error" if timed_out else "parse_error")
                if output.stat().st_size > 64 * 1024 * 1024:
                    raise CollectionFailure("parse_error")
                try:
                    raw = json.loads(output.read_text(encoding="utf-8"))
                    if not isinstance(raw, dict) or type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
                        raise ValueError
                    if raw.get("run_id") != collector_run_id or raw.get("group_id") != group:
                        raise ValueError
                    if raw.get("collector") != "facebook-go" or raw.get("source") != "live":
                        raise ValueError
                    if raw.get("status") not in {"success", "partial", "failed"}:
                        raise ValueError
                    if not isinstance(raw.get("posts"), list) or any(not isinstance(post, dict) for post in raw["posts"]):
                        raise ValueError
                    if not isinstance(raw.get("errors"), list) or any(not isinstance(error, dict) for error in raw["errors"]):
                        raise ValueError
                    bounds = raw.get("window")
                    if not isinstance(bounds, dict) or type(bounds.get("complete")) is not bool:
                        raise ValueError
                    if _instant(bounds.get("from")) != _instant(lower) or _instant(bounds.get("to")) != _instant(upper):
                        raise ValueError
                    if returncode and raw["status"] == "success":
                        raise ValueError
                except (ValueError, TypeError, KeyError, AttributeError):
                    raise CollectionFailure("parse_error") from None
                raw = {key: value for key, value in raw.items() if key in ENVELOPE_KEYS}
                raw["errors"] = [{"code": safe_code(error.get("code"))} for error in raw["errors"]]
                if timed_out:
                    raw["status"] = "partial" if raw["posts"] else "failed"
                    raw["errors"].append({"code": "network_error"})
                    raw["window"].update(complete=False, stop_reason="error")
                return _sanitize(raw, _session_literals())
        except CollectionFailure:
            raise
        except phase0.RunError as exc:
            raise CollectionFailure(exc.code) from None
        except ImportError:
            raise CollectionFailure("dependency_missing") from None
        except OSError:
            raise CollectionFailure("network_error") from None
        except Exception:
            raise CollectionFailure("unknown_error") from None

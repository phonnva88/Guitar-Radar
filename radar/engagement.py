"""Read and submit one Facebook interaction for a stored post."""
from __future__ import annotations

import hashlib
from http.cookies import CookieError, SimpleCookie
import json
import os
import re
import subprocess
import threading
from uuid import uuid4

from psycopg.types.json import Jsonb

from normalizer.engagement import engagement_id, observed_count
from radar.config import ROOT
from radar.db import Conflict, InvalidInput, NotFound, STOP_CODES, save_engagement
from scripts.phase0 import RunError, load_local_env


_ERRORS = STOP_CODES | {"network_error", "parse_error", "group_not_accessible", "group_not_found", "comments_unavailable",
                       "unknown_error", "invalid_input", "write_unknown", "configuration_error",
                       "feedback_unavailable", "page_stale", "write_disabled"}


class EngagementError(Exception):
    def __init__(self, code, http_status=502):
        self.code = code if code in _ERRORS else "unknown_error"
        self.http_status = http_status


class FacebookEngagement:
    def __init__(self, db, write_posts=()):
        self.db = db
        self.write_posts = set(write_posts)
        self._environment = os.environ.copy()
        self._lock = threading.Lock()

    def _session(self):
        env = self._environment.copy()
        try:
            load_local_env(env)
            cookie = SimpleCookie(env.get("FACEBOOK_COOKIE", ""))
            account = cookie.get("c_user")
            account = account.value if account else ""
        except (CookieError, RunError, OSError, UnicodeError):
            raise EngagementError("authentication_failed", 503) from None
        if not re.fullmatch(r"[0-9]{1,32}", account) or not cookie.get("xs") or (env.get("FACEBOOK_USER_ID") or account) != account:
            raise EngagementError("authentication_failed", 503)
        return env, account

    def _target(self, post_id):
        with self.db.connect() as conn:
            row = conn.execute("""SELECT p.id, p.post_id, p.source, g.enabled, g.facebook_group_id, e.feedback_id,
                                  e.comment_count, e.reaction_count, e.comment_count_at, e.reaction_count_at
                                  FROM posts p JOIN monitored_groups g ON g.id = p.group_id
                                  LEFT JOIN post_engagement e ON e.post_id = p.id WHERE p.id = %s""", (post_id,)).fetchone()
        if row is None:
            raise NotFound("Post not found")
        return row

    def info(self, post_id):
        row = self._target(post_id)
        try:
            _, account = self._session()
        except EngagementError:
            account = None
        available = bool(account and row["enabled"] and row["source"] == "live" and engagement_id(row["feedback_id"]))
        return {"account_id": account if available else None, "can_read": available,
                "can_write": available and row["post_id"] in self.write_posts,
                **{key: row[key] for key in ("comment_count", "reaction_count", "comment_count_at", "reaction_count_at")}}

    def _ensure_enabled(self, row):
        if not row["enabled"] or row["source"] != "live" or not engagement_id(row["feedback_id"]):
            raise EngagementError("feedback_unavailable", 409)
        paused = self.db.state()["paused_code"]
        if paused:
            raise EngagementError(paused, 409)

    def _fetch(self, env, account, row, operation, **fields):
        writing = operation != "comments"
        binary = ROOT / ".bin/facebook-go"
        if not binary.is_file():
            raise EngagementError("configuration_error", 503)
        payload = {"operation": operation, "feedback_id": row["feedback_id"], "account_id": account,
                   "post_id": row["post_id"], "group_id": row["facebook_group_id"], **fields}
        try:
            result = subprocess.run([str(binary), "engagement"], cwd=ROOT, env=env, input=json.dumps(payload),
                                    text=True, capture_output=True, check=True, timeout=16)
            value = json.loads(result.stdout)
            if not isinstance(value, dict):
                raise ValueError
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError, ValueError, UnicodeError):
            raise EngagementError("write_unknown" if writing else "parse_error") from None
        except OSError:
            raise EngagementError("configuration_error", 503) from None
        if isinstance(value.get("error"), str):
            code = value["error"]
            if code in STOP_CODES:
                with self.db.connect() as conn:
                    conn.execute("UPDATE app_state SET paused_code = COALESCE(paused_code, %s) WHERE id=1", (code,))
            raise EngagementError(code)
        if value.get("account_id") != account:
            raise EngagementError("write_unknown" if writing else "parse_error")
        return value

    def comments(self, post_id, page, thread, refresh=False):
        row = self._target(post_id)
        env, account = self._session()
        if not self._lock.acquire(blocking=False):
            raise Conflict("Another Facebook interaction is in progress")
        try:
            return self._comments(row, env, account, page, thread, refresh)
        finally:
            self._lock.release()

    def _comments(self, row, env, account, page, thread, refresh):
        post_id = row["id"]
        if page > 1 and (thread is None or refresh):
            raise EngagementError("page_stale", 409)
        with self.db.connect() as conn:
            pages = conn.execute("""SELECT page, thread_id, items, next_cursor, fetched_at, preview_only,
                                    fetched_at > now()-interval '5 minutes' AS fresh
                                    FROM post_comment_pages WHERE post_id = %s AND account_id = %s
                                    AND page IN (1, %s, %s)""", (post_id, account, page, page - 1)).fetchall()
        cached = {item["page"]: item for item in pages}
        if page > 1 and (not cached.get(1) or cached[1]["thread_id"] != thread):
            raise EngagementError("page_stale", 409)
        if not refresh and page in cached and (page > 1 or cached[1]["fresh"]):
            return self._page(cached[page])
        self._ensure_enabled(row)
        if page > 1 and (page - 1 not in cached or not cached[page - 1]["next_cursor"]):
            raise EngagementError("page_stale", 409)
        cursor = cached[page - 1]["next_cursor"] if page > 1 else ""
        value = self._fetch(env, account, row, "comments", cursor=cursor)
        metrics = {key: observed_count(value.get(key)) for key in ("comment_count", "reaction_count")}
        engagement = {"feedback_id": row["feedback_id"], **metrics}
        if value.get("comments_unavailable") is True:
            if any(count is not None for count in metrics.values()):
                with self.db.connect() as conn:
                    save_engagement(conn, post_id, engagement)
            raise EngagementError("comments_unavailable")
        items = value.get("comments")
        next_cursor = value.get("next_cursor") or None
        if not isinstance(items, list) or len(items) > 100 or (
            next_cursor is not None and (not isinstance(next_cursor, str) or len(next_cursor) > 4096 or next_cursor == cursor)
        ):
            raise EngagementError("parse_error")
        comments = [self._comment(item) for item in items]
        with self.db.connect() as conn:
            if page == 1:
                thread = uuid4()
                conn.execute("DELETE FROM post_comment_pages WHERE post_id=%s AND account_id=%s", (post_id, account))
            stored = conn.execute("""INSERT INTO post_comment_pages
                (post_id, account_id, page, thread_id, items, next_cursor, preview_only) VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (post_id, account_id, page) DO UPDATE SET
                  thread_id=EXCLUDED.thread_id, items=EXCLUDED.items, next_cursor=EXCLUDED.next_cursor,
                  preview_only=EXCLUDED.preview_only, fetched_at=now()
                RETURNING page, thread_id, items, next_cursor, fetched_at, preview_only""",
                (post_id, account, page, thread, Jsonb(comments), next_cursor, value.get("preview_only") is True)).fetchone()
            if any(count is not None for count in metrics.values()):
                save_engagement(conn, post_id, engagement)
        return self._page(stored)

    @staticmethod
    def _page(row):
        return {"items": row["items"], "page": row["page"], "thread": row["thread_id"],
                "fetched_at": row["fetched_at"], "has_more": bool(row["next_cursor"]) and row["page"] < 100,
                "preview_only": row["preview_only"]}

    @staticmethod
    def _comment(item):
        if not isinstance(item, dict) or not engagement_id(item.get("id")):
            raise EngagementError("parse_error")
        return {key: item[key] for key in ("id", "authorName", "message", "createdAt", "parent_id")
                if isinstance(item.get(key), str)}

    def write(self, post_id, account, action_id, operation, **fields):
        row = self._target(post_id)
        if row["post_id"] not in self.write_posts:
            raise EngagementError("write_disabled", 403)
        env, current_account = self._session()
        if current_account != account:
            raise EngagementError("authentication_failed", 409)
        if not self._lock.acquire(blocking=False):
            raise Conflict("Another Facebook interaction is in progress")
        try:
            return self._write(row, env, account, action_id, operation, **fields)
        finally:
            self._lock.release()

    def _write(self, row, env, account, action_id, operation, **fields):
        digest = hashlib.sha256(json.dumps([operation, fields], sort_keys=True).encode()).hexdigest()
        post_id = row["id"]
        with self.db.connect() as conn:
            existing = conn.execute("SELECT * FROM facebook_engagement_actions WHERE id=%s", (action_id,)).fetchone()
        if existing:
            if (existing["post_id"], existing["account_id"], existing["request_hash"]) != (post_id, account, digest):
                raise InvalidInput("Mã yêu cầu đã được dùng cho thao tác khác.")
            return existing["result"] or {"state": "unknown", "error": "write_unknown"}
        self._ensure_enabled(row)
        with self.db.connect() as conn:
            inserted = conn.execute("""INSERT INTO facebook_engagement_actions
                   (id,post_id,account_id,request_hash,state) VALUES (%s,%s,%s,%s,'sending')
                   ON CONFLICT (id) DO NOTHING RETURNING id""", (action_id, post_id, account, digest)).fetchone()
        if inserted is None:
            raise Conflict("Another Facebook interaction is in progress")
        try:
            value = self._fetch(env, account, row, operation, **fields)
            output = {"state": "confirmed"}
            if operation == "comment":
                try:
                    output["comment"] = self._comment(value.get("comment"))
                except EngagementError:
                    raise EngagementError("write_unknown") from None
            elif value.get("reaction") != fields["reaction"]:
                raise EngagementError("write_unknown")
            else:
                output["reaction"] = value["reaction"]
        except EngagementError as exc:
            output = {"state": "unknown" if exc.code in {"write_unknown", "unknown_error", "parse_error", "network_error"}
                      else "rejected", "error": exc.code}
        with self.db.connect() as conn:
            conn.execute("UPDATE facebook_engagement_actions SET state=%s,result=%s WHERE id=%s",
                         (output["state"], Jsonb(output), action_id))
            if output["state"] == "confirmed" and operation == "comment":
                conn.execute("UPDATE post_comment_pages SET fetched_at='epoch' WHERE post_id=%s AND account_id=%s",
                             (post_id, account))
        return output

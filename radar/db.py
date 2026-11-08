"""PostgreSQL persistence for the private Phase 1 application.

Connections are short lived except the worker's advisory lock. Every ingestion is
one transaction, including run health and pause state; no cookie/raw payload is stored.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import hashlib
from pathlib import Path
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg.rows import dict_row

from normalizer import FACEBOOK_HOSTS, canonical_permalink, timestamp, web_url


class InvalidInput(ValueError):
    pass


class Conflict(ValueError):
    pass


class NotFound(ValueError):
    pass


STOP_CODES = {"authentication_failed", "session_expired", "checkpoint_required", "rate_limited", "account_restricted"}
ERROR_CODES = STOP_CODES | {"network_error", "parse_error", "group_not_found", "group_not_accessible", "unknown_error",
                            "configuration_error", "dependency_missing", "pagination_stalled", "invalid_cursor",
                            "media_parse_error", "interrupted", "group_disabled", "worker_error", "incomplete_window"}
WORKER_LOCK = 734291086
MIGRATION_LOCK = 734291087


def safe_code(code):
    return code if isinstance(code, str) and code in ERROR_CODES else "unknown_error"


def fingerprint(value):
    if not isinstance(value, str):
        return None
    normalized = " ".join(unicodedata.normalize("NFKC", value).casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest() if len(normalized) >= 40 else None


def _positive_id(value):
    if type(value) is not int or value < 1:
        raise InvalidInput("Expected a positive record ID")
    return value


def _group_url(value):
    if not isinstance(value, str):
        raise InvalidInput("Expected a Facebook group URL or numeric ID")
    value = value.strip()
    if re.fullmatch(r"[1-9][0-9]*", value):
        return f"https://www.facebook.com/groups/{value}", value
    try:
        parsed = urlsplit(value)
        match = re.fullmatch(r"/groups/([A-Za-z0-9._-]+)/?", parsed.path)
        if parsed.scheme != "https" or parsed.hostname not in FACEBOOK_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443) or not match:
            raise ValueError
        handle = match[1]
        if handle.isdigit() and int(handle) == 0:
            raise ValueError
        return f"https://www.facebook.com/groups/{handle}", handle if handle.isdigit() else None
    except ValueError:
        raise InvalidInput("Expected an HTTPS Facebook /groups/<id-or-alias> URL") from None


def _post(item, group_id):
    if not isinstance(item, dict):
        raise InvalidInput("Invalid normalized post")
    post_id = item.get("post_id")
    if not isinstance(post_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", post_id):
        raise InvalidInput("A normalized post needs a valid post ID")
    if item.get("group_id") not in (None, group_id):
        raise InvalidInput("Post group does not match the collected group")
    url = item.get("url")
    key = canonical_permalink(url) if url else None
    if url and (not key or key != "post:" + post_id):
        raise InvalidInput("Post permalink disagrees with its post ID")
    if url:
        parsed = urlsplit(url)
        group_match = re.fullmatch(r"/groups/([0-9]+)/(?:posts|permalink)/[^/]+/?", parsed.path)
        if group_match and group_match[1] != group_id:
            raise InvalidInput("Post permalink belongs to a different group")
        # Keep only the identity parameters on PHP permalinks, never tracking/auth fields.
        if parsed.path in ("/story.php", "/permalink.php"):
            from urllib.parse import parse_qs, urlencode
            query = parse_qs(parsed.query)
            query = urlencode({k: query[k][0] for k in ("story_fbid", "id") if k in query})
        else:
            query = ""
        url = urlunsplit(("https", "www.facebook.com", parsed.path, query, ""))
    posted_at = timestamp(item.get("posted_at"))
    if item.get("posted_at") is not None and not posted_at:
        raise InvalidInput("Invalid published timestamp")
    images = item.get("images", [])
    if not isinstance(images, list) or any(not isinstance(x, str) or not web_url(x) or urlsplit(x).scheme != "https" for x in images):
        raise InvalidInput("Media URLs must be HTTPS")
    # The normalized validation evidence carries dimensions without storing raw bodies.
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


class Database:
    def __init__(self, dsn):
        self.dsn = dsn
        self._worker_connection = None

    def connect(self, **kwargs):
        return psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10, **kwargs)

    def migrate(self):
        sql = (Path(__file__).parent / "migrations/001_initial.sql").read_text()
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK,))
            conn.execute(sql)

    def create_group(self, name, url):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 160:
            raise InvalidInput("Group name must contain 1–160 characters")
        url, numeric = _group_url(url)
        try:
            with self.connect() as conn:
                return conn.execute("INSERT INTO monitored_groups(name,url,facebook_group_id) VALUES(%s,%s,%s) RETURNING *",
                                    (name.strip(), url, numeric)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise Conflict("This group is already configured") from None

    def list_groups(self):
        with self.connect() as conn:
            return conn.execute("SELECT * FROM monitored_groups ORDER BY id").fetchall()

    def get_group(self, group_id):
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM monitored_groups WHERE id=%s", (_positive_id(group_id),)).fetchone()
            if not row:
                raise NotFound("Group not found")
            return row

    def update_group(self, group_id, enabled):
        if type(enabled) is not bool:
            raise InvalidInput("enabled must be a boolean")
        with self.connect() as conn:
            row = conn.execute("UPDATE monitored_groups SET enabled=%s WHERE id=%s RETURNING *", (enabled, _positive_id(group_id))).fetchone()
            if not row:
                raise NotFound("Group not found")
            if not enabled:
                conn.execute("UPDATE collection_runs SET status='failed',error_code='group_disabled',finished_at=now() WHERE group_id=%s AND status='queued'", (group_id,))
            return row

    def enqueue(self, group_id):
        with self.connect() as conn:
            state = conn.execute("SELECT * FROM app_state WHERE id=1 FOR UPDATE").fetchone()
            if state["paused_code"]:
                raise Conflict("Collection is paused; resolve the session/security issue and resume manually")
            group = conn.execute("SELECT * FROM monitored_groups WHERE id=%s FOR UPDATE", (_positive_id(group_id),)).fetchone()
            if not group:
                raise NotFound("Group not found")
            if not group["enabled"]:
                raise Conflict("This group is disabled")
            if conn.execute("SELECT id FROM collection_runs WHERE group_id=%s AND status IN ('queued','running')", (group_id,)).fetchone():
                raise Conflict("This group already has an active collection")
            return conn.execute("INSERT INTO collection_runs(group_id,status) VALUES(%s,'queued') RETURNING *", (group_id,)).fetchone()

    def claim_next(self):
        if self._worker_connection is not None:
            self.ensure_worker_lock()
        with self.connect() as conn:
            if conn.execute("SELECT paused_code FROM app_state WHERE id=1 FOR UPDATE").fetchone()["paused_code"]:
                return None
            if conn.execute("SELECT id FROM collection_runs WHERE status='running' LIMIT 1").fetchone():
                return None
            row = conn.execute("""SELECT r.id,r.group_id,g.url AS group_url,g.name AS group_name
                                  FROM collection_runs r JOIN monitored_groups g ON g.id=r.group_id
                                  WHERE r.status='queued' AND g.enabled ORDER BY r.id FOR UPDATE OF r SKIP LOCKED LIMIT 1""").fetchone()
            if row:
                conn.execute("UPDATE collection_runs SET status='running',started_at=now() WHERE id=%s", (row["id"],))
            return row

    def complete_run(self, run_id, envelope):
        if not isinstance(envelope, dict) or envelope.get("status") not in ("success", "partial", "failed"):
            raise InvalidInput("Invalid collection envelope")
        status = envelope["status"]
        fb_group = envelope.get("group_id")
        posts = envelope.get("posts", [])
        if not isinstance(posts, list) or ((posts or status != "failed") and (not isinstance(fb_group, str) or not fb_group.isdigit())):
            raise InvalidInput("Posts need a resolved numeric group")
        prepared_by_id = {}
        for value in posts:
            post = _post(value, fb_group)
            previous = prepared_by_id.get(post["post_id"])
            if previous is not None and previous != post:
                raise InvalidInput("Conflicting duplicate rows in one collection")
            prepared_by_id[post["post_id"]] = post
        prepared = list(prepared_by_id.values())
        codes = [safe_code(x.get("code")) for x in envelope.get("errors", []) if isinstance(x, dict)]
        pause = next((code for code in codes if code in STOP_CODES), None)
        window_complete = (envelope.get("window") or {}).get("complete") is True
        if status == "success" and (codes or not window_complete):
            status = "partial"
        code = pause or (codes[0] if codes else ("unknown_error" if status == "failed" else ("incomplete_window" if not window_complete else None)))
        source = envelope.get("source") if envelope.get("source") in ("live", "synthetic") else "unknown"
        try:
            with self.connect() as conn:
                conn.execute("SELECT id FROM app_state WHERE id=1 FOR UPDATE")
                run = conn.execute("SELECT * FROM collection_runs WHERE id=%s FOR UPDATE", (_positive_id(run_id),)).fetchone()
                if not run:
                    raise NotFound("Collection run not found")
                if run["status"] != "running":
                    raise Conflict("Only running collections can be completed")
                group = conn.execute("SELECT * FROM monitored_groups WHERE id=%s FOR UPDATE", (run["group_id"],)).fetchone()
                if fb_group and group["facebook_group_id"] and fb_group != group["facebook_group_id"]:
                    raise InvalidInput("Resolved group changed; collection was not ingested")
                if fb_group and isinstance(fb_group, str) and fb_group.isdigit():
                    conn.execute("UPDATE monitored_groups SET facebook_group_id=%s WHERE id=%s", (fb_group, group["id"]))
                for post in prepared:
                    existing = conn.execute("SELECT id,listing_id FROM posts WHERE platform='facebook' AND group_id=%s AND post_id=%s FOR UPDATE",
                                            (group["id"], post["post_id"])).fetchone()
                    if existing:
                        stored_id = existing["id"]
                        conn.execute("""UPDATE posts SET canonical_key=COALESCE(%s,canonical_key),url=COALESCE(%s,url),text=COALESCE(%s,text),
                                        author_id=COALESCE(%s,author_id),author_name=COALESCE(%s,author_name),
                                        posted_at=COALESCE(%s,posted_at),last_seen_at=now(),
                                        text_fingerprint=CASE WHEN %s THEN %s ELSE text_fingerprint END,source=%s WHERE id=%s""",
                                     (post["canonical_key"], post["url"], post["text"], post["author_id"], post["author_name"], post["posted_at"], post["text"] is not None, post["fingerprint"], source, stored_id))
                    else:
                        listing = conn.execute("INSERT INTO listings DEFAULT VALUES RETURNING id").fetchone()["id"]
                        stored_id = conn.execute("""INSERT INTO posts(listing_id,group_id,post_id,canonical_key,url,text,author_id,author_name,posted_at,text_fingerprint,source)
                                                    VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                                                 (listing, group["id"], post["post_id"], post["canonical_key"], post["url"], post["text"], post["author_id"], post["author_name"], post["posted_at"], post["fingerprint"], source)).fetchone()["id"]
                    if not existing or status == "success" or post["media"]:
                        conn.execute("DELETE FROM media WHERE post_id=%s", (stored_id,))
                        for media in post["media"]:
                            conn.execute("INSERT INTO media(post_id,url,position,width,height,type_evidence) VALUES(%s,%s,%s,%s,%s,%s)", (stored_id, *media))
                conn.execute("UPDATE monitored_groups SET last_error_code=%s,last_success_at=CASE WHEN %s='success' THEN now() ELSE last_success_at END WHERE id=%s", (code, status, group["id"]))
                if pause:
                    conn.execute("UPDATE app_state SET paused_code=%s WHERE id=1", (pause,))
                return conn.execute("""UPDATE collection_runs SET status=%s,error_code=%s,finished_at=now(),posts_count=%s,window_complete=%s,source=%s
                                       WHERE id=%s RETURNING *""", (status, code, len(prepared), window_complete, source, run_id)).fetchone()
        except psycopg.errors.UniqueViolation:
            raise InvalidInput("A canonical post identity is already attached to another group or post") from None

    def fail_run(self, run_id, code):
        code = safe_code(code)
        with self.connect() as conn:
            conn.execute("SELECT id FROM app_state WHERE id=1 FOR UPDATE")
            row = conn.execute("UPDATE collection_runs SET status='failed',error_code=%s,finished_at=now() WHERE id=%s AND status IN ('queued','running') RETURNING *", (code, _positive_id(run_id))).fetchone()
            if not row:
                raise Conflict("No active collection found")
            conn.execute("UPDATE monitored_groups SET last_error_code=%s WHERE id=%s", (code, row["group_id"]))
            if code in STOP_CODES:
                conn.execute("UPDATE app_state SET paused_code=%s WHERE id=1", (code,))
            return row

    def resume_collection(self):
        with self.connect() as conn:
            conn.execute("UPDATE app_state SET paused_code=NULL WHERE id=1")
        return self.state()

    def set_worker_heartbeat(self):
        if self._worker_connection is not None:
            self.ensure_worker_lock()
        with self.connect() as conn:
            conn.execute("UPDATE app_state SET worker_heartbeat_at=now() WHERE id=1")

    def clear_worker_heartbeat(self):
        self.ensure_worker_lock()
        with self.connect() as conn:
            conn.execute("UPDATE app_state SET worker_heartbeat_at=NULL WHERE id=1")

    def state(self):
        with self.connect() as conn:
            return conn.execute("SELECT paused_code,worker_heartbeat_at,COALESCE(worker_heartbeat_at > now()-interval '30 seconds',false) AS worker_active FROM app_state WHERE id=1").fetchone()

    def recover_runs(self):
        """Call only while holding worker_lock: a crashed process owns no session lock."""
        with self.connect() as conn:
            rows = conn.execute("UPDATE collection_runs SET status='interrupted',error_code='interrupted',finished_at=now() WHERE status='running' RETURNING group_id").fetchall()
            for row in rows:
                conn.execute("UPDATE monitored_groups SET last_error_code='interrupted' WHERE id=%s", (row["group_id"],))
        return len(rows)

    @contextmanager
    def worker_lock(self):
        with self.connect(autocommit=True) as conn:
            acquired = conn.execute("SELECT pg_try_advisory_lock(%s) AS acquired", (WORKER_LOCK,)).fetchone()["acquired"]
            previous = self._worker_connection
            if acquired:
                self._worker_connection = conn
            try:
                yield acquired
            finally:
                if acquired:
                    self._worker_connection = previous
                    try:
                        conn.execute("SELECT pg_advisory_unlock(%s)", (WORKER_LOCK,))
                    except psycopg.Error:
                        pass  # A lost connection already released its session lock.

    def ensure_worker_lock(self):
        conn = self._worker_connection
        if conn is None:
            raise Conflict("Worker advisory lock is not held")
        row = conn.execute("""SELECT EXISTS(SELECT 1 FROM pg_locks WHERE locktype='advisory'
                              AND pid=pg_backend_pid() AND objid=%s AND granted) AS held""", (WORKER_LOCK,)).fetchone()
        if not row["held"]:
            raise Conflict("Worker advisory lock was lost")

    def list_runs(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise InvalidInput("limit must be between 1 and 100")
        with self.connect() as conn:
            return conn.execute("SELECT r.*,g.name AS group_name FROM collection_runs r JOIN monitored_groups g ON g.id=r.group_id ORDER BY r.id DESC LIMIT %s", (limit,)).fetchall()

    def _decorate(self, conn, rows):
        for row in rows:
            row["images"] = [x["url"] for x in conn.execute("SELECT url FROM media WHERE post_id=%s ORDER BY position", (row["id"],)).fetchall()]
            row["sources"] = conn.execute("SELECT p.id,p.post_id,p.url,g.name AS group_name FROM posts p JOIN monitored_groups g ON g.id=p.group_id WHERE p.listing_id=%s ORDER BY p.id", (row["listing_id"],)).fetchall()
        return rows

    def feed(self, q="", group_id=None, start=None, end=None, has_images=None, page=1, page_size=20):
        if type(page) is not int or page < 1 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise InvalidInput("Invalid pagination")
        if not isinstance(q, str) or len(q) > 300:
            raise InvalidInput("Search must contain at most 300 characters")
        where, params = [], []
        if q:
            where.append("p.text ILIKE %s")
            params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
        if group_id is not None:
            where.append("p.group_id=%s")
            params.append(_positive_id(group_id))
        bounds = []
        for value, operator in ((start, ">="), (end, "<=")):
            if value is not None:
                parsed = timestamp(value.isoformat() if isinstance(value, datetime) else value)
                if not parsed:
                    raise InvalidInput("Date filters require timezone-aware timestamps")
                where.append(f"p.posted_at {operator} %s")
                params.append(parsed)
                bounds.append(parsed)
        if start and end and datetime.fromisoformat(bounds[0].replace("Z", "+00:00")) > datetime.fromisoformat(bounds[1].replace("Z", "+00:00")):
            raise InvalidInput("Start must be before end")
        if has_images is not None:
            if type(has_images) is not bool:
                raise InvalidInput("has_images must be a boolean")
            where.append(("" if has_images else "NOT ") + "EXISTS(SELECT 1 FROM media m WHERE m.post_id=p.id)")
        clause = " WHERE " + " AND ".join(where) if where else ""
        with self.connect() as conn:
            total = conn.execute("SELECT count(DISTINCT p.listing_id) AS total FROM posts p" + clause, params).fetchone()["total"]
            rows = conn.execute("""SELECT * FROM (SELECT DISTINCT ON (p.listing_id) p.*,g.name AS group_name
                                    FROM posts p JOIN monitored_groups g ON g.id=p.group_id""" + clause + """
                                    ORDER BY p.listing_id,p.posted_at DESC NULLS LAST,p.id DESC) selected
                                    ORDER BY posted_at DESC NULLS LAST,id DESC LIMIT %s OFFSET %s""",
                                (*params, page_size, (page-1)*page_size)).fetchall()
            return {"items": self._decorate(conn, rows), "total": total, "page": page, "page_size": page_size}

    def duplicate_candidates(self):
        with self.connect() as conn:
            keys = conn.execute("""SELECT text_fingerprint FROM posts WHERE text_fingerprint IS NOT NULL
                                   GROUP BY text_fingerprint HAVING count(DISTINCT group_id)>1 AND count(DISTINCT listing_id)>1
                                   ORDER BY max(last_seen_at) DESC LIMIT 50""").fetchall()
            results = []
            for key in keys:
                posts = conn.execute("SELECT p.*,g.name AS group_name FROM posts p JOIN monitored_groups g ON g.id=p.group_id WHERE text_fingerprint=%s ORDER BY p.id", (key["text_fingerprint"],)).fetchall()
                results.append({"fingerprint": key["text_fingerprint"], "posts": self._decorate(conn, posts)})
            return results

    def confirm_duplicate(self, post_ids):
        if not isinstance(post_ids, list) or not 2 <= len(post_ids) <= 50 or any(type(value) is not int for value in post_ids) or len(set(post_ids)) != len(post_ids):
            raise InvalidInput("Choose 2–50 different source post IDs")
        ids = sorted(_positive_id(value) for value in post_ids)
        with self.connect() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK+1,))
            rows = conn.execute("SELECT * FROM posts WHERE id=ANY(%s) ORDER BY id FOR UPDATE", (ids,)).fetchall()
            if len(rows) != len(ids):
                raise NotFound("One or more posts were not found")
            if not rows[0]["text_fingerprint"] or len({row["text_fingerprint"] for row in rows}) != 1 or len({row["group_id"] for row in rows}) < 2:
                raise InvalidInput("Only matching exact-text candidates from different groups can be confirmed")
            listing_ids = list({row["listing_id"] for row in rows})
            chosen = min(listing_ids)
            conn.execute("UPDATE posts SET listing_id=%s WHERE listing_id=ANY(%s)", (chosen, listing_ids))
            conn.execute("UPDATE listings SET confirmed_at=now() WHERE id=%s", (chosen,))
            conn.execute("DELETE FROM listings WHERE id=ANY(%s) AND id<>%s", (listing_ids, chosen))
            all_ids = conn.execute("SELECT id FROM posts WHERE listing_id=%s ORDER BY id", (chosen,)).fetchall()
            return {"listing_id": chosen, "post_ids": [row["id"] for row in all_ids]}

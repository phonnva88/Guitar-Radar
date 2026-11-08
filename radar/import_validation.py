"""Import immutable local validation runs into the collector audit only.

This path never invokes a collector, queues work, updates feed posts, or changes
the current session pause/group-health state. Saved files remain untouched.
"""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from data_filter import process_collection
from normalizer import FACEBOOK_HOSTS, timestamp
from radar.db import STOP_CODES, safe_code
from scripts.phase0 import group_handle, RunError
from validation.ground_truth import assess_fixture

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_RUNS = 100
IMPORT_LOCK = 734291091


class InvalidSavedRun(ValueError):
    pass


def _read_json(path: Path, limit: int):
    if path.is_symlink() or not path.is_file():
        raise InvalidSavedRun()
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    if len(payload) > limit:
        raise InvalidSavedRun()
    return json.loads(payload)


def _numeric(value):
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidSavedRun()
    value = str(value)
    if not re.fullmatch(r"[1-9][0-9]*", value):
        raise InvalidSavedRun()
    return value


def _instant(value):
    if not isinstance(value, str):
        raise InvalidSavedRun()
    result = timestamp(value)
    if not result:
        raise InvalidSavedRun()
    return result


def _group_reference(metadata, group_id):
    supplied = metadata.get("group_input")
    if supplied is None:
        return f"https://www.facebook.com/groups/{group_id}", f"Facebook group {group_id}"
    if not isinstance(supplied, str):
        raise InvalidSavedRun()
    try:
        handle = group_handle(supplied)
    except (RunError, ValueError, TypeError):
        raise InvalidSavedRun() from None
    if handle.isdigit() and handle != group_id:
        raise InvalidSavedRun()
    return f"https://www.facebook.com/groups/{handle}", handle


def _load_run(run_id, runs_dir):
    directory = runs_dir / run_id
    if directory.is_symlink() or directory.resolve().parent != runs_dir.resolve():
        raise InvalidSavedRun()
    metadata = _read_json(directory / "metadata.json", MAX_FILE_BYTES)
    raw = _read_json(directory / "facebook-go.raw.json", MAX_FILE_BYTES)
    if not isinstance(metadata, dict) or not isinstance(raw, dict):
        raise InvalidSavedRun()
    if metadata.get("run_id") != run_id or raw.get("run_id") != run_id or raw.get("collector") != "facebook-go":
        raise InvalidSavedRun()
    group = _numeric(metadata.get("group_id"))
    if _numeric(raw.get("group_id")) != group:
        raise InvalidSavedRun()
    source = metadata.get("source")
    if source not in {"live", "synthetic"} or raw.get("source") != source:
        raise InvalidSavedRun()
    lower, upper = _instant(metadata.get("from")), _instant(metadata.get("to"))
    if datetime.fromisoformat(lower.replace("Z", "+00:00")) > datetime.fromisoformat(upper.replace("Z", "+00:00")):
        raise InvalidSavedRun()
    window = raw.get("window")
    if not isinstance(window, dict) or _instant(window.get("from")) != lower or _instant(window.get("to")) != upper:
        raise InvalidSavedRun()
    envelope = process_collection(raw)
    if envelope.get("run_id") != run_id or envelope.get("group_id") != group or envelope.get("source") != source:
        raise InvalidSavedRun()
    group_url, group_name = _group_reference(metadata, group)
    started = timestamp(metadata.get("started_at")) or timestamp(raw.get("started_at")) or lower
    finished = timestamp(raw.get("collected_at")) or timestamp(metadata.get("finished_at")) or upper
    return {"raw": raw, "envelope": envelope, "group_id": group, "group_url": group_url,
            "group_name": group_name, "started_at": started, "finished_at": finished,
            "requested_from": lower, "requested_to": upper}


def _persist(db, run_id, saved):
    from radar.audit import capture_run

    envelope = saved["envelope"]
    codes = [safe_code(item.get("code")) for item in envelope.get("errors", []) if isinstance(item, dict)]
    complete = (envelope.get("window") or {}).get("complete") is True
    status = envelope["status"]
    if status == "success" and (codes or not complete):
        status = "partial"
    code = next((code for code in codes if code in STOP_CODES), None)
    code = code or (codes[0] if codes else ("unknown_error" if status == "failed" else ("incomplete_window" if not complete else None)))
    key = "validation:facebook-go:" + run_id
    with db.connect() as conn:
        # Serialize imports without taking the worker lock or touching its lease.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (IMPORT_LOCK,))
        if conn.execute("SELECT id FROM collection_runs WHERE import_key=%s", (key,)).fetchone():
            return False
        group = conn.execute("SELECT * FROM monitored_groups WHERE facebook_group_id=%s ORDER BY id LIMIT 1",
                             (saved["group_id"],)).fetchone()
        if not group:
            group = conn.execute("SELECT * FROM monitored_groups WHERE lower(url)=lower(%s) ORDER BY id LIMIT 1",
                                 (saved["group_url"],)).fetchone()
        if group and group.get("facebook_group_id") not in (None, saved["group_id"]):
            raise InvalidSavedRun()
        if not group:
            # Historical import must not silently add a live scheduled source.
            group = conn.execute("""INSERT INTO monitored_groups(name,url,facebook_group_id,enabled)
                                    VALUES(%s,%s,%s,false) ON CONFLICT(url) DO NOTHING RETURNING *""",
                                 (saved["group_name"], saved["group_url"], saved["group_id"])).fetchone()
            if not group:
                group = conn.execute("SELECT * FROM monitored_groups WHERE url=%s", (saved["group_url"],)).fetchone()
            if not group or group.get("facebook_group_id") not in (None, saved["group_id"]):
                raise InvalidSavedRun()
        run = conn.execute("""INSERT INTO collection_runs
                              (group_id,status,queued_at,started_at,finished_at,error_code,posts_count,window_complete,
                               source,requested_from,requested_to,import_key)
                              VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
                           (group["id"], status, saved["started_at"], saved["started_at"], saved["finished_at"], code,
                            len(envelope["posts"]), complete, envelope["source"], saved["requested_from"],
                            saved["requested_to"], key)).fetchone()
        capture_run(conn, run, envelope, saved["raw"])
        return True


def _fixture(value):
    if not isinstance(value, dict) or type(value.get("expected_has_images")) is not bool:
        raise InvalidSavedRun()
    url = value.get("url")
    if not isinstance(url, str):
        raise InvalidSavedRun()
    parsed = urlsplit(url)
    match = re.fullmatch(r"/groups/([A-Za-z0-9._-]+)/(?:posts|permalink)/([A-Za-z0-9_-]{1,128})/?", parsed.path)
    if (parsed.scheme != "https" or parsed.hostname not in FACEBOOK_HOSTS or parsed.username or parsed.password or
            parsed.port not in (None, 443) or not match):
        raise InvalidSavedRun()
    post_id = match[2]
    if value.get("post_id") not in (None, post_id):
        raise InvalidSavedRun()
    published = _instant(value["verified_posted_at"]) if value.get("verified_posted_at") is not None else None
    return {"url": f"https://www.facebook.com/groups/{match[1]}/posts/{post_id}/", "post_id": post_id,
            "expected_has_images": value["expected_has_images"], "verified_posted_at": published}


def _import_fixtures(db, manifest, saved_groups, result):
    from radar.audit import add_known_post

    values = manifest.get("evaluated_known_posts", [])
    if not isinstance(values, list) or len(values) > 1000:
        result["errors"].append({"run_id": None, "code": "invalid_fixture"})
        return
    groups = {saved["group_id"]: saved for saved in saved_groups}
    for value in values:
        try:
            item = _fixture(value)
        except (ValueError, TypeError, KeyError):
            result["errors"].append({"run_id": None, "code": "invalid_fixture"})
            continue
        matching = [saved for saved in groups.values()
                    if assess_fixture(item, saved["group_id"], None, observed=False,
                                      group_input=saved["group_url"])["group_status"] == "verified_same_group"]
        if len(matching) != 1:
            result["errors"].append({"run_id": None, "code": "fixture_scope"})
            continue
        saved = matching[0]
        try:
            with db.connect() as conn:
                group = conn.execute("SELECT * FROM monitored_groups WHERE facebook_group_id=%s ORDER BY id LIMIT 1",
                                     (saved["group_id"],)).fetchone()
                if not group:
                    group = conn.execute("SELECT * FROM monitored_groups WHERE lower(url)=lower(%s) ORDER BY id LIMIT 1",
                                         (saved["group_url"],)).fetchone()
                if not group:
                    raise InvalidSavedRun()
                existing = conn.execute("SELECT * FROM collector_known_posts WHERE group_id=%s AND post_id=%s",
                                        (group["id"], item["post_id"])).fetchone()
            if existing:
                expected = existing.get("expected_has_images")
                published = existing.get("verified_posted_at")
                published = timestamp(published.isoformat()) if isinstance(published, datetime) else timestamp(published)
                if ((expected is not None and expected != item["expected_has_images"]) or
                        (published and item["verified_posted_at"] and published != item["verified_posted_at"])):
                    result["errors"].append({"run_id": None, "code": "fixture_conflict"})
                    continue
            # An existing group may use a different alias but has the same
            # independently recorded numeric identity. Submit that numeric URL.
            url = item["url"]
            if group.get("facebook_group_id") == saved["group_id"]:
                url = f"https://www.facebook.com/groups/{saved['group_id']}/posts/{item['post_id']}/"
            elif group.get("facebook_group_id") is None and group["url"].casefold() == saved["group_url"].casefold():
                # The imported run independently binds this configured alias to
                # the numeric source identity without changing live group health.
                handle = group_handle(group["url"])
                url = f"https://www.facebook.com/groups/{handle}/posts/{item['post_id']}/"
            add_known_post(db, group["id"], url, item["expected_has_images"], item["verified_posted_at"],
                           preserve_existing=True)
        except InvalidSavedRun:
            result["errors"].append({"run_id": None, "code": "fixture_scope"})
        except Exception:
            result["errors"].append({"run_id": None, "code": "fixture_storage_error"})
        else:
            if existing is None:
                result["known_posts_imported"] += 1


def import_validation_runs(db, manifest_path, runs_dir):
    """Return safe per-run import results; no network or feed mutation occurs."""
    result = {"imported": 0, "skipped": 0, "known_posts_imported": 0, "errors": []}
    try:
        manifest = _read_json(Path(manifest_path), MAX_MANIFEST_BYTES)
        ids = manifest.get("run_ids") if isinstance(manifest, dict) else None
        if (not isinstance(ids, list) or not 1 <= len(ids) <= MAX_RUNS or
                any(not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value) for value in ids) or
                len(ids) != len(set(ids))):
            raise InvalidSavedRun()
    except (OSError, ValueError, TypeError):
        result["errors"].append({"run_id": None, "code": "invalid_manifest"})
        return result
    runs_dir = Path(runs_dir)
    saved_groups = []
    for run_id in ids:
        try:
            saved = _load_run(run_id, runs_dir)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            result["errors"].append({"run_id": run_id, "code": "invalid_run"})
            continue
        try:
            inserted = _persist(db, run_id, saved)
        except InvalidSavedRun:
            result["errors"].append({"run_id": run_id, "code": "invalid_run"})
        except Exception:
            # Database/driver errors may contain local paths or credentials.
            result["errors"].append({"run_id": run_id, "code": "storage_error"})
        else:
            result["imported" if inserted else "skipped"] += 1
            saved_groups.append(saved)
    _import_fixtures(db, manifest, saved_groups, result)
    return result

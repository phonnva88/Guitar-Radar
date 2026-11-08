"""Audit evidence uses an isolated PostgreSQL schema; never calls Facebook."""
import copy
import json
import os
from pathlib import Path
import unittest
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from radar.db import Database, InvalidInput
from radar.audit import add_known_post, audit_summary, capture_run, run_details


@unittest.skipUnless(os.environ.get("RADAR_TEST_DATABASE_URL"), "Set RADAR_TEST_DATABASE_URL for PostgreSQL audit tests")
class AuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base = os.environ["RADAR_TEST_DATABASE_URL"]
        cls.schema = "radar_audit_" + uuid.uuid4().hex
        with psycopg.connect(cls.base, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.db = Database(make_conninfo(cls.base, options=f"-csearch_path={cls.schema}"))
        cls.db.migrate()
        with cls.db.connect() as conn:
            conn.execute((Path(__file__).resolve().parents[1] / "radar/migrations/002_collector_audit.sql").read_text())

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.base, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("TRUNCATE monitored_groups CASCADE")
            conn.execute("UPDATE app_state SET paused_code=NULL,worker_heartbeat_at=NULL")
        self.group = self.db.create_group("Fixture group", "11")

    def envelope(self, ids=("101",), *, end="2026-11-08T05:00:00Z", status="success"):
        return {"schema_version": 1, "collector": "facebook-go", "group_id": "11", "source": "synthetic",
                "run_id": "fixture-run", "status": status, "errors": [],
                "window": {"from": "2026-11-08T04:00:00Z", "to": end, "complete": status == "success", "stop_reason": "feed_exhausted"},
                "posts": [{"post_id": value, "group_id": "11", "text": "A complete fixture caption", "author_name": "Fixture author",
                           "url": f"https://www.facebook.com/groups/11/posts/{value}/", "posted_at": "2026-11-08T04:30:00Z",
                           "images": ["https://example.invalid/a.jpg"]} for value in ids]}

    def store(self, envelope=None, raw=None, *, snapshot=True):
        envelope = envelope or self.envelope()
        with self.db.connect() as conn:
            run = conn.execute("""INSERT INTO collection_runs(group_id,status,source,posts_count,window_complete)
                                  VALUES(%s,%s,%s,%s,%s) RETURNING *""",
                               (self.group["id"], envelope["status"], envelope["source"], len(envelope["posts"]),
                                envelope["window"]["complete"])).fetchone()
            if snapshot:
                capture_run(conn, run, envelope, raw)
        return run["id"]

    def test_snapshots_are_immutable_and_secrets_sanitized(self):
        envelope = self.envelope()
        raw = {**envelope, "headers": {"Cookie": "xs=SECRET"}, "posts": [{"id": "101", "message": "cookie=SECRET; Bearer BEARER_SECRET",
                "headers": {"Authorization": "BEARER_SECRET"}, "bootstrap_html": "SECRET_BOOTSTRAP"}]}
        run_id = self.store(envelope, raw)
        changed = copy.deepcopy(envelope)
        changed["posts"][0]["text"] = "Changed later"
        with self.db.connect() as conn:
            capture_run(conn, {"id": run_id}, changed)
        details = run_details(self.db, run_id)
        self.assertEqual(details["posts"][0]["text"], "A complete fixture caption")
        encoded = json.dumps(details)
        self.assertNotIn("SECRET", encoded)
        self.assertNotIn("bootstrap_html", encoded)
        self.assertNotIn("headers", encoded)
        self.assertTrue(details["run"]["raw_snapshot_available"])

    def test_raw_and_normalized_identity_mismatch_is_rejected(self):
        envelope = self.envelope()
        with self.assertRaises(InvalidInput):
            self.store(envelope, {**envelope, "run_id": "different"})
        self.assertEqual(audit_summary(self.db)["runs"], [])  # Transaction rolled back.

    def test_worker_captures_raw_before_offline_window_processing(self):
        from radar.worker import Worker
        raw = {"schema_version": 1, "collector": "facebook-go", "group_id": "11", "source": "synthetic",
               "run_id": "pipeline-fixture", "status": "success", "errors": [], "duration_ms": 5,
               "pages_fetched": 1, "collected_at": "2026-11-08T05:00:00Z",
               "window": {"from": "2026-11-08T04:00:00Z", "to": "2026-11-08T05:00:00Z", "complete": True, "stop_reason": "feed_exhausted"},
               "posts": [{"id": "101", "groupId": "11", "message": "In-window source caption", "createdAt": "2026-11-08T04:30:00Z", "attachments": []},
                         {"id": "102", "groupId": "11", "message": "Older source caption", "createdAt": "2026-11-08T03:59:00Z", "attachments": []}]}
        run = self.db.enqueue(self.group["id"], start=raw["window"]["from"], end=raw["window"]["to"])
        self.assertTrue(Worker(self.db, collector=lambda job: raw).process_one())
        details = run_details(self.db, run["id"])
        self.assertEqual(details["run"]["raw_posts"], 2)
        self.assertEqual(details["run"]["normalized_posts"], 1)
        self.assertEqual(details["posts"][0]["raw"]["message"], "In-window source caption")
        self.assertEqual(details["posts"][0]["text"], "In-window source caption")
        self.assertEqual(details["diagnostics"]["raw_only_ids"], ["102"])
        self.assertEqual(raw["posts"][1]["id"], "102")

    def test_rejected_post_retains_safe_evidence_and_security_pause(self):
        from radar.worker import Worker
        raw = {"schema_version": 1, "collector": "facebook-go", "group_id": "11", "source": "synthetic",
               "run_id": "rejected-fixture", "status": "partial", "duration_ms": 5, "pages_fetched": 1,
               "collected_at": "2026-11-08T05:00:00Z", "errors": [{"code": "checkpoint_required"}],
               "window": {"from": "2026-11-08T04:00:00Z", "to": "2026-11-08T05:00:00Z", "complete": False, "stop_reason": "error"},
               "posts": [{"id": "invalid id", "groupId": "11", "message": "Source evidence", "createdAt": "2026-11-08T04:30:00Z", "attachments": []}]}
        run = self.db.enqueue(self.group["id"])
        self.assertTrue(Worker(self.db, collector=lambda job: raw).process_one())
        details = run_details(self.db, run["id"])
        self.assertEqual(details["run"]["status"], "failed")
        self.assertEqual(details["run"]["error_code"], "checkpoint_required")
        self.assertEqual(details["raw_unmatched"][0]["message"], "Source evidence")
        self.assertEqual(self.db.state()["paused_code"], "checkpoint_required")

    def test_stability_requires_exact_window_and_complete_sample(self):
        first = self.store()
        different = self.store(self.envelope(end="2026-11-08T06:00:00Z"))
        partial = self.store(self.envelope(status="partial"))
        same = self.store()
        changed = self.store(self.envelope(ids=("102",)))
        items = {run["id"]: run for run in audit_summary(self.db)["runs"]}
        self.assertEqual(items[first]["stability"]["status"], "insufficient_evidence")
        self.assertEqual(items[different]["stability"]["status"], "insufficient_evidence")
        self.assertEqual(items[partial]["stability"]["status"], "insufficient_evidence")
        self.assertEqual(items[same]["stability"]["status"], "stable_in_observed_window")
        self.assertEqual(items[same]["stability"]["previous_run_id"], first)
        self.assertEqual(items[changed]["stability"]["missing_ids"], ["101"])
        self.assertEqual(items[changed]["stability"]["added_ids"], ["102"])
        self.assertEqual(items[changed]["stability"]["status"], "changed_in_observed_window")

    def test_page_limit_cannot_claim_complete_window_or_stability(self):
        envelope = self.envelope()
        envelope["window"].update(complete=True, stop_reason="page_limit")
        self.store(envelope)
        second = self.store(envelope)
        details = run_details(self.db, second)
        self.assertFalse(details["run"]["window_complete"])
        self.assertEqual(details["run"]["stability"]["status"], "insufficient_evidence")

    def test_known_posts_distinguish_missing_outside_unknown_and_media(self):
        run_id = self.store()
        at = "2026-11-08T04:30:00Z"
        for post_id, instant, images in (("101", at, True), ("102", at, False),
                                        ("103", "2026-11-08T05:01:00Z", True), ("104", None, True)):
            add_known_post(self.db, self.group["id"], f"https://www.facebook.com/groups/11/posts/{post_id}/", images, instant)
        checks = {row["post_id"]: row for row in run_details(self.db, run_id)["known_post_checks"]}
        self.assertEqual(checks["101"]["status"], "detected")
        self.assertEqual(checks["102"]["status"], "missing_in_window")
        self.assertEqual(checks["103"]["status"], "outside_window")
        self.assertEqual(checks["104"]["status"], "timestamp_unknown")
        self.assertIsNone(checks["101"]["text_matches"])
        self.assertIsNone(checks["101"]["image_count_matches"])

    def test_manual_full_text_and_image_count_comparison(self):
        run_id = self.store()
        url, at = "https://www.facebook.com/groups/11/posts/101/", "2026-11-08T04:30:00Z"
        add_known_post(self.db, self.group["id"], url, True, at, expected_text="A complete fixture caption", expected_image_count=2)
        check = run_details(self.db, run_id)["known_post_checks"][0]
        self.assertEqual(check["status"], "image_count_mismatch")
        self.assertTrue(check["text_matches"])
        self.assertFalse(check["image_count_matches"])
        add_known_post(self.db, self.group["id"], url, True, at, expected_text="A complete fixture caption with missing sentence", expected_image_count=1)
        check = run_details(self.db, run_id)["known_post_checks"][0]
        self.assertEqual(check["status"], "text_mismatch")
        self.assertTrue(check["image_count_matches"])
        add_known_post(self.db, self.group["id"], url, None, at, expected_text="A  complete\nfixture caption", expected_image_count=1)
        check = run_details(self.db, run_id)["known_post_checks"][0]
        self.assertEqual(check["status"], "detected")

    def test_observed_post_time_can_scope_fixture_but_absent_time_is_unknown(self):
        run_id = self.store()
        add_known_post(self.db, self.group["id"], "https://www.facebook.com/groups/11/posts/101/", True)
        add_known_post(self.db, self.group["id"], "https://www.facebook.com/groups/11/posts/102/", True)
        checks = {item["post_id"]: item for item in run_details(self.db, run_id)["known_post_checks"]}
        self.assertEqual(checks["101"]["status"], "detected")
        self.assertEqual(checks["101"]["time_basis"], "observed_post")
        self.assertEqual(checks["102"]["status"], "timestamp_unknown")
        self.assertEqual(checks["102"]["time_basis"], "unknown")

    def test_wrong_group_and_share_link_are_rejected(self):
        for url in ("https://www.facebook.com/groups/22/posts/101/", "https://www.facebook.com/share/p/abc/"):
            with self.assertRaises(InvalidInput):
                add_known_post(self.db, self.group["id"], url)
        with self.assertRaises(InvalidInput):
            add_known_post(self.db, self.group["id"], "https://www.facebook.com/groups/11/posts/101/", verified_posted_at="2026-11-08")

    def test_old_runs_have_no_invented_snapshot_or_field_metrics(self):
        run_id = self.store(snapshot=False)
        details = run_details(self.db, run_id)
        self.assertFalse(details["run"]["snapshot_available"])
        self.assertIsNone(details["run"]["normalized_posts"])
        self.assertIsNone(details["run"]["field_presence"]["text"])
        self.assertEqual(details["posts"], [])
        self.assertEqual(details["run"]["stability"]["status"], "insufficient_evidence")

    def test_queued_fixed_window_is_visible_but_has_no_measured_counts(self):
        run = self.db.enqueue(self.group["id"], start="2026-11-08T04:00:00Z", end="2026-11-08T05:00:00Z")
        info = run_details(self.db, run["id"])["run"]
        self.assertEqual(info["window"]["from"], "2026-11-08T04:00:00Z")
        self.assertIsNone(info["normalized_posts"])
        self.assertIsNone(info["duplicates"])
        self.assertIsNone(info["missing_ids"])
        self.assertFalse(info["window_complete"])

    def test_duplicate_unidentified_rows_and_raw_only_ids_are_visible(self):
        envelope = self.envelope(ids=("101", "101"))
        raw = {**envelope, "posts": [{"id": "101"}, {"id": "102"}]}
        run_id = self.store(envelope, raw)
        details = run_details(self.db, run_id)
        self.assertEqual(details["diagnostics"]["duplicate_ids"], ["101"])
        self.assertEqual(details["diagnostics"]["raw_only_ids"], ["102"])
        self.assertEqual(details["run"]["stability"]["status"], "insufficient_evidence")


if __name__ == "__main__":
    unittest.main()

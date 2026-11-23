"""Real PostgreSQL integration tests; each invocation owns an isolated schema.

Set RADAR_TEST_DATABASE_URL to a disposable database. No Facebook requests occur.
"""
import os
import unittest
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor

try:
    import psycopg
    from psycopg.conninfo import make_conninfo
    from psycopg import sql
    from radar.audit import run_details
    from radar.db import Database, Conflict, InvalidInput
except ModuleNotFoundError:
    psycopg = None


@unittest.skipUnless(psycopg and os.environ.get("RADAR_TEST_DATABASE_URL"), "Set RADAR_TEST_DATABASE_URL and install Phase 1 dependencies for PostgreSQL tests")
class DatabaseTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dsn = os.environ["RADAR_TEST_DATABASE_URL"]
        cls.schema = "radar_test_" + uuid.uuid4().hex
        with psycopg.connect(cls.base_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.db = Database(make_conninfo(cls.base_dsn, options=f"-csearch_path={cls.schema}"))
        cls.db.migrate()
        cls.db.migrate()  # Startup migrations are safe for both API and worker.

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.base_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("TRUNCATE media,posts,listings,collection_runs,monitored_groups RESTART IDENTITY CASCADE")
            conn.execute("UPDATE app_state SET paused_code=NULL,worker_heartbeat_at=NULL")

    def group(self, number="11"):
        return self.db.create_group("Group " + number, number)

    def post(self, group="11", number="101", text=None, images=None, when="2026-11-08T04:00:00Z"):
        return {"post_id": number, "group_id": group, "url": f"https://www.facebook.com/groups/{group}/posts/{number}/",
                "text": text or "Fender guitar for sale with complete original case and accessories.",
                "images": images or [], "posted_at": when, "author_name": "Fixture author"}

    def ingest(self, group, posts, status="success", errors=None):
        self.db.enqueue(group["id"])
        run = self.db.claim_next()
        self.assertIsNotNone(run)
        return self.db.complete_run(run["id"], {"group_id": group["facebook_group_id"], "status": status,
                    "source": "synthetic", "errors": errors or [], "posts": posts,
                    "window": {"complete": status == "success"}})

    def test_repeat_ingestion_updates_without_duplicate_and_failed_run_keeps_posts(self):
        group = self.group()
        self.ingest(group, [self.post(images=["https://example.invalid/a.jpg"])])
        original = self.db.feed()["items"][0]
        self.assertEqual(original["videos"], [])
        self.ingest(group, [self.post(text="Updated caption for this specific guitar with original case and stand.", images=["https://example.invalid/b.jpg"])])
        current = self.db.feed()["items"][0]
        self.assertEqual(self.db.feed()["total"], 1)
        self.assertEqual(current["id"], original["id"])
        self.assertEqual(current["first_seen_at"], original["first_seen_at"])
        self.assertEqual(current["images"], ["https://example.invalid/b.jpg"])
        self.assertGreaterEqual(current["last_seen_at"], original["last_seen_at"])
        self.ingest(group, [], status="failed", errors=[{"code": "network_error", "message": "must not persist this secret"}])
        self.assertEqual(self.db.feed()["total"], 1)
        self.assertEqual(self.db.list_runs()[0]["error_code"], "network_error")
        self.assertEqual(self.db.get_group(group["id"])["last_error_code"], "network_error")

    def test_continue_collection_preserves_scope_and_combines_observed_posts(self):
        group = self.group()
        bounds = {"from": "2026-11-07T00:00:00Z", "to": "2026-11-09T00:00:00Z"}
        checkpoint = {**bounds, "cursor": "synthetic-private-cursor"}

        def batch(number, complete):
            post = self.post(number=number)
            result = {
                "group_id": "11", "collector": "facebook-go", "source": "live", "status": "success",
                "window": {**bounds, "complete": complete, "scanned_posts": 1,
                           "stop_reason": "feed_exhausted" if complete else "timeout"},
                "pages_fetched": 1, "posts": [post], "errors": [],
            }
            return result, {**result, "posts": [{"id": number}]}

        first = self.db.enqueue(group["id"], start=bounds["from"], end=bounds["to"])
        self.db.claim_next()
        normalized, raw = batch("101", False)
        partial = self.db.complete_run(first["id"], normalized, raw=raw, checkpoint=checkpoint)
        self.assertEqual(partial["status"], "partial")
        details = run_details(self.db, first["id"])
        self.assertTrue(details["run"]["can_continue"])
        self.assertNotIn(checkpoint["cursor"], repr(details))

        continued = self.db.enqueue(continue_from=first["id"])
        self.assertEqual(continued["continued_from"], first["id"])
        self.assertNotIn("checkpoint", continued)
        self.assertEqual(self.db.claim_next()["checkpoint"], checkpoint)
        normalized, raw = batch("102", True)
        finished = self.db.complete_run(continued["id"], normalized, raw=raw)
        self.assertEqual((finished["status"], finished["posts_count"]), ("success", 2))
        continued_details = run_details(self.db, continued["id"])
        self.assertTrue(continued_details["run"]["window_complete"])
        self.assertEqual(continued_details["window"]["scanned_posts"], 2)
        self.assertEqual([item["post_id"] for item in continued_details["posts"]], ["101", "102"])
        self.assertEqual(self.db.feed()["total"], 2)
        self.assertFalse(run_details(self.db, first["id"])["run"]["can_continue"])

    def test_partial_sparse_rows_preserve_known_fields_and_media(self):
        group = self.group()
        self.ingest(group, [self.post(images=["https://example.invalid/a.jpg"])])
        original = self.db.feed()["items"][0]
        sparse = {"post_id": "101", "group_id": "11", "url": None, "text": None,
                  "author_name": None, "posted_at": None, "images": []}
        self.ingest(group, [sparse], status="partial", errors=[{"code": "network_error"}])
        current = self.db.feed()["items"][0]
        for field in ("text", "url", "author_name", "posted_at", "images", "first_seen_at", "text_fingerprint"):
            self.assertEqual(current[field], original[field], field)
        self.ingest(group, [self.post(images=[])])
        self.assertEqual(self.db.feed()["items"][0]["images"], [])  # Complete success can clear removed images.

    def test_video_metadata_is_separate_from_images_and_expired_source_is_hidden(self):
        group = self.group()
        video = {"video_id": "v101", "kind": "direct", "thumbnail_url": "https://scontent.xx.fbcdn.net/thumb.jpg",
                 "duration_seconds": 12.5, "width": 640, "height": 360,
                 "playback_url": "https://video.xx.fbcdn.net/movie.mp4?oe=F00&oh=fixture%2f%2B",
                 "playback_url_expires_at": "2099-01-01T00:00:00Z", "playback_url_ephemeral": True}
        post = self.post(images=[])
        post["videos"] = [video]
        self.ingest(group, [post])
        current = self.db.feed()["items"][0]
        self.assertEqual(current["images"], [])
        self.assertEqual(current["videos"], [video])
        self.assertEqual(self.db.feed(has_images=False)["total"], 1)
        self.assertEqual(self.db.feed(has_images=True)["total"], 0)
        sparse = {"post_id": "101", "group_id": "11", "videos": [], "images": []}
        self.ingest(group, [sparse], status="partial", errors=[{"code": "network_error"}])
        self.assertEqual(self.db.feed()["items"][0]["videos"], [video])
        expired = {**video, "playback_url_expires_at": "2020-01-01T00:00:00Z"}
        post["videos"] = [expired]
        self.ingest(group, [post])
        self.assertIsNone(self.db.feed()["items"][0]["videos"][0]["playback_url"])
        self.assertEqual(self.db.feed()["items"][0]["videos"][0]["kind"], "direct")
        self.ingest(group, [self.post(images=[])])
        current = self.db.feed()["items"][0]
        self.assertEqual(current["videos"][0]["video_id"], "v101")
        self.assertIsNone(current["videos"][0]["playback_url"])

    def test_video_migration_keeps_existing_posts_photos_and_snapshots(self):
        with self.db.connect() as conn:
            conn.execute("DROP TABLE videos")
            conn.execute("DELETE FROM radar_schema_version WHERE version=3")
        try:
            group = self.group()
            photo = "https://example.invalid/legacy.jpg"
            run = self.ingest(group, [self.post(images=[photo])])
            with self.db.connect() as conn:
                before = conn.execute("SELECT normalized FROM collector_run_snapshots WHERE run_id=%s", (run["id"],)).fetchone()
        finally:
            self.db.migrate()

        self.db.migrate()
        post = self.db.feed()["items"][0]
        self.assertEqual((post["post_id"], post["images"], post["videos"]), ("101", [photo], []))
        with self.db.connect() as conn:
            after = conn.execute("SELECT normalized FROM collector_run_snapshots WHERE run_id=%s", (run["id"],)).fetchone()
        self.assertEqual(before, after)
        self.assertEqual(run_details(self.db, run["id"])["posts"][0]["videos"], [])

    def test_wrong_permalink_identity_rolls_back_the_entire_batch(self):
        group = self.group()
        self.db.enqueue(group["id"])
        run = self.db.claim_next()
        bad = self.post(number="102")
        bad["url"] = "https://www.facebook.com/groups/11/posts/103/"
        with self.assertRaises(InvalidInput):
            self.db.complete_run(run["id"], {"group_id": "11", "status": "success", "posts": [self.post(), bad], "window": {"complete": True}})
        self.assertEqual(self.db.feed()["total"], 0)
        self.assertEqual(self.db.list_runs()[0]["status"], "running")

    def test_security_failure_pauses_all_groups_and_preserves_partial_posts(self):
        first, second = self.group(), self.group("22")
        first_run = self.db.enqueue(first["id"])
        second_run = self.db.enqueue(second["id"])
        claimed = self.db.claim_next()
        self.assertEqual(claimed["id"], first_run["id"])
        self.db.complete_run(claimed["id"], {"group_id": "11", "status": "partial", "source": "synthetic", "posts": [self.post()], "errors": [{"code": "session_expired"}], "window": {"complete": False}})
        self.assertEqual(self.db.state()["paused_code"], "session_expired")
        self.assertEqual(self.db.feed()["total"], 1)
        self.assertIsNone(self.db.claim_next())
        with self.assertRaises(Conflict):
            self.db.enqueue(first["id"])
        self.db.resume_collection()
        self.assertEqual(self.db.claim_next()["id"], second_run["id"])

    def test_feed_filters_and_manual_confirmation_keep_all_source_links(self):
        first, second = self.group(), self.group("22")
        self.ingest(first, [self.post(images=["https://example.invalid/a.jpg"]),
                            self.post(number="102", text="Completely separate listing with literal 20% discount on acoustic guitar.", when="2026-11-07T04:00:00Z")])
        self.ingest(second, [self.post(group="22", number="201")])
        self.assertEqual(self.db.feed()["total"], 3)  # Candidates never merge automatically.
        self.assertEqual(self.db.feed(q="20%", has_images=False)["total"], 1)
        self.assertEqual(self.db.feed(group_id=first["id"])["total"], 2)
        self.assertEqual(self.db.feed(start="2026-11-08T04:00:00Z", end="2026-11-08T04:00:00Z")["total"], 2)
        self.assertEqual(self.db.feed(start=datetime(2026, 11, 8, 4, tzinfo=timezone.utc))["total"], 2)
        with self.assertRaises(InvalidInput):
            self.db.feed(start="2026-11-08T04:00:00.1Z", end="2026-11-08T04:00:00Z")
        self.assertEqual(len(self.db.feed(page_size=1)["items"]), 1)
        candidates = self.db.duplicate_candidates()
        self.assertEqual(len(candidates), 1)
        sources = candidates[0]["posts"]
        result = self.db.confirm_duplicate([post["id"] for post in sources])
        self.assertEqual(len(result["post_ids"]), 2)
        feed = self.db.feed()
        self.assertEqual(feed["total"], 2)
        merged = next(post for post in feed["items"] if len(post["sources"]) == 2)
        self.assertEqual({source["group_name"] for source in merged["sources"]}, {first["name"], second["name"]})
        self.assertEqual(self.db.duplicate_candidates(), [])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM posts").fetchone()["n"], 3)

    def test_canonical_identity_cannot_silently_move_between_groups(self):
        first, second = self.group(), self.group("22")
        self.ingest(first, [self.post()])
        self.db.enqueue(second["id"])
        run = self.db.claim_next()
        with self.assertRaises(InvalidInput):
            self.db.complete_run(run["id"], {"group_id": "22", "status": "success", "posts": [self.post(group="22")], "window": {"complete": True}})
        self.assertEqual(self.db.feed()["total"], 1)
        self.assertEqual(self.db.feed()["items"][0]["group_id"], first["id"])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT count(*) AS n FROM listings").fetchone()["n"], 1)

    def test_generic_text_does_not_create_candidate_and_arbitrary_merge_is_rejected(self):
        first, second = self.group(), self.group("22")
        self.ingest(first, [self.post(text="Up", number="101")])
        self.ingest(second, [self.post(group="22", text="Up", number="201")])
        self.assertEqual(self.db.duplicate_candidates(), [])
        with self.assertRaises(InvalidInput):
            self.db.confirm_duplicate([post["id"] for post in self.db.feed()["items"]])

    def test_active_run_unique_lock_recovery_and_disabled_group(self):
        group = self.group()
        def enqueue():
            try:
                return self.db.enqueue(group["id"])["status"]
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            self.assertCountEqual(list(executor.map(lambda _: enqueue(), range(2))), ["queued", "conflict"])
        with self.db.worker_lock() as acquired:
            self.assertTrue(acquired)
            self.db.ensure_worker_lock()
            self.db.set_worker_heartbeat()
            self.db.clear_worker_heartbeat()
            self.assertFalse(self.db.state()["worker_active"])
            with self.db.worker_lock() as another:
                self.assertFalse(another)
            self.db.claim_next()
            self.assertEqual(self.db.recover_runs(), 1)
        self.db.set_worker_heartbeat()
        self.assertTrue(self.db.state()["worker_active"])
        self.db.enqueue(group["id"])
        self.db.update_group(group["id"], False)
        self.assertIsNone(self.db.claim_next())
        with self.assertRaises(Conflict):
            self.db.enqueue(group["id"])


if __name__ == "__main__":
    unittest.main()

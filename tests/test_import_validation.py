"""Offline validation history import; real database tests own an isolated schema."""
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import uuid

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

from radar.db import Database
from radar.import_validation import import_validation_runs


class ManifestValidationTests(unittest.TestCase):
    def test_manifest_must_have_unique_safe_bounded_run_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "stability.json"
            for value in ({}, {"run_ids": []}, {"run_ids": ["../escape"]},
                          {"run_ids": ["run-1", "run-1"]}, {"run_ids": ["a"] * 101}):
                with self.subTest(value=value):
                    path.write_text(json.dumps(value))
                    result = import_validation_runs(None, path, Path(tmp) / "runs")
                    self.assertEqual(result, {"imported": 0, "skipped": 0, "known_posts_imported": 0,
                                              "errors": [{"run_id": None, "code": "invalid_manifest"}]})


@unittest.skipUnless(os.environ.get("RADAR_TEST_DATABASE_URL"), "Set RADAR_TEST_DATABASE_URL for PostgreSQL import tests")
class ImportValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_dsn = os.environ["RADAR_TEST_DATABASE_URL"]
        cls.schema = "radar_import_" + uuid.uuid4().hex
        with psycopg.connect(cls.base_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(cls.schema)))
        cls.db = Database(make_conninfo(cls.base_dsn, options=f"-csearch_path={cls.schema}"))
        cls.db.migrate()

    @classmethod
    def tearDownClass(cls):
        with psycopg.connect(cls.base_dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(cls.schema)))

    def setUp(self):
        with self.db.connect() as conn:
            conn.execute("TRUNCATE media,posts,listings,collection_runs,monitored_groups RESTART IDENTITY CASCADE")
            conn.execute("UPDATE app_state SET paused_code=NULL,worker_heartbeat_at=NULL")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.manifest = self.root / "stability.json"
        self.runs = self.root / "runs"

    def saved_run(self, run_id="run-1", status="success"):
        metadata = {"run_id": run_id, "group_id": "9", "group_input": "https://www.facebook.com/groups/example/",
                    "source": "synthetic", "started_at": "2026-11-08T12:01:00Z",
                    "from": "2026-11-08T06:00:00Z", "to": "2026-11-08T12:00:00Z"}
        raw = {"schema_version": 1, "collector": "facebook-go", "source": "synthetic", "run_id": run_id,
               "group_id": "9", "status": status, "duration_ms": 25, "pages_fetched": 1,
               "started_at": metadata["started_at"], "collected_at": "2026-11-08T12:01:01Z",
               "posts": [{"id": "901", "groupId": "9", "message": "Synthetic guitar post in preserved raw data.",
                          "createdAt": "2026-11-08T11:00:00Z", "attachments": []}],
               "errors": [] if status == "success" else [{"code": "network_error", "message": "PRIVATE_TOKEN_DO_NOT_LOG"}],
               "window": {"from": metadata["from"], "to": metadata["to"], "complete": status == "success",
                          "stop_reason": "feed_exhausted" if status == "success" else "error"}}
        return metadata, raw

    def write(self, records):
        self.manifest.write_text(json.dumps({"run_ids": list(records)}))
        for run_id, (metadata, raw) in records.items():
            directory = self.runs / run_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "metadata.json").write_text(json.dumps(metadata))
            (directory / "facebook-go.raw.json").write_text(json.dumps(raw))

    def test_import_is_idempotent_audit_only_and_preserves_saved_files(self):
        records = {"run-1": self.saved_run(), "run-2": self.saved_run("run-2", "partial")}
        self.write(records)
        group = self.db.create_group("Pilot", "https://www.facebook.com/groups/example")
        queued = self.db.enqueue(group["id"])
        with self.db.connect() as conn:
            conn.execute("UPDATE monitored_groups SET last_error_code='network_error',last_success_at='2026-11-01T00:00:00Z' WHERE id=%s", (group["id"],))
            conn.execute("UPDATE app_state SET paused_code='checkpoint_required'")
        original_group = self.db.get_group(group["id"])
        before = {path: path.read_bytes() for path in self.runs.rglob("*.json")}
        with patch("scripts.phase0.resolve_group", side_effect=AssertionError("No Facebook requests")), \
             patch.object(self.db, "enqueue", side_effect=AssertionError("No new collection")):
            first = import_validation_runs(self.db, self.manifest, self.runs)
            second = import_validation_runs(self.db, self.manifest, self.runs)
        self.assertEqual(first, {"imported": 2, "skipped": 0, "known_posts_imported": 0, "errors": []})
        self.assertEqual(second, {"imported": 0, "skipped": 2, "known_posts_imported": 0, "errors": []})
        self.assertEqual(self.db.feed()["total"], 0)
        self.assertEqual(self.db.get_group(group["id"]), original_group)
        self.assertEqual(self.db.state()["paused_code"], "checkpoint_required")
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM collection_runs ORDER BY id").fetchall()
            self.assertEqual(len(rows), 3)
            self.assertEqual(rows[0]["id"], queued["id"])
            self.assertEqual(rows[0]["status"], "queued")
            self.assertEqual([row["status"] for row in rows[1:]], ["success", "partial"])
            self.assertEqual(rows[2]["error_code"], "network_error")
            self.assertEqual(rows[1]["posts_count"], 1)
            snapshots = conn.execute("SELECT * FROM collector_run_snapshots").fetchall()
            self.assertEqual(len(snapshots), 2)
            self.assertNotIn("PRIVATE_TOKEN_DO_NOT_LOG", json.dumps(snapshots, default=str))
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_new_imported_group_is_disabled_and_no_post_is_reingested(self):
        self.write({"run-1": self.saved_run()})
        self.assertEqual(import_validation_runs(self.db, self.manifest, self.runs)["imported"], 1)
        groups = self.db.list_groups()
        self.assertEqual(len(groups), 1)
        self.assertFalse(groups[0]["enabled"])
        self.assertIsNone(groups[0]["last_success_at"])
        self.assertIsNone(groups[0]["last_error_code"])
        self.assertEqual(self.db.feed()["total"], 0)

    def test_confirmed_fixtures_import_for_existing_alias_without_retyping(self):
        self.write({"run-1": self.saved_run()})
        group = self.db.create_group("Pilot", "https://www.facebook.com/groups/example")
        value = json.loads(self.manifest.read_text())
        value["evaluated_known_posts"] = [{"post_id": "901", "url": "https://www.facebook.com/groups/9/posts/901/",
                                          "expected_has_images": False, "verified_posted_at": "2026-11-08T11:00:00Z"}]
        self.manifest.write_text(json.dumps(value))
        first = import_validation_runs(self.db, self.manifest, self.runs)
        self.assertEqual(first["errors"], [])
        self.assertEqual(first["known_posts_imported"], 1)
        second = import_validation_runs(self.db, self.manifest, self.runs)
        self.assertEqual(second["known_posts_imported"], 0)
        self.assertEqual(second["errors"], [])
        with self.db.connect() as conn:
            known = conn.execute("SELECT * FROM collector_known_posts WHERE group_id=%s", (group["id"],)).fetchall()
        self.assertEqual(len(known), 1)
        self.assertFalse(known[0]["expected_has_images"])
        self.assertIsNotNone(known[0]["verified_posted_at"])
        self.assertEqual(self.db.feed()["total"], 0)

    def test_fixture_import_does_not_replace_conflicting_confirmed_expectations(self):
        from radar.audit import add_known_post
        self.write({"run-1": self.saved_run()})
        group = self.db.create_group("Pilot", "https://www.facebook.com/groups/example")
        add_known_post(self.db, group["id"], "https://www.facebook.com/groups/example/posts/901/", True,
                       expected_text="Independently confirmed content", expected_image_count=2)
        value = json.loads(self.manifest.read_text())
        value["evaluated_known_posts"] = [{"url": "https://www.facebook.com/groups/example/posts/901/", "expected_has_images": False}]
        self.manifest.write_text(json.dumps(value))
        result = import_validation_runs(self.db, self.manifest, self.runs)
        self.assertEqual(result["errors"], [{"run_id": None, "code": "fixture_conflict"}])
        with self.db.connect() as conn:
            row = conn.execute("SELECT * FROM collector_known_posts WHERE group_id=%s", (group["id"],)).fetchone()
        self.assertTrue(row["expected_has_images"])
        self.assertEqual(row["expected_image_count"], 2)
        self.assertEqual(row["expected_text"], "Independently confirmed content")

    def test_identity_source_window_and_symlink_mismatches_are_rejected(self):
        metadata, raw = self.saved_run()
        for key, value in (("run_id", "different"), ("group_id", "77"), ("source", "live"), ("collector", "fbgql")):
            changed = deepcopy(raw)
            changed[key] = value
            with self.subTest(key=key):
                self.write({"run-1": (metadata, changed)})
                result = import_validation_runs(self.db, self.manifest, self.runs)
                self.assertEqual(result["errors"], [{"run_id": "run-1", "code": "invalid_run"}])
        changed = deepcopy(raw)
        changed["window"]["to"] = "2026-11-08T12:10:00Z"
        self.write({"run-1": (metadata, changed)})
        self.assertEqual(import_validation_runs(self.db, self.manifest, self.runs)["errors"][0]["code"], "invalid_run")
        self.write({"run-1": (metadata, raw)})
        raw_path = self.runs / "run-1/facebook-go.raw.json"
        outside = self.root / "outside.json"
        raw_path.rename(outside)
        raw_path.symlink_to(outside)
        self.assertEqual(import_validation_runs(self.db, self.manifest, self.runs)["errors"][0]["code"], "invalid_run")
        self.assertEqual(self.db.list_groups(), [])

    def test_capture_failure_rolls_back_group_and_run_with_safe_error(self):
        self.write({"run-1": self.saved_run()})
        with patch("radar.audit.capture_run", side_effect=RuntimeError("postgres password=PRIVATE_SECRET")):
            result = import_validation_runs(self.db, self.manifest, self.runs)
        self.assertEqual(result, {"imported": 0, "skipped": 0, "known_posts_imported": 0,
                                  "errors": [{"run_id": "run-1", "code": "storage_error"}]})
        self.assertEqual(self.db.list_groups(), [])
        self.assertEqual(self.db.list_runs(), [])


if __name__ == "__main__":
    unittest.main()

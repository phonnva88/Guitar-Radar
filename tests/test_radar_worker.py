"""Worker integration boundaries tested without Facebook or a database."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from radar.worker import CollectionFailure, FacebookCollector, Worker
from scripts import phase0


class FakeDatabase:
    def __init__(self, *, paused=None, lock=True, active=False):
        self.paused, self.lock, self.active = paused, lock, active
        self.queued = [{"id": 1, "group_id": 2, "group_url": "https://www.facebook.com/groups/9/"}]
        self.completed, self.failed, self.enqueued = [], [], []
        self.recovered = self.heartbeats = 0
        self.cleared = 0
        self.lock_held = False
        self.groups = []

    def state(self):
        return {"paused_code": self.paused, "worker_active": self.active}

    def claim_next(self):
        return self.queued.pop(0) if self.queued else None

    def complete_run(self, run_id, envelope, **kwargs):
        self.completed.append((run_id, envelope))
        errors = envelope.get("errors", [])
        if any(e.get("code") == "checkpoint_required" for e in errors):
            self.paused = "checkpoint_required"

    def fail_run(self, run_id, code, **kwargs):
        self.failed.append((run_id, code))
        if code in {"session_expired", "checkpoint_required"}:
            self.paused = code

    def enqueue(self, group_id):
        self.enqueued.append(group_id)

    def list_groups(self):
        return self.groups

    @contextmanager
    def worker_lock(self):
        self.lock_held = self.lock
        try:
            yield self.lock
        finally:
            self.lock_held = False

    def recover_runs(self):
        self.recovered += 1

    def set_worker_heartbeat(self):
        self.heartbeats += 1

    def clear_worker_heartbeat(self):
        if not self.lock_held:
            raise RuntimeError("lock lost")
        self.cleared += 1
        self.active = False


class WorkerTests(unittest.TestCase):
    def test_raw_acquisition_and_offline_processing_are_distinct_steps(self):
        db = FakeDatabase()
        raw = {"posts": [{"id": "11", "message": "source-shaped"}]}
        normalized = {"posts": [{"post_id": "11", "text": "source-shaped"}], "errors": []}
        crawler = Mock(return_value=raw)
        processor = Mock(return_value=normalized)
        worker = Worker(db, crawler, processor=processor)
        self.assertTrue(worker.process_one())
        processor.assert_called_once_with(raw)
        self.assertEqual(db.completed, [(1, normalized)])
        self.assertEqual(raw["posts"][0], {"id": "11", "message": "source-shaped"})

    def test_processing_failure_closes_run_without_ingesting_raw(self):
        db = FakeDatabase()
        worker = Worker(db, Mock(return_value={"posts": []}), processor=Mock(side_effect=ValueError("SECRET")))
        self.assertTrue(worker.process_one())
        self.assertEqual(db.failed, [(1, "parse_error")])
        self.assertFalse(db.completed)

    def test_processing_error_cannot_hide_a_security_stop(self):
        db = FakeDatabase()
        raw = {"posts": [], "errors": [{"code": "checkpoint_required"}]}
        worker = Worker(db, Mock(return_value=raw), processor=Mock(side_effect=ValueError("parse failure")))
        self.assertTrue(worker.process_one())
        self.assertEqual(db.failed, [(1, "checkpoint_required")])
        self.assertFalse(worker.process_one())

    def test_pause_does_not_claim_or_collect(self):
        db = FakeDatabase(paused="checkpoint_required")
        collector = Mock()
        self.assertFalse(Worker(db, collector).process_one())
        collector.assert_not_called()
        self.assertEqual(len(db.queued), 1)

    def test_partial_rows_reach_storage_and_security_error_pauses_next_claim(self):
        db = FakeDatabase()
        envelope = {"status": "partial", "posts": [{"post_id": "11"}],
                    "errors": [{"code": "checkpoint_required"}]}
        worker = Worker(db, Mock(return_value=envelope), processor=lambda raw: raw)
        self.assertTrue(worker.process_one())
        self.assertEqual(db.completed, [(1, envelope)])
        self.assertFalse(worker.process_one())

    def test_unknown_exception_text_does_not_reach_status(self):
        db = FakeDatabase()
        worker = Worker(db, Mock(side_effect=RuntimeError("xs=PRIVATE_SECRET")))
        self.assertTrue(worker.process_one())
        self.assertEqual(db.failed, [(1, "unknown_error")])
        self.assertFalse(db.completed)

    def test_session_failure_blocks_subsequent_run(self):
        db = FakeDatabase()
        worker = Worker(db, Mock(side_effect=CollectionFailure("session_expired")))
        worker.process_one()
        self.assertEqual(db.failed, [(1, "session_expired")])
        self.assertFalse(worker.process_one())

    def test_storage_validation_failure_closes_claimed_run(self):
        db = FakeDatabase()
        db.complete_run = Mock(side_effect=ValueError("invalid source payload SECRET"))
        worker = Worker(db, Mock(return_value={"posts": []}), processor=lambda raw: raw)
        self.assertTrue(worker.process_one())
        self.assertEqual(db.failed, [(1, "parse_error")])

    def test_uncertain_database_result_stops_worker_without_raw_message(self):
        db = FakeDatabase()
        db.complete_run = Mock(side_effect=RuntimeError("postgres password=SECRET"))
        worker = Worker(db, Mock(return_value={"posts": []}), processor=lambda raw: raw)
        with self.assertRaisesRegex(CollectionFailure, "^unknown_error$"):
            worker.process_one()
        self.assertTrue(worker._stop.is_set())
        self.assertEqual(db.failed, [(1, "unknown_error")])

    def test_scheduler_respects_disabled_recent_and_interval(self):
        db = FakeDatabase()
        now = datetime(2026, 11, 8, 12, tzinfo=timezone.utc)
        db.groups = [{"id": 1, "enabled": True}, {"id": 2, "enabled": False},
                     {"id": 3, "enabled": True, "last_success_at": now}]
        worker = Worker(db, Mock(), interval_seconds=300, clock=lambda: 1000, now=lambda: now)
        worker._schedule()
        worker._schedule()
        self.assertEqual(db.enqueued, [1])

    def test_lock_prevents_second_worker_recovering_or_running(self):
        db = FakeDatabase(lock=False)
        collector = Mock()
        self.assertFalse(Worker(db, collector).run_forever())
        self.assertEqual(db.recovered, 0)
        collector.assert_not_called()

    def test_shutdown_restores_loop_and_recovers_once(self):
        db = FakeDatabase()
        stop = threading.Event()
        stop.set()
        self.assertTrue(Worker(db, Mock()).run_forever(stop))
        self.assertEqual(db.recovered, 1)
        self.assertEqual(db.heartbeats, 1)
        self.assertEqual(db.cleared, 1)

    def test_fresh_previous_worker_lease_prevents_recovery_or_claim(self):
        db = FakeDatabase(active=True)
        stop = threading.Event()
        stop.set()
        collector = Mock()
        self.assertTrue(Worker(db, collector).run_forever(stop))
        self.assertEqual(db.recovered, 0)
        self.assertEqual(db.heartbeats, 0)
        self.assertEqual(db.cleared, 0)
        collector.assert_not_called()

    def test_lost_lock_does_not_clear_the_heartbeat_lease(self):
        db = FakeDatabase()
        def lost_ownership():
            db.heartbeats += 1
            db.lock_held = False
        db.set_worker_heartbeat = lost_ownership
        stop = threading.Event()
        stop.set()
        with self.assertLogs("radar.worker", level="WARNING") as logs:
            self.assertTrue(Worker(db, Mock()).run_forever(stop))
        self.assertEqual(db.cleared, 0)
        self.assertEqual(logs.output, ["WARNING:radar.worker:worker_heartbeat_not_cleared"])


class CollectorTests(unittest.TestCase):
    def run_collector(self, *, change=None, returncode=0, timed_out=False, reload_cookie=False,
                      exported_cookie=False, cookie_file=False):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / ".bin").mkdir()
        (root / ".bin/facebook-go").touch()
        self.output_path, self.command, self.kwargs = None, None, None
        self.seen_cookies = []

        def execute(command, **kwargs):
            self.command, self.kwargs = command, kwargs
            self.seen_cookies.append(os.environ.get("FACEBOOK_COOKIE"))
            field = lambda key: command[command.index(key) + 1]
            raw = {"schema_version": 1, "collector": "facebook-go", "source": "live",
                   "run_id": field("--run-id"), "group_id": "9", "status": "success",
                   "duration_ms": 12, "pages_fetched": 1, "collected_at": field("--to"),
                   "posts": [{"id": "11", "groupId": "9", "message": "An actual post-shaped fixture",
                              "createdAt": field("--to"), "attachments": []}], "errors": [],
                   "window": {"from": field("--from"), "to": field("--to"), "complete": True,
                              "stop_reason": "feed_exhausted"}}
            if change:
                change(raw)
            self.output_path = Path(field("--output"))
            self.output_path.write_text(json.dumps(raw))
            if timed_out:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return SimpleNamespace(returncode=returncode)

        initial_env = {} if reload_cookie and not exported_cookie else {"FACEBOOK_COOKIE": "synthetic-not-a-cookie"}
        with patch.dict(os.environ, initial_env, clear=True), patch.object(phase0, "ROOT", root):
            if cookie_file:
                (root / "private-header.txt").write_text("c_user=12; xs=FIRST_SYNTHETIC")
                (root / ".env").write_text(f"FACEBOOK_COOKIE_FILE='{root / 'private-header.txt'}'\n")
            else:
                (root / ".env").write_text("FACEBOOK_COOKIE='c_user=12; xs=FIRST_SYNTHETIC'\n")
            collector = FacebookCollector(root=root, execute=execute,
                                          load_env=phase0.load_local_env if reload_cookie else lambda: None,
                                          resolve_group=lambda *_args, **_kw: "9",
                                          now=lambda: datetime(2026, 11, 8, 12, tzinfo=timezone.utc))
            result = collector({"group_url": "https://www.facebook.com/groups/9/"})
            if reload_cookie:
                if cookie_file:
                    (root / "private-header.txt").write_text("c_user=12; xs=SECOND_SYNTHETIC")
                else:
                    (root / ".env").write_text("FACEBOOK_COOKIE='c_user=12; xs=SECOND_SYNTHETIC'\n")
                result = collector({"group_url": "https://www.facebook.com/groups/9/"})
            return result

    def test_uses_private_temp_output_bounded_window_and_no_debug_or_logs(self):
        result = self.run_collector()
        self.assertEqual(result["posts"][0]["post_id"], "11")
        self.assertEqual(result["window"]["from"], "2026-11-05T12:00:00Z")
        self.assertEqual(self.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(self.kwargs["stderr"], subprocess.DEVNULL)
        self.assertNotIn("--debug-dir", self.command)
        self.assertNotIn("synthetic-not-a-cookie", str(self.command))
        self.assertFalse(self.output_path.exists())

    def test_identity_source_or_window_mismatch_rejected(self):
        for key, value in (("run_id", "old-run"), ("group_id", "other"),
                           ("collector", "fbgql"), ("source", "synthetic")):
            with self.subTest(key=key), self.assertRaisesRegex(CollectionFailure, "parse_error"):
                self.run_collector(change=lambda raw: raw.update({key: value}))
        with self.assertRaisesRegex(CollectionFailure, "parse_error"):
            self.run_collector(change=lambda raw: raw["window"].update(to="2026-11-09T12:00:00Z"))

    def test_partial_network_result_preserved_with_safe_error_code(self):
        def partial(raw):
            raw.update(status="partial", errors=[{"code": "network_error", "message": "xs=DO_NOT_SAVE"}])
        result = self.run_collector(change=partial, returncode=1)
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(result["errors"], [{"code": "network_error"}])
        self.assertNotIn("DO_NOT_SAVE", json.dumps(result))

    def test_timeout_preserves_valid_rows_without_claiming_success(self):
        result = self.run_collector(timed_out=True)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["posts"]), 1)
        self.assertFalse(result["window"]["complete"])
        self.assertEqual(result["errors"], [{"code": "network_error"}])

    def test_nonzero_exit_cannot_be_disguised_as_success(self):
        with self.assertRaisesRegex(CollectionFailure, "parse_error"):
            self.run_collector(returncode=1)

    def test_file_sourced_session_is_refreshed_between_collections(self):
        self.run_collector(reload_cookie=True)
        self.assertEqual(self.seen_cookies, ["c_user=12; xs=FIRST_SYNTHETIC", "c_user=12; xs=SECOND_SYNTHETIC"])

    def test_worker_launch_exports_keep_priority_over_file_updates(self):
        self.run_collector(reload_cookie=True, exported_cookie=True)
        self.assertEqual(self.seen_cookies, ["synthetic-not-a-cookie", "synthetic-not-a-cookie"])

    def test_cookie_file_is_reread_between_collections(self):
        self.run_collector(reload_cookie=True, cookie_file=True)
        self.assertEqual(self.seen_cookies, ["c_user=12; xs=FIRST_SYNTHETIC", "c_user=12; xs=SECOND_SYNTHETIC"])

    def test_cancel_terminates_and_reaps_real_child(self):
        collector = FacebookCollector()
        started = threading.Event()
        processes, failures = [], []
        real_popen = subprocess.Popen

        def start(*args, **kwargs):
            process = real_popen(*args, **kwargs)
            processes.append(process)
            started.set()
            return process

        def collect():
            try:
                collector._execute([sys.executable, "-c", "import time; time.sleep(60)"],
                                   cwd=Path.cwd(), timeout=70)
            except CollectionFailure as exc:
                failures.append(exc.code)

        with patch("facebook_crawler.collector.subprocess.Popen", side_effect=start):
            thread = threading.Thread(target=collect)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                collector.cancel_event.set()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, ["interrupted"])
                self.assertIsNotNone(processes[0].returncode)
            finally:
                collector.cancel_event.set()
                for process in processes:
                    if process.poll() is None:
                        process.kill()
                        process.wait()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

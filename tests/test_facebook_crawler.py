"""Acquisition contract regressions; no Facebook requests or database needed."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from facebook_crawler import CollectionFailure, FacebookCrawler, write_json
from facebook_crawler.__main__ import main
from scripts import phase0


class CrawlerTests(unittest.TestCase):
    def crawl(self, change=None, *, returncode=0, timed_out=False, requested=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        (root / ".bin").mkdir()
        (root / ".bin/facebook-go").touch()
        self.command, self.kwargs = None, None

        def execute(command, **kwargs):
            self.command, self.kwargs = command, kwargs
            field = lambda key: command[command.index(key) + 1]
            raw = {"schema_version": 1, "collector": "facebook-go", "source": "live", "group_id": "9",
                   "run_id": field("--run-id"), "status": "success", "duration_ms": 10, "pages_fetched": 1,
                   "collected_at": field("--to"), "errors": [],
                   "posts": [{"id": "11", "groupId": "9", "message": "Original untouched caption",
                              "createdAt": "2026-10-01T00:00:00Z", "attachments": []},
                             {"id": "11", "groupId": "9", "message": "Original untouched caption",
                              "createdAt": "2026-10-01T00:00:00Z", "attachments": []}],
                   "window": {"from": field("--from"), "to": field("--to"), "complete": True, "stop_reason": "feed_exhausted"}}
            if change:
                change(raw)
            self.output = Path(field("--output"))
            self.output.write_text(json.dumps(raw))
            if timed_out:
                raise subprocess.TimeoutExpired(command, kwargs["timeout"])
            return SimpleNamespace(returncode=returncode)

        with patch.dict(os.environ, {"FACEBOOK_COOKIE": "c_user=12; xs=SYNTHETIC_SESSION_SECRET"}, clear=True):
            return FacebookCrawler(root=root, execute=execute, resolve_group=lambda *_a, **_kw: "9",
                                   load_env=lambda: None, now=lambda: datetime(2026, 11, 8, 12, tzinfo=timezone.utc))(
                                       {"group_url": "9", **(requested or {})})

    def test_acquisition_preserves_raw_fields_duplicates_and_outside_window_rows(self):
        raw = self.crawl()
        self.assertEqual(len(raw["posts"]), 2)
        self.assertEqual(raw["posts"][0]["id"], "11")
        self.assertEqual(raw["posts"][0]["createdAt"], "2026-10-01T00:00:00Z")
        self.assertNotIn("post_id", raw["posts"][0])
        self.assertNotIn("normalization", raw)
        self.assertFalse(self.output.exists())
        self.assertEqual(self.kwargs["stdout"], subprocess.DEVNULL)
        self.assertNotIn("--debug-dir", self.command)
        self.assertNotIn("SYNTHETIC_SESSION_SECRET", str(self.command))

    def test_raw_export_removes_sensitive_fields_and_session_literals(self):
        def change(raw):
            raw["headers"] = {"Cookie": "c_user=12; xs=SYNTHETIC_SESSION_SECRET"}
            raw["errors"] = [{"code": "network_error", "message": "xs=SYNTHETIC_SESSION_SECRET"}]
            raw["status"] = "partial"
            raw["posts"][0]["authorization"] = "private"
            raw["posts"][0]["message"] = "Caption SYNTHETIC_SESSION_SECRET"
        raw = self.crawl(change, returncode=1)
        self.assertEqual(raw["errors"], [{"code": "network_error"}])
        self.assertNotIn("headers", raw)
        self.assertNotIn("authorization", raw["posts"][0])
        self.assertNotIn("SYNTHETIC_SESSION_SECRET", json.dumps(raw))
        self.assertEqual(raw["posts"][0]["message"], "Caption [REDACTED]")

    def test_timeout_retains_valid_raw_rows_and_incomplete_state(self):
        raw = self.crawl(timed_out=True)
        self.assertEqual(raw["status"], "partial")
        self.assertEqual(len(raw["posts"]), 2)
        self.assertFalse(raw["window"]["complete"])
        self.assertEqual(raw["errors"], [{"code": "network_error"}])

    def test_fixed_window_is_identical_across_runs_and_keeps_outside_rows(self):
        requested = {"requested_from": "2026-11-07T18:00:00+07:00",
                     "requested_to": datetime(2026, 11, 7, 15, tzinfo=timezone.utc)}
        first = self.crawl(requested=requested)
        second = self.crawl(requested=requested)
        for raw in (first, second):
            self.assertEqual(raw["window"]["from"], "2026-11-07T11:00:00Z")
            self.assertEqual(raw["window"]["to"], "2026-11-07T15:00:00Z")
            self.assertEqual(len(raw["posts"]), 2)
            self.assertEqual(raw["posts"][0]["createdAt"], "2026-10-01T00:00:00Z")
        self.assertNotEqual(first["run_id"], second["run_id"])

    def test_invalid_fixed_window_fails_before_session_or_network_access(self):
        for requested in ({"requested_from": "2026-11-07T11:00:00Z"},
                          {"requested_from": "2026-11-07T12:00:00", "requested_to": "2026-11-07T13:00:00Z"},
                          {"requested_from": "2026-11-08T00:00:00Z", "requested_to": "2026-11-07T00:00:00Z"}):
            load, resolve = Mock(), Mock()
            crawler = FacebookCrawler(load_env=load, resolve_group=resolve)
            with self.subTest(requested=requested), self.assertRaisesRegex(CollectionFailure, "^configuration_error$"):
                crawler({"group_url": "9", **requested})
            load.assert_not_called()
            resolve.assert_not_called()

    def test_invalid_identity_schema_status_or_window_is_rejected(self):
        for field, value in (("run_id", "stale"), ("group_id", "77"), ("collector", "fbgql"),
                             ("source", "synthetic"), ("schema_version", 2), ("status", "bogus"),
                             ("posts", ["not a post"]), ("errors", "not errors")):
            with self.subTest(field=field), self.assertRaisesRegex(CollectionFailure, "^parse_error$"):
                self.crawl(lambda raw: raw.update({field: value}))
        with self.assertRaisesRegex(CollectionFailure, "^parse_error$"):
            self.crawl(lambda raw: raw["window"].update(to="2026-11-09T00:00:00Z"))
        with self.assertRaisesRegex(CollectionFailure, "^parse_error$"):
            self.crawl(returncode=1)

    def test_missing_session_never_starts_network_or_adapter(self):
        resolve, execute = Mock(), Mock()
        with patch.dict(os.environ, {}, clear=True):
            crawler = FacebookCrawler(load_env=lambda: None, resolve_group=resolve, execute=execute)
            with self.assertRaisesRegex(CollectionFailure, "^authentication_failed$"):
                crawler({"group_url": "9"})
        resolve.assert_not_called()
        execute.assert_not_called()

    def test_security_failure_is_fixed_code_without_retry(self):
        resolve = Mock(side_effect=phase0.RunError("checkpoint_required", "xs=DO_NOT_EXPOSE"))
        execute = Mock()
        with patch.dict(os.environ, {"FACEBOOK_COOKIE": "synthetic"}, clear=True):
            crawler = FacebookCrawler(load_env=lambda: None, resolve_group=resolve, execute=execute)
            with self.assertRaisesRegex(CollectionFailure, "^checkpoint_required$"):
                crawler({"group_url": "9"})
        resolve.assert_called_once()
        execute.assert_not_called()

    def test_export_is_private_and_atomically_replaces_old_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "exports/raw.json"
            write_json(path, {"posts": [1]})
            path.chmod(0o644)
            write_json(path, {"posts": [2]})
            self.assertEqual(json.loads(path.read_text()), {"posts": [2]})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_cli_exports_partial_result_and_returns_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "raw.json"
            raw = {"status": "partial", "posts": [{"id": "11"}], "window": {"complete": False}}
            with patch("facebook_crawler.__main__.FacebookCrawler", return_value=Mock(return_value=raw)):
                self.assertEqual(main(["--group", "9", "--output", str(path)]), 1)
            self.assertEqual(json.loads(path.read_text()), raw)

    def test_cancel_reaps_the_real_child(self):
        crawler = FacebookCrawler()
        started, failures, children = threading.Event(), [], []
        actual = subprocess.Popen

        def popen(*args, **kwargs):
            child = actual(*args, **kwargs)
            children.append(child)
            started.set()
            return child

        def run():
            try:
                crawler._execute([sys.executable, "-c", "import time; time.sleep(30)"], cwd=Path.cwd(), timeout=40)
            except CollectionFailure as exc:
                failures.append(exc.code)

        with patch("facebook_crawler.collector.subprocess.Popen", side_effect=popen):
            thread = threading.Thread(target=run)
            thread.start()
            try:
                self.assertTrue(started.wait(5))
                crawler.cancel_event.set()
                thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(failures, ["interrupted"])
                self.assertIsNotNone(children[0].returncode)
            finally:
                crawler.cancel_event.set()
                for child in children:
                    if child.poll() is None:
                        child.kill()
                        child.wait()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()

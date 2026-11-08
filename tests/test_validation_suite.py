import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from unittest.mock import MagicMock

from scripts import phase0
from validation import suite


class SuiteTests(unittest.TestCase):
    def scoped(self, post_id, expected=True, group="9", **extra):
        return {"post_id": post_id, "url": f"https://www.facebook.com/groups/{group}/posts/{post_id}/",
                "expected_has_images": expected, **extra}

    def test_timezone_window_and_inclusive_order(self):
        self.assertEqual(suite.window("2026-11-07T18:00:00+07:00", "2026-11-07T22:00:00+07:00"),
                         ("2026-11-07T11:00:00Z", "2026-11-07T15:00:00Z"))
        for bounds in (("2026-11-07", "2026-11-08"), ("2026-11-07T12:00:00Z", "2026-11-07T11:00:00Z")):
            with self.assertRaises(ValueError):
                suite.window(*bounds)

    def test_image_truth_separates_missing_and_extraction_and_text_only(self):
        known = [self.scoped(p, p != "4", verified_posted_at="2026-11-07T11:00:00Z" if p == "3" else None)
                 for p in ("1", "2", "3", "4")]
        for item in known:
            if item["verified_posted_at"] is None:
                item.pop("verified_posted_at")
        posts = [{"post_id": "1", "images": ["https://example.invalid/img"]},
                 {"post_id": "2", "images": []}, {"post_id": "4", "images": []}]
        result = suite.image_validation({"group_id": "9", "window": {"from": "2026-11-07T10:00:00Z", "to": "2026-11-07T12:00:00Z"}, "posts": posts}, known)
        self.assertEqual(result["missing_known_image_posts"], ["3"])
        self.assertEqual(result["extraction_failures"], ["2"])
        self.assertEqual(result["image_detection_recall"], 1 / 3)
        self.assertEqual(result["conditional_image_detection_recall"], 1 / 2)
        self.assertEqual(result["observation_recall"], 2 / 3)
        self.assertEqual(result["records"][-1]["status"], "expected_text_only")

    def test_conflicting_image_duplicate_not_counted_as_verified(self):
        known = self.scoped("1")
        result = suite.image_validation({"group_id": "9", "posts": [{"post_id": "1", "images": []},
            {"post_id": "1", "images": ["https://example.invalid/p"]}]}, [known])
        self.assertEqual(result["ambiguous_known_posts"], ["1"])
        self.assertEqual(result["detected_image_posts"], 0)

    def test_fixture_scope_separates_wrong_group_unresolved_unknown_and_out_of_window(self):
        start, end = "2026-11-07T10:00:00Z", "2026-11-07T12:00:00Z"
        known = [
            self.scoped("1", group="77"),
            {"url": "https://www.facebook.com/share/p/abc123/", "unresolved": True, "expected_has_images": True},
            self.scoped("2", verified_posted_at="2026-11-07T09:00:00Z"),
            self.scoped("3", verified_posted_at="2026-11-07T11:00:00Z"),
            self.scoped("4"),
        ]
        posts = [{"post_id": "1", "images": ["https://example.invalid/a"]},
                 {"post_id": "2", "images": ["https://example.invalid/b"]},
                 {"post_id": "3", "images": []}]
        result = suite.image_validation({"group_id": "9", "window": {"from": start, "to": end}, "posts": posts}, known)
        self.assertEqual(result["wrong_group_posts"], ["1"])
        self.assertEqual(result["unresolved_posts"], ["https://www.facebook.com/share/p/abc123/"])
        self.assertEqual(result["outside_window_verified_posts"], ["2"])
        self.assertEqual(result["timestamp_unknown_posts"], ["4"])
        self.assertEqual(result["extraction_failures"], ["3"])
        self.assertEqual(result["image_detection_recall"], 0)
        self.assertEqual(result["eligible_known_image_posts"], 1)

    def test_known_fixture_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "known.json"
            phase0.write_json(path, [{"url": "https://www.facebook.com/groups/9/posts/123/", "expected_has_images": True}])
            self.assertEqual(suite.known_posts(path)[0]["post_id"], "123")
            phase0.write_json(path, [{"post_id": "999", "url": "https://www.facebook.com/groups/9/posts/123/", "expected_has_images": True}])
            with self.assertRaisesRegex(ValueError, "does not match"):
                suite.known_posts(path)
            with self.assertRaises(ValueError):
                suite.known_posts(path, "https://not-facebook.invalid/?secret")

    def test_share_fixture_is_unresolved_until_canonical_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "known.json"
            phase0.write_json(path, [{"url": "https://www.facebook.com/share/p/abc123/", "expected_has_images": True}])
            known = suite.known_posts(path)
            self.assertIsNone(known[0]["post_id"])
            self.assertTrue(known[0]["unresolved"])
            response = MagicMock()
            response.status_code, response.headers = 200, {}
            response.__enter__.return_value = response
            response.iter_content.return_value = [b'<html><script>requireLazy(["CometLoginForm"], function () {});</script><link rel="canonical" href="https://www.facebook.com/groups/9/posts/123/"></html>']
            with patch.dict(os.environ, {"FACEBOOK_COOKIE": "c_user=12; xs=TEST_SECRET_SESSION"}), patch("requests.Session.request", return_value=response), patch.object(suite.time, "sleep"):
                result, error = suite.resolve_known(known)
            self.assertIsNone(error)
            self.assertEqual(result[0]["post_id"], "123")
            self.assertFalse(result[0]["unresolved"])

    def test_archive_preserves_resolved_fixture_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            phase0.write_json(directory / "known-posts-resolved.json", [{"post_id": "123"}])
            (directory / "collector-health.md").write_text("old READY")
            suite.archive_summaries(directory)
            self.assertTrue((directory / "known-posts-resolved.json").exists())
            self.assertFalse((directory / "collector-health.md").exists())
            self.assertEqual(json.loads((directory / "validation-status.json").read_text())["status"], "running")

    def test_offline_analyze_uses_saved_share_resolution(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(suite, "ROOT", Path(tmp)):
            root = Path(tmp)
            directory = root / "data/runs/replay-test"
            args = argparse.Namespace(max_pages=2, page_size=20)
            resolved = [{"post_id": "123", "url": "https://www.facebook.com/groups/9/posts/123/",
                         "verified_posted_at": "2026-11-06T12:00:00Z", "expected_has_images": True}]
            metadata = {"run_id": "replay-test", "group_id": "9", "from": "2026-11-06T00:00:00Z", "to": "2026-11-07T00:00:00Z",
                        "source": "live", "known_posts": resolved}
            phase0.write_json(directory / "metadata.json", metadata)
            for collector in suite.COLLECTORS:
                raw = suite.failed_run(collector, "9", "9", "replay-test", metadata["from"], metadata["to"], args,
                                       phase0.RunError("network_error", "Offline replay fixture"))
                phase0.write_json(directory / f"{collector}.raw.json", raw)
            original = [{"post_id": None, "url": "https://www.facebook.com/share/p/abc123/", "unresolved": True, "expected_has_images": True}]
            with patch.object(phase0, "load_local_env"), patch.object(suite, "known_posts", return_value=original), contextlib.redirect_stderr(io.StringIO()):
                result = suite.main(["analyze", "--runs-dir", str(root / "data/runs"), "--run-ids", "replay-test", "--output-dir", str(root / "reports")])
            self.assertEqual(result, 1)
            saved = json.loads((root / "reports/image-investigation.json").read_text())
            self.assertEqual(saved["runs"][0]["images"]["facebook-go"]["missing_known_image_posts"], ["123"])

    def test_offline_analyze_flags_resolved_fixture_from_another_group(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(suite, "ROOT", Path(tmp)):
            root = Path(tmp)
            directory = root / "data/runs/wrong-scope"
            args = argparse.Namespace(max_pages=2, page_size=20)
            metadata = {"run_id": "wrong-scope", "group_id": "9", "from": "2026-11-06T00:00:00Z", "to": "2026-11-07T00:00:00Z",
                        "source": "live", "known_posts": [{"post_id": "123", "url": "https://www.facebook.com/groups/77/posts/123/",
                                                               "expected_has_images": True}]}
            phase0.write_json(directory / "metadata.json", metadata)
            for collector in suite.COLLECTORS:
                raw = suite.failed_run(collector, "9", "9", "wrong-scope", metadata["from"], metadata["to"], args,
                                       phase0.RunError("network_error", "Offline replay fixture"))
                phase0.write_json(directory / f"{collector}.raw.json", raw)
            with patch.object(phase0, "load_local_env"), patch.object(suite, "known_posts", return_value=[]), contextlib.redirect_stderr(io.StringIO()):
                result = suite.main(["analyze", "--runs-dir", str(root / "data/runs"), "--run-ids", "wrong-scope", "--output-dir", str(root / "reports")])
            self.assertEqual(result, 1)
            saved = json.loads((root / "reports/stability.json").read_text())
            image = saved["facebook_go"]["per_run"][0]["image_validation"]
            self.assertEqual(image["wrong_group_posts"], ["123"])
            self.assertIsNone(saved["facebook_go"]["images"]["image_detection_recall"])

    def test_failed_pair_stops_other_collector_and_preserves_unique_runs(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(suite, "ROOT", Path(tmp)), patch.dict(os.environ, {"FACEBOOK_COOKIE": "unused-test-secret"}):
            args = argparse.Namespace(group="9", timeout=20, max_pages=5, page_size=20)
            executed = []
            def fake_execute(command, **_kwargs):
                executed.append(command)
                output = Path(command[command.index("--output") + 1])
                run_id = command[command.index("--run-id") + 1]
                raw = suite.failed_run("facebook-go", "9", "9", run_id, "2026-11-07T11:00:00Z", "2026-11-07T15:00:00Z", args,
                                       phase0.RunError("checkpoint_required", "Manual checkpoint required"))
                phase0.write_json(output, raw)
                return argparse.Namespace(returncode=1)
            with patch.object(phase0, "load_local_env"), patch.object(phase0, "resolve_group", return_value="9"), contextlib.redirect_stderr(io.StringIO()):
                first = suite.collect_pair(args, "2026-11-07T11:00:00Z", "2026-11-07T15:00:00Z", [], execute=fake_execute)
                second = suite.collect_pair(args, "2026-11-07T11:00:00Z", "2026-11-07T15:00:00Z", [], execute=fake_execute)
            self.assertEqual(len(executed), 2)  # once per pair; Python skipped after checkpoint
            self.assertNotEqual(first["metadata"]["run_id"], second["metadata"]["run_id"])
            files = list((Path(tmp) / "data/runs").glob("*/*.raw.json"))
            self.assertEqual(len(files), 4)
            for path in files:
                self.assertNotIn("unused-test-secret", path.read_text())
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_repeat_expired_session_explains_fix_and_stops_without_retry(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(suite, "ROOT", Path(tmp)):
            args = argparse.Namespace(max_pages=2, page_size=20)
            runs = {c: suite.failed_run(c, None, "9", "expired-test",
                                       "2026-11-06T00:00:00Z", "2026-11-07T00:00:00Z", args,
                                       phase0.RunError("session_expired", "xs=SECRET_TEST_SESSION"))
                    for c in suite.COLLECTORS}
            with patch.object(phase0, "load_local_env"), \
                 patch.object(suite, "known_posts", return_value=[]), \
                 patch.object(suite, "resolve_known", return_value=([], None)), \
                 patch.object(suite, "collect_pair", return_value={"metadata": {"run_id": "expired-test"}, "collectors": runs}) as collect, \
                 patch.object(suite, "persist_suite"), patch.object(suite.time, "sleep") as sleep, \
                 contextlib.redirect_stderr(io.StringIO()) as logs:
                result = suite.main(["repeat", "--group", "9", "--runs", "10", "--output-dir", str(Path(tmp) / "reports")])
            self.assertEqual(result, 1)
            collect.assert_called_once()
            sleep.assert_not_called()
            self.assertIn("STOP: session_expired", logs.getvalue())
            self.assertIn("update FACEBOOK_COOKIE in .env or FACEBOOK_COOKIE_FILE", logs.getvalue())
            self.assertIn("Exported values override .env", logs.getvalue())
            self.assertNotIn("SECRET_TEST_SESSION", logs.getvalue())


if __name__ == "__main__":
    unittest.main()

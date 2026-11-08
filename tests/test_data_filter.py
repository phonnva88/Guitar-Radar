"""Pure/offline module contract, using synthetic collector fixtures only."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from data_filter import (FilterError, filter_collection, fingerprint, normalize_filters,
                         prepare_post, process_collection, process_normalized)
from data_filter.__main__ import main


ROOT = Path(__file__).resolve().parents[1]


class DataFilterTests(unittest.TestCase):
    def raw(self):
        return json.loads((ROOT / "tests/fixtures/facebook-go.json").read_text())

    def test_processing_is_pure_deterministic_and_preserves_all_default_posts(self):
        raw = self.raw()
        before = deepcopy(raw)
        with patch.dict(os.environ, {}, clear=True), patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            first, second = process_collection(raw), process_collection(raw)
        self.assertEqual(first, second)
        self.assertEqual(raw, before)
        self.assertEqual([x["post_id"] for x in first["posts"]], ["1001", "1002", "1003"])
        self.assertEqual(first["source"], "synthetic")
        self.assertEqual(first["run_id"], raw["run_id"])

    def test_partial_rows_and_fixed_errors_survive_without_secret_messages(self):
        raw = self.raw()
        raw.update(status="partial", errors=[{"code": "network_error", "message": "xs=PRIVATE"}, {"code": "UNKNOWN_PRIVATE"}, {"code": ["malformed"]}])
        result = process_collection(raw)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["posts"]), 3)
        self.assertEqual(result["errors"], [{"code": "network_error"}, {"code": "unknown_error"}, {"code": "unknown_error"}])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_normalized_input_requires_explicit_path_and_keeps_images(self):
        normalized = process_collection(self.raw())
        replayed = process_normalized(normalized)
        self.assertEqual(replayed["posts"], normalized["posts"])
        self.assertEqual(replayed["source"], normalized["source"])
        with self.assertRaises(FilterError):
            process_normalized(self.raw())

    def test_explicit_selection_keeps_window_status_metadata_and_reports_counts(self):
        normalized = process_collection(self.raw())
        first = filter_collection(normalized, q="EXAMPLE GUITAR", has_images=True)
        self.assertEqual([x["post_id"] for x in first["posts"]], ["1001"])
        self.assertEqual(first["status"], normalized["status"])
        self.assertEqual(first["filtering"]["input_posts"], 3)
        self.assertEqual(first["filtering"]["output_posts"], 1)
        self.assertEqual(len(normalized["posts"]), 3)
        dated = filter_collection(normalized, start="2026-11-07T10:00:00Z", end="2026-11-07T11:00:00Z")
        self.assertEqual(len(dated["posts"]), 2)

    def test_post_preparation_retains_identity_and_media_dimensions(self):
        post = process_collection(self.raw())["posts"][0]
        post["validation"] = {"image_candidates": [{"url": post["images"][0], "width": 800, "height": 600, "source": "media"}]}
        result = prepare_post(post, "123456789")
        self.assertEqual(result["canonical_key"], "post:1001")
        self.assertEqual(result["media"][0][2:4], (800, 600))
        self.assertTrue(result["fingerprint"])
        bad = deepcopy(post)
        bad["url"] = "https://www.facebook.com/groups/123456789/posts/other/"
        with self.assertRaises(FilterError):
            prepare_post(bad, "123456789")

    def test_fingerprint_and_filter_validation_match_existing_rules(self):
        self.assertIsNone(fingerprint("up"))
        self.assertEqual(fingerprint("ＦＥＮＤＥＲ " * 8), fingerprint("fender " * 8))
        filters = normalize_filters(start=datetime(2026, 11, 8, tzinfo=timezone.utc))
        self.assertEqual(filters["start"], "2026-11-08T00:00:00Z")
        for kwargs in ({"page": 0}, {"has_images": "false"}, {"start": "2026-11-08"},
                       {"start": "2026-11-08T00:00:00.1Z", "end": "2026-11-08T00:00:00Z"}):
            with self.assertRaises(FilterError):
                normalize_filters(**kwargs)

    def test_cli_offline_replay_private_output_and_input_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "raw.json", Path(tmp) / "processed.json"
            source.write_text(json.dumps(self.raw()))
            before = source.read_bytes()
            with patch("socket.socket", side_effect=AssertionError("Network forbidden")):
                self.assertEqual(main(["--input", str(source), "--output", str(output), "--has-images", "true"]), 0)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            self.assertEqual(len(json.loads(output.read_text())["posts"]), 1)
            saved = output.read_bytes()
            source.write_text('{"message":"PRIVATE malformed"')
            self.assertEqual(main(["--input", str(source), "--output", str(output)]), 1)
            self.assertEqual(output.read_bytes(), saved)

    def test_package_import_does_not_pull_crawler_credentials_or_database(self):
        code = "import sys; import data_filter; assert 'radar' not in sys.modules; assert 'facebook_crawler' not in sys.modules; assert 'psycopg' not in sys.modules"
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

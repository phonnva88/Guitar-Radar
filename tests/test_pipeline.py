"""Synthetic offline regression tests; no Facebook traffic or benchmark claims."""

import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from compare import build_report, markdown_report
from normalizer import canonical_permalink, normalize_run, timestamp, write_json


def raw(collector="facebook-go", posts=None, **changes):
    result = {"schema_version": 1, "collector": collector, "group_id": "987", "run_id": "test-run",
              "started_at": "2026-11-07T10:00:00Z", "collected_at": "2026-11-07T10:00:02Z",
              "duration_ms": 2000, "pages_fetched": 2, "status": "success", "errors": [],
              "source": "synthetic", "requested_pages": 2, "page_size": 20, "sort": "CHRONOLOGICAL",
              "posts": [] if posts is None else posts}
    result.update(changes)
    return result


def go_post(post_id="123", **changes):
    result = {"id": post_id, "groupId": "987", "authorId": "42", "authorName": "Nguyen Van A",
              "message": "Pass Fender Japan ST62", "permalink": "https://www.facebook.com/groups/987/posts/123/",
              "attachments": ["https://scontent.example/image.jpg?token=public-image-url"],
              "createdAt": "2026-11-07T17:00:00+07:00"}
    result.update(changes)
    return result


def gql_post(post_id="123", **changes):
    result = {"post_id": post_id, "text": "Pass Fender Japan ST62", "permalink": "https://www.facebook.com/groups/987/posts/123/",
              "created_time": 1794045600, "page_name": "DO NOT INFER AUTHOR"}
    result.update(changes)
    return result


def paired(first=None, second=None, **metadata):
    return (normalize_run(raw(posts=[go_post()] if first is None else first, **metadata)),
            normalize_run(raw("fbgql", [gql_post()] if second is None else second, **metadata)))


class NormalizerTests(unittest.TestCase):
    def test_actual_go_shape(self):
        post = normalize_run(raw(posts=[go_post()]))["posts"][0]
        self.assertEqual(post["post_id"], "123")
        self.assertEqual(post["author_name"], "Nguyen Van A")
        self.assertEqual(post["posted_at"], "2026-11-07T10:00:00Z")
        self.assertEqual(len(post["images"]), 1)
        self.assertIsNone(post["group_name"])
        self.assertEqual(set(post), {"collector", "post_id", "group_id", "group_name", "author_id", "author_name", "text", "url", "images", "posted_at", "collected_at"})

    def test_fbgql_does_not_invent_author_or_images(self):
        post = normalize_run(raw("fbgql", [gql_post()]))["posts"][0]
        self.assertIsNone(post["author_name"])
        self.assertIsNone(post["author_id"])
        self.assertIsNone(post["group_name"])
        self.assertEqual(post["images"], [])
        self.assertIsNotNone(post["posted_at"])

    def test_unknown_images_and_zero_timestamp_stay_missing(self):
        post = normalize_run(raw(posts=[go_post(createdAt="0001-01-01T00:00:00Z", attachments=[
            "https://facebook.com/groups/987/posts/444", "https://cdn.example/video.mp4", "https://cdn.example/unknown",
            "https://cdn.example/image.PNG", "javascript:example.jpg", "https://cdn.example/image.PNG"])]))["posts"][0]
        self.assertEqual(post["images"], ["https://cdn.example/image.PNG"])
        self.assertIsNone(post["posted_at"])

    def test_bad_record_isolated(self):
        run = normalize_run(raw(posts=[go_post(), "bad", go_post("456", groupId="another"), go_post("789")]))
        self.assertEqual([p["post_id"] for p in run["posts"]], ["123", "789"])
        self.assertEqual(run["status"], "partial")
        self.assertEqual([e["post_index"] for e in run["errors"]], [1, 2])
        self.assertTrue(all(e["code"] == "parse_error" for e in run["errors"]))

    def test_no_invented_id_from_story_id_or_url(self):
        post = normalize_run(raw(posts=[go_post(None, storyId="encoded-story")]))["posts"][0]
        self.assertIsNone(post["post_id"])
        self.assertIsNotNone(post["url"])

    def test_bad_envelope_rejected(self):
        for changes in ({"posts": {}}, {"errors": {}}, {"duration_ms": float("nan")}, {"pages_fetched": True}, {"schema_version": True}, {"run_id": ""}, {"group_id": None}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                normalize_run(raw(**changes))

    def test_failed_unresolved_run_allowed_without_fabricating_id(self):
        run = normalize_run(raw(group_id=None, group_input="https://www.facebook.com/groups/vegmpremium", status="failed", errors=[{"code": "network_error", "stage": "resolution", "message": "Group resolution unavailable"}]))
        self.assertIsNone(run["group_id"])
        self.assertEqual(run["group_input"], "https://www.facebook.com/groups/vegmpremium")
        self.assertEqual(run["posts"], [])

    def test_timestamp_rejects_naive_false_and_infinite(self):
        for value in (False, float("inf"), "2026-11-07T10:00:00", "invalid", 0, -1):
            self.assertIsNone(timestamp(value))


class IdentityTests(unittest.TestCase):
    def test_same_id_matches_despite_urls(self):
        report = build_report(*paired([go_post(permalink="https://www.facebook.com/123")]))
        self.assertEqual(report["comparison"]["matched_posts"], 1)
        self.assertEqual(report["comparison"]["matched"][0]["match_by"], "post_id")

    def test_fallback_missing_id(self):
        report = build_report(*paired([go_post(None, permalink="https://m.facebook.com/groups/987/permalink/123/?ref=share")]))
        self.assertEqual(report["comparison"]["matched_posts"], 1)
        self.assertEqual(report["comparison"]["matched"][0]["match_by"], "canonical_permalink")

    def test_conflicting_explicit_ids_never_match_by_url(self):
        report = build_report(*paired([go_post("123")], [gql_post("456")]))
        self.assertEqual(report["comparison"]["matched_posts"], 0)
        self.assertEqual(report["comparison"]["only_facebook_go"], 1)
        self.assertEqual(report["comparison"]["only_fbgql"], 1)

    def test_duplicate_rows_do_not_inflate_match(self):
        report = build_report(*paired([go_post(None), go_post(), go_post()], [gql_post(), gql_post()]))
        self.assertEqual(report["facebook_go"]["total_posts"], 3)
        self.assertEqual(report["facebook_go"]["duplicate_rows"], 2)
        self.assertEqual(report["facebook_go"]["unique_posts"], 1)
        self.assertEqual(report["comparison"]["matched_posts"], 1)

    def test_ambiguous_url_not_arbitrarily_paired(self):
        report = build_report(*paired([go_post("123"), go_post("456")], [gql_post(None)]))
        self.assertEqual(report["comparison"]["matched_posts"], 0)
        self.assertEqual(len(report["comparison"]["ambiguous_facebook_go"]), 2)
        self.assertEqual(len(report["comparison"]["ambiguous_fbgql"]), 1)
        self.assertEqual(report["comparison"]["only_facebook_go"], 0)

    def test_unidentifiable_not_claimed_missing(self):
        report = build_report(*paired([go_post(None, permalink=None)], [gql_post(None, permalink=None)]))
        self.assertEqual(report["comparison"]["matched_posts"], 0)
        self.assertEqual(report["facebook_go"]["unidentifiable_posts"], 1)
        self.assertEqual(report["comparison"]["only_facebook_go"], 0)
        self.assertEqual(report["assessment"]["status"], "inconclusive")

    def test_foreign_host_or_group_not_canonical(self):
        for url in ("https://facebook.com.attacker.example/groups/987/posts/123", "https://example.com/groups/987/posts/123",
                    "https://www.facebook.com/groups/444/posts/123", "https://www.facebook.com@attacker.example/groups/987/posts/123"):
            self.assertIsNone(canonical_permalink(url, "987"))

    def test_mismatched_group_or_run_rejected(self):
        first, second = paired()
        for key, value in (("group_id", "444"), ("run_id", "previous-run")):
            altered = copy.deepcopy(second)
            altered[key] = value
            with self.assertRaises(ValueError):
                build_report(first, altered)


class ReportTests(unittest.TestCase):
    def test_noncomparable_metrics_are_not_ranked(self):
        for metadata in ({"source": "synthetic"}, {"source": "live", "status": "failed"}):
            first, second = paired(**metadata)
            first["duration_ms"], second["duration_ms"] = 1, 1000
            report = build_report(first, second)
            self.assertEqual(report["assessment"]["observed_sample_metrics"],
                             {"more_unique_identifiable_posts": None, "shorter_duration": None})

    def test_mixed_data_sources_rejected(self):
        first, second = paired(source="live")
        second["source"] = "synthetic"
        with self.assertRaises(ValueError):
            build_report(first, second)

    def test_synthetic_never_benchmark_winner(self):
        report = build_report(*paired())
        self.assertEqual(report["assessment"]["status"], "inconclusive")
        self.assertIsNone(report["assessment"]["recommended_collector"])
        self.assertEqual(report["assessment"]["stability"], "not_evaluated")
        self.assertIn("synthetic", markdown_report(report))

    def test_empty_has_null_completeness_not_perfect(self):
        report = build_report(*paired([], [], source="live"))
        self.assertIsNone(report["field_completeness"]["facebook_go"]["text"])
        self.assertEqual(report["assessment"]["status"], "inconclusive")
        self.assertIn("N/A", markdown_report(report))

    def test_failed_nonempty_is_not_success(self):
        report = build_report(*paired(source="live", status="partial", errors=[{"code": "rate_limited", "stage": "page", "message": "Stopped"}]))
        self.assertEqual(report["assessment"]["status"], "inconclusive")
        self.assertEqual(report["facebook_go"]["errors"], 1)
        self.assertIn("rate_limited", markdown_report(report))

    def test_controlled_live_observation_still_not_recommendation(self):
        report = build_report(*paired(source="live"))
        self.assertEqual(report["assessment"]["status"], "observed_single_run")
        self.assertIsNone(report["assessment"]["recommended_collector"])
        self.assertEqual(report["field_completeness"]["fbgql"]["author"], 0)
        self.assertEqual(report["field_completeness"]["facebook_go"]["author"], 1)

    def test_changed_settings_warn_and_make_inconclusive(self):
        first, second = paired(source="live")
        second["page_size"] = 10
        report = build_report(first, second)
        self.assertEqual(report["assessment"]["status"], "inconclusive")
        self.assertTrue(any("page_size" in note for note in report["notes"]))

    def test_pair_evidence_preserves_uncertainty(self):
        report = build_report(*paired([go_post(message="Pass Fender")]))
        pair = report["comparison"]["matched"][0]
        self.assertEqual(pair["possible_truncation_in"], "facebook-go")
        self.assertFalse(pair["text_equal"])
        self.assertIsNone(pair["image_url_sets_equal"])
        self.assertTrue(any("not proof" in note for note in report["notes"]))

    def test_same_unresolved_failed_group_report_inconclusive(self):
        first, second = paired([], [], group_id=None, group_input="https://www.facebook.com/groups/vegmpremium", status="failed", source="live")
        report = build_report(first, second)
        self.assertIsNone(report["group_id"])
        self.assertEqual(report["assessment"]["status"], "inconclusive")
        self.assertIn("vegmpremium", markdown_report(report))
        second["group_input"] = "https://www.facebook.com/groups/other"
        with self.assertRaises(ValueError):
            build_report(first, second)

    def test_output_atomic_private_and_cli_pipeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, source in zip(("facebook-go", "fbgql"), (raw(posts=[go_post()]), raw("fbgql", [gql_post()]))):
                path = root / "raw" / (name + ".json")
                write_json(path, source)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            command = [sys.executable, "-m", "normalizer", "--raw-dir", str(root / "raw"), "--output-dir", str(root / "normalized")]
            subprocess.run(command, check=True, capture_output=True)
            command = [sys.executable, "-m", "compare", "--input-dir", str(root / "normalized"), "--output-dir", str(root / "reports")]
            subprocess.run(command, check=True, capture_output=True)
            report = json.loads((root / "reports/comparison.json").read_text())
            self.assertEqual(report["comparison"]["matched_posts"], 1)
            self.assertTrue((root / "reports/comparison.md").exists())
            self.assertEqual((root / "reports/comparison.md").stat().st_mode & 0o777, 0o600)
            # A changed run ID cannot replace the previous report with a stale mix.
            path = root / "normalized/fbgql.json"
            content = json.loads(path.read_text())
            content["run_id"] = "stale"
            write_json(path, content)
            result = subprocess.run(command, capture_output=True)
            self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()

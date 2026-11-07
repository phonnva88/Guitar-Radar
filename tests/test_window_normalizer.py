"""Time-window and raw-media evidence checks, using synthetic offline records."""

import copy
import unittest

from compare import build_report, markdown_report
from normalizer import normalize_run


def window(**changes):
    result = {"from": "2026-11-07T18:00:00+07:00", "to": "2026-11-07T22:00:00+07:00",
              "complete": True, "stop_reason": "feed_exhausted", "scanned_posts": 3,
              "unknown_timestamp_posts": 0, "ordering_violations": 0}
    result.update(changes)
    return result


def envelope(collector="facebook-go", posts=None, **changes):
    result = {"schema_version": 1, "collector": collector, "group_id": "987", "run_id": "window-test",
              "started_at": "2026-11-07T16:00:00Z", "collected_at": "2026-11-07T16:00:02Z",
              "duration_ms": 2000, "pages_fetched": 2, "status": "success", "errors": [],
              "source": "live", "requested_pages": 50, "page_size": 20, "sort": "CHRONOLOGICAL",
              "library_commit": "library-pinned-commit", "git_commit": "app-commit", "window": window(),
              "posts": [] if posts is None else posts}
    result.update(changes)
    return result


def post(post_id="123", when="2026-11-07T12:00:00Z", **changes):
    result = {"id": post_id, "groupId": "987", "message": "SYNTHETIC caption",
              "authorId": "42", "authorName": "Synthetic Author", "createdAt": when,
              "permalink": f"https://www.facebook.com/groups/987/posts/{post_id}/", "attachments": []}
    result.update(changes)
    return result


def pair(**metadata):
    first = normalize_run(envelope(posts=[post()], **metadata))
    second = normalize_run(envelope("fbgql", [{"post_id": "123", "text": "SYNTHETIC caption",
                                              "created_time": 1794052800,
                                              "permalink": "https://www.facebook.com/groups/987/posts/123/"}], **metadata))
    return first, second


class TimeWindowNormalizerTests(unittest.TestCase):
    def test_inclusive_boundaries_offsets_and_fractional_seconds(self):
        records = [post("1", "2026-11-07T17:59:59.999999+07:00"),
                   post("2", "2026-11-07T18:00:00+07:00"),
                   post("3", "2026-11-07T15:00:00Z"),
                   post("4", "2026-11-07T15:00:00.000001Z")]
        raw = envelope(posts=records)
        before = copy.deepcopy(raw)
        normalized = normalize_run(raw)
        self.assertEqual(raw, before, "Normalization must not mutate preserved raw data")
        self.assertEqual([p["post_id"] for p in normalized["posts"]], ["2", "3"])
        self.assertEqual(normalized["window"]["from"], "2026-11-07T11:00:00Z")
        self.assertEqual(normalized["window"]["to"], "2026-11-07T15:00:00Z")
        self.assertEqual(normalized["window_filter"]["before_window_posts"], 1)
        self.assertEqual(normalized["window_filter"]["after_window_posts"], 1)
        self.assertEqual(normalized["window_filter"]["in_window_posts"], 2)
        self.assertEqual(normalized["window"]["scanned_posts"], 4)

    def test_unknown_timestamp_records_are_excluded_and_identified(self):
        run = normalize_run(envelope(posts=[post("1"), post("2", None), post("3", "0001-01-01T00:00:00Z")]))
        self.assertEqual([p["post_id"] for p in run["posts"]], ["1"])
        self.assertEqual(run["window"]["unknown_timestamp_posts"], 2)
        diagnostics = run["window_filter"]
        self.assertEqual(diagnostics["unknown_timestamp_posts"], 2)
        self.assertEqual([p["post_id"] for p in diagnostics["unknown_timestamp_references"]], ["2", "3"])
        self.assertEqual(run["status"], "success", "Membership uncertainty is recorded separately from parse errors")

    def test_invalid_windows_rejected_before_filtering(self):
        for value in ({"from": "2026-11-07T11:00:00Z"},
                      {"from": "2026-11-07T11:00:00", "to": "2026-11-07T15:00:00Z"},
                      window(**{"from": "2026-11-08T11:00:00Z"}),
                      window(complete="true"), window(unknown_timestamp_posts=-1),
                      window(ordering_violations=True), "not-an-object"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_run(envelope(window=value))

    def test_vetted_extensionless_media_preserves_evidence_and_dimensions(self):
        candidate = {"url": "https://cdn.example/media?id=7", "source": "attachment",
                     "path": "$.attachments[0].media.image", "width": 1080, "height": 1440}
        raw_post = post(image_candidates=[candidate, candidate], possible_type="photo_post",
                        type_evidence=["attachment.media.__typename=Photo"],
                        debug_file="data/debug/run/facebook-go/123.json", debug_response_saved=True)
        run = normalize_run(envelope(posts=[raw_post]))
        normalized = run["posts"][0]
        self.assertEqual(normalized["images"], [candidate["url"]])
        self.assertEqual(normalized["validation"]["image_candidates"], [candidate])
        self.assertEqual(normalized["validation"]["possible_type"], "photo_post")
        self.assertTrue(normalized["validation"]["debug_response_saved"])
        self.assertEqual(run["git_commit"], "app-commit")
        self.assertEqual(run["library_commit"], "library-pinned-commit")

    def test_fbgql_media_from_raw_does_not_invent_author(self):
        candidate = {"url": "https://cdn.example/content/opaque", "source": "media", "path": "$.media.photo_image",
                     "width": -1, "height": True}
        run = normalize_run(envelope("fbgql", [{"post_id": "123", "created_time": 1794052800,
                                                "text": "SYNTHETIC", "image_candidates": [candidate]}]))
        normalized = run["posts"][0]
        self.assertEqual(normalized["images"], [candidate["url"]])
        self.assertIsNone(normalized["author_id"])
        self.assertIsNone(normalized["author_name"])
        self.assertIsNone(normalized["validation"]["image_candidates"][0]["width"])
        self.assertIsNone(normalized["validation"]["image_candidates"][0]["height"])

    def test_explicit_empty_media_does_not_reintroduce_rejected_thumbnail(self):
        run = normalize_run(envelope(posts=[post(attachments=["https://cdn.example/video-thumb.jpg"], image_candidates=[])]))
        self.assertEqual(run["posts"][0]["images"], [])

    def test_legacy_attachment_fallback_remains_available(self):
        run = normalize_run(envelope(posts=[post(attachments=["https://cdn.example/image.jpg", "https://example.com/post"])]))
        self.assertEqual(run["posts"][0]["images"], ["https://cdn.example/image.jpg"])
        self.assertNotIn("validation", run["posts"][0])

    def test_malformed_media_isolated_and_counted(self):
        run = normalize_run(envelope(posts=[post("1", image_candidates="broken"), post("2")]))
        self.assertEqual([p["post_id"] for p in run["posts"]], ["2"])
        self.assertEqual(run["window_filter"]["invalid_posts"], 1)
        self.assertEqual(run["status"], "partial")
        self.assertEqual(run["errors"][0]["code"], "parse_error")


class WindowComparisonTests(unittest.TestCase):
    def test_equal_time_windows_allow_different_page_limits_and_sizes(self):
        first, second = pair()
        second["requested_pages"], second["page_size"] = 100, 10
        second["window"]["from"] = "2026-11-07T18:00:00+07:00"
        second["window"]["to"] = "2026-11-07T22:00:00+07:00"
        report = build_report(first, second)
        self.assertEqual(report["assessment"]["status"], "observed_single_run")
        self.assertEqual(report["comparison"]["matched_posts"], 1)
        self.assertEqual(report["window"], {"from": "2026-11-07T11:00:00Z", "to": "2026-11-07T15:00:00Z"})
        self.assertIn("Shared time window", markdown_report(report))

    def test_missing_or_different_window_is_rejected(self):
        first, second = pair()
        for changed in (None, window(**{"to": "2026-11-07T23:00:00+07:00"})):
            second["window"] = changed
            with self.subTest(window=changed), self.assertRaises(ValueError):
                build_report(first, second)

    def test_page_limit_unknowns_and_ordering_suppress_rankings(self):
        for changes in ({"complete": False, "stop_reason": "page_limit"},
                        {"stop_reason": "error"}, {"unknown_timestamp_posts": 1}, {"ordering_violations": 1}):
            first, second = pair()
            second["window"].update(changes)
            report = build_report(first, second)
            self.assertEqual(report["assessment"]["status"], "inconclusive")
            self.assertTrue(all(value is None for value in report["assessment"]["observed_sample_metrics"].values()))

    def test_valid_boundary_stop_is_comparable(self):
        first, second = pair()
        second["window"]["stop_reason"] = "older_than_from"
        report = build_report(first, second)
        self.assertEqual(report["assessment"]["status"], "observed_single_run")

    def test_window_sort_mismatch_still_inconclusive(self):
        first, second = pair()
        second["sort"] = "TOP_POSTS"
        self.assertEqual(build_report(first, second)["assessment"]["status"], "inconclusive")

    def test_external_normalized_outside_window_or_undated_rejected(self):
        for when in ("2026-11-08T00:00:00Z", None):
            first, second = pair()
            second["posts"][0]["posted_at"] = when
            with self.subTest(timestamp=when), self.assertRaises(ValueError):
                build_report(first, second)

    def test_raw_rows_and_sample_counts_are_distinct(self):
        first = normalize_run(envelope(posts=[post("1"), post("2", "2026-11-07T10:59:59Z")]))
        second = normalize_run(envelope("fbgql", [{"post_id": "1", "created_time": 1794052800}]))
        report = build_report(first, second)
        self.assertEqual(report["facebook_go"]["rows_collected"], 2)
        self.assertEqual(report["facebook_go"]["total_posts"], 1)
        self.assertEqual(report["facebook_go"]["identifiable_posts"], 1)
        self.assertEqual(report["facebook_go"]["window_filter"]["before_window_posts"], 1)


if __name__ == "__main__":
    unittest.main()

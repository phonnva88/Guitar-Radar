"""Synthetic reports exercise evidence gates; they do not benchmark Facebook."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from validation.reports import build_stability, write_reports


START = "2026-11-07T10:00:00Z"
END = "2026-11-07T12:00:00Z"
KNOWN = [{"post_id": "1001", "url": "https://www.facebook.com/groups/987/posts/1001/", "expected_has_images": True},
         {"post_id": "1002", "url": "https://www.facebook.com/groups/987/posts/1002/", "expected_has_images": False}]


def post(pid="1001", collector="facebook-go", **changes):
    value = {"collector": collector, "post_id": pid, "group_id": "987", "group_name": None,
             "author_id": "user-id" if collector == "facebook-go" else None,
             "author_name": "Synthetic Test Author" if collector == "facebook-go" else None,
             "text": "Synthetic example " + str(pid),
             "url": "https://www.facebook.com/groups/987/posts/" + str(pid) + "/",
             "images": ["https://example.invalid/photo.jpg"] if pid == "1001" else [],
             "posted_at": "2026-11-07T11:00:00Z", "collected_at": END,
             "validation": {"possible_type": "photo_post" if pid == "1001" else "normal_post"}}
    value.update(changes)
    return value


def run(rid="one", source="live"):
    envelopes = {}
    for collector in ("facebook-go", "fbgql"):
        posts = [post(collector=collector), post("1002", collector)]
        envelopes[collector] = {"schema_version": 1, "collector": collector, "group_id": "987",
            "run_id": rid, "source": source, "started_at": END, "collected_at": END,
            "duration_ms": 100 if collector == "facebook-go" else 200, "pages_fetched": 3,
            "status": "success", "errors": [], "posts": posts,
            "library_commit": "test-library-sha",
            "normalization": {"input_posts": 3},
            "window": {"from": START, "to": END, "complete": True, "stop_reason": "older_than_from",
                       "scanned_posts": 3, "unknown_timestamp_posts": 0, "ordering_violations": 0}}
    return {"metadata": {"run_id": rid, "group_id": "987", "from": START, "to": END,
                         "started_at": END, "source": source, "git_commit": "test-sha",
                         "facebook_go_version": "test-library-sha", "fbgql_version": "test-library-sha"},
            "collectors": envelopes, "diagnostics": {"facebook-go": [], "fbgql": []}}


def suite(source="live"):
    return [run(str(i), source) for i in range(3)]


class ValidationReportTests(unittest.TestCase):
    def test_complete_live_samples_can_support_go_without_hardcoding(self):
        result = build_stability(suite(), KNOWN)
        self.assertEqual(result["facebook_go"]["success_rate"], 1.0)
        self.assertEqual(result["facebook_go"]["avg_duration_ms"], 100)
        self.assertEqual(result["facebook_go"]["avg_posts"], 2)
        self.assertEqual(result["facebook_go"]["images"]["image_detection_recall"], 1.0)
        self.assertEqual(result["recommendation"]["primary"], "facebook-go")
        self.assertEqual(result["recommendation"]["fallback"], "fbgql")
        self.assertEqual(result["recommendation"]["phase1_status"], "READY FOR PHASE 1")
        self.assertEqual(set(result["answers"]), set("ABCDEF"))
        self.assertIn("author data was absent", result["answers"]["D"]["answer"])
        self.assertEqual(result["answers"]["D"]["evidence"]["additional_posts_in_sample"], 0)
        self.assertEqual(result["facebook_go"]["per_run"][0]["rows_collected"], 3)
        self.assertEqual(result["facebook_go"]["per_run"][0]["in_window_posts"], 2)

    def test_another_collector_can_be_primary(self):
        runs = suite()
        for item in runs:
            for p in item["collectors"]["fbgql"]["posts"]:
                p["author_name"], p["author_id"] = "Synthetic", "42"
            item["collectors"]["facebook-go"]["duration_ms"] = 300
        result = build_stability(runs, KNOWN)
        self.assertEqual(result["recommendation"]["primary"], "fbgql")

    def test_synthetic_durations_are_not_measured_and_no_readiness(self):
        result = build_stability(suite("synthetic"), KNOWN)
        self.assertIsNone(result["facebook_go"]["avg_duration_ms"])
        self.assertIsNone(result["facebook_go"]["avg_observed_union_coverage"])
        self.assertIsNone(result["recommendation"]["primary"])
        self.assertIsNone(result["recommendation"]["primary_candidate"])
        self.assertEqual(result["recommendation"]["phase1_status"], "NOT READY FOR PHASE 1")

    def test_failed_run_is_not_a_fast_success(self):
        runs = suite()
        failed = runs[1]["collectors"]["facebook-go"]
        failed.update(status="failed", errors=[{"code": "checkpoint_required"}], duration_ms=1, posts=[])
        result = build_stability(runs, KNOWN)
        self.assertAlmostEqual(result["facebook_go"]["success_rate"], 2 / 3)
        self.assertEqual(result["facebook_go"]["avg_duration_ms"], 100)
        self.assertEqual(result["facebook_go"]["errors"], 1)
        self.assertIsNone(result["recommendation"]["primary"])

    def test_missing_known_posts_differ_from_extraction_failures(self):
        runs = suite()
        for item in runs:
            item["collectors"]["facebook-go"]["posts"][0]["images"] = []
        result = build_stability(runs, KNOWN + [{"post_id": "1003", "url": "https://www.facebook.com/groups/987/posts/1003/",
                                                 "verified_posted_at": "2026-11-07T11:00:00Z", "expected_has_images": True}])
        observed = result["facebook_go"]["per_run"][0]["image_validation"]
        self.assertEqual(observed["extraction_failures"], ["1001"])
        self.assertEqual(observed["missing_known_image_posts"], ["1003"])
        self.assertEqual(observed["image_detection_recall"], 0)
        self.assertEqual(observed["observation_recall"], 0.5)
        self.assertIsNone(result["recommendation"]["primary"])

    def test_no_ground_truth_cannot_pass_media_gate(self):
        result = build_stability(suite(), [])
        self.assertIsNone(result["facebook_go"]["images"]["image_detection_recall"])
        self.assertTrue(any("No user-confirmed" in blocker for blocker in result["health"]["facebook_go"]["blockers"]))
        self.assertIsNone(result["recommendation"]["primary"])

    def test_unresolved_share_links_count_missing_not_fake_ids(self):
        known = [{"url": "https://www.facebook.com/share/p/OpaqueExample/?access_token=DO_NOT_SAVE",
                  "unresolved": True, "expected_has_images": True}]
        result = build_stability(suite(), known)
        image = result["facebook_go"]["per_run"][0]["image_validation"]
        self.assertEqual(image["known_image_posts"], 1)
        self.assertEqual(image["unresolved_posts"], ["https://www.facebook.com/share/p/OpaqueExample/"])
        self.assertEqual(image["missing_known_image_posts"], [])
        self.assertIsNone(image["image_detection_recall"])
        self.assertNotIn("DO_NOT_SAVE", json.dumps(result))
        self.assertIsNone(result["recommendation"]["primary"])

    def test_fixture_scope_does_not_count_wrong_group_or_post_id_only(self):
        known = [
            {"post_id": "1001", "url": "https://www.facebook.com/groups/555/posts/1001/", "expected_has_images": True},
            {"post_id": "1002", "expected_has_images": True},
        ]
        result = build_stability(suite(), known)
        image = result["facebook_go"]["images"]
        self.assertIsNone(image["image_detection_recall"])
        self.assertEqual(image["expected_image_observations"], 0)
        self.assertEqual(image["wrong_group_observations"], 3)
        self.assertEqual(image["group_unverified_observations"], 3)
        self.assertEqual(image["extraction_failures"], 0)
        self.assertTrue(any("different numeric group" in text for text in result["health"]["facebook_go"]["blockers"]))
        self.assertEqual(result["recommendation"]["phase1_status"], "NOT READY FOR PHASE 1")

    def test_window_scope_categories_and_zero_eligible_recall(self):
        known = [
            {"post_id": "1001", "url": "https://www.facebook.com/groups/987/posts/1001/", "verified_posted_at": "2026-11-07T09:00:00Z", "expected_has_images": True},
            {"post_id": "9999", "url": "https://www.facebook.com/groups/987/posts/9999/", "expected_has_images": True},
        ]
        result = build_stability(suite(), known)
        image = result["facebook_go"]["per_run"][0]["image_validation"]
        self.assertEqual(image["fixture_scope_counts"]["outside_window_verified"], 1)
        self.assertEqual(image["fixture_scope_counts"]["timestamp_unknown"], 1)
        self.assertEqual(image["fixture_scope_counts"]["not_observed"], 0)
        self.assertEqual(image["eligible_known_image_posts"], 0)
        self.assertIsNone(result["facebook_go"]["images"]["image_detection_recall"])

    def test_text_only_post_images_are_a_false_positive_to_review(self):
        runs = suite()
        for item in runs:
            item["collectors"]["facebook-go"]["posts"][1]["images"] = ["https://example.invalid/avatar.jpg"]
        result = build_stability(runs, KNOWN)
        self.assertEqual(result["facebook_go"]["images"]["unexpected_image_observations"], 3)
        self.assertIsNone(result["recommendation"]["primary"])

    def test_set_and_field_changes_are_reported_without_claiming_a_bug(self):
        runs = suite()
        changed = runs[2]["collectors"]["facebook-go"]["posts"]
        changed[0]["text"] += " edited"
        changed[0]["images"] = ["https://example.invalid/a.jpg", "https://example.invalid/b.jpg"]
        changed.pop()
        result = build_stability(runs, KNOWN)
        transition = result["consistency"]["facebook_go"]["transitions"][1]
        self.assertEqual(transition["removed"], ["id:1002"])
        self.assertEqual(transition["changed_posts"][0]["changed_fields"], ["text", "images_count"])
        self.assertEqual(transition["field_change_counts"]["images_count"], 1)
        self.assertEqual(result["consistency"]["facebook_go"]["status"], "changes_observed")
        self.assertIsNone(result["recommendation"]["primary"])
        self.assertIn("not automatically collector bugs", result["consistency"]["facebook_go"]["interpretation"])

    def test_collector_only_evidence_is_derived_even_without_diagnostics(self):
        runs = suite()
        for item in runs:
            item["collectors"]["facebook-go"]["posts"].pop()
        result = build_stability(runs, KNOWN)
        missing = result["collector_only"]["facebook_go"]
        self.assertEqual(missing["unresolved_cases"], 3)
        self.assertEqual(missing["repeated_missing_identities"][0]["identity"], "1002")
        self.assertEqual(result["facebook_go"]["avg_observed_union_coverage"], 0.5)
        self.assertEqual(result["fbgql"]["avg_observed_union_coverage"], 1.0)
        self.assertIsNone(result["recommendation"]["primary"])

    def test_conflicting_duplicates_are_not_arbitrarily_paired(self):
        runs = suite()
        for item in runs:
            item["collectors"]["facebook-go"]["posts"].append(post(text="Conflicting observation"))
        result = build_stability(runs, KNOWN)
        metrics = result["facebook_go"]
        self.assertEqual(metrics["duplicate_rows"], 3)
        self.assertEqual(metrics["ambiguous_posts"], 3)
        self.assertEqual(metrics["per_run"][0]["identifiable_posts"], 1)
        self.assertEqual(result["controlled_paired_runs"], 0)
        self.assertIsNone(result["recommendation"]["primary"])

    def test_exact_duplicate_rows_are_counted_but_not_inflated(self):
        runs = suite()
        for item in runs:
            item["collectors"]["facebook-go"]["posts"].append(copy.deepcopy(item["collectors"]["facebook-go"]["posts"][0]))
        result = build_stability(runs, KNOWN)
        self.assertEqual(result["facebook_go"]["duplicate_rows"], 3)
        self.assertEqual(result["facebook_go"]["per_run"][0]["identifiable_posts"], 2)
        self.assertEqual(result["matching"][0]["matched"], 2)

    def test_incomplete_or_uncertain_window_is_not_controlled(self):
        for changes in ({"complete": False}, {"unknown_timestamp_posts": 1}, {"ordering_violations": 1}, {"stop_reason": "page_limit"}):
            with self.subTest(changes=changes):
                runs = suite()
                for item in runs:
                    item["collectors"]["facebook-go"]["window"].update(changes)
                result = build_stability(runs, KNOWN)
                self.assertEqual(result["controlled_paired_runs"], 0)
                self.assertIsNone(result["recommendation"]["primary"])

    def test_incompatible_groups_windows_runids_and_sources_rejected(self):
        for mutation in ("group", "window", "run_id", "source"):
            with self.subTest(mutation=mutation):
                runs = suite()
                if mutation == "group":
                    runs[1]["metadata"]["group_id"] = "other"
                elif mutation == "window":
                    runs[1]["metadata"]["from"] = "2026-11-06T10:00:00Z"
                elif mutation == "run_id":
                    runs[1]["metadata"]["run_id"] = runs[0]["metadata"]["run_id"]
                else:
                    runs[1]["metadata"]["source"] = "synthetic"
                with self.assertRaises(ValueError):
                    build_stability(runs, KNOWN)

    def test_fractional_time_bounds_use_actual_chronology(self):
        runs = [run()]
        for value in (runs[0]["metadata"], *(envelope["window"] for envelope in runs[0]["collectors"].values())):
            value["from"], value["to"] = "2026-11-07T11:00:00.500000Z", "2026-11-07T11:00:00Z"
        with self.assertRaises(ValueError):
            build_stability(runs, KNOWN)
        runs = [run()]
        for value in (runs[0]["metadata"], *(envelope["window"] for envelope in runs[0]["collectors"].values())):
            value["from"], value["to"] = "2026-11-07T11:00:00Z", "2026-11-07T11:00:00.500000Z"
        result = build_stability(runs, KNOWN)
        self.assertEqual(result["facebook_go"]["runs"], 1)

    def test_unresolved_failed_suites_create_a_blocked_health_report(self):
        runs = [run()]
        runs[0]["metadata"].update(group_id=None, group_input="https://www.facebook.com/groups/vegmpremium")
        for envelope in runs[0]["collectors"].values():
            envelope.update(group_id=None, group_input="https://www.facebook.com/groups/vegmpremium", status="failed", posts=[], errors=[{"code": "authentication_failed"}])
        result = build_stability(runs, KNOWN)
        self.assertIsNone(result["group_id"])
        self.assertEqual(result["recommendation"]["phase1_status"], "NOT READY FOR PHASE 1")
        self.assertEqual(result["facebook_go"]["success_rate"], 0)

    def test_empty_suite_remains_unknown(self):
        result = build_stability([], [])
        self.assertEqual(result["facebook_go"]["runs"], 0)
        self.assertIsNone(result["facebook_go"]["success_rate"])
        self.assertIsNone(result["recommendation"]["primary"])

    def test_all_requested_artifacts_are_private_and_json_serializable(self):
        result = build_stability(suite("synthetic"), KNOWN)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_reports(result, root)
            expected = {"stability.json", "stability.md", "consistency.json", "consistency.md", "collector-health.md", "final-report.md"}
            self.assertEqual({path.name for path in root.iterdir()}, expected)
            for path in root.iterdir():
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            data = json.loads((root / "stability.json").read_text())
            self.assertIsNone(data["recommendation"]["primary"])
            self.assertIn("NOT READY FOR PHASE 1", (root / "collector-health.md").read_text())
            self.assertIn("synthetic", (root / "stability.md").read_text())


if __name__ == "__main__":
    unittest.main()

"""Resolver regressions use mocked HTML only; no Facebook requests are made."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from validation import suite
from validation.ground_truth import numeric_group_from_url


class FixtureResolutionTests(unittest.TestCase):
    def resolve(self, html, *, expected=True, source="https://www.facebook.com/share/p/FixtureExample/"):
        response = MagicMock()
        response.status_code, response.headers = 200, {}
        response.__enter__.return_value = response
        response.iter_content.return_value = [html.encode()]
        known = [{"post_id": None, "url": source, "unresolved": True,
                  "expected_has_images": expected}]
        with patch.dict(os.environ, {"FACEBOOK_COOKIE": "c_user=12; xs=RESOLUTION_TEST_SESSION"}, clear=True), \
             patch("requests.Session.request", return_value=response) as request, \
             patch.object(suite.time, "sleep"):
            result, stop = suite.resolve_known(known)
        self.assertIsNone(stop)
        request.assert_called_once()
        self.assertIsNone(known[0]["post_id"])
        self.assertTrue(known[0]["unresolved"])
        return result[0]

    def test_numeric_group_permalink_is_preferred_over_alias_or_profile_url(self):
        numeric = "https://www.facebook.com/groups/9/permalink/123/"
        alternatives = ["https://www.facebook.com/groups/examplealias/posts/123/",
                        "https://www.facebook.com/exampleprofile/posts/123/"]
        for alternative in alternatives:
            for first, second in ((numeric, alternative), (alternative, numeric)):
                with self.subTest(first=first, second=second):
                    html = (f'<link rel="canonical" href="{first}">'
                            f'<meta property="og:url" content="{second}">')
                    result = self.resolve(html)
                    self.assertEqual(result["post_id"], "123")
                    self.assertFalse(result["unresolved"])
                    self.assertEqual(numeric_group_from_url(result["url"]), "9")
                    self.assertEqual(result["url"], numeric)

    def test_same_post_id_with_conflicting_numeric_groups_is_rejected(self):
        html = ('<link rel="canonical" href="https://www.facebook.com/groups/9/posts/123/">'
                '<meta property="og:url" content="https://www.facebook.com/groups/77/permalink/123/">')
        result = self.resolve(html)
        self.assertIsNone(result["post_id"])
        self.assertTrue(result["unresolved"])
        self.assertEqual(result["resolution_error"], "ambiguous_canonical_group")

    def test_different_canonical_post_ids_are_rejected(self):
        html = ('<link rel="canonical" href="https://www.facebook.com/groups/9/posts/123/">'
                '<meta property="og:url" content="https://www.facebook.com/groups/9/posts/456/">')
        result = self.resolve(html)
        self.assertIsNone(result["post_id"])
        self.assertTrue(result["unresolved"])
        self.assertEqual(result["resolution_error"], "ambiguous_or_missing_canonical_post")

    def test_resolution_and_reload_preserve_user_expectation_time_and_safe_source(self):
        source = "https://www.facebook.com/share/p/FixtureExample/"
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "known.json"
            fixture = [{"url": source + "?access_token=DO_NOT_SAVE#secret", "expected_has_images": False,
                        "verified_posted_at": "2026-11-07T18:00:00+07:00"}]
            path.write_text(json.dumps(fixture))
            known = suite.known_posts(path)
            original = copy.deepcopy(known)
            response = MagicMock()
            response.status_code, response.headers = 200, {}
            response.__enter__.return_value = response
            response.iter_content.return_value = [b'<link rel="canonical" href="https://www.facebook.com/groups/9/posts/123/?access_token=DO_NOT_SAVE">']
            with patch.dict(os.environ, {"FACEBOOK_COOKIE": "c_user=12; xs=RESOLUTION_TEST_SESSION"}, clear=True), \
                 patch("requests.Session.request", return_value=response), patch.object(suite.time, "sleep"):
                result, stop = suite.resolve_known(known)
            self.assertIsNone(stop)
            self.assertEqual(known, original)
            self.assertFalse(result[0]["expected_has_images"])
            self.assertEqual(result[0]["source_url"], source)
            self.assertEqual(result[0]["url"], "https://www.facebook.com/groups/9/posts/123/")
            self.assertEqual(result[0]["verified_posted_at"], "2026-11-07T11:00:00Z")
            self.assertNotIn("DO_NOT_SAVE", json.dumps(result))
            path.write_text(json.dumps(result))
            reloaded = suite.known_posts(path)
            self.assertFalse(reloaded[0]["expected_has_images"])
            self.assertEqual(reloaded[0]["source_url"], source)
            self.assertEqual(reloaded[0]["verified_posted_at"], "2026-11-07T11:00:00Z")


if __name__ == "__main__":
    unittest.main()

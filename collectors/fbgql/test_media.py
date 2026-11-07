"""Synthetic media evidence, sanitation and bounded-window regression cases."""
import contextlib
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import test_collector as fixtures
from validation.media import classify_post, extract_images, sanitize

collector, FakeTransport, page, story = fixtures.collector, fixtures.FakeTransport, fixtures.page, fixtures.story


def photo(url="https://cdn.example.invalid/image-no-extension?oh=signed&oe=123", width=1080, height=1440):
    return {"__typename": "Photo", "image": {"uri": url, "width": width, "height": height}}


class MediaTests(unittest.TestCase):
    def test_nested_carousel_and_extensionless_image_have_path_evidence(self):
        value = {"comet_sections": {"content": {"story": {"attachments": [{"subattachments": {"nodes": [
            {"media": photo()}, {"media": photo("https://cdn.example.invalid/second", 800, 600)}]}}]}}}}
        images = extract_images(value)
        self.assertEqual(len(images), 2)
        self.assertEqual((images[0]["width"], images[0]["height"]), (1080, 1440))
        self.assertEqual(images[0]["source"], "attachment")
        self.assertIn("subattachments.nodes[0].media.image.uri", images[0]["path"])
        self.assertEqual(classify_post(value)[0], "photo_post")

    def test_avatar_link_video_and_unrelated_images_are_excluded(self):
        value = {"actors": [{"profile_picture": {"uri": "https://cdn.example.invalid/avatar.jpg"}}],
                 "attachments": [{"media": {"__typename": "Video", "image": {"uri": "https://cdn.example.invalid/thumbnail.jpg"}}},
                                 {"target": {"__typename": "ExternalUrl"}, "image": {"uri": "https://cdn.example.invalid/link.jpg"}}],
                 "recommendations": [{"attachments": [{"media": photo()}]}],
                 "attached_story": {"attachments": [{"media": photo()}]},
                 "shared_story": {"attachments": [{"media": photo()}]},
                 "url": "https://cdn.example.invalid/arbitrary.jpg"}
        self.assertEqual(extract_images(value), [])
        self.assertEqual(classify_post(value)[0], "shared_post")
        self.assertEqual(classify_post({"attachments": [{"media": {"__typename": "Video"}}]})[0], "video")

    def test_external_style_list_photo_is_link_preview(self):
        self.assertEqual(extract_images({"attachments": [{"style_list": ["external"], "media": photo()}]}), [])

    def test_unknown_caption_is_not_a_photo_or_type_assertion(self):
        value = {"__typename": "Story", "message": {"text": "new guitar video photo marketplace reel"}}
        self.assertEqual(extract_images(value), [])
        self.assertEqual(classify_post(value), ("unknown", []))

    def test_urls_and_dimensions_are_validated_and_deduplicated(self):
        value = {"photos": [photo("javascript:evil", -1, True), photo(), photo(), photo("https://user:secret@cdn.example.invalid/photo")]}
        self.assertEqual(len(extract_images(value)), 1)

    def test_explicit_anonymous_and_shared_markers_classify(self):
        self.assertEqual(classify_post({"actors": [{"__typename": "GroupAnonAuthorProfile"}]})[0], "anonymous_post")
        self.assertEqual(classify_post({"attached_story": {"__typename": "Story"}})[0], "shared_post")

    def test_sanitizer_removes_keys_query_and_known_echoes_without_erasing_ids(self):
        value = {"post_id": "1001", "author_id": "123456789", "__typename": "Story", "xs": "SESSION_SECRET",
                 "cookies": {"c_user": "123456789"}, "authorization": "Bearer other-token", "extensions": {"token": "ignored"},
                 "headers": {"x-fb-lsd": "HEADER_LEAK"}, "request": {"x-api-key": "API_LEAK"}, "csrf": "CSRF_LEAK", "encrypted_tracking": "ENC_LEAK",
                 "message": {"text": "Echo SESSION_SECRET and fb_dtsg=opaque"},
                 "url": "https://cdn.example.invalid/photo?oh=a%2Fb%3D&oe=123&stp=abc&access_token=SECRET&xs=SESSION_SECRET#x"}
        result = sanitize(value, secrets=["SESSION_SECRET", "1", "123456789"])
        encoded = json.dumps(result)
        self.assertNotIn("SESSION_SECRET", encoded)
        self.assertNotIn("opaque", encoded)
        self.assertNotIn("access_token", encoded)
        self.assertNotIn("authorization", result)
        self.assertNotIn("cookies", result)
        self.assertNotIn("LEAK", encoded)
        self.assertEqual(result["post_id"], "1001")
        self.assertEqual(result["author_id"], "123456789")
        self.assertEqual(result["__typename"], "Story")
        self.assertEqual(result["url"], "https://cdn.example.invalid/photo?oh=a%2Fb%3D&oe=123&stp=abc")


class WindowTests(unittest.TestCase):
    """Reuse fixture harness, without loading the Phase0 cases twice."""
    def setUp(self):
        fixtures.CollectorTests.setUp(self)
        self.args.from_time, self.args.to_time = "2026-05-28T20:26:40Z", "2026-05-28T20:30:00Z"
        self.lower = datetime.fromisoformat(self.args.from_time.replace("Z", "+00:00")).timestamp()

    def run_collect(self):
        return fixtures.CollectorTests.run_collect(self)

    def dated(self, post_id, timestamp):
        result = story(post_id)
        result["creation_time"] = timestamp
        return result

    def test_window_retains_raw_outside_bounds_and_crosses_older_page(self):
        self.args.pages = "4"
        FakeTransport.responses = [page([self.dated("11", self.lower + 5), self.dated("12", self.lower)], cursor="one", has_next=True),
                                   page([self.dated("13", self.lower - 1)], cursor="two", has_next=True)]
        result = self.run_collect()
        self.assertEqual(len(result["posts"]), 3)
        self.assertTrue(result["window"]["complete"])
        self.assertEqual(result["window"]["stop_reason"], "older_than_from")
        self.assertEqual(result["window"]["scanned_posts"], 3)
        self.assertEqual(len(FakeTransport.instances[0].forms), 2)

    def test_page_cap_is_not_complete(self):
        self.args.pages = "1"
        FakeTransport.responses = [page([self.dated("11", self.lower)], cursor="more", has_next=True)]
        result = self.run_collect()
        self.assertEqual(result["status"], "success")
        self.assertFalse(result["window"]["complete"])
        self.assertEqual(result["window"]["stop_reason"], "page_limit")

    def test_unknown_timestamp_is_preserved_but_window_not_complete(self):
        FakeTransport.responses = [page([self.dated("11", None)])]
        result = self.run_collect()
        self.assertEqual(len(result["posts"]), 1)
        self.assertFalse(result["window"]["complete"])
        self.assertEqual(result["window"]["unknown_timestamp_posts"], 1)

    def test_older_pinned_post_does_not_stop_before_newer_story(self):
        self.args.pages = "3"
        FakeTransport.responses = [page([self.dated("11", self.lower - 10), self.dated("12", self.lower + 5)], cursor="more", has_next=True),
                                   page([self.dated("13", self.lower - 20)])]
        result = self.run_collect()
        self.assertEqual(result["pages_fetched"], 2)
        self.assertEqual(result["window"]["ordering_violations"], 1)
        self.assertFalse(result["window"]["complete"])

    def test_naive_or_one_sided_window_is_configuration_error(self):
        self.args.from_time = "2026-05-28T20:26:40"
        self.assertEqual(self.run_collect()["errors"][0]["code"], "configuration_error")
        self.assertFalse(FakeTransport.instances)

    def test_deferred_stream_page_info_and_nested_post_scope(self):
        outer = self.dated("11", self.lower)
        outer["attached_story"] = self.dated("999", self.lower - 999)
        first = json.loads(page([outer]))
        del first["data"]["node"]["group_feed"]["page_info"]
        FakeTransport.responses = [json.dumps(first) + "\n" + json.dumps({"path": ["node", "group_feed", "edges", 1], "data": {"node": self.dated("12", self.lower - 1)}}) + "\n" +
                                   json.dumps({"path": ["node", "group_feed"], "data": {"page_info": {"has_next_page": False, "end_cursor": None}}})]
        result = self.run_collect()
        self.assertEqual([post["post_id"] for post in result["posts"]], ["11", "12"])
        self.assertTrue(result["window"]["complete"])

    def test_debug_is_private_sanitized_real_story_and_duplicate_not_overwritten(self):
        item = self.dated("11", self.lower)
        item.update(attachments=[{"media": photo()}], fb_dtsg="should-not-persist")
        item["message"]["text"] = "caption echoes SECRET_TEST_SESSION"
        with tempfile.TemporaryDirectory() as directory:
            self.args.debug_dir = directory
            FakeTransport.responses = [page([item, item])]
            result = self.run_collect()
            files = sorted(Path(directory).glob("*.json"))
            self.assertEqual([file.name for file in files], ["11-2.json", "11.json"])
            for file in files:
                self.assertEqual(file.stat().st_mode & 0o777, 0o600)
                text = file.read_text()
                self.assertNotIn("SECRET_TEST_SESSION", text)
                self.assertNotIn("should-not-persist", text)
                value = json.loads(text)
                self.assertIn("attachments", value["payload"])
            self.assertEqual(len(result["posts"]), 2)
            self.assertTrue(all(post["debug_response_saved"] for post in result["posts"]))
            self.assertEqual(result["posts"][0]["image_candidates"][0]["width"], 1080)

    def test_debug_evidence_survives_model_parse_failure(self):
        bad = {"post_id": "bad", "comet_sections": 7, "attachments": [{"media": photo()}]}
        with tempfile.TemporaryDirectory() as directory:
            self.args.debug_dir = directory
            FakeTransport.responses = [page([bad])]
            result = self.run_collect()
            self.assertEqual(result["status"], "failed")
            self.assertEqual(len(result["unparsed_debug_files"]), 1)
            value = json.loads(Path(result["unparsed_debug_files"][0]).read_text())
            self.assertEqual(len(value["image_candidates"]), 1)


if __name__ == "__main__":
    unittest.main()

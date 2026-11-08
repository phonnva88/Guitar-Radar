"""Offline collector tests against the real pinned fbgql parser and payload builder."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("phase0_fbgql", Path(__file__).with_name("main.py"))
collector = importlib.util.module_from_spec(spec)
spec.loader.exec_module(collector)


def page(stories, *, cursor=None, has_next=False):
    return json.dumps({"data": {"node": {"group_feed": {
        "edges": [{"node": story} for story in stories],
        "page_info": {"end_cursor": cursor, "has_next_page": has_next},
    }}}})


def story(post_id):
    return {"post_id": post_id, "message": {"text": "Pass Fender Japan"}, "creation_time": 1780000000,
            "url": f"https://www.facebook.com/groups/123/posts/{post_id}"}


class FakeTransport:
    responses = []
    instances = []

    def __init__(self, timeout):
        self.forms = []
        self.closed = False
        self.responses = iter(type(self).responses)
        type(self).instances.append(self)

    def get(self, url, cookies, proxy=None, headers=None):
        return '"DTSGInitialData",[],{"token":"not-a-real-token"}'

    def post_form(self, headers, data, cookies, proxy=None):
        self.forms.append(data)
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class FakeResponse:
    def __init__(self, status=200, body="", headers=None):
        self.status_code, self.body, self.headers = status, body, headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield self.body.encode()


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.args = argparse.Namespace(group="123", pages="2", page_size="20", timeout="10", run_id="test-run")
        self.env = {"FACEBOOK_COOKIE": "c_user=456; xs=SECRET_TEST_SESSION"}
        FakeTransport.instances = []
        FakeTransport.responses = []

    def run_collect(self):
        with contextlib.redirect_stderr(io.StringIO()) as logs, patch.object(collector.time, "sleep"):
            result = collector.collect(self.args, self.env, FakeTransport)
        self.assertNotIn("SECRET_TEST_SESSION", logs.getvalue())
        self.assertNotIn("SECRET_TEST_SESSION", json.dumps(result))
        return result

    def test_real_parser_two_pages_and_chronological_payload(self):
        FakeTransport.responses = [page([story("11")], cursor="next", has_next=True), page([story("22"), story("11")])]
        result = self.run_collect()
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["pages_fetched"], 2)
        self.assertEqual([p["post_id"] for p in result["posts"]], ["11", "22", "11"])
        self.assertEqual(result["posts"][0]["text"], "Pass Fender Japan")
        self.assertIsNone(result["posts"][0]["page_name"])
        transport = FakeTransport.instances[0]
        variables = [json.loads(form["variables"]) for form in transport.forms]
        self.assertEqual([v["cursor"] for v in variables], [None, "next"])
        self.assertTrue(all(v["sortingSetting"] == "CHRONOLOGICAL" and v["count"] == 20 for v in variables))
        self.assertTrue(transport.closed)

    def test_partial_posts_survive_network_failure(self):
        FakeTransport.responses = [page([story("11")], cursor="next", has_next=True), collector.CollectionError("network_error")]
        result = self.run_collect()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["posts"]), 1)
        self.assertEqual(result["errors"][0]["code"], "network_error")
        self.assertEqual(result["errors"][0]["page"], 2)

    def test_bad_post_does_not_discard_other_posts(self):
        bad = {"post_id": "bad", "comet_sections": 7}
        FakeTransport.responses = [page([bad, story("11")])]
        result = self.run_collect()
        self.assertEqual([p["post_id"] for p in result["posts"]], ["11"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"][0]["stage"], "parse_post")

    def test_missing_or_invalid_cookie_fails_before_any_network(self):
        for cookie in ("", "xs=invalid", "c_user=456; xs=foo\nbar"):
            with self.subTest(cookie_valid=False):
                self.env["FACEBOOK_COOKIE"] = cookie
                result = self.run_collect()
                self.assertEqual(result["errors"][0]["code"], "authentication_failed")
                self.assertFalse(FakeTransport.instances)

    def test_mismatched_user_fails_before_network(self):
        self.env["FACEBOOK_USER_ID"] = "999"
        self.assertEqual(self.run_collect()["errors"][0]["code"], "authentication_failed")
        self.assertFalse(FakeTransport.instances)

    def test_repeated_cursor_is_parse_error_not_infinite_loop(self):
        FakeTransport.responses = [page([story("11")], cursor="next", has_next=True), page([story("22")], cursor="next", has_next=True)]
        result = self.run_collect()
        self.assertEqual(result["pages_fetched"], 2)
        self.assertEqual(result["errors"][0]["code"], "pagination_stalled")

    def test_empty_valid_group_is_success_but_null_group_is_inaccessible(self):
        FakeTransport.responses = [page([])]
        self.assertEqual(self.run_collect()["status"], "success")
        FakeTransport.responses = ['{"data":{"node":null}}']
        self.assertEqual(self.run_collect()["errors"][0]["code"], "group_not_accessible")

    def test_error_classification_and_no_retries(self):
        for code, text, expected in [(1357001, "login_required", "session_expired"),
                                      (1, "checkpoint required", "checkpoint_required"),
                                      (368, "blocked", "rate_limited"), (1, "missing_required_variable_value doc_id", "parse_error")]:
            with self.subTest(expected=expected):
                FakeTransport.responses = [json.dumps({"errors": [{"code": code, "message": text + " SECRET_TEST_SESSION"}]})]
                result = self.run_collect()
                self.assertEqual(result["errors"][0]["code"], expected)
                self.assertEqual(len(FakeTransport.instances[-1].forms), 1)

    def test_caption_error_words_are_not_treated_as_session_error(self):
        item = story("11")
        item["message"]["text"] = "checkpoint and login_required in an ordinary caption"
        FakeTransport.responses = [page([item])]
        self.assertEqual(self.run_collect()["status"], "success")

    def test_malformed_http_200_is_parse_failure(self):
        FakeTransport.responses = ["this is not JSON"]
        self.assertEqual(self.run_collect()["errors"][0]["code"], "parse_error")

    def test_unknown_exception_text_never_logged(self):
        FakeTransport.responses = [RuntimeError("xs=SECRET_TEST_SESSION")]
        self.assertEqual(self.run_collect()["errors"][0]["code"], "unknown_error")

    def test_group_inputs_reject_foreign_hosts_and_aliases(self):
        self.assertEqual(collector.group_id("https://www.facebook.com/groups/123/?ref=foo"), "123")
        for value in ("https://facebook.com.attacker.invalid/groups/123", "https://www.facebook.com/groups/vanity", "https://evil@www.facebook.com/groups/123", "0"):
            with self.assertRaises(collector.CollectionError):
                collector.group_id(value)

    def test_invalid_limits_are_configuration_errors(self):
        self.args.pages = "201"
        self.assertEqual(self.run_collect()["errors"][0]["code"], "configuration_error")
        self.assertFalse(FakeTransport.instances)

    def test_atomic_output_is_private_and_replaces_stale_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw" / "fbgql.json"
            collector.atomic_json(str(path), {"status": "failed"})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            collector.atomic_json(str(path), {"status": "success"})
            self.assertEqual(json.loads(path.read_text())["status"], "success")

    def test_transport_does_not_forward_cookies_on_external_redirect(self):
        transport = collector.SafeTransport(2)
        try:
            with patch.object(transport.session, "request", return_value=FakeResponse(302, headers={"Location": "https://attacker.invalid/"})) as request:
                with self.assertRaises(collector.CollectionError) as raised:
                    transport.get("https://www.facebook.com/", {"xs": "SECRET_TEST_SESSION"})
                self.assertEqual(raised.exception.code, "network_error")
                self.assertEqual(request.call_count, 1)
        finally:
            transport.close()

    def test_transport_records_only_validated_final_url(self):
        transport = collector.SafeTransport(2)
        final_url = "https://www.facebook.com/groups/123/posts/11/"
        try:
            with patch.object(transport.session, "request", side_effect=[FakeResponse(302, headers={"Location": final_url}), FakeResponse(200, "<html>post</html>")]):
                transport.get("https://www.facebook.com/share/p/example/", {})
                self.assertEqual(transport.last_url, final_url)
            with patch.object(transport.session, "request", return_value=FakeResponse(302, headers={"Location": "https://evil.invalid/"})):
                with self.assertRaises(collector.CollectionError):
                    transport.get("https://www.facebook.com/share/p/example/", {})
                self.assertIsNone(transport.last_url)
        finally:
            transport.close()

    def test_transport_detects_login_and_checkpoint_on_http_200(self):
        for html, expected in [('<form name="login"><input name="email"></form>', "session_expired"),
                               ("<FORM ACTION='/login/device-based/regular/login/'>", "session_expired"),
                               ('<form><input name="email"><input type="password" name="pass"></form>', "session_expired"),
                               ('<form action="/checkpoint/"></form>', "checkpoint_required")]:
            transport = collector.SafeTransport(2)
            try:
                with patch.object(transport.session, "request", return_value=FakeResponse(200, html)):
                    with self.assertRaises(collector.CollectionError) as raised:
                        transport.get("https://www.facebook.com/", {})
                    self.assertEqual(raised.exception.code, expected)
            finally:
                transport.close()

    def test_transport_does_not_treat_login_javascript_or_unrelated_fields_as_logout(self):
        pages = [
            '<html><script>requireLazy(["CometLoginForm", "login_form"], function () {});</script></html>',
            '<html><!-- <form id="login_form"> --><a href="/login/">Log in</a></html>',
            '<html><form id="contact"><input name="email"></form><form><input type="password"></form></html>',
        ]
        for html in pages:
            with self.subTest(html=html):
                transport = collector.SafeTransport(2)
                try:
                    with patch.object(transport.session, "request", return_value=FakeResponse(200, html)):
                        self.assertEqual(transport.get("https://www.facebook.com/", {}), html)
                finally:
                    transport.close()


    def test_alias_resolution_uses_explicit_group_markers(self):
        responses = ['<script>{"groupID":"123"}</script>',
                     '<meta content="fb://group/?id=123">',
                     '<script>{"groupID":"123"}</script><meta content="fb://group/?id=123">']
        for body in responses:
            transport = FakeTransport(5)
            with patch.dict(os.environ, self.env, clear=True), patch.object(collector, "SafeTransport", return_value=transport), patch.object(transport, "get", return_value=body):
                self.assertEqual(collector.resolve_group_alias("vegmpremium"), "123")
                self.assertTrue(transport.closed)

    def test_alias_resolution_does_not_guess_or_swallow_block(self):
        for body in ('<script>{"groupID":"123","more":{"groupID":"456"}}</script>', '<html>no group metadata</html>'):
            transport = FakeTransport(5)
            with patch.dict(os.environ, self.env, clear=True), patch.object(collector, "SafeTransport", return_value=transport), patch.object(transport, "get", return_value=body):
                with self.assertRaises(collector.CollectorError) as raised:
                    collector.resolve_group_alias("vegmpremium")
                self.assertEqual(raised.exception.code, "parse_error")
        for code in ("network_error", "checkpoint_required", "session_expired"):
            transport = FakeTransport(5)
            with patch.dict(os.environ, self.env, clear=True), patch.object(collector, "SafeTransport", return_value=transport), patch.object(transport, "get", side_effect=collector.CollectionError(code)):
                with self.assertRaises(collector.CollectorError) as raised:
                    collector.resolve_group_alias("vegmpremium")
                self.assertEqual(raised.exception.code, code)

    def test_http_403_policy_denial_does_not_misreport_session(self):
        transport = collector.SafeTransport(2)
        try:
            with patch.object(transport.session, "request", return_value=FakeResponse(403, "Blocked by proxy: destination not allowed by network policy")):
                with self.assertRaises(collector.CollectionError) as raised:
                    transport.get("https://www.facebook.com/", {})
                self.assertEqual(raised.exception.code, "network_error")
        finally:
            transport.close()

    def test_partial_stream_decode_error_is_recorded(self):
        FakeTransport.responses = [page([story("11")]) + "\nnot-json"]
        result = self.run_collect()
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["errors"][0]["code"], "parse_error")
        self.assertEqual(len(result["posts"]), 1)


if __name__ == "__main__":
    unittest.main()

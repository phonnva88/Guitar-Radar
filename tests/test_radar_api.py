import base64
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from radar.app import create_app
from radar.config import Settings
from radar.db import Conflict


class APITests(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.state.return_value = {"paused_code": None, "worker_active": True}
        self.db.list_groups.return_value = []
        settings = Settings("postgresql://unused", "owner", "SYNTHETIC-test-password", allowed_hosts=("testserver",))
        self.client = TestClient(create_app(settings, self.db))
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"owner:SYNTHETIC-test-password").decode(),
                     "X-Radar-Request": "1"}

    def test_auth_protects_page_api_and_assets(self):
        for path in ("/", "/api/groups", "/static/app.js"):
            self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get("/healthz").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/api/groups", headers=self.auth).json(), [])

    def test_csrf_and_foreign_origin_rejected_before_mutation(self):
        auth_only = {"Authorization": self.auth["Authorization"]}
        for headers in (auth_only, {**self.auth, "Origin": "https://attacker.invalid"}):
            self.assertEqual(self.client.post("/api/collection/resume", headers=headers).status_code, 403)
        self.db.resume_collection.assert_not_called()

    def test_safe_group_configuration_and_pause_conflict(self):
        self.assertEqual(self.client.post("/api/groups", headers=self.auth,
                         json={"name": "Test", "url": "https://evil.invalid/groups/1"}).status_code, 422)
        self.db.create_group.return_value = {"id": 1}
        self.assertEqual(self.client.post("/api/groups", headers=self.auth,
                         json={"name": "Test", "url": "https://www.facebook.com/groups/example?tracking=x"}).status_code, 201)
        self.db.create_group.assert_called_once_with("Test", "https://www.facebook.com/groups/example")
        self.db.enqueue.side_effect = Conflict("Collection is paused")
        self.assertEqual(self.client.post("/api/groups/1/collect", headers=self.auth).status_code, 409)

    def test_filter_validation_and_safe_defaults(self):
        self.db.feed.return_value = {"items": [], "total": 0, "page": 1, "page_size": 20}
        response = self.client.get("/api/posts?has_images=true&q=Fender", headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.db.feed.assert_called_once_with(q="Fender", group_id=None, start=None, end=None, has_images=True, page=1, page_size=20)
        for query in ("page=0", "page_size=101", "start=2026-11-08T00:00:00", "start=2026-11-09T00:00:00Z&end=2026-11-08T00:00:00Z"):
            self.assertEqual(self.client.get("/api/posts?" + query, headers=self.auth).status_code, 422)

    def test_manual_merge_requires_distinct_selected_posts(self):
        self.assertEqual(self.client.post("/api/duplicates/confirm", headers=self.auth,
                         json={"post_ids": [1, 1]}).status_code, 422)
        self.db.confirm_duplicate.assert_not_called()


if __name__ == "__main__":
    unittest.main()

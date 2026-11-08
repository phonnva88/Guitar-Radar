"""Opt-in real PostgreSQL + Chromium smoke. All posts are synthetic; no Facebook calls.

RADAR_TEST_DATABASE_URL=... .venv/bin/python tests/phase1_browser_smoke.py
Requires playwright and an installed Chromium (RADAR_CHROMIUM or /usr/bin/chromium).
"""
import os
from pathlib import Path
import socket
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo
from playwright.sync_api import sync_playwright, expect
import uvicorn

from radar.app import create_app
from radar.config import Settings
from radar.db import Database


def main():
    dsn = os.environ["RADAR_TEST_DATABASE_URL"]
    schema = "radar_browser_" + uuid.uuid4().hex
    server = thread = None
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    try:
        db = Database(make_conninfo(dsn, options=f"-c search_path={schema}"))
        db.migrate()
        duplicate = "Bán Fender Telecaster Nhật, nguyên bản, xem đàn và giao dịch trực tiếp. Dữ liệu thử."
        saved = []
        for n in (101, 202):
            group = db.create_group(f"Nhóm demo {n}", f"https://www.facebook.com/groups/{n}")
            run = db.enqueue(group["id"])
            db.claim_next()
            posts = [{"post_id": str(n*100), "group_id": str(n), "url": f"https://www.facebook.com/groups/{n}/posts/{n*100}/",
                      "text": duplicate, "author_name": "Người bán demo", "posted_at": "2026-11-08T04:00:00Z",
                      "images": ["https://images.example.invalid/guitar.svg"]}]
            if n == 101:
                posts.append({"post_id": "10101", "group_id": "101", "url": "https://www.facebook.com/groups/101/posts/10101/",
                              "text": "Tìm chỗ sửa guitar acoustic. Dữ liệu thử riêng tư.", "images": [], "posted_at": "2026-11-07T03:00:00Z"})
            envelope = {"schema_version": 1, "collector": "facebook-go", "run_id": f"browser-{n}",
                        "group_id": str(n), "source": "synthetic", "status": "success", "errors": [],
                        "window": {"from": "2026-11-06T00:00:00Z", "to": "2026-11-09T00:00:00Z", "complete": True, "stop_reason": "feed_exhausted"},
                        "posts": posts}
            raw = {**envelope, "posts": [{"id": post["post_id"], "message": post["text"], "createdAt": post["posted_at"]} for post in posts]}
            db.complete_run(run["id"], envelope, raw=raw)
            saved.append((group, envelope, raw))
        group, envelope, raw = saved[0]
        repeated = db.enqueue(group["id"])
        db.claim_next()
        db.complete_run(repeated["id"], {**envelope, "run_id": "browser-repeat"}, raw={**raw, "run_id": "browser-repeat"})
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        settings = Settings(db.dsn, "owner", "browser-SYNTHETIC-password")
        server = uvicorn.Server(uvicorn.Config(create_app(settings, db), log_level="error", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.05)
        assert server.started, "Server failed to start"
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=os.environ.get("RADAR_CHROMIUM", "/usr/bin/chromium"),
                                        headless=True, args=["--no-sandbox"])
            context = browser.new_context(http_credentials={"username": settings.username, "password": settings.password},
                                          viewport={"width": 1440, "height": 1000})
            context.route("https://images.example.invalid/**", lambda route: route.fulfill(
                content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"><rect width="800" height="450" fill="#d1b38b"/><text x="90" y="230" font-size="50" fill="#312d28">Guitar Radar · Demo</text></svg>'))
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{port}")
            expect(page.locator("#panel-audit")).to_be_visible()
            expect(page.locator(".audit-run-card")).to_have_count(3)
            expect(page.locator("#audit-runs")).to_contain_text("Không đổi trong cửa sổ đã quan sát")
            page.locator(".audit-known-section > summary").click()
            page.locator("#audit-known-group").select_option(str(saved[0][0]["id"]))
            page.locator("#audit-known-url").fill("https://www.facebook.com/groups/101/posts/99999/")
            page.locator("#audit-known-time").fill("2026-11-08T11:00")
            page.locator("#audit-known-images").select_option("true")
            page.locator("#audit-known-image-count").fill("2")
            page.locator("#audit-known-text").fill("Nội dung chuẩn được xác nhận độc lập để kiểm tra lỗi thiếu bài.")
            page.locator("#audit-known-form button").click()
            expect(page.locator("#audit-known-count")).to_have_text("(1)")
            page.locator(".audit-run-card").first.get_by_role("button", name="Xem từng bài").click()
            expect(page.locator("#audit-run-details")).to_contain_text("Chưa thấy trong cửa sổ")
            page.locator(".audit-post").first.locator("summary").first.click()
            expect(page.locator("#audit-run-details .audit-post-text").first).to_contain_text("Fender")
            page.locator("#audit-run-details .audit-raw").first.locator("summary").click()
            expect(page.locator("#audit-run-details .audit-raw pre").first).to_contain_text('"message"')
            ROOT.joinpath("reports").mkdir(exist_ok=True)
            page.screenshot(path=str(ROOT / "reports/collector-audit-desktop.png"), full_page=True)
            page.locator(".audit-run-card").first.get_by_role("button", name="Lặp lại cùng cửa sổ").click()
            expect(page.locator("#audit-notice")).to_contain_text("Đã xếp lượt")
            repeat = db.list_runs()[0]
            assert repeat["status"] == "queued" and repeat["requested_from"].isoformat().startswith("2026-11-06")
            page.locator('[data-tab="feed"]').click()
            expect(page.locator("#feed-list .post-card")).to_have_count(3)
            ROOT.joinpath("reports").mkdir(exist_ok=True)
            page.screenshot(path=str(ROOT / "reports/phase1-desktop.png"), full_page=True)
            page.locator("#search").fill("sửa guitar")
            page.locator("#filter-form button[type=submit]").click()
            expect(page.locator("#feed-list .post-card")).to_have_count(1)
            page.locator("#clear-filters").click()
            expect(page.locator("#feed-list .post-card")).to_have_count(3)
            page.locator("#filter-start").fill("2026-11-08")
            expect(page.locator("#feed-list .post-card")).to_have_count(2)
            page.locator("#clear-filters").click()
            page.locator('[data-tab="duplicates"]').click()
            expect(page.locator(".duplicate-cluster")).to_have_count(1)
            page.once("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Gom các bài đã chọn").click()
            expect(page.locator(".duplicate-cluster")).to_have_count(0)
            page.locator('[data-tab="feed"]').click()
            page.locator("#refresh-button").click()
            expect(page.locator("#feed-list .post-card")).to_have_count(2)
            expect(page.locator(".source-links a")).to_have_count(2)
            page.locator('[data-tab="groups"]').click()
            page.locator("#group-name").fill("Nhóm thêm thử")
            page.locator("#group-url").fill("303")
            page.locator("#group-form button").click()
            expect(page.locator(".group-card")).to_have_count(3)
            page.locator(".group-card").last.get_by_role("button", name="Thu thập ngay").click()
            page.locator('[data-tab="runs"]').click()
            expect(page.locator("#runs-list")).to_contain_text("Đang chờ")
            page.locator('[data-tab="feed"]').click()
            page.set_viewport_size({"width": 390, "height": 844})
            expect(page.locator("#feed-list .post-card")).to_have_count(2)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Mobile overflow"
            page.screenshot(path=str(ROOT / "reports/phase1-mobile.png"), full_page=True)
            page.locator('[data-tab="audit"]').click()
            expect(page.locator("#panel-audit")).to_be_visible()
            expect(page.locator(".audit-run-card")).to_have_count(5)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Audit mobile overflow"
            page.screenshot(path=str(ROOT / "reports/collector-audit-mobile.png"), full_page=True)
            assert not errors, errors
            browser.close()
        print("PASS: real database, authenticated audit/known-post/raw-details/fixed-window repeat/feed/search/merge/mobile; no Facebook requests")
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    main()

"""Opt-in real PostgreSQL + Chromium smoke. All posts are synthetic; no Facebook calls.

RADAR_TEST_DATABASE_URL=... .venv/bin/python tests/phase1_browser_smoke.py
Requires Playwright Chromium or a Chromium executable in RADAR_CHROMIUM.
"""
import os
from pathlib import Path
import socket
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

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

PLAYBACK_URL = "https://video.xx.fbcdn.net/synthetic.webm?oe=F00&oh=fixture%2f%2B"
THUMBNAIL_URL = "https://scontent.xx.fbcdn.net/synthetic-video-thumb.svg"


def synthetic_video(page):
    return bytes(page.evaluate("""async () => {
        const canvas = document.createElement('canvas');
        canvas.width = 64;
        canvas.height = 48;
        const drawing = canvas.getContext('2d');
        const stream = canvas.captureStream(20);
        const recorder = new MediaRecorder(stream, {mimeType: 'video/webm;codecs=vp8'});
        const chunks = [];
        recorder.ondataavailable = event => chunks.push(event.data);
        const stopped = new Promise(resolve => { recorder.onstop = resolve; });
        recorder.start();
        for (let frame = 0; frame < 12; frame++) {
            drawing.fillStyle = frame % 2 ? '#253d32' : '#d1b38b';
            drawing.fillRect(0, 0, canvas.width, canvas.height);
            await new Promise(resolve => setTimeout(resolve, 50));
        }
        recorder.stop();
        await stopped;
        for (const track of stream.getTracks()) track.stop();
        return Array.from(new Uint8Array(await new Blob(chunks).arrayBuffer()));
    }"""))


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
                      "images": [f"https://images.example.invalid/guitar-{i}.svg" for i in range(1, 5)]}]
            if n == 101:
                posts[0]["videos"] = [
                    {"video_id": "synthetic-direct", "kind": "direct", "thumbnail_url": THUMBNAIL_URL,
                     "playback_url": PLAYBACK_URL, "duration_seconds": 0.6},
                    {"video_id": "synthetic-broken", "kind": "live", "thumbnail_url": THUMBNAIL_URL,
                     "playback_url": "https://video.xx.fbcdn.net/broken.webm"},
                ]
                posts.append({"post_id": "10101", "group_id": "101", "url": "https://www.facebook.com/groups/101/posts/10101/",
                              "text": "Tìm chỗ sửa guitar acoustic. Dữ liệu thử riêng tư.", "images": [],
                              "videos": [{"video_id": "synthetic-reel-1", "kind": "reel",
                                          "thumbnail_url": THUMBNAIL_URL,
                                          "duration_seconds": 83, "playback_url": None}],
                              "posted_at": "2026-11-07T03:00:00Z"})
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
            browser = p.chromium.launch(executable_path=os.environ.get("RADAR_CHROMIUM"),
                                        headless=True, args=["--no-sandbox"])
            context = browser.new_context(http_credentials={"username": settings.username, "password": settings.password},
                                          viewport={"width": 1440, "height": 1000})
            # Deny every external request except the synthetic media routes below.
            context.route("**/*", lambda route: route.continue_()
                          if urlsplit(route.request.url).hostname == "127.0.0.1" else route.abort())
            context.route("https://images.example.invalid/**", lambda route: route.fulfill(
                content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"><rect width="800" height="450" fill="#d1b38b"/><text x="90" y="230" font-size="50" fill="#312d28">Guitar Radar · Demo</text></svg>'))
            context.route("https://scontent.xx.fbcdn.net/**", lambda route: route.fulfill(
                content_type="image/svg+xml", body='<svg xmlns="http://www.w3.org/2000/svg" width="800" height="450"><rect width="800" height="450" fill="#253d32"/></svg>'))
            page = context.new_page()
            clip = synthetic_video(page)
            context.route("https://video.xx.fbcdn.net/**", lambda route: route.fulfill(
                content_type="video/webm", body=clip if route.request.url == PLAYBACK_URL else b"invalid synthetic media"))
            errors = []
            console_errors = []
            remote_requests = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            context.on("request", lambda request: remote_requests.append(request.url)
                       if urlsplit(request.url).hostname not in {"127.0.0.1", "images.example.invalid", "scontent.xx.fbcdn.net", "video.xx.fbcdn.net"} else None)
            page.goto(f"http://127.0.0.1:{port}")
            expect(page.locator("#panel-audit")).to_be_visible()
            rows = page.locator("#audit-runs .tabulator-row")
            expect(rows).to_have_count(3)
            expect(page.locator("#audit-runs")).to_contain_text("Không đổi trong cửa sổ đã quan sát")
            # Tabulator search and status filters work without a server request.
            page.locator("#audit-search").fill("Nhóm demo 202")
            expect(rows).to_have_count(1)
            page.locator("#audit-search").fill("")
            expect(rows).to_have_count(3)
            page.locator("#audit-status").select_option("failed")
            expect(rows).to_have_count(0)
            page.locator("#audit-status").select_option("")
            expect(rows).to_have_count(3)
            page.locator('#audit-runs .tabulator-col[tabulator-field="id"]').click()
            expect(rows.first).to_contain_text("#1 ·")
            page.locator('#audit-runs .tabulator-col[tabulator-field="id"]').click()
            expect(rows.first).to_contain_text("#3 ·")
            # The latest and oldest rows are repeats of group101's exact window.
            rows.first.locator(".audit-select").check()
            rows.last.locator(".audit-select").check()
            page.locator("#audit-compare").click()
            expect(page.locator("#audit-comparison")).to_be_visible()
            expect(page.locator("#audit-comparison")).to_contain_text("Không đổi")
            # Individually complete runs from different groups cannot imply stability.
            rows.last.locator(".audit-select").uncheck()
            rows.nth(1).locator(".audit-select").check()
            page.locator("#audit-compare").click()
            expect(page.locator("#audit-comparison")).to_contain_text("Chọn lại các lượt tương thích")
            rows.nth(1).locator(".audit-select").uncheck()
            rows.last.locator(".audit-select").check()
            page.locator("#audit-compare").click()
            expect(page.locator("#audit-comparison")).to_contain_text("Không đổi")
            page.locator(".audit-known-section > summary").click()
            page.locator("#audit-known-group").select_option(str(saved[0][0]["id"]))
            page.locator("#audit-known-url").fill("https://www.facebook.com/groups/101/posts/99999/")
            page.locator("#audit-known-time").fill("2026-11-08T11:00")
            page.locator("#audit-known-images").select_option("true")
            page.locator("#audit-known-image-count").fill("2")
            page.locator("#audit-known-text").fill("Nội dung chuẩn được xác nhận độc lập để kiểm tra lỗi thiếu bài.")
            page.locator("#audit-known-form button").click()
            expect(page.locator("#audit-known-count")).to_have_text("(1)")
            rows.first.locator(".audit-inspect").click()
            expect(page.locator("#audit-run-details")).to_contain_text("Chưa thấy trong cửa sổ")
            page.locator(".audit-post").first.locator("summary").first.click()
            expect(page.locator("#audit-run-details .audit-post-text").first).to_contain_text("Fender")
            gallery = page.locator("#audit-run-details .audit-post").first
            expect(gallery.locator(".gallery-open")).to_have_count(4)
            player = gallery.locator(".radar-video-player")
            expect(player).to_have_count(1)
            expect(player).to_have_attribute("src", PLAYBACK_URL)
            expect(player).to_have_attribute("poster", THUMBNAIL_URL)
            assert player.evaluate("video => video.controls && !video.autoplay && video.paused")
            player.evaluate("video => video.play()")
            page.wait_for_function("document.querySelector('.audit-post video')?.currentTime > 0")
            player.evaluate("video => video.pause()")
            broken_video = gallery.get_by_role("region", name="Video phát trực tiếp")
            expect(broken_video.locator(".radar-video-status")).to_contain_text("Không phát được video")
            expect(broken_video.locator(".radar-video-original")).to_have_attribute(
                "href", "https://www.facebook.com/groups/101/posts/10100/")
            gallery.locator(".gallery-open").first.click()
            expect(page.locator(".pswp")).to_be_visible()
            # PhotoSwipe binds keyboard controls after its opening animation.
            expect(page.locator(".pswp")).to_be_focused()
            expect(page.locator(".pswp__counter")).to_have_text("1 / 4")
            page.keyboard.press("ArrowRight")
            expect(page.locator(".pswp__counter")).to_have_text("2 / 4")
            page.keyboard.press("Escape")
            expect(page.locator(".pswp")).to_have_count(0)
            expect(gallery.locator(".gallery-open").first).to_be_focused()
            video_post = page.locator("#audit-run-details .audit-post").filter(has_text="Bài 10101")
            video_post.locator("summary").first.click()
            expect(video_post.locator("summary").first).to_contain_text("0 ảnh")
            expect(video_post.locator("summary").first).to_contain_text("1 video")
            expect(video_post.locator(".radar-video-thumbnail")).to_have_count(1)
            expect(video_post.locator(".radar-video-duration")).to_have_text("1:23")
            expect(video_post.locator(".radar-video-status")).to_contain_text("Chưa có URL phát")
            expect(video_post.locator(".radar-video-player")).to_have_count(0)
            expect(video_post.locator(".radar-video-original")).to_have_attribute("href", "https://www.facebook.com/groups/101/posts/10101/")
            page.locator("#audit-run-details .audit-raw").first.locator("summary").click()
            expect(page.locator("#audit-run-details .audit-raw pre").first).to_contain_text('"message"')
            ROOT.joinpath("reports").mkdir(exist_ok=True)
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(ROOT / "reports/collector-audit-desktop.png"), full_page=True)
            rows.first.locator(".audit-repeat").click()
            expect(page.locator("#audit-notice")).to_contain_text("Đã xếp lượt")
            repeat = db.list_runs()[0]
            assert repeat["status"] == "queued" and repeat["requested_from"].isoformat().startswith("2026-11-06")
            page.locator('[data-tab="feed"]').click()
            expect(page.locator("#feed-list .post-card")).to_have_count(3)
            expect(page.locator("#feed-list .radar-video-status").filter(has_text="Chưa có URL phát")).to_be_visible()
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
            expect(rows).to_have_count(5)
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Audit mobile overflow"
            page.evaluate("window.scrollTo(0, 0)")
            page.screenshot(path=str(ROOT / "reports/collector-audit-mobile.png"), full_page=True)
            assert not errors, errors
            assert not console_errors, console_errors
            assert not remote_requests, remote_requests
            browser.close()
        print("PASS: PostgreSQL, audit comparison, photo gallery, synthetic video playback and fallbacks, feed/search/merge, mobile layout, and no external network requests")
    finally:
        if server:
            server.should_exit = True
        if thread:
            thread.join(timeout=10)
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


if __name__ == "__main__":
    main()

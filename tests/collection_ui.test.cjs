// npm install --prefix .cache/ui-tests --ignore-scripts --no-audit --no-fund jsdom@26.1.0
// node --test tests/collection_ui.test.cjs
const {test} = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const {JSDOM} = require("../.cache/ui-tests/node_modules/jsdom");
const root = path.join(__dirname, "..", "radar", "static");

async function until(check, context = () => "") {
  for (let n = 0; n < 100; n++) {
    if (check()) return;
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.ok(check(), `UI did not reach the expected state: ${context()}`);
}

async function mount(t, paused = "session_expired") {
  const dom = new JSDOM(fs.readFileSync(path.join(root, "index.html"), "utf8"), {
    url: "http://localhost:8000", runScripts: "outside-only",
  });
  t.after(async () => { await new Promise(resolve => setTimeout(resolve, 20)); dom.window.close(); });
  const w = dom.window, $ = selector => w.document.querySelector(selector);
  const server = {paused, resumeFails: false, conflict: false, calls: [], posts: [], runDetailPosts: []};
  w.setInterval = () => 0;
  w.HTMLElement.prototype.scrollIntoView = () => {};
  w.RadarUI = {enhance() {}, icon() { return w.document.createElement("i"); }};
  w.RadarGallery = {create(images) {
    const gallery = w.document.createElement("div"); gallery.className = "radar-gallery";
    gallery.dataset.images = String(images.length); return gallery;
  }};
  w.Tabulator = class {
    constructor(target, options) { this.target = target; this.options = options; }
    on(event, ready) { if (event === "tableBuilt") w.setTimeout(ready, 0); }
    async replaceData(runs) {
      this.target.replaceChildren(...runs.map(run => this.options.columns.at(-1).formatter({
        getRow: () => ({getData: () => run}),
      })));
    }
    setFilter() {}
    redraw() {}
  };
  w.fetch = async (url, options = {}) => {
    server.calls.push({url, method: options.method || "GET", body: options.body, headers: options.headers});
    let status = 200, body;
    if (url === "/api/collection/resume") {
      status = server.resumeFails ? 503 : 200;
      if (!server.resumeFails) server.paused = null;
      body = server.resumeFails ? {detail: "Chưa kết nối được cơ sở dữ liệu."} : {paused_code: null};
    } else if (url === "/api/status") body = {paused_code: server.paused, worker_active: true};
    else if (url === "/api/groups") body = [{id: 1, name: "Synthetic group", enabled: true}];
    else if (url === "/api/posts?page=1&page_size=12") body = {items: server.posts, total: server.posts.length, page: 1, page_size: 12};
    else if (url === "/api/audit/runs/1") body = {
      run: {id: 1, group_name: "Synthetic group", status: "success", source: "synthetic", normalized_posts: server.runDetailPosts.length},
      window: {from: "2026-11-13T00:00:00Z", to: "2026-11-14T00:00:00Z"},
      posts: server.runDetailPosts,
    };
    else if (url.startsWith("/api/audit?") || url === "/api/audit") body = {
      runs: [{id: 1, group_id: 1, status: "success", window: {from: "2026-11-13T00:00:00Z", to: "2026-11-14T00:00:00Z"}}], known_posts: [],
    };
    else if (url === "/api/groups/1/collect") {
      status = server.conflict ? 409 : 202;
      body = server.conflict ? {detail: "Group đang có lượt chờ hoặc đang chạy."} : {id: 2, status: "queued"};
    } else throw new Error(`Unexpected test request: ${url}`);
    return {ok: status < 400, status, json: async () => body};
  };
  for (const name of ["video.js", "app.js", "audit.js"]) w.eval(fs.readFileSync(path.join(root, name), "utf8"));
  // Use minute precision; jsdom appends .000 to nonzero seconds in these inputs.
  $("#audit-start").value = "2026-11-13T00:00";
  $("#audit-end").value = "2026-11-14T00:00";
  await until(() => $("#audit-known-count").textContent === "(0)" && w.RadarCollection.status);
  $("#audit-group").value = "1";
  $("#audit-group").dispatchEvent(new w.Event("change"));
  await new Promise(resolve => setTimeout(resolve, 20));
  return {w, $, server};
}

test("paused collection shows recovery and blocks collect/repeat in both tabs", async t => {
  const {$, server} = await mount(t);
  assert.equal($("#collection-status").hidden, false);
  assert.match($("#collection-status-message").textContent, /hết hạn/);
  assert.equal($("#audit-collect-form button").disabled, true);
  assert.equal($("#groups-list .group-actions button").disabled, true);
  assert.equal($(".audit-repeat").disabled, true);
  assert.equal(server.calls.some(call => call.method === "POST"), false);
});

test("resume sends CSRF header, hides pause and permits a new collection", async t => {
  const {w, $, server} = await mount(t);
  $("#resume-collection").click();
  await until(() => $("#collection-status").hidden);
  assert.equal(server.calls.find(call => call.url === "/api/collection/resume").headers["X-Radar-Request"], "1");
  assert.equal($("#audit-collect-form button").disabled, false);
  assert.equal($("#groups-list .group-actions button").disabled, false);
  assert.equal($(".audit-repeat").disabled, false);
  $("#audit-collect-form").dispatchEvent(new w.Event("submit", {cancelable: true}));
  await until(() => $("#audit-notice").textContent.includes("Đã xếp lượt"), () => $("#audit-notice").textContent);
  assert.equal(server.calls.filter(call => call.url === "/api/groups/1/collect").length, 1);
});

test("a pause since the last refresh prevents another collect request", async t => {
  const {w, $, server} = await mount(t, null);
  assert.equal($("#audit-collect-form button").disabled, false);
  server.paused = "session_expired";
  $("#audit-collect-form").dispatchEvent(new w.Event("submit", {cancelable: true}));
  await until(() => $("#audit-notice").textContent.includes("hết hạn"), () => $("#audit-notice").textContent);
  assert.equal($("#audit-collect-form button").disabled, true);
  assert.equal($("#collection-status").hidden, false);
  assert.equal(server.calls.some(call => call.url === "/api/groups/1/collect"), false);
});

test("failed resume preserves pause and allows retry", async t => {
  const {$, server} = await mount(t);
  server.resumeFails = true;
  $("#resume-collection").click();
  await until(() => $("#notice").textContent.includes("cơ sở dữ liệu"));
  assert.equal($("#collection-status").hidden, false);
  assert.equal($("#audit-collect-form button").disabled, true);
  assert.equal($("#resume-collection").disabled, false);
});

test("an active-run conflict remains visible without creating retries", async t => {
  const {w, $, server} = await mount(t, null);
  server.conflict = true;
  $("#audit-collect-form").dispatchEvent(new w.Event("submit", {cancelable: true}));
  await until(() => $("#audit-notice").textContent.includes("lượt chờ"), () => $("#audit-notice").textContent);
  assert.equal(server.calls.filter(call => call.url === "/api/groups/1/collect").length, 1);
});

test("video without playback stays visible in feed and audit without inflating image count", async t => {
  const {w, $, server} = await mount(t, null);
  const post = {post_id: "video-1", url: "https://www.facebook.com/groups/1/posts/1/", text: "Video synthetic",
    images: [], videos: [{video_id: "v1", kind: "reel", thumbnail_url: "https://scontent.xx.fbcdn.net/thumb.jpg",
      duration_seconds: 83, playback_url: null}]};
  server.posts = [post, {post_id: "old-1", url: "https://www.facebook.com/groups/1/posts/2/", text: "Legacy photo",
    images: ["https://scontent.xx.fbcdn.net/photo.jpg"]}];
  server.runDetailPosts = [post];
  $('[data-tab="feed"]').click();
  await until(() => $("#feed-list .radar-video"));
  assert.equal(w.document.querySelectorAll("#feed-list .post-card").length, 2);
  assert.equal(w.document.querySelectorAll("#feed-list .radar-gallery").length, 1);
  assert.equal($("#feed-list .radar-video video"), null);
  assert.ok($("#feed-list .radar-video-thumbnail"));
  assert.match($("#feed-list .radar-video").textContent, /Reel/);
  assert.match($("#feed-list .radar-video").textContent, /1:23/);
  assert.match($("#feed-list .radar-video").textContent, /Chưa có URL phát/);
  assert.equal($("#feed-list .radar-video-original").href, post.url);

  $('[data-tab="audit"]').click();
  $(".audit-inspect").click();
  await until(() => $("#audit-run-details .audit-post"));
  $("#audit-run-details .audit-post > summary").click();
  await until(() => $("#audit-run-details .audit-videos"));
  assert.match($("#audit-run-details .audit-post > summary").textContent, /0 ảnh.*1 video/);
  assert.equal($("#audit-run-details .audit-videos video"), null);
  assert.match($("#audit-run-details .audit-videos").textContent, /Chưa có URL phát/);
  assert.equal($("#audit-run-details .audit-videos .radar-video-original").href, post.url);
});

test("only valid unexpired Facebook media URL gets a controlled non-autoplay player", async t => {
  const {w, $, server} = await mount(t, null);
  const base = {post_id: "video-2", url: "https://www.facebook.com/groups/1/posts/2/", text: "Video synthetic", images: []};
  server.runDetailPosts = [{...base, videos: [
    {video_id: "v2", kind: "direct", thumbnail_url: "https://scontent.xx.fbcdn.net/thumb.jpg",
      playback_url: "https://video.xx.fbcdn.net/synthetic.mp4", playback_url_ephemeral: true,
      playback_url_expires_at: "2100-01-01T00:00:00Z", duration_seconds: 3723},
    {video_id: "v3", kind: "shared", playback_url: "https://untrusted.example.invalid/video.mp4"},
    {video_id: "v4", kind: "live", playback_url: "https://video.xx.fbcdn.net/expired.mp4",
      playback_url_expires_at: "2000-01-01T00:00:00Z"},
  ]}];
  $(".audit-inspect").click();
  await until(() => $("#audit-run-details .audit-post"));
  $("#audit-run-details .audit-post > summary").click();
  await until(() => $("#audit-run-details .audit-videos"));
  const players = w.document.querySelectorAll("#audit-run-details .radar-video-player");
  assert.equal(players.length, 1);
  assert.equal(players[0].controls, true);
  assert.equal(players[0].autoplay, false);
  assert.equal(players[0].preload, "metadata");
  assert.equal(players[0].poster, "https://scontent.xx.fbcdn.net/thumb.jpg");
  assert.match($("#audit-run-details .audit-videos").textContent, /1:02:03/);
  assert.match($("#audit-run-details .audit-videos").textContent, /URL phát có thời hạn/);
  assert.match($("#audit-run-details .audit-videos").textContent, /URL phát đã hết hạn/);
  players[0].dispatchEvent(new w.Event("error"));
  assert.equal($("#audit-run-details .radar-video-player"), null);
  assert.match($("#audit-run-details .radar-video-status").textContent, /Không phát được/);
});

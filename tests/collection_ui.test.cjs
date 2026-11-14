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
  const server = {paused, resumeFails: false, conflict: false, calls: []};
  w.setInterval = () => 0;
  w.RadarUI = {enhance() {}, icon() { return w.document.createElement("i"); }};
  w.Tabulator = class {
    constructor(target, options) { this.target = target; this.options = options; }
    on(event, ready) { if (event === "tableBuilt") w.setTimeout(ready, 0); }
    async replaceData(runs) {
      this.target.replaceChildren(...runs.map(run => this.options.columns.at(-1).formatter({
        getRow: () => ({getData: () => run}),
      })));
    }
    setFilter() {}
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
    else if (url === "/api/posts?page=1&page_size=12") body = {items: [], total: 0};
    else if (url.startsWith("/api/audit?") || url === "/api/audit") body = {
      runs: [{id: 1, group_id: 1, status: "success", window: {from: "2026-11-13T00:00:00Z", to: "2026-11-14T00:00:00Z"}}], known_posts: [],
    };
    else if (url === "/api/groups/1/collect") {
      status = server.conflict ? 409 : 202;
      body = server.conflict ? {detail: "Group đang có lượt chờ hoặc đang chạy."} : {id: 2, status: "queued"};
    } else throw new Error(`Unexpected test request: ${url}`);
    return {ok: status < 400, status, json: async () => body};
  };
  for (const name of ["app.js", "audit.js"]) w.eval(fs.readFileSync(path.join(root, name), "utf8"));
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

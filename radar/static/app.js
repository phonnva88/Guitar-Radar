"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const state = {tab: "audit", groups: [], groupsLoaded: false, page: 1, feed: null, paused: false, feedRequest: 0};
  const pageInfo = {
    audit: ["Kiểm chứng bộ cào", "Theo dõi chất lượng dữ liệu, kiểm tra từng bài và đối chiếu các lượt thu thập."],
    feed: ["Nhịp mới của cộng đồng.", "Những bài đăng bạn quan tâm, được tập hợp gọn gàng."],
    groups: ["Quản lí nhóm", "Quản lý các nhóm và chủ động thu thập khi cần."],
    duplicates: ["Xem lại trước khi gom bài.", "Bạn quyết định những bài nào thực sự là cùng một nội dung."],
    runs: ["Mọi lượt thu thập đều có dấu vết.", "Xem kết quả gần đây và những nguồn cần được chú ý."]
  };
  const errors = {
    authentication_failed: "Phiên Facebook cần được kiểm tra lại.", session_expired: "Phiên Facebook đã hết hạn.",
    checkpoint_required: "Facebook yêu cầu xác minh thủ công.", account_restricted: "Tài khoản Facebook đang bị hạn chế.",
    rate_limited: "Facebook đang giới hạn yêu cầu. Hãy chờ trước khi tiếp tục.", group_not_accessible: "Tài khoản chưa đọc được nhóm này.",
    group_not_found: "Không tìm thấy nhóm.", network_error: "Không kết nối được với Facebook.", parse_error: "Chưa đọc được dữ liệu phản hồi.",
    invalid_cursor: "Lượt thu thập bị gián đoạn khi chuyển trang.", pagination_stalled: "Lượt thu thập không thể tiếp tục chuyển trang.",
    interrupted: "Lượt thu thập đã bị gián đoạn.", unknown_error: "Lượt thu thập gặp sự cố."
  };
  const dates = new Intl.DateTimeFormat("vi-VN", {day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Ho_Chi_Minh"});
  function formatDate(value) { const d = new Date(value); return value && !Number.isNaN(d.getTime()) ? dates.format(d) : "Chưa có thời gian"; }
  function node(tag, cls, text) { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = text; return n; }
  function safeURL(value) { try { const u = new URL(value); return u.protocol === "https:" && !u.username && !u.password ? u.href : null; } catch { return null; } }
  function link(text, value, cls = "source-link") { const u = safeURL(value); if (!u) return node("span", "muted", "Nguồn chưa có đường dẫn"); const a = node("a", cls, text); a.href = u; a.target = "_blank"; a.rel = "noopener noreferrer"; return a; }
  function notify(message, error = false) { const n = $("notice"); n.textContent = message; n.className = `notice${error ? " error" : ""}`; n.hidden = false; }
  function errorMessage(error) { return error.message || "Không tải được dữ liệu. Vui lòng thử lại."; }
  async function api(path, options = {}) {
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 20000);
    try {
      const headers = {"Accept": "application/json", ...(options.headers || {})};
      if (options.method && options.method !== "GET") { headers["Content-Type"] = "application/json"; headers["X-Radar-Request"] = "1"; }
      const response = await fetch(path, {...options, headers, signal: controller.signal, credentials: "same-origin"});
      let body; try { body = await response.json(); } catch { body = null; }
      if (!response.ok) {
        let message = response.status === 401 ? "Vui lòng đăng nhập lại để tiếp tục." : "Yêu cầu chưa thực hiện được. Vui lòng thử lại.";
        if (body && typeof body.detail === "string") message = errors[body.detail] || body.detail;
        if (body && typeof body.error === "string") message = errors[body.error] || body.error;
        if (response.status === 422) message = "Thông tin chưa hợp lệ. Hãy kiểm tra các ô đã nhập.";
        throw new Error(message);
      }
      return body;
    } catch (e) { if (e.name === "AbortError") throw new Error("Kết nối đang mất nhiều thời gian. Vui lòng thử lại."); throw e; }
    finally { clearTimeout(timer); }
  }
  function loading(target) { target.replaceChildren(node("div", "loading-state", "Đang tải dữ liệu…")); }
  function empty(target, title, description, symbol = "♬") { const e = node("div", "empty-state"); e.append(node("span", "empty-symbol", symbol), node("strong", "", title), node("p", "", description)); target.replaceChildren(e); }
  function failed(target, error, retry) { empty(target, "Chưa tải được dữ liệu", errorMessage(error), "↻"); const b = node("button", "button button-outline error-retry", "Thử lại"); b.addEventListener("click", retry); target.firstChild.append(b); }
  async function action(button, fn) { const previous = button.disabled; button.disabled = true; try { await fn(); } catch (e) { notify(errorMessage(e), true); } finally { button.disabled = previous; } }

  function renderPost(post, compact = false) {
    const card = node("article", compact ? "duplicate-item" : "post-card");
    const images = Array.isArray(post.images) ? post.images.map(x => safeURL(typeof x === "string" ? x : x.url)).filter(Boolean) : [];
    if (images.length && !compact) {
      card.append(window.RadarGallery.create(post.images, {postId: post.post_id || post.id, compact: true}));
    }
    const body = compact ? card : node("div", "post-body");
    const top = node("div", "post-topline"); top.append(node("span", "group-chip", post.group_name || "Nhóm Facebook"), node("time", "post-time", formatDate(post.posted_at))); body.append(top);
    if (post.author_name) body.append(node("div", "post-author", post.author_name));
    const content = post.text || "Bài đăng không có nội dung chữ."; const text = node("div", `post-text${post.text ? "" : " post-no-text"}`, content); body.append(text);
    if (content.length > 220 && !compact) { const toggle = node("button", "expand-text", "Xem thêm"); toggle.addEventListener("click", () => { const expanded = text.classList.toggle("expanded"); toggle.textContent = expanded ? "Thu gọn" : "Xem thêm"; toggle.setAttribute("aria-expanded", String(expanded)); }); toggle.setAttribute("aria-expanded", "false"); body.append(toggle); }
    const bottom = node("div", "post-bottom"); bottom.append(link("Xem bài gốc →", post.url));
    const sources = Array.isArray(post.sources) ? post.sources : [];
    if (sources.length > 1) bottom.append(node("span", "source-count", `${sources.length} bài đã gom`));
    body.append(bottom);
    if (sources.length > 1) { const refs = node("div", "source-links"); for (const source of sources) refs.append(link(source.group_name || "Nguồn Facebook", source.url)); body.append(refs); }
    if (!compact) card.append(body); return card;
  }

  async function loadGroups() {
    try {
      const groups = await api("/api/groups"); state.groups = Array.isArray(groups) ? groups : []; state.groupsLoaded = true;
      $("group-count").textContent = String(state.groups.length); $("stat-groups").textContent = String(state.groups.filter(g => g.enabled).length);
      const select = $("filter-group"); const old = select.value; select.replaceChildren(new Option("Tất cả nhóm", "")); for (const g of state.groups) select.append(new Option(g.name, g.id)); select.value = old;
      const successes = state.groups.map(g => g.last_success_at).filter(Boolean).sort(); $("stat-last-sync").textContent = successes.length ? formatDate(successes.at(-1)) : "Chưa có dữ liệu";
      renderGroups();
    } catch (e) { failed($("groups-list"), e, () => loadGroups().catch(() => {})); throw e; }
  }
  function renderGroups() {
    const target = $("groups-list"); $("groups-summary").textContent = `${state.groups.length} nhóm`;
    if (!state.groups.length) { empty(target, "Bắt đầu từ một cộng đồng", "Thêm đường dẫn nhóm bạn muốn theo dõi ở phía trên. Bạn luôn có thể tắt theo dõi sau này."); return; }
    target.replaceChildren();
    for (const group of state.groups) {
      const card = node("article", "group-card"); card.append(node("div", "group-symbol", "◎")); const details = node("div", "group-details"); details.append(node("h3", "", group.name), link("Mở nhóm trên Facebook →", group.url));
      details.append(node("div", "group-meta", `Thành công gần nhất: ${group.last_success_at ? formatDate(group.last_success_at) : "chưa có"}`));
      if (group.last_error_code) details.append(node("div", "group-meta group-error", errors[group.last_error_code] || "Lượt thu thập gần nhất cần được kiểm tra.")); card.append(details);
      const actions = node("div", "group-actions"); const label = node("label", "toggle-label"); const toggle = node("input"); toggle.type = "checkbox"; toggle.checked = Boolean(group.enabled); label.append(toggle, node("span", "", "Theo dõi")); toggle.addEventListener("change", () => action(toggle, async () => { try { await api(`/api/groups/${group.id}`, {method: "PATCH", body: JSON.stringify({enabled: toggle.checked})}); await loadGroups(); } catch (e) { toggle.checked = !toggle.checked; throw e; } }));
      const button = node("button", "button button-outline", "Thu thập ngay"); button.disabled = !group.enabled || state.paused; button.addEventListener("click", () => action(button, async () => { await api(`/api/groups/${group.id}/collect`, {method: "POST", body: "{}"}); notify(`Đã xếp lượt thu thập cho ${group.name}. Xem tiến trình ở Lịch sử thu thập.`); await loadStatus(); })); actions.append(label, button); card.append(actions); target.append(card);
    }
  }
  async function loadStatus() {
    const s = await api("/api/status"); state.paused = Boolean(s.paused_code);
    if (state.groupsLoaded) renderGroups();
  }
  function feedParams() {
    const params = new URLSearchParams({page: String(state.page), page_size: "12"}); const q = $("search").value.trim(); if (q) params.set("q", q);
    for (const [key, id] of [["group_id", "filter-group"], ["has_images", "filter-images"]]) if ($(id).value) params.set(key, $(id).value);
    const start = $("filter-start").value; const end = $("filter-end").value;
    if (start && end && start > end) throw new Error("Ngày bắt đầu phải trước hoặc bằng ngày kết thúc.");
    if (start) params.set("start", new Date(`${start}T00:00:00+07:00`).toISOString());
    if (end) params.set("end", new Date(`${end}T23:59:59.999+07:00`).toISOString());
    return params;
  }
  async function loadFeed() {
    const requestId = ++state.feedRequest; const target = $("feed-list"); loading(target); $("pagination").hidden = true;
    try {
      const data = await api(`/api/posts?${feedParams()}`); if (requestId !== state.feedRequest) return; state.feed = data; $("feed-summary").textContent = `${data.total || 0} mục phù hợp`; $("stat-posts").textContent = String(data.total || 0); $("feed-count").textContent = String(data.total || 0);
      if (!data.items || !data.items.length) { empty(target, "Chưa có bài đăng phù hợp", "Thử thay đổi bộ lọc, hoặc thêm nhóm rồi thu thập lượt đầu tiên. Bài đăng sẽ xuất hiện tại đây."); return; }
      target.replaceChildren(...data.items.map(post => renderPost(post))); const pages = Math.ceil(data.total / data.page_size); $("pagination").hidden = pages <= 1; $("page-label").textContent = `Trang ${data.page} / ${pages}`; $("previous-page").disabled = data.page <= 1; $("next-page").disabled = data.page >= pages;
    } catch (e) { if (requestId !== state.feedRequest) return; $("feed-summary").textContent = ""; failed(target, e, loadFeed); }
  }
  async function loadRuns() {
    const target = $("runs-list"); loading(target);
    try {
      const runs = await api("/api/runs"); if (!runs.length) { empty(target, "Mỗi lượt mới đều bắt đầu từ đây", "Chọn Thu thập ngay trong Nhóm theo dõi. Trạng thái của lượt đó sẽ được lưu tại đây.", "◷"); return; }
      target.replaceChildren(); const labels = {queued: "Đang chờ", running: "Đang thu thập", success: "Thành công", completed: "Thành công", partial: "Chưa hoàn chỉnh", failed: "Có lỗi", interrupted: "Bị gián đoạn"};
      for (const run of runs) {
        const card = node("article", "run-card"); const name = node("div"); name.append(node("div", "run-name", run.group_name || "Nhóm Facebook")); if (run.error_code) name.append(node("div", "run-detail group-error", errors[run.error_code] || "Có lỗi trong lượt thu thập.")); else name.append(node("div", "run-detail", run.status === "queued" ? "Chờ đến lượt xử lý" : "Đã lưu các bài thu thập được."));
        const status = node("div"); status.append(node("span", `status-badge ${run.status}`, labels[run.status] || "Đang xử lý")); const time = node("div", "run-detail run-times", `Bắt đầu: ${run.started_at ? formatDate(run.started_at) : "đang chờ"}`); if (run.finished_at) time.append(node("div", "", `Kết thúc: ${formatDate(run.finished_at)}`)); const count = node("div", "run-count", String(run.posts_count || 0)); count.append(node("span", "", "bài")); card.append(name, status, time, count); target.append(card);
      }
    } catch (e) { failed(target, e, loadRuns); }
  }
  async function loadDuplicates() {
    const target = $("duplicates-list"); loading(target);
    try {
      const candidates = await api("/api/duplicates"); $("duplicates-summary").textContent = `${candidates.length} nhóm cần xem lại`;
      if (!candidates.length) { empty(target, "Chưa có bài cần xem lại", "Khi có bài từ các nhóm khác nhau với nội dung chữ giống hệt nhau, chúng sẽ được đề xuất tại đây.", "▱"); return; }
      target.replaceChildren();
      candidates.forEach((candidate, index) => {
        const cluster = node("article", "duplicate-cluster"); const heading = node("div", "duplicate-heading"); const posts = Array.isArray(candidate.posts) ? candidate.posts : []; heading.append(node("h3", "", `Đề xuất ${index + 1} · ${posts.length} bài`)); const button = node("button", "button button-soft", "Gom các bài đã chọn"); heading.append(button); cluster.append(heading); const list = node("div", "duplicate-posts"); const selected = new Set(posts.map(p => p.id));
        for (const post of posts) { const card = renderPost(post, true); const label = node("label", "duplicate-select"); const checkbox = node("input"); checkbox.type = "checkbox"; checkbox.checked = true; checkbox.addEventListener("change", () => { if (checkbox.checked) selected.add(post.id); else selected.delete(post.id); button.disabled = selected.size < 2; }); label.append(checkbox, node("span", "", "Chọn bài này")); card.prepend(label); list.append(card); }
        button.addEventListener("click", () => { if (selected.size < 2 || !window.confirm(`Gom ${selected.size} bài đã chọn thành một mục trong bảng tin? Bạn vẫn xem được tất cả liên kết bài gốc.`)) return; action(button, async () => { await api("/api/duplicates/confirm", {method: "POST", body: JSON.stringify({post_ids: [...selected]})}); notify("Đã gom các bài bạn xác nhận. Các liên kết nguồn vẫn được giữ lại."); await loadDuplicates(); }); }); cluster.append(list); target.append(cluster);
      });
    } catch (e) { failed(target, e, loadDuplicates); }
  }
  async function refresh() { await Promise.allSettled([loadGroups(), loadStatus()]); if (state.tab === "audit") document.dispatchEvent(new Event("radar:audit-refresh")); else if (state.tab === "feed") await loadFeed(); else if (state.tab === "runs") await loadRuns(); else if (state.tab === "duplicates") await loadDuplicates(); }
  function selectTab(tab) {
    state.tab = tab; for (const button of document.querySelectorAll("[data-tab]")) { const selected = button.dataset.tab === tab; button.classList.toggle("active", selected); if (selected) button.setAttribute("aria-current", "page"); else button.removeAttribute("aria-current"); }
    for (const panel of document.querySelectorAll(".tab-panel")) panel.hidden = panel.id !== `panel-${tab}`;
    const [title,description] = pageInfo[tab]; $("page-title").textContent = title; $("page-description").textContent = description; document.title = `${title} — Guitar Radar`;
    document.dispatchEvent(new CustomEvent("radar:tab", {detail: tab}));
    if (tab === "feed") loadFeed(); else if (tab === "runs") loadRuns(); else if (tab === "duplicates") loadDuplicates();
  }
  for (const button of document.querySelectorAll("[data-tab]")) button.addEventListener("click", () => selectTab(button.dataset.tab));
  $("refresh-button").addEventListener("click", (e) => action(e.currentTarget, refresh));
  $("filter-form").addEventListener("submit", e => { e.preventDefault(); state.page = 1; loadFeed(); });
  for (const id of ["filter-group", "filter-images", "filter-start", "filter-end"]) $(id).addEventListener("change", () => { state.page = 1; loadFeed(); });
  $("clear-filters").addEventListener("click", () => { $("filter-form").reset(); state.page = 1; loadFeed(); });
  $("previous-page").addEventListener("click", () => { if (state.page > 1) { state.page--; loadFeed(); } });
  $("next-page").addEventListener("click", () => { state.page++; loadFeed(); });
  $("group-form").addEventListener("submit", e => { e.preventDefault(); const button = e.currentTarget.querySelector("button"); action(button, async () => { await api("/api/groups", {method: "POST", body: JSON.stringify({name: $("group-name").value.trim(), url: $("group-url").value.trim()})}); $("group-form").reset(); notify("Đã thêm nhóm. Bạn có thể thu thập lượt đầu tiên ngay bên dưới."); await loadGroups(); }); });
  (async () => { const results = await Promise.allSettled([loadGroups(), loadStatus(), loadFeed()]); if (results.some(x => x.status === "rejected")) notify("Một phần trạng thái chưa tải được. Chọn Làm mới để thử lại.", true); })();
  setInterval(() => { if (!document.hidden) { loadStatus().catch(() => {}); if (state.tab === "runs") loadRuns(); } }, 30000);
})();

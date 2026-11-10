"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const state = {active: true, groups: [], runs: [], request: 0, selectedRun: null, table: null, tableReady: null, selected: new Set(), comparison: 0};
  const fields = {text: "Nội dung", author: "Tác giả", url: "Permalink", images: "Bài có ảnh", posted_at: "Giờ đăng"};
  const compareFields = {text: "Nội dung", author_id: "ID tác giả", author_name: "Tên tác giả", url: "Permalink", posted_at: "Giờ đăng", images: "URL ảnh"};
  const statuses = {success: "Thành công", partial: "Chưa hoàn chỉnh", failed: "Có lỗi", interrupted: "Bị gián đoạn", queued: "Đang chờ", running: "Đang thu thập"};
  const checkLabels = {detected: "Khớp các kỳ vọng đã cung cấp", missing_in_window: "Chưa thấy trong cửa sổ", outside_window: "Ngoài cửa sổ", timestamp_unknown: "Chưa xác nhận giờ đăng", image_missing: "Chưa lấy được ảnh", image_unverified: "Chưa xác minh ảnh", unexpected_images: "Thu được ảnh ngoài kỳ vọng", text_mismatch: "Nội dung chưa khớp", image_count_mismatch: "Số ảnh chưa khớp"};
  const dateFormat = new Intl.DateTimeFormat("vi-VN", {day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Ho_Chi_Minh"});
  const el = (tag, cls, value) => { const n = document.createElement(tag); if (cls) n.className = cls; if (value !== undefined) n.textContent = value; return n; };
  const icon = name => window.RadarUI.icon(name);
  const enhance = target => window.RadarUI.enhance(target);
  const date = value => { const instant = new Date(value); return value && !Number.isNaN(instant.getTime()) ? dateFormat.format(instant) : "Chưa có"; };
  const count = value => Number.isFinite(Number(value)) && value !== null && value !== undefined ? String(value) : "—";
  const percent = value => typeof value === "number" && Number.isFinite(value) ? `${Math.round(value * 100)}%` : "Chưa đo";
  function safeURL(value) { try { const u = new URL(value); return u.protocol === "https:" && !u.username && !u.password ? u.href : null; } catch { return null; } }
  function sourceLink(label, value) { const url = safeURL(value); if (!url) return el("span", "muted", "Chưa có permalink"); const link = el("a", "source-link", label); link.href = url; link.target = "_blank"; link.rel = "noopener noreferrer"; return link; }
  function notify(message, error = false) { $("audit-notice").textContent = message; $("audit-notice").className = `notice${error ? " error" : ""}`; $("audit-notice").hidden = false; }
  async function api(path, options = {}) {
    const controller = new AbortController(); const timer = setTimeout(() => controller.abort(), 30000);
    try {
      const headers = {Accept: "application/json"};
      if (options.method) Object.assign(headers, {"Content-Type": "application/json", "X-Radar-Request": "1"});
      const response = await fetch(path, {...options, headers, credentials: "same-origin", signal: controller.signal});
      let body; try { body = await response.json(); } catch { body = null; }
      if (!response.ok) throw new Error(typeof body?.detail === "string" ? body.detail : "Không đọc được dữ liệu kiểm chứng. Kiểm tra kết nối và thử lại.");
      return body;
    } catch (error) { if (error.name === "AbortError") throw new Error("Đọc dữ liệu kiểm chứng quá lâu. Hãy thử lại."); throw error; }
    finally { clearTimeout(timer); }
  }
  async function action(button, task) { button.disabled = true; try { await task(); } catch (error) { notify(error.message, true); } finally { button.disabled = false; updateCompareButton(); } }
  function localValue(instant) { return new Date(instant.getTime() + 7 * 3600000).toISOString().slice(0, 19); }
  function fromVietnam(value) {
    if (!value) return null;
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(value)) throw new Error("Nhập ngày giờ hợp lệ theo giờ Việt Nam.");
    const instant = new Date(`${value.length === 16 ? value + ":00" : value}+07:00`);
    if (Number.isNaN(instant.getTime())) throw new Error("Ngày giờ chưa hợp lệ.");
    return instant.toISOString();
  }
  function fillGroups(groups) {
    state.groups = Array.isArray(groups) ? groups : [];
    for (const [id, first] of [["audit-group", "Tất cả nhóm"], ["audit-known-group", "Chọn nhóm"]]) {
      const select = $(id); const previous = select.value;
      select.replaceChildren(new Option(first, ""));
      for (const group of state.groups) select.append(new Option(`${group.name}${group.enabled ? "" : " (đang tắt)"}`, group.id));
      select.value = previous;
    }
  }
  function empty(target, message) { target.replaceChildren(el("div", "audit-empty", message)); }
  function metric(label, value, symbol, description, tone = "") {
    const item = el("div", `audit-metric ${tone}`); const top = el("div", "metric-top"); top.append(el("span", "", label), icon(symbol));
    item.append(top, el("strong", "", value), el("p", "", description)); return item;
  }
  function attention(run) {
    return ["partial", "failed", "interrupted"].includes(run.status) || (run.snapshot_available && (!run.window_complete || run.duplicates || run.missing_ids || run.stability?.status === "changed_in_observed_window"));
  }
  function overview(data) {
    const runs = Array.isArray(data.runs) ? data.runs : [];
    const snapshots = runs.filter(run => run.snapshot_available);
    const complete = snapshots.filter(run => run.status === "success" && run.window_complete);
    const stable = snapshots.filter(run => run.stability?.status === "stable_in_observed_window");
    const issues = runs.filter(attention);
    $("audit-overview").replaceChildren(metric("Có bản chụp", String(snapshots.length), "layers", `Trong ${runs.length} lượt gần nhất`), metric("Hoàn tất cửa sổ", String(complete.length), "circle-check", "Theo bằng chứng collector", "metric-success"), metric("Lượt cần xem lại", String(issues.length), "scan-eye", "Lỗi, chưa hoàn tất hoặc khác lượt trước", issues.length ? "metric-warning" : ""), metric("Lượt lặp không đổi", String(stable.length), "git-compare-arrows", "Chỉ tính mẫu đã quan sát"));
    $("audit-coverage-note").textContent = data.coverage_note || "Các bài đối chiếu chỉ xác minh những bài đã biết, chưa đại diện cho toàn bộ bài thật của group.";
    $("audit-stability-note").textContent = data.stability_note || "Chỉ so sánh các lượt thành công, hoàn tất cùng group, cùng thời gian và cùng nguồn dữ liệu. Facebook có thể sửa hoặc xóa bài giữa các lần.";
    enhance($("audit-overview"));
  }
  function stabilityLabel(item) {
    if (item?.status === "stable_in_observed_window") return "Không đổi trong cửa sổ đã quan sát";
    if (item?.status === "changed_in_observed_window") return "Có khác biệt cần xem lại";
    return "Chưa đủ lượt cùng cửa sổ để so sánh";
  }
  function fieldMetrics(run) {
    const wrap = el("div", "audit-field-metrics");
    for (const [key, label] of Object.entries(fields)) { const item = el("div"); item.append(el("span", "", label), el("strong", "", percent(run.field_presence?.[key]))); wrap.append(item); }
    return wrap;
  }
  function windowText(window) { return window?.from && window?.to ? `${date(window.from)} → ${date(window.to)} (giờ Việt Nam)` : "Chưa lưu cửa sổ thời gian"; }
  function canCompare(run) { return run.snapshot_available && run.status === "success" && run.window_complete && run.normalized_posts > 0 && !run.duplicates && !run.missing_ids; }
  function updateCompareButton() { const button = $("audit-compare"); button.disabled = state.selected.size !== 2; button.querySelector("span").textContent = `So sánh (${state.selected.size}/2)`; }
  function statusBadge(run) { return el("span", `status-badge ${Object.hasOwn(statuses, run.status) ? run.status : ""}`, statuses[run.status] || "Chưa rõ trạng thái"); }
  function duration(run) { const ms = new Date(run.finished_at) - new Date(run.started_at); return run.started_at && run.finished_at && Number.isFinite(ms) && ms >= 0 ? `${(ms / 1000).toFixed(1)} giây` : "Chưa kết thúc"; }
  async function repeatRun(run) {
    await api(`/api/audit/runs/${run.id}/repeat`, {method: "POST", body: "{}"}); notify("Đã xếp lượt mới với cùng cửa sổ cố định."); await refresh();
  }
  function applyTableFilter() {
    if (!state.table) return;
    const query = $("audit-search").value.trim().toLocaleLowerCase("vi"); const status = $("audit-status").value;
    state.table.setFilter(run => (!query || `${run.id} ${run.group_name || ""} ${run.error_code || ""}`.toLocaleLowerCase("vi").includes(query)) && (!status || (status === "attention" ? attention(run) : run.status === status)));
  }
  function createTable() {
    if (!window.Tabulator) throw new Error("Chưa tải được thư viện bảng dữ liệu. Tải lại trang để thử lại.");
    let ready;
    state.tableReady = new Promise(resolve => { ready = resolve; });
    state.table = new window.Tabulator($("audit-runs"), {
      data: [], index: "id", layout: "fitColumns", responsiveLayout: "hide", placeholder: "Chưa có lượt phù hợp. Nhập các lượt đã lưu hoặc thu thập một cửa sổ mới.",
      pagination: true, paginationMode: "local", paginationSize: 10, paginationSizeSelector: [10, 25, 50, 100], paginationCounter: "rows", movableColumns: false, initialSort: [{column: "id", dir: "desc"}],
      locale: "vi", langs: {vi: {pagination: {page_size: "Mỗi trang", page_title: "Trang", first: "Đầu", first_title: "Trang đầu", last: "Cuối", last_title: "Trang cuối", prev: "Trước", prev_title: "Trang trước", next: "Sau", next_title: "Trang sau", all: "Tất cả", counter: {showing: "Đang xem", of: "/", rows: "lượt", pages: "trang"}}}},
      columns: [
        {title: "Chọn", width: 50, minWidth: 44, responsive: 0, headerSort: false, hozAlign: "center", formatter(cell) {
          const run = cell.getRow().getData(); const checkbox = el("input", "audit-select form-check-input"); checkbox.type = "checkbox"; checkbox.setAttribute("aria-label", `Chọn lượt #${run.id} để so sánh`); checkbox.checked = state.selected.has(run.id); checkbox.disabled = !canCompare(run);
          if (checkbox.disabled) checkbox.title = "Cần lượt thành công, hoàn tất cửa sổ, có bài xác định được ID.";
          checkbox.addEventListener("change", () => { if (checkbox.checked && state.selected.size >= 2) { checkbox.checked = false; notify("Chọn tối đa hai lượt để so sánh.", true); return; } if (checkbox.checked) state.selected.add(run.id); else state.selected.delete(run.id); updateCompareButton(); }); return checkbox;
        }},
        {title: "Lượt / Nhóm", field: "id", sorter: "number", minWidth: 150, widthGrow: 2, responsive: 0, formatter(cell) {
          const run = cell.getRow().getData(); const wrap = el("div", "table-run-name"); wrap.append(el("strong", "", `#${run.id} · ${run.group_name || "Nhóm Facebook"}`), el("span", "table-source", !run.snapshot_available ? "Chưa có bản chụp" : run.source === "live" ? "Dữ liệu live" : run.source === "synthetic" ? "Dữ liệu synthetic" : "Chưa rõ nguồn")); const mobileStatus = statusBadge(run); mobileStatus.classList.add("table-name-status"); wrap.append(mobileStatus); if (run.error_code) wrap.append(el("span", "table-mobile-error", run.error_code)); return wrap;
        }},
        {title: "Trạng thái", field: "status", minWidth: 150, responsive: 2, formatter(cell) { const run = cell.getRow().getData(); const wrap = el("div", "table-state"); wrap.append(statusBadge(run), el("span", "", run.error_code || duration(run))); return wrap; }},
        {title: "Cửa sổ · giờ VN", field: "window.from", minWidth: 180, responsive: 4, formatter(cell) { const run = cell.getRow().getData(); const wrap = el("div", "table-window"); if (run.window?.from && run.window?.to) wrap.append(el("span", "", date(run.window.from)), el("span", "", `→ ${date(run.window.to)}`)); else wrap.append(el("span", "", "Chưa có cửa sổ")); return wrap; }},
        {title: "Bài", field: "normalized_posts", width: 65, responsive: 3, hozAlign: "right", sorter: "number", formatter(cell) { return el("strong", "table-number", count(cell.getValue())); }},
        {title: "Kiểm chứng", field: "window_complete", minWidth: 185, responsive: 5, formatter(cell) { const run = cell.getRow().getData(); const wrap = el("div", "table-result"); wrap.append(el("span", run.window_complete ? "result-complete" : "", run.window_complete ? "Hoàn tất cửa sổ" : "Chưa xác nhận hoàn tất"), el("span", "", stabilityLabel(run.stability))); return wrap; }},
        {title: "Thao tác", width: 142, minWidth: 132, responsive: 0, headerSort: false, formatter(cell) {
          const run = cell.getRow().getData(); const wrap = el("div", "table-actions"); const inspect = el("button", "button button-outline audit-inspect", "Chi tiết"); inspect.type = "button"; inspect.setAttribute("aria-label", `Xem từng bài lượt #${run.id}`); inspect.addEventListener("click", () => action(inspect, () => showRun(run.id))); wrap.append(inspect);
          if (run.window?.from && run.window?.to && !["queued", "running"].includes(run.status)) { const repeat = el("button", "button button-icon audit-repeat", "↻"); repeat.type = "button"; repeat.setAttribute("aria-label", `Lặp lại cùng cửa sổ lượt #${run.id}`); repeat.title = "Lặp lại cùng cửa sổ"; repeat.addEventListener("click", () => action(repeat, () => repeatRun(run))); wrap.append(repeat); } return wrap;
        }},
      ],
    });
    state.table.on("tableBuilt", ready);
  }
  async function renderRuns(data) {
    state.runs = Array.isArray(data.runs) ? data.runs : [];
    const allowed = new Set(state.runs.filter(canCompare).map(run => run.id));
    state.selected = new Set([...state.selected].filter(id => allowed.has(id))); updateCompareButton();
    $("audit-runs-summary").textContent = `${state.runs.length} lượt gần nhất`;
    if (!state.table) createTable();
    await state.tableReady;
    await state.table.replaceData(state.runs); applyTableFilter(); $("audit-runs").setAttribute("aria-busy", "false");
  }
  function renderKnown(data) {
    const posts = Array.isArray(data.known_posts) ? data.known_posts : [];
    $("audit-known-count").textContent = `(${posts.length})`;
    if (!posts.length) { empty($("audit-known-posts"), "Chưa có bài đối chiếu độc lập. Thêm một bài bạn đã kiểm tra trực tiếp trên Facebook."); return; }
    const list = el("div", "audit-reference-list");
    for (const post of posts) { const item = el("div", "audit-reference"); const label = el("div"); label.append(sourceLink(`Bài ${post.post_id || "đối chiếu"} →`, post.url), el("span", "muted", post.verified_posted_at ? `Đăng: ${date(post.verified_posted_at)}` : "Giờ đăng chưa xác nhận")); item.append(label, el("span", "count-pill", post.expected_image_count === null ? "Chưa đếm ảnh" : `${post.expected_image_count} ảnh chuẩn`)); list.append(item); }
    $("audit-known-posts").replaceChildren(list);
  }
  function changes(stability) {
    const section = el("section", "audit-change-list"); section.append(el("h3", "", "So với lượt cùng cửa sổ trước đó"), el("p", "", stabilityLabel(stability)));
    if (stability?.previous_run_id) section.append(el("p", "audit-help", `Lượt đối chiếu: #${stability.previous_run_id}`));
    for (const [label, values] of [["ID không còn thấy", stability?.missing_ids], ["ID xuất hiện thêm", stability?.added_ids]]) if (Array.isArray(values) && values.length) section.append(el("p", "audit-id-list", `${label}: ${values.join(", ")}`));
    for (const item of Array.isArray(stability?.changed) ? stability.changed : []) section.append(el("p", "audit-id-list", `Bài ${item.post_id}: thay đổi ${Array.isArray(item.fields) ? item.fields.map(field => compareFields[field] || field).join(", ") : "trường dữ liệu"}`));
    return section;
  }
  function postDetails(post, index) {
    const details = el("details", "audit-post"); const summary = el("summary"); const identity = el("div", "post-summary-identity");
    identity.append(el("strong", "", `Bài ${post.post_id || `chưa có ID · dòng ${index + 1}`}`), el("span", "muted", `${post.author_name || "Chưa có tác giả"} · ${date(post.posted_at)}`)); summary.append(identity, el("span", "count-pill", `${Array.isArray(post.images) ? post.images.length : 0} ảnh`)); details.append(summary);
    details.addEventListener("toggle", () => {
      if (!details.open || details.dataset.loaded) return; details.dataset.loaded = "1";
      const body = el("div", "audit-post-body"); body.append(sourceLink("Mở bài Facebook →", post.url));
      const attributes = el("dl", "audit-attributes");
      for (const [name, value] of [["Post ID", post.post_id], ["Author ID", post.author_id], ["Tác giả", post.author_name], ["Giờ đăng (Việt Nam)", post.posted_at ? date(post.posted_at) : null]]) attributes.append(el("dt", "", name), el("dd", value ? "" : "audit-missing", value || "Chưa thu được"));
      body.append(attributes, el("h4", "", "Nội dung đã chuẩn hóa"), el("div", "audit-post-text", post.text || "Chưa thu được nội dung chữ."));
      const images = Array.isArray(post.images) ? post.images : []; body.append(el("h4", "", `Ảnh đã thu được (${images.length})`));
      if (images.length) body.append(window.RadarGallery.create(images, {postId: post.post_id}));
      else body.append(el("p", "audit-help", "Không có URL ảnh trong dữ liệu chuẩn hóa. Dùng bài đối chiếu để xác nhận kỳ vọng."));
      const raw = el("details", "audit-raw"); raw.append(el("summary", "", "Dữ liệu nguồn đã lưu (đã loại thông tin phiên)"));
      raw.addEventListener("toggle", () => { if (raw.open && !raw.dataset.loaded) { raw.dataset.loaded = "1"; raw.append(el("pre", "", post.raw ? JSON.stringify(post.raw, null, 2) : "Không có bản chụp nguồn phù hợp cho bài.")); } });
      if (post.raw_matches_count > 1) raw.append(el("p", "audit-help", `Có ${post.raw_matches_count} dòng nguồn cùng ID. Bản dưới đây là một dòng khớp.`));
      const normalized = el("details", "audit-raw audit-normalized"); normalized.append(el("summary", "", "Dữ liệu sau chuẩn hóa (JSON)"));
      normalized.addEventListener("toggle", () => { if (normalized.open && !normalized.dataset.loaded) { normalized.dataset.loaded = "1"; const output = {...post}; delete output.raw; delete output.raw_matches_count; normalized.append(el("pre", "", JSON.stringify(output, null, 2))); } });
      body.append(raw, normalized); details.append(body);
    });
    return details;
  }
  function runChecks(checks) {
    const references = el("section", "audit-checks"); references.append(el("h3", "", "Đối chiếu bài đã biết"));
    if (!checks.length) references.append(el("p", "audit-help", "Chưa có bài xác nhận độc lập để đo phần thiếu của lượt này."));
    for (const check of checks) {
      const row = el("div", "audit-check"); const label = el("div");
      const textCheck = !check.observed ? "chưa thấy bài" : check.text_matches == null ? "chưa có bản chuẩn" : check.text_matches ? "khớp bản chuẩn" : "không khớp bản chuẩn";
      const imageCheck = !check.observed ? "chưa thấy bài" : check.image_count_matches == null ? "chưa có số chuẩn" : check.image_count_matches ? "khớp số chuẩn" : "không khớp số chuẩn";
      label.append(sourceLink(`Bài ${check.post_id} →`, check.url), el("p", "audit-help", `Nội dung: ${textCheck}. Số ảnh: ${imageCheck}${check.image_count != null ? ` (${check.image_count} ảnh quan sát)` : ""}.`));
      row.append(label, el("span", `audit-check-status ${Object.hasOwn(checkLabels, check.status) ? check.status : ""}`, checkLabels[check.status] || "Chưa xác minh")); references.append(row);
    }
    return references;
  }
  async function showRun(id) {
    const target = $("audit-run-details"); state.selectedRun = id; target.hidden = false; target.setAttribute("aria-busy", "true"); empty(target, "Đang đọc bản chụp lượt kiểm chứng…");
    try {
      const data = await api(`/api/audit/runs/${id}`); if (state.selectedRun !== id) return;
      target.replaceChildren(); const run = data.run || {}; const header = el("div", "card-heading"); const heading = el("div"); heading.append(el("span", "section-kicker", "BẢN CHỤP DỮ LIỆU"), el("h2", "", `Chi tiết lượt #${id} · ${run.group_name || "Nhóm Facebook"}`)); header.append(heading);
      const close = el("button", "button button-outline", "Đóng chi tiết"); close.addEventListener("click", () => { target.hidden = true; state.selectedRun = null; }); header.append(close); target.append(header);
      const status = el("div", "run-detail-status"); status.append(statusBadge(run), el("span", "audit-source", run.source === "live" ? "Dữ liệu live" : run.source === "synthetic" ? "Dữ liệu synthetic" : "Chưa rõ nguồn"), el("span", "muted", duration(run))); target.append(status, el("p", "audit-window", windowText(data.window || run.window)));
      const numbers = el("div", "audit-counts"); numbers.append(metric("Dòng nguồn", count(run.raw_posts), "file-json-2", `${count(run.raw_duplicates)} trùng · ${count(run.raw_missing_ids)} thiếu ID`), metric("Bài trong cửa sổ", count(run.normalized_posts), "files", `${count(run.duplicates)} trùng · ${count(run.missing_ids)} thiếu ID`)); target.append(numbers, fieldMetrics(run), el("p", "audit-help", "Tỷ lệ trường có dữ liệu; chưa xác nhận nội dung đầy đủ hoặc toàn bộ album ảnh."), changes(run.stability), runChecks(Array.isArray(data.known_post_checks) ? data.known_post_checks : []));
      const diagnostic = data.diagnostics || {};
      if (diagnostic.raw_only_ids?.length) target.append(el("p", "audit-help", `ID có trong nguồn nhưng không vào tập chuẩn hóa: ${diagnostic.raw_only_ids.join(", ")}. Bài ngoài cửa sổ không được tính là bị bỏ sót.`));
      if (diagnostic.duplicate_ids?.length) target.append(el("p", "audit-help", `ID có dòng trùng: ${diagnostic.duplicate_ids.join(", ")}`));
      const posts = Array.isArray(data.posts) ? data.posts : []; const toolbar = el("div", "post-list-heading"); toolbar.append(el("h3", "", `${posts.length} bài đã chuẩn hóa`)); const search = el("input", "form-control"); search.type = "search"; search.placeholder = "Tìm ID, tác giả, nội dung bài…"; search.setAttribute("aria-label", "Tìm bài trong lượt"); toolbar.append(search); target.append(toolbar);
      const list = el("div", "audit-posts"); const rendered = posts.map((post, i) => ({node: postDetails(post, i), text: `${post.post_id || ""} ${post.author_name || ""} ${post.text || ""}`.toLocaleLowerCase("vi")}));
      rendered.forEach(item => list.append(item.node)); if (!posts.length) empty(list, "Bản chụp chưa có bài chuẩn hóa. Xem trạng thái cửa sổ và lỗi trước khi đánh giá.");
      search.addEventListener("input", () => { const q = search.value.trim().toLocaleLowerCase("vi"); for (const item of rendered) item.node.hidden = !item.text.includes(q); }); target.append(list);
      if (Array.isArray(data.raw_unmatched) && data.raw_unmatched.length) { const extra = el("details", "audit-raw"); extra.append(el("summary", "", `Bản ghi đầu vào chưa có trong dữ liệu chuẩn hóa (${data.raw_unmatched.length})`)); extra.addEventListener("toggle", () => { if (extra.open && !extra.dataset.loaded) { extra.dataset.loaded = "1"; extra.append(el("pre", "", JSON.stringify(data.raw_unmatched, null, 2))); } }); target.append(extra); }
      enhance(target); target.scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) { if (state.selectedRun === id) { empty(target, error.message); const retry = el("button", "button button-outline", "Thử lại"); retry.addEventListener("click", () => showRun(id)); target.append(retry); } }
    finally { if (state.selectedRun === id) target.setAttribute("aria-busy", "false"); }
  }
  function comparisonKey(data) {
    const run = data.run || {}; if (!canCompare(run) || !Array.isArray(data.posts) || !data.posts.length) return null;
    const groups = new Set(data.posts.map(post => post.group_id)); const numeric = [...groups][0];
    if (groups.size !== 1 || typeof numeric !== "string" || !/^\d+$/.test(numeric)) return null;
    const ids = data.posts.map(post => post.post_id); if (ids.some(id => typeof id !== "string" || !id) || new Set(ids).size !== ids.length) return null;
    const start = new Date(data.window?.from), end = new Date(data.window?.to); if (!Number.isFinite(start.getTime()) || !Number.isFinite(end.getTime())) return null;
    return JSON.stringify([numeric, start.toISOString(), end.toISOString(), run.source]);
  }
  function comparable(value, field) { return field === "images" ? JSON.stringify([...new Set(value || [])].sort()) : JSON.stringify(value ?? null); }
  function comparisonValue(post, field) {
    const value = post?.[field]; if (!post) return el("p", "comparison-missing", "Không thấy bài trong lượt này.");
    if (field === "images") { const gallery = window.RadarGallery.create(Array.isArray(value) ? value : [], {postId: post.post_id}); const wrap = el("div"); wrap.append(el("p", "audit-help", `${Array.isArray(value) ? value.length : 0} URL ảnh`), gallery); return wrap; }
    return el("div", "comparison-value", field === "posted_at" && value ? date(value) : value || "Chưa có dữ liệu");
  }
  async function compareSelected() {
    const ids = [...state.selected].sort((a, b) => a - b); if (ids.length !== 2) return;
    const request = ++state.comparison; const target = $("audit-comparison"); target.hidden = false; empty(target, "Đang đọc hai bản chụp để so sánh…");
    try {
      const [before, after] = await Promise.all(ids.map(id => api(`/api/audit/runs/${id}`))); if (request !== state.comparison) return;
      const key = comparisonKey(before); if (!key || key !== comparisonKey(after)) throw new Error("Hai lượt cần cùng nhóm Facebook, nguồn và mốc thời gian; cả hai phải hoàn tất, có bài và không trùng/thiếu ID. Chọn lại các lượt tương thích.");
      target.replaceChildren(); const header = el("div", "card-heading"); const title = el("div"); title.append(el("span", "section-kicker", "SO SÁNH BẢN CHỤP"), el("h2", "", `Lượt #${ids[0]} → #${ids[1]}`)); const close = el("button", "button button-outline", "Đóng so sánh"); close.addEventListener("click", () => { state.comparison++; target.hidden = true; }); header.append(title, close); target.append(header, el("p", "audit-window", windowText(before.window)), el("p", "audit-help", `${before.run.source === "synthetic" ? "Dữ liệu synthetic" : "Dữ liệu live"} · ${before.run.group_name}. So sánh các trường đã lưu; URL ảnh có thể thay đổi theo phiên. Facebook có thể sửa hoặc xóa bài giữa các lượt.`));
      const left = new Map(before.posts.map(post => [post.post_id, post])), right = new Map(after.posts.map(post => [post.post_id, post]));
      const removed = [...left.keys()].filter(id => !right.has(id)), added = [...right.keys()].filter(id => !left.has(id));
      const changed = [...left.keys()].filter(id => right.has(id)).map(id => ({id, fields: Object.keys(compareFields).filter(field => comparable(left.get(id)[field], field) !== comparable(right.get(id)[field], field))})).filter(item => item.fields.length);
      const stats = el("div", "comparison-stats"); stats.append(metric("Không còn thấy", String(removed.length), "file-minus-2", `Có ở lượt #${ids[0]}`), metric("Xuất hiện thêm", String(added.length), "file-plus-2", `Có ở lượt #${ids[1]}`), metric("Bài thay đổi", String(changed.length), "git-compare-arrows", "Một hoặc nhiều trường khác")); target.append(stats);
      const unchanged = !removed.length && !added.length && !changed.length;
      target.append(el("p", `comparison-outcome ${unchanged ? "unchanged" : "changed"}`, unchanged ? `Không đổi trong cửa sổ đã quan sát · ${left.size} bài khớp giữa hai lượt.` : "Có khác biệt cần xem lại. Mở từng bài để kiểm tra hai phía."));
      const rows = [...removed.map(id => ({id, kind: "Không còn thấy", fields: ["text", "images"]})), ...added.map(id => ({id, kind: "Xuất hiện thêm", fields: ["text", "images"]})), ...changed.map(item => ({...item, kind: "Trường thay đổi"}))];
      for (const item of rows) {
        const details = el("details", "comparison-post"); const summary = el("summary", "", `Bài ${item.id} · ${item.kind} · ${item.fields.map(field => compareFields[field]).join(", ")}`); details.append(summary);
        details.addEventListener("toggle", () => { if (!details.open || details.dataset.loaded) return; details.dataset.loaded = "1"; for (const field of item.fields) { const row = el("div", "comparison-field"); row.append(el("h4", "", compareFields[field])); const columns = el("div", "comparison-columns"); for (const [index, post] of [[0, left.get(item.id)], [1, right.get(item.id)]]) { const column = el("section"); column.append(el("h5", "", `Lượt #${ids[index]}`), comparisonValue(post, field)); columns.append(column); } row.append(columns); details.append(row); } }); target.append(details);
      }
      enhance(target); target.scrollIntoView({behavior: "smooth", block: "start"});
    } catch (error) { if (request === state.comparison) { empty(target, error.message); notify(error.message, true); } }
  }
  async function refresh() {
    const request = ++state.request;
    try {
      const group = $("audit-group").value; const query = group ? `?group_id=${encodeURIComponent(group)}` : "";
      const [data, groups] = await Promise.all([api(`/api/audit${query}`), api("/api/groups")]);
      if (request !== state.request) return; fillGroups(groups); overview(data); await renderRuns(data); renderKnown(data);
    } catch (error) { if (request !== state.request) return; $("audit-runs").setAttribute("aria-busy", "false"); if (!state.table) empty($("audit-runs"), "Chưa đọc được dữ liệu kiểm chứng. Chọn Làm mới để thử lại."); notify(error.message, true); }
  }
  const end = new Date(); $("audit-end").value = localValue(end); $("audit-start").value = localValue(new Date(end.getTime() - 24 * 3600000));
  $("audit-group").addEventListener("change", () => { state.selected.clear(); state.comparison++; $("audit-comparison").hidden = true; refresh(); });
  $("audit-search").addEventListener("input", applyTableFilter); $("audit-status").addEventListener("change", applyTableFilter);
  $("audit-compare").addEventListener("click", event => action(event.currentTarget, compareSelected));
  $("audit-collect-form").addEventListener("submit", event => { event.preventDefault(); const button = event.currentTarget.querySelector("button"); action(button, async () => {
    const group = $("audit-group").value; if (!group) throw new Error("Chọn một group trước khi thu thập cửa sổ kiểm chứng.");
    const start = fromVietnam($("audit-start").value), end = fromVietnam($("audit-end").value); if (!start || !end || start >= end) throw new Error("Nhập khoảng thời gian có điểm bắt đầu trước điểm kết thúc.");
    await api(`/api/groups/${group}/collect`, {method: "POST", body: JSON.stringify({start, end})}); notify("Đã xếp lượt kiểm chứng với cửa sổ cố định. Chọn Làm mới sau khi worker xử lý xong."); await refresh();
  }); });
  $("audit-import").addEventListener("click", event => action(event.currentTarget, async () => {
    const data = await api("/api/audit/import", {method: "POST", body: "{}"});
    const errors = Array.isArray(data.errors) ? data.errors : [];
    const reasons = {invalid_manifest: "Thiếu hoặc không đọc được reports/stability.json.", invalid_run: "Thiếu raw/metadata hoặc định dạng không khớp.", storage_error: "Chưa lưu được lượt vào database.", invalid_fixture: "Bài đối chiếu trong manifest chưa hợp lệ.", fixture_scope: "Bài đối chiếu không khớp group của lượt đã nhập.", fixture_conflict: "Kỳ vọng xung đột với bản đã xác nhận; giữ nguyên bản hiện có.", fixture_storage_error: "Chưa lưu được bài đối chiếu."};
    const detail = errors.map(error => `${error.run_id ? `Lượt ${error.run_id}: ` : ""}${reasons[error.code] || "Cần kiểm tra dữ liệu nhập."}`).slice(0, 10).join(" ");
    notify(`Đã nhập ${data.imported || 0} lượt và ${data.known_posts_imported || 0} bài đối chiếu; bỏ qua ${data.skipped || 0} lượt đã có. Không có yêu cầu Facebook mới. ${detail}`, errors.length > 0); await refresh();
  }));
  $("audit-known-form").addEventListener("submit", event => { event.preventDefault(); const button = event.currentTarget.querySelector("button"); action(button, async () => {
    const expected = $("audit-known-images").value, imageCount = $("audit-known-image-count").value;
    await api("/api/audit/known-posts", {method: "POST", body: JSON.stringify({group_id: Number($("audit-known-group").value), url: $("audit-known-url").value.trim(), expected_has_images: expected === "" ? null : expected === "true", expected_image_count: imageCount === "" ? null : Number(imageCount), expected_text: $("audit-known-text").value.trim() || null, verified_posted_at: fromVietnam($("audit-known-time").value)})});
    const selectedGroup = $("audit-known-group").value; $("audit-known-form").reset(); $("audit-known-group").value = selectedGroup; notify("Đã lưu bài đối chiếu. Mở chi tiết lượt để kiểm tra kết quả."); await refresh(); if (state.selectedRun) await showRun(state.selectedRun);
  }); });
  document.addEventListener("radar:tab", event => { state.active = event.detail === "audit"; if (state.active) { if (state.table) state.table.redraw(true); refresh(); } });
  document.addEventListener("radar:audit-refresh", refresh);
  setInterval(() => { if (state.active && !document.hidden) refresh(); }, 30000);
  refresh();
})();

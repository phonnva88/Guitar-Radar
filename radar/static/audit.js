"use strict";

(() => {
  const $ = id => document.getElementById(id);
  const state = {active: true, groups: [], runs: [], request: 0, selectedRun: null};
  const fields = {text: "Có nội dung", author: "Có tác giả", url: "Có permalink", images: "Có ảnh", posted_at: "Có giờ đăng"};
  const statuses = {success: "Thành công", partial: "Chưa hoàn chỉnh", failed: "Có lỗi", interrupted: "Bị gián đoạn", queued: "Đang chờ", running: "Đang thu thập"};
  const checkLabels = {detected: "Đã thấy, các đối chiếu đã cung cấp khớp", missing_in_window: "Chưa thấy trong cửa sổ", outside_window: "Ngoài cửa sổ", timestamp_unknown: "Chưa xác nhận giờ đăng", image_missing: "Bài có ảnh nhưng chưa lấy được ảnh", image_unverified: "Chưa xác minh ảnh", unexpected_images: "Bài đối chiếu không ảnh nhưng thu được ảnh", text_mismatch: "Nội dung chưa khớp bản đối chiếu", image_count_mismatch: "Số ảnh chưa khớp bản đối chiếu"};
  const dateFormat = new Intl.DateTimeFormat("vi-VN", {day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: "Asia/Ho_Chi_Minh"});
  const el = (tag, cls, value) => { const n = document.createElement(tag); if (cls) n.className = cls; if (value !== undefined) n.textContent = value; return n; };
  const date = value => { const instant = new Date(value); return value && !Number.isNaN(instant.getTime()) ? dateFormat.format(instant) : "Chưa có"; };
  const count = value => Number.isFinite(Number(value)) && value !== null ? String(value) : "Chưa có";
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
  async function action(button, task) { button.disabled = true; try { await task(); } catch (error) { notify(error.message, true); } finally { button.disabled = false; } }
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
  function metric(label, value) { const item = el("div", "audit-metric"); item.append(el("span", "", label), el("strong", "", value)); return item; }
  function overview(data) {
    const runs = Array.isArray(data.runs) ? data.runs : [];
    const snapshots = runs.filter(run => run.snapshot_available);
    const complete = snapshots.filter(run => run.status === "success" && run.window_complete);
    const stable = snapshots.filter(run => run.stability?.status === "stable_in_observed_window");
    $("audit-overview").replaceChildren(metric("Lượt có bản chụp", String(snapshots.length)), metric("Hoàn tất cửa sổ", String(complete.length)), metric("Lượt lặp không đổi", String(stable.length)));
    $("audit-coverage-note").textContent = data.coverage_note || "Các bài đối chiếu chỉ xác minh những bài đã biết, chưa đại diện cho toàn bộ bài thật của group.";
    $("audit-stability-note").textContent = data.stability_note || "Chỉ so sánh các lượt thành công, hoàn tất cùng group, cùng thời gian và cùng nguồn dữ liệu. Facebook có thể sửa hoặc xóa bài giữa các lần.";
  }
  function stabilityLabel(item) {
    if (item?.status === "stable_in_observed_window") return "Không đổi trong cửa sổ đã quan sát";
    if (item?.status === "changed_in_observed_window") return "Có khác biệt cần xem lại";
    return "Chưa đủ lượt cùng cửa sổ để so sánh";
  }
  function fieldMetrics(run) {
    const wrap = el("div", "audit-field-metrics");
    for (const [key, label] of Object.entries(fields)) {
      const item = el("span", ""); item.append(el("span", "", label), el("strong", "", percent(run.field_presence?.[key]))); wrap.append(item);
    }
    return wrap;
  }
  function windowText(window) { return window?.from && window?.to ? `${date(window.from)} → ${date(window.to)} (giờ Việt Nam)` : "Chưa lưu cửa sổ thời gian"; }
  function renderRuns(data) {
    const target = $("audit-runs"); state.runs = Array.isArray(data.runs) ? data.runs : [];
    $("audit-runs-summary").textContent = `${state.runs.length} lượt`;
    if (!state.runs.length) { empty(target, "Chưa có lượt kiểm chứng. Nhập các lượt đã lưu, hoặc chọn group và thu thập một cửa sổ cố định."); return; }
    target.replaceChildren();
    for (const run of state.runs) {
      const card = el("article", "audit-run-card"); const header = el("div", "audit-run-header"); const title = el("div");
      title.append(el("h3", "", `${run.group_name || "Nhóm Facebook"} · lượt #${run.id}`), el("p", "audit-window", windowText(run.window)));
      const badges = el("div", "audit-badges"); badges.append(el("span", `status-badge ${run.status}`, statuses[run.status] || "Chưa rõ trạng thái"));
      badges.append(el("span", "audit-source", !run.snapshot_available ? "Chưa có bản chụp" : run.source === "live" ? "Dữ liệu live" : run.source === "synthetic" ? "Dữ liệu synthetic" : "Chưa rõ nguồn"));
      header.append(title, badges); card.append(header);
      const numbers = el("div", "audit-counts"); numbers.append(metric("Dòng nguồn", count(run.raw_posts)), metric("Bài trong cửa sổ", count(run.normalized_posts)), metric("Dòng chuẩn hóa trùng", count(run.duplicates)), metric("Dòng chuẩn hóa thiếu ID", count(run.missing_ids))); card.append(numbers);
      if (run.raw_snapshot_available) card.append(el("p", "audit-help", `Nguồn: ${count(run.raw_duplicates)} dòng trùng, ${count(run.raw_missing_ids)} dòng thiếu ID. Xem bản ghi đầu vào trong chi tiết lượt.`));
      if (run.snapshot_available) card.append(fieldMetrics(run), el("p", "audit-help", "Tỷ lệ trường có dữ liệu; chưa chứng minh nội dung đầy đủ hay toàn bộ album ảnh."));
      else card.append(el("p", "audit-help", "Lượt cũ chưa có bản chụp để đối chiếu trường dữ liệu. Nhập lại từ raw validation nếu còn file gốc."));
      const footer = el("div", "audit-run-footer"); const status = el("div", "");
      status.append(el("strong", "", run.window_complete ? "Đã đi hết cửa sổ theo bằng chứng collector" : "Cửa sổ chưa được xác nhận hoàn tất"));
      status.append(el("p", "audit-help", stabilityLabel(run.stability)));
      if (run.error_code) status.append(el("p", "audit-error-code", `Mã lỗi: ${run.error_code}`));
      const controls = el("div", "audit-run-buttons"); const inspect = el("button", "button button-outline", "Xem từng bài"); inspect.addEventListener("click", () => action(inspect, () => showRun(run.id))); controls.append(inspect);
      if (run.window?.from && run.window?.to && !["queued", "running"].includes(run.status)) {
        const repeat = el("button", "button button-soft", "Lặp lại cùng cửa sổ");
        repeat.addEventListener("click", () => action(repeat, async () => { await api(`/api/audit/runs/${run.id}/repeat`, {method: "POST", body: "{}"}); notify("Đã xếp lượt thu thập cùng group và cùng cửa sổ. Kết quả sẽ xuất hiện khi worker xử lý xong."); await refresh(); })); controls.append(repeat);
      }
      footer.append(status, controls); card.append(footer); target.append(card);
    }
  }
  function renderKnown(data) {
    const posts = Array.isArray(data.known_posts) ? data.known_posts : [];
    $("audit-known-count").textContent = `(${posts.length})`;
    if (!posts.length) { empty($("audit-known-posts"), "Chưa có bài đối chiếu độc lập."); return; }
    const list = el("div", "audit-reference-list");
    for (const post of posts) { const item = el("div", "audit-reference"); item.append(sourceLink(`Bài ${post.post_id || "đối chiếu"} →`, post.url), el("span", "muted", post.verified_posted_at ? `Đăng: ${date(post.verified_posted_at)}` : "Giờ đăng chưa xác nhận")); list.append(item); }
    $("audit-known-posts").replaceChildren(list);
  }
  function changes(stability) {
    const section = el("section", "audit-change-list"); section.append(el("h3", "", "So với lượt cùng cửa sổ trước đó"), el("p", "", stabilityLabel(stability)));
    if (stability?.previous_run_id) section.append(el("p", "audit-help", `Lượt đối chiếu: #${stability.previous_run_id}`));
    for (const [label, values] of [["ID không còn thấy", stability?.missing_ids], ["ID xuất hiện thêm", stability?.added_ids]]) {
      if (Array.isArray(values) && values.length) section.append(el("p", "audit-id-list", `${label}: ${values.join(", ")}`));
    }
    for (const item of Array.isArray(stability?.changed) ? stability.changed : []) section.append(el("p", "audit-id-list", `Bài ${item.post_id}: thay đổi ${Array.isArray(item.fields) ? item.fields.join(", ") : "trường dữ liệu"}`));
    if (stability?.reason) section.append(el("p", "audit-help", "Mức đánh giá phụ thuộc vào cửa sổ, trạng thái lượt và bản chụp đã lưu; thay đổi trên Facebook có thể làm kết quả khác nhau."));
    return section;
  }
  function postDetails(post, index) {
    const details = el("details", "audit-post"); const summary = el("summary", "");
    summary.append(el("strong", "", `Bài ${post.post_id || `chưa có ID · dòng ${index + 1}`}`), el("span", "muted", `${post.author_name || "Chưa có tác giả"} · ${date(post.posted_at)}`)); details.append(summary);
    const body = el("div", "audit-post-body"); body.append(sourceLink("Mở bài Facebook →", post.url));
    const attributes = el("dl", "audit-attributes");
    for (const [name, value] of [["Post ID", post.post_id], ["Author ID", post.author_id], ["Tác giả", post.author_name], ["Giờ đăng (Việt Nam)", post.posted_at ? date(post.posted_at) : null]]) attributes.append(el("dt", "", name), el("dd", value ? "" : "audit-missing", value || "Chưa thu được"));
    body.append(attributes, el("h4", "", "Nội dung đã chuẩn hóa"), el("div", "audit-post-text", post.text || "Chưa thu được nội dung chữ."));
    const images = Array.isArray(post.images) ? post.images : []; body.append(el("h4", "", `Ảnh đã thu được (${images.length})`));
    if (images.length) {
      const gallery = el("div", "audit-gallery");
      images.forEach((image, i) => { const url = safeURL(typeof image === "string" ? image : image?.url); if (!url) return; const figure = el("figure"); const img = el("img"); img.src = url; img.alt = `Ảnh ${i + 1} của bài ${post.post_id || "chưa có ID"}`; img.loading = "lazy"; img.referrerPolicy = "no-referrer"; figure.append(img, sourceLink(`Mở ảnh ${i + 1} →`, url)); gallery.append(figure); }); body.append(gallery);
    } else body.append(el("p", "audit-help", "Không có URL ảnh trong dữ liệu chuẩn hóa. Cần bài đối chiếu để xác định đây có phải bài có ảnh hay không."));
    const raw = el("details", "audit-raw"); raw.append(el("summary", "", "Dữ liệu nguồn đã lưu (đã loại thông tin phiên)"));
    raw.append(el("pre", "", post.raw ? JSON.stringify(post.raw, null, 2) : "Lượt này không có bản chụp nguồn phù hợp cho bài."));
    if (post.raw_matches_count > 1) raw.append(el("p", "audit-help", `Có ${post.raw_matches_count} dòng nguồn cùng ID. Bản dưới đây là một dòng khớp; xem chỉ số dòng trùng của lượt.`));
    body.append(raw); details.append(body); return details;
  }
  async function showRun(id) {
    const target = $("audit-run-details"); state.selectedRun = id; target.hidden = false; empty(target, "Đang đọc bản chụp lượt kiểm chứng…");
    const data = await api(`/api/audit/runs/${id}`); if (state.selectedRun !== id) return;
    target.replaceChildren(); const run = data.run || {}; const header = el("div", "section-line"); header.append(el("h2", "", `Chi tiết lượt #${id} · ${run.group_name || "Nhóm Facebook"}`));
    const close = el("button", "button button-outline", "Đóng chi tiết"); close.addEventListener("click", () => { target.hidden = true; state.selectedRun = null; }); header.append(close); target.append(header, el("p", "audit-window", windowText(data.window || run.window)), changes(run.stability));
    const checks = Array.isArray(data.known_post_checks) ? data.known_post_checks : [];
    const references = el("section", "audit-checks"); references.append(el("h3", "", "Đối chiếu bài đã biết"));
    if (!checks.length) references.append(el("p", "audit-help", "Chưa có bài xác nhận độc lập để đo phần thiếu của lượt này."));
    for (const check of checks) {
      const row = el("div", "audit-check"); const label = el("div");
      const textCheck = !check.observed ? "chưa thấy bài để đối chiếu" : check.text_matches === null || check.text_matches === undefined ? "chưa có bản chuẩn để đối chiếu" : check.text_matches ? "khớp bản chuẩn" : "không khớp bản chuẩn";
      const imageCheck = !check.observed ? "chưa thấy bài để đối chiếu" : check.image_count_matches === null || check.image_count_matches === undefined ? "chưa có số lượng chuẩn" : check.image_count_matches ? "khớp số lượng chuẩn" : "không khớp số lượng chuẩn";
      label.append(sourceLink(`Bài ${check.post_id} →`, check.url), el("p", "audit-help", `Nội dung: ${textCheck}. Số ảnh: ${imageCheck}.`));
      row.append(label, el("span", `audit-check-status ${check.status}`, checkLabels[check.status] || "Chưa xác minh")); references.append(row);
    }
    target.append(references);
    const diagnostic = data.diagnostics || {};
    if (diagnostic.raw_only_ids?.length) target.append(el("p", "audit-help", `ID có trong nguồn nhưng không vào tập chuẩn hóa của cửa sổ: ${diagnostic.raw_only_ids.join(", ")}. Các bài ngoài cửa sổ không được tính là bị bỏ sót.`));
    if (diagnostic.duplicate_ids?.length) target.append(el("p", "audit-help", `ID có dòng nguồn trùng: ${diagnostic.duplicate_ids.join(", ")}`));
    const posts = Array.isArray(data.posts) ? data.posts : []; target.append(el("h3", "audit-post-heading", `${posts.length} bài đã chuẩn hóa · bấm từng bài để xem mọi trường`));
    if (!posts.length) target.append(el("p", "audit-help", "Bản chụp chưa có bài chuẩn hóa. Xem trạng thái cửa sổ và lỗi của lượt trước khi diễn giải kết quả."));
    posts.forEach((post, i) => target.append(postDetails(post, i)));
    if (Array.isArray(data.raw_unmatched) && data.raw_unmatched.length) { const extra = el("details", "audit-raw"); extra.append(el("summary", "", `Bản ghi đầu vào chưa có trong dữ liệu chuẩn hóa (${data.raw_unmatched.length})`), el("pre", "", JSON.stringify(data.raw_unmatched, null, 2))); target.append(extra); }
    target.scrollIntoView({behavior: "smooth", block: "start"});
  }
  async function refresh() {
    const request = ++state.request;
    try {
      const group = $("audit-group").value; const query = group ? `?group_id=${encodeURIComponent(group)}` : "";
      const [data, groups] = await Promise.all([api(`/api/audit${query}`), api("/api/groups")]);
      if (request !== state.request) return; fillGroups(groups); overview(data); renderRuns(data); renderKnown(data);
    } catch (error) { if (request !== state.request) return; empty($("audit-runs"), "Chưa đọc được dữ liệu kiểm chứng. Chọn Làm mới để thử lại."); notify(error.message, true); }
  }

  const end = new Date(); $("audit-end").value = localValue(end); $("audit-start").value = localValue(new Date(end.getTime() - 24 * 3600000));
  $("audit-group").addEventListener("change", refresh);
  $("audit-collect-form").addEventListener("submit", event => { event.preventDefault(); const button = event.currentTarget.querySelector("button"); action(button, async () => {
    const group = $("audit-group").value; if (!group) throw new Error("Chọn một group trước khi thu thập cửa sổ kiểm chứng.");
    const start = fromVietnam($("audit-start").value), end = fromVietnam($("audit-end").value); if (!start || !end || start > end) throw new Error("Nhập khoảng thời gian có điểm bắt đầu trước điểm kết thúc.");
    await api(`/api/groups/${group}/collect`, {method: "POST", body: JSON.stringify({start, end})}); notify("Đã xếp lượt kiểm chứng với cửa sổ cố định. Chọn Làm mới sau khi worker xử lý xong."); await refresh();
  }); });
  $("audit-import").addEventListener("click", event => action(event.currentTarget, async () => {
    const data = await api("/api/audit/import", {method: "POST", body: "{}"});
    const errors = Array.isArray(data.errors) ? data.errors : [];
    const reasons = {invalid_manifest: "Không đọc được reports/stability.json hợp lệ trên máy đang chạy ứng dụng.", invalid_run: "Raw hoặc metadata của lượt đã lưu không hợp lệ/không còn đủ file.", storage_error: "Chưa lưu được bản chụp vào PostgreSQL.", invalid_fixture: "Bài đối chiếu trong report chưa có permalink/kỳ vọng hợp lệ.", fixture_scope: "Bài đối chiếu không khớp group của các lượt đã nhập.", fixture_conflict: "Kỳ vọng bài đối chiếu xung đột với bản đã xác nhận; giữ nguyên bản hiện có.", fixture_storage_error: "Chưa lưu được bài đối chiếu."};
    const detail = errors.map(error => `${error.run_id ? `Lượt ${error.run_id}: ` : ""}${reasons[error.code] || "Cần kiểm tra dữ liệu nhập."}`).slice(0, 10).join(" ");
    notify(`Đã nhập ${data.imported || 0} lượt và ${data.known_posts_imported || 0} bài đối chiếu; bỏ qua ${data.skipped || 0} lượt đã có. Không có yêu cầu Facebook mới. ${detail}`, errors.length > 0);
    await refresh();
  }));
  $("audit-known-form").addEventListener("submit", event => { event.preventDefault(); const button = event.currentTarget.querySelector("button"); action(button, async () => {
    const expected = $("audit-known-images").value;
    const imageCount = $("audit-known-image-count").value;
    await api("/api/audit/known-posts", {method: "POST", body: JSON.stringify({group_id: Number($("audit-known-group").value), url: $("audit-known-url").value.trim(), expected_has_images: expected === "" ? null : expected === "true", expected_image_count: imageCount === "" ? null : Number(imageCount), expected_text: $("audit-known-text").value.trim() || null, verified_posted_at: fromVietnam($("audit-known-time").value)})});
    const selectedGroup = $("audit-known-group").value; $("audit-known-form").reset(); $("audit-known-group").value = selectedGroup; notify("Đã lưu bài đối chiếu. Mở chi tiết lượt để xem bài có được thu trong cửa sổ hay chưa."); await refresh(); if (state.selectedRun) await showRun(state.selectedRun);
  }); });
  document.addEventListener("radar:tab", event => { state.active = event.detail === "audit"; if (state.active) refresh(); });
  document.addEventListener("radar:audit-refresh", refresh);
  setInterval(() => { if (state.active && !document.hidden) refresh(); }, 30000);
  refresh();
})();

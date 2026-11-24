"use strict";

window.RadarEngagement = (() => {
  const reactions = {
    LIKE: "👍 Thích", LOVE: "❤️ Yêu thích", CARE: "🥰 Thương thương",
    HAHA: "😆 Haha", WOW: "😮 Wow", SAD: "😢 Buồn", ANGRY: "😡 Phẫn nộ"
  };
  const errors = {
    feedback_unavailable: "Radar chưa có dữ liệu tương tác cho bài này.",
    comments_unavailable: "Facebook chưa trả danh sách bình luận cho Radar.",
    page_stale: "Danh sách đã đổi. Hãy tải lại bình luận từ đầu.",
    write_disabled: "Bài này chưa được bật gửi thử.",
    write_unknown: "Facebook chưa xác nhận kết quả. Hãy kiểm tra bài gốc trước khi gửi lại."
  };

  function create(post, {api, node, link, formatDate}) {
    const root = node("details", "post-engagement");
    const summary = node("summary", "engagement-summary");
    const body = node("div", "engagement-body");
    const notice = node("p", "muted");
    notice.setAttribute("role", "status");
    const list = node("ul", "engagement-comments");
    const actions = node("div", "engagement-actions");
    const more = node("button", "button button-outline", "Xem thêm bình luận");
    const refresh = node("button", "button button-outline", "Tải lại bình luận");
    more.type = refresh.type = "button";
    more.hidden = refresh.hidden = true;
    body.append(notice, list, more, refresh, actions);
    root.append(summary, body);
    const endpoint = `/api/posts/${post.id}`;
    let page = 0, thread = null, loaded = false, reading = false, submitting = false;
    const commentIds = new Set();

    function showCounts(metrics) {
      const labels = [];
      if (Number.isInteger(metrics?.comment_count)) labels.push(`${metrics.comment_count} bình luận`);
      if (Number.isInteger(metrics?.reaction_count)) labels.push(`${metrics.reaction_count} cảm xúc`);
      summary.textContent = labels.length ? labels.join(" · ") : "Xem bình luận và cảm xúc";
      const times = [metrics?.comment_count_at, metrics?.reaction_count_at].filter(Boolean).map(formatDate);
      summary.title = times.length ? `Số liệu cập nhật ${[...new Set(times)].join(" / ")}` : "";
    }

    function renderComment(item, prepend = false) {
      if (commentIds.has(item.id)) return;
      commentIds.add(item.id);
      const row = node("li", item.parent_id ? "engagement-comment engagement-reply" : "engagement-comment");
      row.append(node("strong", "", item.authorName || "Người dùng Facebook"));
      if (item.createdAt && !item.createdAt.startsWith("0001-")) {
        row.append(node("span", "muted", formatDate(item.createdAt)));
      }
      row.append(node("p", "", item.message || "Bình luận không có nội dung chữ."));
      if (prepend) list.prepend(row);
      else list.append(row);
    }

    async function loadComments(next = 1, force = false) {
      if (reading) return;
      reading = true;
      more.disabled = refresh.disabled = true;
      notice.textContent = "Đang tải bình luận…";
      try {
        const params = new URLSearchParams({page: next});
        if (next > 1 && thread) params.set("thread", thread);
        if (force) params.set("refresh", "true");
        const value = await api(`${endpoint}/comments?${params}`);
        if (next === 1) {
          list.replaceChildren();
          commentIds.clear();
        }
        for (const item of value.items) renderComment(item);
        page = value.page;
        thread = value.thread;
        more.hidden = !value.has_more;
        notice.textContent = commentIds.size ? `Cập nhật ${formatDate(value.fetched_at)}` : "Facebook chưa trả bình luận nào.";
        if (value.preview_only) {
          notice.textContent = "Bình luận xem trước. ";
          notice.append(link("Xem thêm trên Facebook", post.url));
        }
        if (next === 1) showCounts(await api(`${endpoint}/engagement`));
      } catch (error) {
        notice.textContent = errors[error.message] || `Chưa tải được bình luận. ${error.message}`;
        notice.append(" ", link("Xem bài gốc", post.url));
        if (next === 1) api(`${endpoint}/engagement`).then(showCounts).catch(() => {});
      } finally {
        reading = false;
        more.disabled = refresh.disabled = false;
        refresh.hidden = false;
      }
    }

    function addActions(account) {
      const actor = node("p", "muted", `Đăng trên Facebook bằng tài khoản ${account}`);
      const form = node("form", "engagement-actions");
      const input = node("textarea", "engagement-input");
      input.setAttribute("aria-label", "Nội dung bình luận trên Facebook");
      input.placeholder = "Viết bình luận…";
      input.maxLength = 2000;
      input.required = true;
      input.rows = 2;
      const send = node("button", "button button-dark", "Gửi bình luận");
      const choose = node("select", "engagement-reaction");
      choose.setAttribute("aria-label", "Chọn cảm xúc trên Facebook");
      choose.append(new Option("Chọn cảm xúc", ""));
      for (const [value, label] of Object.entries(reactions)) choose.append(new Option(label, value));
      const react = node("button", "button button-outline", "Thả cảm xúc");
      react.type = "button";
      react.disabled = true;
      const status = node("p", "muted");
      status.setAttribute("role", "status");
      form.append(input, send);
      actions.append(actor, form, choose, react, status);

      async function submit(path, fields) {
        if (submitting) return;
        if (!window.crypto?.randomUUID) {
          status.textContent = "Hãy mở Radar qua localhost hoặc HTTPS để gửi.";
          return;
        }
        submitting = true;
        send.disabled = react.disabled = choose.disabled = true;
        let uncertain = true;
        status.textContent = "Đang gửi lên Facebook…";
        try {
          const value = await api(`${endpoint}/${path}`, {method: "POST", body: JSON.stringify({
            account_id: account, action_id: crypto.randomUUID(), ...fields
          })});
          uncertain = value.state === "unknown";
          if (value.state !== "confirmed") {
            status.textContent = uncertain ? errors.write_unknown : errors[value.error] || "Facebook chưa nhận thao tác.";
          } else if (value.comment) {
            input.value = "";
            renderComment(value.comment, true);
            notice.textContent = "";
            status.textContent = "Đã gửi bình luận lên Facebook.";
          } else {
            status.textContent = `Đã gửi ${reactions[value.reaction]}.`;
          }
        } catch (error) {
          status.textContent = errors.write_unknown;
        } finally {
          if (uncertain) status.append(" ", link("Kiểm tra bài gốc", post.url));
          submitting = uncertain;
          send.disabled = choose.disabled = uncertain;
          react.disabled = uncertain || !choose.value;
        }
      }
      form.addEventListener("submit", event => {
        event.preventDefault();
        if (input.value.trim()) submit("comments", {message: input.value.trim()});
      });
      choose.addEventListener("change", () => { react.disabled = submitting || !choose.value; });
      react.addEventListener("click", () => {
        if (choose.value) submit("reaction", {reaction: choose.value});
      });
    }

    showCounts(post.engagement);
    root.addEventListener("toggle", async () => {
      if (!root.open || loaded) return;
      loaded = true;
      notice.textContent = "Đang tải tương tác…";
      try {
        const info = await api(`${endpoint}/engagement`);
        showCounts(info);
        if (info.can_write) addActions(info.account_id);
        if (info.can_read) await loadComments();
        else {
          notice.textContent = errors.feedback_unavailable;
          notice.append(" ", link("Xem bài gốc", post.url));
        }
      } catch (error) {
        notice.textContent = error.message;
        loaded = false;
      }
    });
    refresh.addEventListener("click", () => loadComments(1, true));
    more.addEventListener("click", () => loadComments(page + 1));
    return root;
  }
  return {create};
})();

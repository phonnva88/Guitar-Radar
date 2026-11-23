"use strict";

(() => {
  const labels = {direct: "Video trong bài", reel: "Reel", shared: "Video được chia sẻ", live: "Video phát trực tiếp"};
  function node(tag, cls, value) {
    const element = document.createElement(tag);
    if (cls) element.className = cls;
    if (value !== undefined) element.textContent = value;
    return element;
  }
  function sensitiveKey(value) {
    const key = value.toLowerCase();
    return key.startsWith("__") || /token|cookie|authorization|passwd|password|credential|session|tracking|encrypt|csrf|fb_dtsg|jazoest|header|api[-_]key|secret/.test(key) ||
      ["xs", "c_user", "datr", "sb", "fr", "lsd", "request", "extensions"].includes(key);
  }
  function safeURL(value, media = false, playback = false) {
    if (typeof value !== "string" || !value || value !== value.trim() || /[\x00-\x20\x7f#\\"'<>]/.test(value)) return null;
    try {
      const url = new URL(value);
      if (url.protocol !== "https:" || url.username || url.password || url.port || url.hash) return null;
      const host = url.hostname.toLowerCase();
      if (media) {
        const authority = value.match(/^https:\/\/([^/]+)\//i)?.[1];
        if (!authority || authority.toLowerCase() !== host || !/^(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+fbcdn\.net$/.test(host) ||
            url.search.includes(";") || value.includes("[REDACTED]") || /%(?![0-9a-f]{2})/i.test(value) ||
            [...url.searchParams.keys()].some(sensitiveKey)) return null;
      }
      if (playback && /\.(?:jpe?g|png|gif|webp|avif|svg|mpd)$/i.test(decodeURIComponent(url.pathname))) return null;
      return value;
    } catch { return null; }
  }
  function duration(value) {
    if (typeof value !== "number" || !Number.isFinite(value) || value < 0) return null;
    const seconds = Math.floor(value);
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor(seconds % 3600 / 60);
    const remainder = String(seconds % 60).padStart(2, "0");
    return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${remainder}` : `${minutes}:${remainder}`;
  }
  function safePostURL(value) {
    const href = safeURL(value);
    if (!href) return null;
    const host = new URL(href).hostname.toLowerCase();
    return host === "facebook.com" || host.endsWith(".facebook.com") || host === "fb.com" || host.endsWith(".fb.com") ? href : null;
  }
  function create(video, postURL) {
    const record = video && typeof video === "object" ? video : {};
    const wrap = node("section", "radar-video");
    const kind = labels[record.kind] || "Video";
    wrap.setAttribute("aria-label", kind);
    const thumbnail = safeURL(record.thumbnail_url, true);
    const expiresAt = record.playback_url_expires_at ? Date.parse(record.playback_url_expires_at) : NaN;
    const expired = Number.isFinite(expiresAt) && expiresAt <= Date.now();
    const playable = !expired ? safeURL(record.playback_url, true, true) : null;
    const visual = node("div", "radar-video-visual");
    const still = () => {
      if (thumbnail) {
        const image = node("img", "radar-video-thumbnail");
        image.src = thumbnail;
        image.alt = `Ảnh đại diện ${kind.toLowerCase()}`;
        image.loading = "lazy";
        image.referrerPolicy = "no-referrer";
        return image;
      }
      return node("div", "radar-video-placeholder", "▶");
    };
    const status = node("p", "radar-video-status");
    if (playable) {
      const player = node("video", "radar-video-player");
      player.controls = true;
      player.autoplay = false;
      player.preload = "metadata";
      player.playsInline = true;
      player.referrerPolicy = "no-referrer";
      if (thumbnail) player.poster = thumbnail;
      player.addEventListener("error", () => {
        player.replaceWith(still());
        status.textContent = "Không phát được video tại đây. URL phát có thể đã hết hạn.";
        status.hidden = false;
      }, {once: true});
      visual.append(player);
      player.src = playable;
    } else {
      visual.append(still());
      status.textContent = expired ? "URL phát đã hết hạn. Mở bài Facebook để xem video." : "Chưa có URL phát. Mở bài Facebook để xem video.";
    }
    wrap.append(visual);
    const details = node("div", "radar-video-details");
    const heading = node("div", "radar-video-heading");
    heading.append(node("strong", "", kind));
    const time = duration(record.duration_seconds);
    if (time !== null) heading.append(node("span", "radar-video-duration", time));
    details.append(heading);
    if (playable && (record.playback_url_ephemeral || Number.isFinite(expiresAt))) {
      details.append(node("p", "radar-video-note", "URL phát có thời hạn; bài Facebook gốc là liên kết lâu dài."));
    }
    status.hidden = Boolean(playable);
    details.append(status);
    const original = safePostURL(postURL);
    if (original) {
      const link = node("a", "radar-video-original", "Mở bài Facebook gốc ↗");
      link.href = original;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      details.append(link);
    }
    wrap.append(details);
    return wrap;
  }
  window.RadarVideo = {create};
})();

"use strict";

/* A shared, locally hosted PhotoSwipe viewer for feed and audit photographs. */
(() => {
  let coreModule;
  let activeViewer;
  const node = (tag, className, text) => {
    const element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const dimension = value => Number.isFinite(Number(value)) && Number(value) > 0 && Number(value) <= 100000 ? Number(value) : 0;
  function safeURL(value) {
    try {
      const url = new URL(value);
      return url.protocol === "https:" && !url.username && !url.password ? url.href : null;
    } catch { return null; }
  }
  function sourceLink(url, label = "Mở ảnh gốc ↗") {
    const link = node("a", "gallery-source", label);
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.referrerPolicy = "no-referrer";
    return link;
  }
  function naturalSize(image) {
    return image?.naturalWidth && image?.naturalHeight ? {width: image.naturalWidth, height: image.naturalHeight} : null;
  }
  function loadDimensions(record) {
    const loaded = naturalSize(record.thumbnail);
    if (loaded) return Promise.resolve(loaded);
    if (record.width && record.height) return Promise.resolve({width: record.width, height: record.height});
    return new Promise((resolve, reject) => {
      // Probe only the selected photograph. Other photos load as the viewer
      // needs them; opening a gallery never probes every original up front.
      const probe = new Image();
      probe.referrerPolicy = "no-referrer";
      const finish = (size, error) => {
        clearTimeout(timer);
        probe.onload = probe.onerror = null;
        if (error) reject(error); else resolve(size);
      };
      const timer = setTimeout(() => finish(null, new Error("image_timeout")), 15000);
      probe.onload = () => finish(naturalSize(probe), null);
      probe.onerror = () => finish(null, new Error("image_unavailable"));
      probe.src = record.url;
    });
  }
  async function showViewer(records, index, trigger) {
    coreModule ||= import("/static/vendor/photoswipe/photoswipe.esm.min.js").catch(error => { coreModule = null; throw error; });
    const [module, selectedSize] = await Promise.all([coreModule, loadDimensions(records[index])]);
    Object.assign(records[index], selectedSize);
    const previousViewer = activeViewer;
    const items = records.map((record, position) => {
      const size = naturalSize(record.thumbnail);
      if (size) Object.assign(record, size);
      return {src: record.url, width: record.width || 1080, height: record.height || 1080,
        alt: record.alt, galleryIndex: position};
    });
    const viewer = new module.default({
      dataSource: items, index, preload: [0, 1],
      showHideAnimationType: "fade", bgOpacity: 0.96, loop: false,
      closeTitle: "Đóng (Esc)", zoomTitle: "Phóng to hoặc thu nhỏ", arrowPrevTitle: "Ảnh trước", arrowNextTitle: "Ảnh tiếp theo",
      errorMsg: "Không tải được ảnh. Liên kết ảnh có thể đã hết hạn.",
      returnFocus: false, trapFocus: true,
    });
    activeViewer = viewer;
    previousViewer?.close();
    let destroyed = false;
    viewer.on("destroy", () => {
      destroyed = true;
      // Restore the actual opener after async imports/loading. A viewer that
      // is being replaced must not steal focus back from the newer viewer.
      if (activeViewer !== viewer) return;
      activeViewer = null;
      if (trigger?.isConnected && !trigger.disabled) trigger.focus({preventScroll: true});
    });
    viewer.on("contentLoadImage", event => { if (event.content.element) event.content.element.referrerPolicy = "no-referrer"; });
    viewer.on("loadComplete", event => {
      if (event.isError) return;
      const size = naturalSize(event.content.element);
      const position = event.content.index;
      const item = items[position];
      if (!size || !item || item.width === size.width && item.height === size.height) return;
      Object.assign(records[position], size);
      Object.assign(item, size);
      // Unknown neighbors initially have provisional dimensions. Once loaded,
      // recreate only that slide using its actual aspect ratio and zoom bounds.
      requestAnimationFrame(() => { if (!destroyed) viewer.refreshSlideContent(position); });
    });
    viewer.addFilter("contentErrorElement", (element, content) => {
      const wrap = node("div", "gallery-viewer-error");
      wrap.append(node("p", "", "Không tải được ảnh. Liên kết có thể đã hết hạn."));
      const url = safeURL(content.data.src);
      if (url) wrap.append(sourceLink(url, "Thử mở ảnh gốc ↗"));
      return wrap;
    });
    viewer.on("uiRegister", () => viewer.ui.registerElement({
      name: "custom-caption", order: 9, isButton: false, appendTo: "root",
      onInit: element => {
        const update = () => {
          const current = viewer.currIndex;
          element.replaceChildren(node("span", "gallery-viewer-position", `Ảnh ${current + 1} / ${records.length}`), sourceLink(records[current].url));
        };
        viewer.on("change", update);
        update();
      },
    }));
    viewer.init();
  }
  function create(images, {postId, compact = false} = {}) {
    const gallery = node("div", `radar-gallery${compact ? " gallery-compact" : ""}`);
    const records = (Array.isArray(images) ? images : []).map(value => {
      const data = typeof value === "string" ? {url: value} : value;
      const url = safeURL(data?.url);
      if (!url) return null;
      return {url, width: dimension(data.width), height: dimension(data.height)};
    }).filter(Boolean);
    if (!records.length) { gallery.append(node("p", "gallery-error", "Chưa có ảnh hợp lệ để hiển thị.")); return gallery; }
    gallery.setAttribute("aria-label", `Bộ ảnh${postId ? ` bài ${postId}` : ""}: ${records.length} ảnh`);
    const status = node("p", "gallery-status"); status.setAttribute("role", "status"); status.hidden = true;
    let opening = false;
    const open = async (index, button) => {
      if (opening) return;
      opening = true; button.setAttribute("aria-busy", "true");
      status.textContent = "Đang mở ảnh…"; status.hidden = false;
      try { await showViewer(records, index, button); status.hidden = true; }
      catch { status.textContent = "Chưa mở được ảnh. Bạn có thể thử lại hoặc dùng liên kết ảnh gốc."; }
      finally { opening = false; button.removeAttribute("aria-busy"); }
    };
    records.forEach((record, index) => {
      record.alt = `Ảnh ${index + 1} / ${records.length}${postId ? ` của bài ${postId}` : ""}`;
      if (compact && index > 0) return;
      const tile = node("figure", "gallery-tile");
      const button = node("button", "gallery-open"); button.type = "button";
      button.setAttribute("aria-label", `Mở ${record.alt.toLowerCase()}`);
      const image = node("img"); image.loading = "lazy"; image.decoding = "async"; image.referrerPolicy = "no-referrer"; image.alt = record.alt;
      record.thumbnail = image;
      const error = node("p", "gallery-error", "Chưa tải được ảnh; hãy thử mở ảnh gốc."); error.setAttribute("role", "status"); error.hidden = true;
      image.addEventListener("load", () => { const size = naturalSize(image); if (size) Object.assign(record, size); tile.classList.remove("gallery-image-failed"); error.hidden = true; });
      image.addEventListener("error", () => { tile.classList.add("gallery-image-failed"); error.hidden = false; });
      image.src = record.url;
      button.append(image, node("span", "gallery-position", `${index + 1} / ${records.length}`));
      button.addEventListener("click", () => open(index, button));
      const caption = node("figcaption", "gallery-caption");
      if (compact) {
        const all = node("button", "gallery-view-all", `Xem ${records.length} ảnh`); all.type = "button";
        all.addEventListener("click", () => open(0, all)); caption.append(all);
      } else caption.append(node("span", "", `Ảnh ${index + 1}`));
      caption.append(sourceLink(record.url));
      tile.append(button, caption, error); gallery.append(tile);
    });
    gallery.append(status);
    return gallery;
  }
  window.RadarGallery = Object.freeze({create});
})();

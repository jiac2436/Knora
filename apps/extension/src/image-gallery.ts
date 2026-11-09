// @ts-check

/** Keep self-contained: executed in the page's MAIN world; returns only current-note image URLs. */
export function readXiaohongshuImages(capturedUrl) {
  try {
    if (location.href !== capturedUrl) return null;
    const match = location.pathname.match(/\/(?:explore|discovery\/item|search_result)\/([a-zA-Z0-9]+)(?:\/|$)/);
    if (!match) return null;
    const id = match[1];
    const state = window.__INITIAL_STATE__;
    const note = state?.note?.noteDetailMap?.[id]?.note;
    if (!note || (note.noteId && note.noteId !== id) || !Array.isArray(note.imageList)) return null;
    const imageUrls = note.imageList.slice(0, 200).map((image) => {
      const candidates = [image.urlDefault, ...(Array.isArray(image.infoList) ? image.infoList.map(item => item.url) : []), image.url, image.urlPre];
      return candidates.find(value => typeof value === "string" && /^https?:\/\//i.test(value)) ?? null;
    }).filter(Boolean);
    return { imageUrls, total: note.imageList.length };
  } catch { return null; }
}

export function galleryImages(values = []) {
  return [...new Set(values.filter(value => {
    if (typeof value !== "string") return false;
    try {
      const url = new URL(value);
      return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password
        || /^data:image\/(png|jpeg|gif|webp);base64,/i.test(value);
    } catch { return false; }
  }))].slice(0, 200);
}

/** Single-image viewer; resources are displayed by URL, not archived or passed into the page. */
export function createImageGallery(container) {
  let images = [];
  let index = 0;
  let visible = false;
  const scroll = container.querySelector(".image-gallery-scroll");
  const previous = container.querySelector(".image-gallery-previous");
  const next = container.querySelector(".image-gallery-next");
  const counter = container.querySelector(".image-gallery-counter");
  const status = container.querySelector(".image-gallery-status");
  const paint = () => {
    previous.disabled = !images.length || index === 0;
    next.disabled = !images.length || index === images.length - 1;
    counter.textContent = `${images.length ? index + 1 : 0}/${images.length}`;
    scroll.replaceChildren();
    if (!visible || !images.length) {
      status.textContent = images.length ? "" : "当前未读取到帖子图片。";
      return;
    }
    status.textContent = "正在加载图片…";
    const image = document.createElement("img");
    image.alt = `帖子图片 ${index + 1}`;
    image.referrerPolicy = "no-referrer";
    image.decoding = "async";
    image.addEventListener("load", () => { if (image.parentElement === scroll) status.textContent = ""; });
    image.addEventListener("error", () => {
      if (image.parentElement !== scroll) return;
      image.hidden = true;
      status.textContent = "此图片无法加载，可能受防盗链或链接有效期限制。仍可切换其他图片。";
    });
    image.src = images[index];
    scroll.append(image);
    scroll.scrollTop = 0;
  };
  const move = (delta) => {
    const target = Math.max(0, Math.min(images.length - 1, index + delta));
    if (target !== index) { index = target; paint(); }
  };
  previous.addEventListener("click", () => { move(-1); container.focus({ preventScroll: true }); });
  next.addEventListener("click", () => { move(1); container.focus({ preventScroll: true }); });
  container.addEventListener("keydown", (event) => {
    if (!visible || event.altKey || event.ctrlKey || event.metaKey || event.shiftKey) return;
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    move(event.key === "ArrowLeft" ? -1 : 1);
  });
  return {
    reset(values) { images = galleryImages(values); index = 0; paint(); },
    show(value) { visible = value; container.hidden = !value; paint(); if (value) container.focus({ preventScroll: true }); }
  };
}

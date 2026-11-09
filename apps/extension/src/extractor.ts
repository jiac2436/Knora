// @ts-check

/**
 * Keep this function self-contained: Chrome serializes it into the active tab.
 * HTML is a static snapshot of the capture region, not a runnable page.
 * @param {'page'|'selection'} mode
 */
export function extractFromPage(mode) {
  const normalize = (value) => value.replace(/\u00a0/g, " ").replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
  const warnings = [];
  const warn = (code, message) => {
    if (!warnings.some((item) => item.code === code)) warnings.push({ code, message });
  };
  const capturedAt = new Date().toISOString();
  let title = document.title.trim();
  const isXiaohongshu = location.hostname === "xiaohongshu.com" || location.hostname.endsWith(".xiaohongshu.com");
  const selection = globalThis.getSelection?.();
  const selectedText = normalize(selection?.toString() ?? "");
  const excluded = "script, style, noscript, template, form, input, textarea, select, button, [hidden], [aria-hidden='true']";
  const unsupported = "iframe, frame, object, embed, canvas, video, audio";
  const maxHtmlBytes = 1_048_576;
  const maxNodes = 12_000;
  const maxStyledNodes = 2_000;
  const safeAttributes = new Set(["id", "class", "title", "alt", "lang", "dir", "role", "aria-label", "colspan", "rowspan", "datetime", "start", "reversed", "open", "viewBox", "d", "fill", "stroke", "stroke-width", "points", "cx", "cy", "r", "x", "y", "width", "height"]);
  const styleProperties = ["display", "box-sizing", "color", "background-color", "font-family", "font-size", "font-weight", "font-style", "line-height", "letter-spacing", "text-align", "text-decoration", "text-indent", "white-space", "word-break", "overflow-wrap", "width", "max-width", "min-width", "padding-top", "padding-right", "padding-bottom", "padding-left", "margin-top", "margin-right", "margin-bottom", "margin-left", "border-top", "border-right", "border-bottom", "border-left", "border-radius", "box-shadow", "list-style-type", "list-style-position", "flex-direction", "flex-wrap", "align-items", "justify-content", "gap", "grid-template-columns", "grid-column", "vertical-align", "border-collapse", "object-fit"];
  const visible = (node) => {
    const style = getComputedStyle(node);
    // Opacity is deliberately ignored: lazy/animated content may be temporarily transparent.
    return (typeof node.checkVisibility !== "function" || node.checkVisibility({ checkVisibilityCSS: true }))
      && style.display !== "none" && style.visibility !== "hidden" && style.visibility !== "collapse";
  };
  const absoluteUrl = (value, image = false) => {
    if (!value) return null;
    try {
      const url = new URL(value, document.baseURI);
      if (["http:", "https:"].includes(url.protocol)) return url.href;
      if (image && /^data:image\/(png|jpeg|gif|webp);base64,/i.test(value)) return value;
    } catch { /* Invalid or unsupported URLs are omitted. */ }
    return null;
  };

  let source;
  let scope;
  let noteDetail = null;
  const blocked = (reason, message) => ({
    mode, title, url: location.href, text: "", imageUrls: [], capturedAt, coverage: "link_only",
    site: "xiaohongshu", captureBlocked: reason, warnings: [{ code: reason, message }], htmlSnapshot: null, preview: null
  });
  if (isXiaohongshu) {
    // A mask may also contain recommendations. Capture only its note container.
    const details = Array.from(document.querySelectorAll(
      ".note-detail-mask #noteContainer, .note-detail-mask .note-container, #noteContainer, [role='dialog'] .note-container"
    )).filter((node) => visible(node) && !node.closest("[hidden], [aria-hidden='true']")
      && node.querySelector("#detail-title, #detail-desc, .note-content, .media-container, .note-slider, .slider-container"));
    noteDetail = details.at(-1) ?? null;
    if (!noteDetail) return blocked("xiaohongshu_detail_required", "请先点击一篇小红书帖子打开详情，再点击“读取页面正文”抓取；当前不会采集下方帖子列表。");
    source = noteDetail;
    scope = "article";
    const heading = noteDetail.querySelector("#detail-title, .note-content .title");
    if (heading && visible(heading) && normalize(heading.textContent ?? "")) title = normalize(heading.textContent ?? "");
  } else {
    const candidates = Array.from(document.querySelectorAll("article, main, [role='main']"))
      .filter((node) => visible(node) && !node.closest(excluded));
    const semanticSource = candidates.sort((a, b) => (b.textContent?.length ?? 0) - (a.textContent?.length ?? 0))[0];
    source = semanticSource ?? document.body;
    scope = semanticSource ? "article" : "body";
  }
  let range = null;
  if (mode === "selection") {
    scope = "selection";
    if (selection?.rangeCount && selectedText) {
      range = selection.getRangeAt(0);
      if (noteDetail && (!noteDetail.contains(range.startContainer) || !noteDetail.contains(range.endContainer))) {
        return blocked("xiaohongshu_selection_outside_detail", "请在当前帖子的详情内选择文字，再点击“读取选中片段”；不会采集背景列表中的选区。");
      }
      source = range.commonAncestorContainer.nodeType === Node.ELEMENT_NODE
        ? range.commonAncestorContainer : range.commonAncestorContainer.parentElement;
    } else {
      return { mode, title, url: location.href, text: "", imageUrls: [], capturedAt, coverage: "link_only", warnings, htmlSnapshot: null, preview: null };
    }
  }
  if (!(source instanceof Element)) {
    return { mode, title, url: location.href, text: "", imageUrls: [], capturedAt, coverage: "link_only", warnings, htmlSnapshot: null, preview: null };
  }
  if (scope === "body") warn("body_fallback", "未识别到独立正文区域，采集范围可能包含导航、推荐或评论。可改用“读取选中片段”。");

  let noteContent = null;
  if (noteDetail && mode === "page") {
    // Read only visible text nodes; preserve author prose while separating topic links and UI.
    const readText = (root, omitted = new Set()) => {
      const parts = [];
      let visited = 0;
      const walk = (node) => {
        if (++visited > 12000) return;
        if (node.nodeType === Node.TEXT_NODE) { parts.push(node.textContent ?? ""); return; }
        if (!(node instanceof Element) || omitted.has(node) || node.matches(excluded) || !visible(node)) return;
        if (node.matches("img")) { if (node.getAttribute("alt")) parts.push(node.getAttribute("alt")); return; }
        const block = node.matches("p, div, section, li, br");
        if (block) parts.push("\n");
        for (const child of node.childNodes) walk(child);
        if (block) parts.push("\n");
      };
      if (root) walk(root);
      if (visited > 12000) warn("note_structure_limit", "部分文字结构过大，当前识别可能不完整。");
      return normalize(parts.join(""));
    };
    const description = noteDetail.querySelector("#detail-desc, .note-content .desc, .note-content .description");
    const content = description ?? noteDetail.querySelector(".note-content");
    const topicNodes = Array.from(content?.querySelectorAll("a, .tag, .tag-link") ?? [])
      .filter(node => visible(node) && readText(node).startsWith("#"));
    const tags = [...new Set(topicNodes.map(node => readText(node).replace(/^#/, "").trim()).filter(Boolean))].slice(0, 100);
    const omitted = new Set(topicNodes);
    if (!description) {
      for (const heading of content?.querySelectorAll("#detail-title, h1, .title") ?? []) omitted.add(heading);
      if (content) warn("note_body_fallback", "未识别到独立正文节点，正文按帖子内容区域提取，请核对。");
    }
    const body = readText(content, omitted);
    const authorNode = [".author .username", ".author .name", ".author-wrapper .username", ".author-wrapper .name", ".author"]
      .map(selector => noteDetail.querySelector(selector))
      .find(node => node && !node.closest(".comments-container, .comments-el, .comment-list"));
    const author = readText(authorNode).slice(0, 100);
    const commentRoot = noteDetail.querySelector(".comments-container, .comments-el, .comment-list");
    let items = Array.from(commentRoot?.querySelectorAll(".comment-item") ?? []);
    const itemSelector = items.length ? ".comment-item" : ".comment-inner-container";
    if (!items.length) items = Array.from(commentRoot?.querySelectorAll(itemSelector) ?? []);
    items = items.filter(visible);
    const comments = [];
    const itemIDs = new Map();
    for (const item of items.slice(0, 200)) {
      const own = (selector) => Array.from(item.querySelectorAll(selector))
        .find(node => node.closest(itemSelector) === item && visible(node));
      const contentNode = own(".content .note-text, .content .text, .comment-content, .content");
      const text = readText(contentNode);
      if (!text) continue;
      const id = `comment-${comments.length + 1}`;
      const name = readText(own(".author-wrapper .name, .author .name, .user-name, .nickname, .name")).slice(0, 100);
      const authorBadge = own(".author-wrapper .tag, .author-tag");
      const group = item.closest(".parent-comment");
      const parentItem = item.parentElement?.closest(itemSelector)
        ?? (group ? group.querySelector(itemSelector) : null);
      const parentId = parentItem && parentItem !== item ? itemIDs.get(parentItem) ?? null : null;
      const replyTo = readText(own(".reply-to .name, .reply-user, .reply-to")).slice(0, 100) || null;
      comments.push({ id, author: name || "未识别作者", text: text.slice(0, 2000),
        isAuthor: readText(authorBadge) === "作者", parentId, replyTo });
      itemIDs.set(item, id);
      if (text.length > 2000) warn("comment_limit", "部分评论超过 2000 字，已截断；请核对评论预览。");
    }
    if (items.length > 200) warn("comment_limit", "评论较多，本次最多识别 200 条已加载评论。");
    const countMatch = readText(commentRoot?.querySelector(".total, .comments-total, .comment-count") ?? commentRoot)
      .match(/共\s*([\d,]+)\s*条评论/);
    const commentTotal = countMatch ? Number(countMatch[1].replace(/,/g, "")) : null;
    if (commentRoot) warn("comments_loaded_only", "评论仅包含当前页面已加载且可识别的文字，不包含未加载或折叠的回复。");
    noteContent = { title, author, body: body.slice(0, 100000), tags, comments, commentTotal, loadedComments: comments.length };
    if (body.length > 100000) warn("text_limit", "正文超过 100,000 字符，本次仅保留前 100,000 字符。");
  }

  const intersects = (node) => !range || range.intersectsNode(node);
  let nodeCount = 0;
  let styledNodes = 0;
  let limitReached = false;
  const imageUrls = [];
  const textParts = [];
  if (noteDetail && mode === "page") {
    // Inactive carousel slides may be hidden. Read their URLs without including
    // hidden text or unrelated avatars in the semantic snapshot.
    const media = noteDetail.querySelector(".media-container, .note-slider, .slider-container, .swiper");
    for (const image of media?.querySelectorAll("img") ?? []) {
      const src = absoluteUrl(image.getAttribute("data-src") || image.getAttribute("data-original") || image.currentSrc || image.getAttribute("src"), true);
      if (src && !imageUrls.includes(src) && imageUrls.length < 200) imageUrls.push(src);
    }
  }
  // Construct snapshots in a detached document so resource nodes stay inert.
  const snapshotDocument = document.implementation.createHTMLDocument("");
  // Build a semantic HTML tree and a separate styled preview in the same traversal.
  const cloneNode = (node, depth = 0) => {
    if (!intersects(node)) return null;
    if (++nodeCount > maxNodes || depth > 100) {
      limitReached = true;
      return null;
    }
    if (node.nodeType === Node.TEXT_NODE) {
      let value = node.textContent ?? "";
      if (range?.startContainer === node) value = value.slice(range.startOffset);
      if (range?.endContainer === node) value = value.slice(0, range.endOffset - (range.startContainer === node ? range.startOffset : 0));
      textParts.push(value);
      return [snapshotDocument.createTextNode(value), snapshotDocument.createTextNode(value)];
    }
    if (!(node instanceof Element) || node.matches(excluded) || !visible(node)) return null;
    if (isXiaohongshu && node.matches(".comments-container, .comment-list, .interaction-container, .engage-bar, .note-recommend, .recommend-container, .related-notes, .close-circle")) return null;
    if (node.shadowRoot) warn("shadow_dom", "检测到 Shadow DOM 组件，组件内部内容尚未采集，预览和保存内容可能不完整。封闭的 Shadow DOM 无法可靠检测。");
    if (node.matches(unsupported)) {
      const tag = node.tagName.toLowerCase();
      if (tag === "iframe" || tag === "frame") warn("iframe", "检测到嵌入页面（iframe），其中内容尚未采集。可在新标签页打开嵌入页面后单独保存。");
      else warn("special_content", "检测到画布、音视频或嵌入对象，这些内容无法从 HTML 正文直接获取，需单独采集或解析。");
      const placeholder = snapshotDocument.createElement("p");
      placeholder.textContent = `[未采集的 ${tag} 内容]`;
      // Placeholders appear only in the preview, never in indexed article text.
      return [snapshotDocument.createTextNode(""), placeholder];
    }
    if (node.matches("link, meta, base, foreignObject, use, animate, animateMotion, animateTransform, set")) return null;
    const tag = node.tagName.toLowerCase();
    if (tag.includes("-") && !node.shadowRoot) warn("custom_component", "检测到自定义网页组件，其封闭 Shadow DOM 内容无法可靠检测或采集，可能存在遗漏。");
    const semantic = tag.includes("-") || tag === "body" ? snapshotDocument.createElement("div") : snapshotDocument.createElementNS(node.namespaceURI, node.localName);
    for (const attribute of Array.from(semantic.attributes)) semantic.removeAttribute(attribute.name);
    for (const attribute of Array.from(node.attributes)) {
      if (safeAttributes.has(attribute.name)) semantic.setAttribute(attribute.name, attribute.value);
    }
    if (tag === "a") {
      const href = absoluteUrl(node.getAttribute("href"));
      if (href) semantic.setAttribute("href", href);
    }
    if (tag === "img") {
      const src = absoluteUrl(node.currentSrc || node.getAttribute("src") || node.getAttribute("data-src"), true);
      if (src) {
        semantic.setAttribute("src", src);
        if (!isXiaohongshu && /^https?:/.test(src) && !imageUrls.includes(src) && imageUrls.length < 50) imageUrls.push(src);
      }
      if (!node.complete || !node.naturalWidth) warn("unloaded_images", "部分图片尚未加载，快照可能只保留图片链接或说明。图片引用不会作为原图归档。");
    }
    const styled = semantic.cloneNode(false);
    // Preview links cannot navigate the sandbox away from the snapshot.
    styled.removeAttribute("href");
    if (tag === "img") {
      styled.setAttribute("referrerpolicy", "no-referrer");
      styled.setAttribute("loading", "eager");
    }
    if (++styledNodes <= maxStyledNodes) {
      const computed = getComputedStyle(node);
      for (const property of styleProperties) {
        const value = computed.getPropertyValue(property);
        // Do not carry remote CSS resources into the preview.
        if (value && !/url\s*\(/i.test(value)) styled.style.setProperty(property, value);
      }
      // Remove internal clipping so the snapshot can be read from top to bottom.
      styled.style.setProperty("height", "auto");
      styled.style.setProperty("max-height", "none");
      styled.style.setProperty("overflow", "visible");
      if (["fixed", "sticky"].includes(computed.position)) styled.style.setProperty("position", "static");
    }
    if (node.matches("p, div, section, article, main, li, h1, h2, h3, h4, h5, h6, pre, blockquote, tr, br")) textParts.push("\n");
    if (node.matches("td, th")) textParts.push(" ");
    for (const child of node.childNodes) {
      if (limitReached) break;
      const cloned = cloneNode(child, depth + 1);
      if (cloned) {
        semantic.append(cloned[0]);
        styled.append(cloned[1]);
      }
    }
    if (node.matches("p, div, section, article, main, li, h1, h2, h3, h4, h5, h6, pre, blockquote, tr, br")) textParts.push("\n");
    if (node.matches("td, th")) textParts.push(" ");
    return [semantic, styled];
  };
  const trees = cloneNode(source);
  const fullText = noteContent ? noteContent.body || noteContent.title : normalize(textParts.join(""));
  const text = fullText.slice(0, 100_000);
  if (fullText.length > text.length) warn("text_limit", "正文超过 100,000 字符，当前文字保存仅包含前 100,000 字符。可分段选中保存。");
  if (limitReached) warn("node_limit", "页面结构过大，已停止采集后续节点，当前内容不完整。可分段选中保存。");
  if (styledNodes > maxStyledNodes) warn("style_limit", "页面较复杂，后半部分预览采用简化样式，内容仍可滚动查看。");
  const escape = (value) => value.replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
  let html = trees ? `<!doctype html><html><head><meta charset="utf-8"><title>${escape(title)}</title></head><body>${trees[0].outerHTML}</body></html>` : "";
  if (new TextEncoder().encode(html).byteLength > maxHtmlBytes) {
    html = "";
    warn("html_limit", "HTML 快照超过 1 MB，本次不保留 HTML；仍可保存已提取文字。可分段选中保存。");
  }
  let preview = null;
  if (trees) {
    const width = Math.max(320, Math.ceil(source.getBoundingClientRect().width || 720));
    trees[1].style.setProperty("width", `${width}px`);
    trees[1].style.setProperty("min-width", "0");
    trees[1].style.setProperty("max-width", "none");
    trees[1].style.setProperty("margin", "0");
    const previewHtml = trees[1].outerHTML;
    if (new TextEncoder().encode(previewHtml).byteLength <= 2_097_152) preview = { html: previewHtml, width };
    else warn("preview_limit", "样式快照过大，已切换为可滚动的文字预览。");
  }
  const partial = mode === "selection" || scope === "body" || warnings.some((item) => ["iframe", "shadow_dom", "special_content", "node_limit", "text_limit", "comment_limit", "note_structure_limit", "note_body_fallback"].includes(item.code));
  return {
    mode, title, url: location.href, text, imageUrls, capturedAt, noteContent,
    site: isXiaohongshu ? "xiaohongshu" : null,
    coverage: text ? (partial || (noteContent?.commentTotal ?? 0) > (noteContent?.loadedComments ?? 0) ? "partial_text" : "full_text") : "link_only",
    warnings, preview,
    htmlSnapshot: html ? { schemaVersion: 1, format: "html", html, sourceUrl: location.href, baseUrl: document.baseURI, capturedAt, scope, warnings } : null
  };
}

// @ts-check
import { createCapturePayload, createQueueItem, MAX_NOTE_LENGTH, STORAGE_KEYS } from "./core.js";
import { connectLocalApp, LOCAL_CATALOG } from "./local-connection.js";
import { captureText, noteSections, commentTreeLines } from "./note-text.js";
import { createImageGallery, galleryImages, readXiaohongshuImages } from "./image-gallery.js";
import { isXiaohongshuURL } from "./site.js";
import { extractFromPage } from "./extractor.js";
import { createPreviewDocument, renderCaptureWarnings } from "./preview.js";
import { fetchDomains, fetchProjects } from "./api.js";
import { getBinding, getQueue, getRecent, readStorage, writeStorage } from "./storage.js";

const state = { binding: null, capture: null, mode: "page", previewKind: "snapshot", busy: false };

/** @param {string} id */
const element = (id) => document.getElementById(id);
const imageGallery = createImageGallery(element("image-gallery"));

function setStatus(message, kind = "info") {
  const target = element("status");
  target.textContent = message;
  target.dataset.kind = kind;
}

function setBusy(busy) {
  state.busy = busy;
  element("save").disabled = busy || !state.binding || Boolean(state.capture?.captureBlocked)
    || Boolean(state.capture?.noteContent && state.capture?.mode === "page" && !captureText(state.capture).text.trim());
  for (const checkbox of element("content-sections").querySelectorAll("input")) checkbox.disabled = busy;
  element("capture-page").disabled = busy;
  element("capture-selection").disabled = busy;
}

async function activeTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab?.id) throw new Error("无法读取当前标签页");
  return tab;
}

/** @param {'page'|'selection'} mode */
async function capture(mode) {
  setBusy(true);
  setStatus("正在读取当前页面…");
  try {
    const tab = await activeTab();
    element("capture-site").hidden = !isXiaohongshuURL(tab.url ?? "");
    state.capture = null;
    renderPreview();
    let result;
    try {
      const injected = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func: extractFromPage, args: [mode] });
      result = injected[0]?.result;
    } catch {
      result = {
        mode,
        title: tab.title || "未命名页面",
        url: tab.url || "",
        text: "",
        imageUrls: [],
        capturedAt: new Date().toISOString(),
        coverage: "link_only",
        warnings: [{ code: "restricted_page", message: "浏览器不允许插件读取此页面，当前只能保存链接和备注。" }],
        preview: null,
        htmlSnapshot: null
      };
    }
    if (!result) throw new Error("页面没有返回可保存内容");
    if (result.site === "xiaohongshu" && !result.captureBlocked && mode === "page") {
      try {
        const media = await chrome.scripting.executeScript({ target: { tabId: tab.id }, world: "MAIN",
          func: readXiaohongshuImages, args: [result.url] });
        const list = media[0]?.result;
        if (Array.isArray(list?.imageUrls) && list.imageUrls.length) {
          result.imageUrls = galleryImages(list.imageUrls);
          result.imageListTotal = list.total;
        }
      } catch { /* Page state is optional; keep the current detail's DOM image references. */ }
    }
    if (isXiaohongshuURL(tab.url ?? "") && result.warnings?.some((warning) => warning.code === "restricted_page")) {
      result.captureBlocked = "restricted_page";
    }
    if (result.captureBlocked) {
      setStatus(result.warnings?.[0]?.message ?? "请先打开小红书帖子详情后再读取", "warning");
    } else if (mode === "selection" && !result.text) {
      setStatus("当前没有选中文字，请先在页面中选择内容", "warning");
    } else if (!result.text) {
      setStatus("未提取到正文，将保存链接和备注", "warning");
    } else {
      setStatus(mode === "selection" ? "已读取选中片段" : result.site === "xiaohongshu" ? "已读取小红书帖子详情" : "已读取页面正文", "success");
    }
    if (result.noteContent && result.mode === "page") result.selectedSections = noteSections(result).map(section => section.id);
    state.capture = result;
    state.mode = mode;
    renderPreview();
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "读取页面失败", "error");
  } finally {
    setBusy(false);
  }
}

function renderPreview() {
  const capture = state.capture;
  const ready = Boolean(capture && !capture.captureBlocked);
  state.previewKind = capture?.preview ? "snapshot" : "plain";
  renderSectionChoices();
  imageGallery.show(false);
  imageGallery.reset(ready ? capture.imageUrls ?? [] : []);
  element("preview-images").hidden = !ready || capture.site !== "xiaohongshu";
  element("preview-images").setAttribute("aria-pressed", "false");
  element("preview-empty").hidden = ready;
  element("preview-empty").textContent = capture?.captureBlocked
    ? "打开帖子详情后重新读取，预览将在这里显示。" : "尚未读取页面";
  element("preview-content").hidden = !ready;
  if (!ready) element("preview-frame").removeAttribute("srcdoc");
  if (!ready) return;
  element("preview-title").textContent = capture.title || "未命名页面";
  element("preview-url").textContent = capture.url;
  renderSavedText();
  const frame = element("preview-frame");
  const hasSnapshot = Boolean(capture.preview);
  element("preview-mode").hidden = !hasSnapshot && capture.site !== "xiaohongshu";
  element("preview-snapshot").hidden = !hasSnapshot;
  frame.hidden = !hasSnapshot;
  element("preview-text").hidden = hasSnapshot;
  element("preview-snapshot").setAttribute("aria-pressed", String(hasSnapshot));
  element("preview-plain").setAttribute("aria-pressed", String(!hasSnapshot));
  if (hasSnapshot) {
    frame.srcdoc = createPreviewDocument(capture.preview, Math.max(1, element("preview-viewport").clientWidth - 16));
  } else {
    frame.removeAttribute("srcdoc");
  }
  element("preview-note").textContent = capture.noteContent
    ? "切换“保存文字”查看标题、正文、话题标签及已加载评论；评论与作者正文分区保存。"
    : hasSnapshot
    ? "当前采集区域的静态缩影，可上下滚动；切换“保存文字”可核对本次保存内容。"
    : "可上下滚动查看本次保存的文字。";

}

function renderSectionChoices() {
  const container = element("content-sections");
  container.replaceChildren();
  const capture = state.capture;
  const sections = capture && !capture.captureBlocked ? noteSections(capture) : [];
  container.hidden = !sections.length || state.previewKind !== "plain";
  for (const section of sections) {
    const label = document.createElement("label");
    label.className = "section-choice";
    label.dataset.section = section.id;
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.value = section.id;
    checkbox.checked = !Array.isArray(capture.selectedSections) || capture.selectedSections.includes(section.id);
    checkbox.disabled = state.busy;
    checkbox.addEventListener("change", () => {
      capture.selectedSections = Array.from(container.querySelectorAll("input:checked"), input => input.value);
      renderSavedText();
      setBusy(state.busy);
    });
    label.append(checkbox, document.createTextNode(section.label));
    container.append(label);
  }
}

function renderSavedText() {
  const capture = state.capture;
  if (!capture || capture.captureBlocked) return;
  const formatted = captureText(capture);
  const target = element("preview-text");
  target.replaceChildren();
  if (formatted.sections.length && !formatted.truncated) {
    formatted.sections.forEach((section, index) => {
      if (index) target.append(document.createTextNode("\n\n"));
      const block = document.createElement("span");
      block.dataset.section = section.id;
      if (section.id === "comments") {
        const lines = commentTreeLines(capture.noteContent.comments);
        const tree = lines.map(line => line.text).join("\n");
        block.append(document.createTextNode(tree ? section.text.slice(0, -tree.length) : section.text));
        lines.forEach((line, lineIndex) => {
          if (lineIndex) block.append(document.createTextNode("\n"));
          if (line.heading) {
            const heading = document.createElement("span");
            heading.className = "comment-heading";
            heading.textContent = line.text;
            heading.title = line.text.trim();
            block.append(heading);
          } else block.append(document.createTextNode(line.text));
        });
      } else block.textContent = section.text;
      target.append(block);
    });
  } else {
    target.textContent = formatted.text || (capture.noteContent
      ? "请至少勾选一项内容后保存。" : "未提取到正文，将按仅链接保存。");
  }
  renderCaptureWarnings(element("capture-warnings"), [...(capture.warnings ?? []),
    ...(formatted.truncated ? [{ code: "formatted_text_limit", message: "标题、正文和评论的总文字超过保存上限，后续内容未包含；请核对保存文字。" }] : [])]);
  const count = capture.imageUrls?.length ?? 0;
  element("preview-meta").textContent = `${formatted.text.length.toLocaleString()} 字符 · 检测到 ${count} 个图片引用（整页保存时提交可下载的 HTTP 图片链接，由本地 App 下载归档）${capture.noteContent ? ` · 已识别 ${capture.noteContent.comments.length} 条评论/回复` : ""}`;
}

/** @param {HTMLSelectElement} select @param {Array<{id: string, name: string}>} items @param {string} placeholder */
function fillSelect(select, items, placeholder) {
  select.replaceChildren(new Option(placeholder, ""));
  for (const item of items) select.add(new Option(item.name, item.id));
}

async function loadTaxonomy() {
  if (!state.binding) return;
  try {
    const [domains, projects] = await Promise.all([fetchDomains(state.binding), fetchProjects(state.binding)]);
    fillSelect(element("domain"), domains?.items ?? [], "选择知识域（必选）");
    fillSelect(element("project"), projects?.items ?? [], "不关联项目");
    await writeStorage(LOCAL_CATALOG, { libraryId: state.binding.accountId, domains: domains?.items ?? [], projects: projects?.items ?? [] });
  } catch (error) {
    const cached = await readStorage(LOCAL_CATALOG, null);
    if (cached?.libraryId === state.binding.accountId) {
      fillSelect(element("domain"), cached.domains, "选择知识域（必选）");
      fillSelect(element("project"), cached.projects, "不关联项目");
    } else {
      fillSelect(element("domain"), [], "请启动 App 后读取知识域");
      fillSelect(element("project"), [], "不关联项目");
    }
  }
}

async function save() {
  if (!state.binding) {
    setStatus("请先启动本地 App 完成首次连接", "warning");
    return;
  }
  if (state.capture?.captureBlocked) {
    setStatus(state.capture.warnings?.[0]?.message ?? "请先打开帖子详情后重新读取", "warning");
    return;
  }
  if (!state.capture) {
    setStatus("请先读取页面正文或选中片段", "warning");
    return;
  }
  const domainId = element("domain").value;
  if (!domainId) {
    setStatus("请选择知识域", "warning");
    return;
  }
  setBusy(true);
  try {
    const context = {
      domainId,
      projectId: element("project").value,
      note: element("note").value,
      saveReason: element("save-reason").value
    };
    const manifest = chrome.runtime.getManifest();
    const payload = createCapturePayload(state.capture, context, manifest.version);
    const sections = noteSections(state.capture);
    const allSelected = sections.every(section => state.capture.selectedSections?.includes(section.id) ?? true);
    const item = { ...createQueueItem(payload, state.binding), htmlSnapshot: allSelected ? state.capture.htmlSnapshot ?? null : null };
    const queued = await chrome.runtime.sendMessage({ type: "ENQUEUE_CAPTURE", item });
    if (!queued?.ok) throw new Error(queued?.error || "无法写入本机队列");
    await writeStorage(STORAGE_KEYS.draft, { domainId, projectId: context.projectId });
    setStatus("已保存在插件队列，正在同步到本地 App", "success");
    element("note").value = "";
    element("save-reason").value = "";
    await renderQueue();
    chrome.runtime.sendMessage({ type: "SYNC_QUEUE" }).catch(() => {});
  } catch (error) {
    setStatus(error instanceof Error ? error.message : "本机保存失败", "error");
  } finally {
    setBusy(false);
  }
}

async function renderQueue() {
  const target = element("queue-list");
  if (!state.binding) {
    target.textContent = "首次连接本地 App 后即可保存和同步";
    return;
  }
  const [queue, recent] = await Promise.all([getQueue(), getRecent()]);
  const own = queue.filter((item) => item.accountId === state.binding.accountId);
  const latest = recent.find((item) => item.accountId === state.binding.accountId);
  target.replaceChildren();
  if (!own.length && !latest) {
    target.textContent = "暂无同步任务";
    return;
  }
  for (const item of own.slice(0, 3)) {
    const row = document.createElement("div");
    row.className = "queue-row";
    const label = item.status === "syncing" ? "同步中" : item.status === "failed" ? "同步失败" : "本机已保存";
    row.textContent = `${label} · ${item.payload.title}${item.error ? ` · ${item.error}` : ""}`;
    target.append(row);
  }
  if (latest) {
    const row = document.createElement("div");
    row.className = "queue-row success";
    row.textContent = `本地知识库已保存 · ${latest.title}`;
    target.append(row);
  }
  element("retry").hidden = !own.some((item) => item.status === "failed");
}

async function connectLocal(launch = false) {
  element("connect").disabled = true;
  element("local-connection-status").textContent = launch ? "正在启动并连接本地 App…" : "正在查找本地 App…";
  const result = await connectLocalApp(launch);
  state.binding = result?.binding ?? await getBinding();
  element("account-name").textContent = "本地知识库";
  element("connect").hidden = Boolean(result?.online);
  element("connect").disabled = false;
  element("connect").dataset.reload = String(Boolean(result.requiresReload));
  element("connect").textContent = result.requiresReload ? "重新加载插件" : "启动本地 App 并连接";
  element("local-connection-status").textContent = result.online ? "已自动连接本地 App"
    : (result.message || "本地 App 尚未连接，请启动 App 后重试。")
      + (state.binding ? " 连接已保留，可保存到本机队列，连接恢复后自动同步。" : "");
  const draft = await readStorage(STORAGE_KEYS.draft, {});
  await loadTaxonomy();
  element("domain").value = draft.domainId ?? "";
  element("project").value = draft.projectId ?? "";
  setBusy(false);
  return result;
}

async function initialize() {
  try {
    const tab = await activeTab();
    element("capture-site").hidden = !isXiaohongshuURL(tab.url ?? "");
  } catch { /* Restricted pages do not prevent local connection. */ }
  element("capture-panel").hidden = false;
  await connectLocal();
  await capture("page");
  await renderQueue();
}

function showPreview(kind) {
  state.previewKind = kind;
  renderSectionChoices();
  const images = kind === "images" && state.capture?.site === "xiaohongshu";
  const snapshot = !images && kind === "snapshot" && Boolean(state.capture?.preview);
  element("preview-frame").hidden = !snapshot;
  element("preview-text").hidden = images || snapshot;
  element("preview-snapshot").setAttribute("aria-pressed", String(snapshot));
  element("preview-plain").setAttribute("aria-pressed", String(!images && !snapshot));
  element("preview-images").setAttribute("aria-pressed", String(images));
  imageGallery.show(images);
  const total = state.capture?.imageListTotal;
  const count = galleryImages(state.capture?.imageUrls ?? []).length;
  element("preview-note").textContent = images
    ? `${Number.isInteger(total) && total > count ? `已读取 ${count}/${total} 张，图片列表可能不完整。` : ""}点击左右箭头或使用键盘 ← → 切换；长图可上下滚动。整页保存时提交 HTTP 图片链接，由本地 App 下载归档；需登录或已过期的链接可能失败。`
    : snapshot ? "当前采集区域的静态缩影，可上下滚动；切换“保存文字”可核对本次保存内容。"
    : state.capture?.noteContent ? "标题、正文与评论已分区；评论仅包含当前页面已加载的内容。" : "可上下滚动查看本次保存的文字。";
}

element("preview-snapshot").addEventListener("click", () => showPreview("snapshot"));
element("preview-images").addEventListener("click", () => showPreview("images"));
element("preview-plain").addEventListener("click", () => showPreview("plain"));
element("capture-page").addEventListener("click", () => capture("page"));
element("capture-selection").addEventListener("click", () => capture("selection"));
element("save").addEventListener("click", () => save());
element("settings").addEventListener("click", () => chrome.runtime.openOptionsPage());
element("connect").addEventListener("click", () => {
  if (element("connect").dataset.reload === "true") { chrome.runtime.reload(); return; }
  connectLocal(true).then(renderQueue).catch(error => setStatus(String(error), "error"));
});
element("retry").addEventListener("click", () => chrome.runtime.sendMessage({ type: "RETRY_QUEUE" }).then(renderQueue));
chrome.runtime.onMessage.addListener((message) => {
  if (message?.type === "QUEUE_UPDATED") renderQueue().catch(console.error);
});
element("note").maxLength = MAX_NOTE_LENGTH;
initialize().catch((error) => setStatus(String(error), "error"));

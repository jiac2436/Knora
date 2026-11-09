// @ts-check
import { captureText, noteSections } from "./note-text.js";

export const STORAGE_KEYS = Object.freeze({
  binding: "pka.binding.v1",
  queue: "pka.queue.v1",
  recent: "pka.recent.v1",
  draft: "pka.draft.v1"
});

export const MAX_TEXT_LENGTH = 100_000;
export const MAX_NOTE_LENGTH = 4_000;
export const MAX_ATTEMPTS = 8;

/** @param {string} value */
export function normalizeApiBaseUrl(value) {
  const trimmed = value.trim().replace(/\/+$/, "");
  const url = new URL(trimmed);
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new Error("API 地址只支持 http 或 https");
  }
  if (url.username || url.password || url.search || url.hash) {
    throw new Error("API 地址不能包含账号、查询参数或片段");
  }
  return url.toString().replace(/\/$/, "");
}

/** @param {string} apiBaseUrl */
export function apiOriginPattern(apiBaseUrl) {
  const url = new URL(normalizeApiBaseUrl(apiBaseUrl));
  return `${url.protocol}//${url.host}/*`;
}

/** @param {number} attempt */
export function retryDelayMs(attempt) {
  const exponent = Math.max(0, Math.min(attempt - 1, 8));
  return Math.min(15 * 60_000, 5_000 * 2 ** exponent);
}

/**
 * @param {{id?: string, user_id?: string, account_id?: string, user?: {id?: string}}} payload
 */
export function accountIdFromMe(payload) {
  const id = payload.account_id ?? payload.user_id ?? payload.user?.id ?? payload.id;
  if (typeof id !== "string" || !id.trim()) {
    throw new Error("账户接口未返回可识别的账户 ID");
  }
  return id.trim();
}

/**
 * @param {{mode: 'page'|'selection', title: string, url: string, text: string, imageUrls?: string[], capturedAt?: string, coverage?: string, captureBlocked?: string}} capture
 * @param {{domainId?: string, projectId?: string, note?: string, saveReason?: string}} context
 * @param {string} extensionVersion
 */
export function createCapturePayload(capture, context, extensionVersion) {
  if (capture.captureBlocked) throw new Error("请先打开小红书帖子详情并重新读取，再保存到知识库");
  const formatted = captureText(capture, MAX_TEXT_LENGTH);
  const text = formatted.text;
  if (capture.noteContent && capture.mode === "page" && !text.trim()) throw new Error("请至少勾选一项内容后保存");
  const note = (context.note ?? "").trim().slice(0, MAX_NOTE_LENGTH);
  const saveReason = (context.saveReason ?? "").trim().slice(0, 500);
  const filtered = capture.noteContent && Array.isArray(capture.selectedSections)
    && noteSections(capture).some(section => !capture.selectedSections.includes(section.id));
  return {
    capture_type: capture.mode === "selection" ? "browser_selection" : "browser_page",
    title: capture.site === "xiaohongshu" && capture.mode === "page"
      && Array.isArray(capture.selectedSections) && !capture.selectedSections.includes("title")
      ? "小红书笔记" : capture.title.trim() || "未命名页面",
    source_url: capture.url,
    captured_at: capture.capturedAt ?? new Date().toISOString(),
    content: { text, image_urls: capture.mode === "page"
      ? [...new Set((capture.imageUrls ?? []).filter(url => {
        try { return ["http:", "https:"].includes(new URL(url).protocol); } catch { return false; }
      }).map(value => {
        const url = new URL(value);
        // Page state may contain HTTP CDN URLs that an HTTPS browser upgrades.
        if (capture.site === "xiaohongshu" && url.protocol === "http:"
          && url.hostname.endsWith(".xhscdn.com")) url.protocol = "https:";
        return url.href;
      }))].slice(0, 200) : [] },
    coverage: formatted.truncated || filtered ? "partial_text" : capture.coverage ?? (text ? (capture.mode === "selection" ? "partial_text" : "full_text") : "link_only"),
    context: {
      domain_id: context.domainId || null,
      project_id: context.projectId || null,
      note: note || null,
      save_reason: saveReason || null
    },
    client: { kind: "browser_extension", version: extensionVersion }
  };
}

/**
 * @param {ReturnType<typeof createCapturePayload>} payload
 * @param {{accountId: string}} binding
 * @param {number} [now]
 * @param {() => string} [idFactory]
 */
export function createQueueItem(payload, binding, now = Date.now(), idFactory = () => crypto.randomUUID()) {
  if (!binding.accountId) throw new Error("保存前需要连接本地 App");
  return {
    id: idFactory(),
    accountId: binding.accountId,
    idempotencyKey: idFactory(),
    payload,
    status: "pending",
    attempts: 0,
    nextAttemptAt: now,
    createdAt: now,
    updatedAt: now,
    error: null
  };
}

/**
 * @param {Array<any>} items
 * @param {string | undefined} accountId
 * @param {number} [now]
 */
export function dueQueueItems(items, accountId, now = Date.now()) {
  if (!accountId) return [];
  return items
    .filter((item) => item.accountId === accountId)
    .filter((item) => item.status === "pending"
      || (item.status === "failed" && item.retryable !== false)
      || (item.status === "syncing" && item.updatedAt + 2 * 60_000 <= now))
    .filter((item) => (item.nextAttemptAt ?? 0) <= now && item.attempts < MAX_ATTEMPTS)
    .sort((a, b) => a.createdAt - b.createdAt);
}

/** @param {any} item @param {{message: string, retryable: boolean}} failure @param {number} [now] */
export function markQueueFailure(item, failure, now = Date.now()) {
  const attempts = item.attempts + 1;
  const retryable = failure.retryable && attempts < MAX_ATTEMPTS;
  return {
    ...item,
    status: "failed",
    attempts,
    retryable,
    nextAttemptAt: retryable ? now + retryDelayMs(attempts) : Number.MAX_SAFE_INTEGER,
    updatedAt: now,
    error: failure.message
  };
}

/** @param {unknown} error */
export function friendlyError(error) {
  if (error instanceof TypeError) return { message: "网络不可用，已保留在本机队列", retryable: true };
  if (error && typeof error === "object" && "status" in error) {
    const status = Number(error.status);
    const message = "message" in error && typeof error.message === "string" ? error.message : `服务返回 ${status}`;
    if (status === 401 || status === 403) return { message: "本地访问状态已改变，请重新连接 App", retryable: false };
    if (status === 409) return { message: message || "请求冲突", retryable: false };
    if (status === 429 || status >= 500) return { message, retryable: true };
    return { message, retryable: false };
  }
  return { message: error instanceof Error ? error.message : "未知错误", retryable: false };
}

export const MAX_HTML_SNAPSHOT_BYTES = 1_048_576;
export const MAX_RECENT_HTML_BYTES = 2_097_152;

/** Prepare a future Worker request; this helper does not upload or execute HTML. */
export function createCrawl4AIRequest(snapshot) {
  if (snapshot?.schemaVersion !== 1 || snapshot.format !== "html" || typeof snapshot.html !== "string" || !snapshot.html.trim()) {
    throw new Error("无效的 HTML 快照");
  }
  if (new TextEncoder().encode(snapshot.html).byteLength > MAX_HTML_SNAPSHOT_BYTES) {
    throw new Error("HTML 快照超过 1 MB");
  }
  return {
    urls: [`raw://${snapshot.html}`],
    browser_config: {},
    crawler_config: {}
  };
}

/** Keep recent local HTML bounded; text upload and old queue entries remain compatible. */
export function retainRecentSnapshots(entries) {
  let bytes = 0;
  let count = 0;
  return entries.slice(0, 20).map((entry) => {
    if (!entry.htmlSnapshot) return entry;
    const size = new TextEncoder().encode(entry.htmlSnapshot.html ?? "").byteLength;
    if (count >= 3 || size > MAX_HTML_SNAPSHOT_BYTES || bytes + size > MAX_RECENT_HTML_BYTES) {
      const { htmlSnapshot: _snapshot, ...metadata } = entry;
      return metadata;
    }
    bytes += size;
    count++;
    return entry;
  });
}

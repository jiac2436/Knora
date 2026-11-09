import test from "node:test";
import assert from "node:assert/strict";
import {
  accountIdFromMe,
  apiOriginPattern,
  createCapturePayload,
  createQueueItem,
  dueQueueItems,
  markQueueFailure,
  normalizeApiBaseUrl,
  retryDelayMs
} from "../dist/core.js";

test("normalizes API base URL and requests only its origin", () => {
  assert.equal(normalizeApiBaseUrl(" https://api.example.com/dev/ "), "https://api.example.com/dev");
  assert.equal(apiOriginPattern("https://api.example.com/dev"), "https://api.example.com/*");
  assert.throws(() => normalizeApiBaseUrl("file:///tmp/api"));
});

test("extracts supported account identity response shapes", () => {
  assert.equal(accountIdFromMe({ account_id: "account-a" }), "account-a");
  assert.equal(accountIdFromMe({ user: { id: "account-b" } }), "account-b");
  assert.throws(() => accountIdFromMe({}));
});

test("creates a bounded capture payload with honest coverage", () => {
  const payload = createCapturePayload({
    mode: "selection",
    title: " Example ",
    url: "https://example.com/article",
    text: " selected text ",
    imageUrls: ["https://example.com/a.png", "data:image/png;base64,abc"]
  }, { domainId: "work", projectId: "project", note: " context ", saveReason: " useful " }, "0.1.0");
  assert.equal(payload.capture_type, "browser_selection");
  assert.equal(payload.coverage, "partial_text");
  assert.deepEqual(payload.content.image_urls, []);
  assert.equal(payload.context.note, "context");
  assert.equal(payload.context.save_reason, "useful");
});

test("page capture submits ordered public image links, selections do not", () => {
  const capture = {mode:"page", title:"图文", url:"https://example.com/", text:"正文",
    imageUrls:["https://example.com/2.png", "blob:https://example.com/a", "https://example.com/1.png", "https://example.com/2.png", "data:image/png;base64,abc"]};
  assert.deepEqual(createCapturePayload(capture, {domainId:"work"}, "0.1.0").content.image_urls,
    ["https://example.com/2.png", "https://example.com/1.png"]);
  assert.deepEqual(createCapturePayload({...capture, mode:"selection"}, {domainId:"work"}, "0.1.0").content.image_urls, []);
  assert.deepEqual(createCapturePayload({...capture, site:"xiaohongshu", imageUrls:["http://sns-webpic-qc.xhscdn.com/a.png", "https://sns-webpic-qc.xhscdn.com/a.png"]}, {domainId:"work"}, "0.1.0").content.image_urls,
    ["https://sns-webpic-qc.xhscdn.com/a.png"]);
});

test("recovers a queue item left syncing after the service worker stopped", () => {
  const item = {
    id: "capture",
    accountId: "a",
    status: "syncing",
    attempts: 0,
    nextAttemptAt: 0,
    createdAt: 0,
    updatedAt: 1_000
  };
  assert.equal(dueQueueItems([item], "a", 120_999).length, 0);
  assert.equal(dueQueueItems([item], "a", 121_000).length, 1);
});

test("never returns another account queue item for synchronization", () => {
  const payload = createCapturePayload({ mode: "page", title: "A", url: "https://a.test", text: "body" }, { domainId: "work" }, "0.1.0");
  let nextId = 0;
  const idFactory = () => `id-${++nextId}`;
  const accountA = createQueueItem(payload, { accountId: "a" }, 100, idFactory);
  const accountB = createQueueItem(payload, { accountId: "b" }, 100, idFactory);
  assert.deepEqual(dueQueueItems([accountA, accountB], "b", 100).map((item) => item.accountId), ["b"]);
  assert.deepEqual(dueQueueItems([accountA, accountB], undefined, 100), []);
});

test("applies bounded exponential retry and stops non-retryable failures", () => {
  assert.equal(retryDelayMs(1), 5_000);
  assert.equal(retryDelayMs(20), 15 * 60_000);
  const failed = markQueueFailure({ attempts: 0 }, { message: "offline", retryable: true }, 1_000);
  assert.equal(failed.nextAttemptAt, 6_000);
  assert.equal(failed.retryable, true);
  const unauthorized = markQueueFailure({ attempts: 0 }, { message: "unauthorized", retryable: false }, 1_000);
  assert.equal(unauthorized.retryable, false);
  assert.equal(dueQueueItems([{ ...unauthorized, accountId: "a", createdAt: 0 }], "a", Number.MAX_SAFE_INTEGER).length, 0);
});

test("prepares uploaded HTML for Crawl4AI without changing the text API payload", async () => {
  const { createCrawl4AIRequest } = await import("../dist/core.js");
  const snapshot = { schemaVersion: 1, format: "html", html: "<h1>标题</h1><p>正文</p>" };
  const request = createCrawl4AIRequest(snapshot);
  assert.equal(request.urls[0], `raw://${snapshot.html}`);
  assert.deepEqual(request.crawler_config, {});
  assert.throws(() => createCrawl4AIRequest({ ...snapshot, html: "汉".repeat(400_000) }), /1 MB/);
  assert.throws(() => createCrawl4AIRequest({ ...snapshot, schemaVersion: 2 }), /无效/);
  const payload = createCapturePayload({ mode: "page", title: "标题", url: "https://example.com", text: "正文", htmlSnapshot: snapshot }, { domainId: "work" }, "0.1.0");
  assert.deepEqual(Object.keys(payload.content).sort(), ["image_urls", "text"]);
});

test("bounds local recent HTML retention and preserves account metadata and retry snapshots", async () => {
  const { retainRecentSnapshots } = await import("../dist/core.js");
  const htmlSnapshot = { html: "a".repeat(800_000) };
  const entries = Array.from({ length: 5 }, (_, i) => ({ id: String(i), accountId: `account-${i}`, htmlSnapshot }));
  const recent = retainRecentSnapshots(entries);
  assert.equal(recent.filter((item) => item.htmlSnapshot).length, 2);
  assert.deepEqual(recent.map((item) => item.accountId), entries.map((item) => item.accountId));
  assert.equal(retainRecentSnapshots(entries.map((item) => ({ ...item, htmlSnapshot: { html: "small" } }))).filter((item) => item.htmlSnapshot).length, 3);
  assert.equal(markQueueFailure({ attempts: 0, htmlSnapshot }, { message: "offline", retryable: true }).htmlSnapshot, htmlSnapshot);
});

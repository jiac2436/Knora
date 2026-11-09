// @ts-check
import { discoverLocal, LOCAL_BINDING } from "./local-connection.js";
import { dueQueueItems, friendlyError, markQueueFailure, STORAGE_KEYS } from "./core.js";
import { uploadCapture } from "./api.js";
import { addRecent, getBinding, getQueue, setQueue } from "./storage.js";

const ALARM_NAME = "pka-sync-queue";
let syncing = false;
let queueMutation = Promise.resolve();

/** @param {(queue: Array<any>) => Array<any>} update */
function mutateQueue(update) {
  const operation = queueMutation.then(async () => {
    const queue = await getQueue();
    const updated = update(queue);
    await setQueue(updated);
    return updated;
  });
  queueMutation = operation.catch(() => {});
  return operation;
}

async function ensureAlarm() {
  const existing = await chrome.alarms.get(ALARM_NAME);
  if (!existing) await chrome.alarms.create(ALARM_NAME, { periodInMinutes: 1 });
}

async function syncQueue() {
  if (syncing) return;
  syncing = true;
  try {
    const discovered = await discoverLocal();
    if (!discovered.online) return;
    const initialBinding = discovered.binding;
    if (!initialBinding) return;
    const due = dueQueueItems(await getQueue(), initialBinding.accountId);

    for (const candidate of due) {
      const binding = await getBinding();
      if (!binding || binding.accountId !== candidate.accountId) break;

      const queue = await getQueue();
      const current = queue.find((item) => item.id === candidate.id);
      if (!current || current.accountId !== binding.accountId) continue;

      await mutateQueue((latest) => latest.map((item) => item.id === current.id
        ? { ...item, status: "syncing", updatedAt: Date.now(), error: null }
        : item));

      try {
        const response = await uploadCapture(binding, current);
        await mutateQueue((latest) => latest.filter((item) => item.id !== current.id));
        await addRecent({
          id: current.id,
          accountId: current.accountId,
          title: current.payload.title,
          sourceId: response?.id ?? response?.source_id ?? null,
          syncedAt: Date.now(),
          status: "synced",
          // HTML stays local; the capture API receives text and image links.
          htmlSnapshot: current.htmlSnapshot ?? null
        });
      } catch (error) {
        const failure = friendlyError(error);
        await mutateQueue((latest) => latest.map((item) => item.id === current.id
          ? markQueueFailure({ ...item, status: "failed" }, failure)
          : item));
      }
    }
  } finally {
    syncing = false;
    chrome.runtime.sendMessage({ type: "QUEUE_UPDATED" }).catch(() => {});
  }
}

async function retryCurrentAccount() {
  await discoverLocal();
  const binding = await getBinding();
  if (!binding) return;
  await mutateQueue((queue) => queue.map((item) => item.accountId === binding.accountId
    ? { ...item, status: "pending", retryable: true, nextAttemptAt: Date.now(), error: null }
    : item));
  await syncQueue();
}

chrome.runtime.onInstalled.addListener(() => {
  ensureAlarm().then(syncQueue).catch(console.error);
});
chrome.runtime.onStartup.addListener(() => {
  ensureAlarm().then(syncQueue).catch(console.error);
});
chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === ALARM_NAME) syncQueue().catch(console.error);
});
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && (changes[LOCAL_BINDING] || changes[STORAGE_KEYS.queue])) {
    ensureAlarm().then(syncQueue).catch(console.error);
  }
});
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message?.type === "CONNECT_LOCAL") {
    if (![chrome.runtime.getURL("popup.html"), chrome.runtime.getURL("options.html")].includes(sender.url)) {
      sendResponse({ ok: false, error: "不支持的连接来源" }); return false;
    }
    discoverLocal(Boolean(message.launch)).then(result => {
      sendResponse({ ok: true, ...result });
      if (result.online) syncQueue().catch(console.error);
    }).catch(() => sendResponse({ ok: false, error: "无法连接本地 App" }));
    return true;
  }
  if (message?.type === "SYNC_QUEUE") {
    syncQueue().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "RETRY_QUEUE") {
    retryCurrentAccount().then(() => sendResponse({ ok: true })).catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  if (message?.type === "ENQUEUE_CAPTURE") {
    mutateQueue((queue) => [...queue, message.item])
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: String(error) }));
    return true;
  }
  return false;
});

ensureAlarm().then(syncQueue).catch(console.error);

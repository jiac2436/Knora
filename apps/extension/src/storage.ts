// @ts-check
import { retainRecentSnapshots, STORAGE_KEYS } from "./core.js";

/** @param {string} key @param {any} fallback */
export async function readStorage(key, fallback) {
  const result = await chrome.storage.local.get(key);
  return result[key] ?? fallback;
}

/** @param {string} key @param {any} value */
export async function writeStorage(key, value) {
  await chrome.storage.local.set({ [key]: value });
}

export const getBinding = () => readStorage("pka.local.v1", null);
export const getQueue = () => readStorage(STORAGE_KEYS.queue, []);
export const setQueue = (queue) => writeStorage(STORAGE_KEYS.queue, queue);
export const getRecent = () => readStorage(STORAGE_KEYS.recent, []);

/** @param {any} entry */
export async function addRecent(entry) {
  const recent = await getRecent();
  await writeStorage(STORAGE_KEYS.recent, retainRecentSnapshots([entry, ...recent]));
}

/** @param {any} item */
export async function enqueue(item) {
  const queue = await getQueue();
  await setQueue([...queue, item]);
}

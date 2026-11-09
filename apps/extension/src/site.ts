// @ts-check
/** @param {string} value */
export function isXiaohongshuURL(value) {
  try {
    const url = new URL(value);
    return ["http:", "https:"].includes(url.protocol)
      && (url.hostname === "xiaohongshu.com" || url.hostname.endsWith(".xiaohongshu.com"));
  } catch { return false; }
}

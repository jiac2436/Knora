// @ts-check

/** Build an inert, scaled document. The owning iframe MUST use sandbox="". */
export function createPreviewDocument(preview, availableWidth) {
  const width = Number(preview.width);
  if (!Number.isFinite(width) || width <= 0) throw new Error("无效的预览宽度");
  const scale = Math.min(1, Math.max(1, availableWidth) / width);
  return `<!doctype html><html><head><meta charset="utf-8"><meta name="referrer" content="no-referrer"><meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src https: http: data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"><style>html,body{margin:0;padding:0;background:#fff}body{overflow-x:hidden}#snapshot{width:${width}px;zoom:${scale};display:flow-root}a{pointer-events:none}img{max-width:100%}</style></head><body><div id="snapshot">${preview.html}</div></body></html>`;
}

/** @param {Array<{code: string, message: string}>} warnings */
export function renderCaptureWarnings(container, warnings) {
  container.replaceChildren();
  container.hidden = !warnings.length;
  if (!warnings.length) return;
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.className = "capture-warning-toggle";
  summary.setAttribute("aria-label", `查看 ${warnings.length} 项采集限制`);
  const icon = document.createElement("span");
  icon.className = "warning-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "!";
  const label = document.createElement("span");
  label.textContent = `部分内容或样式可能不完整（${warnings.length}）`;
  summary.append(icon, label);
  const list = document.createElement("ul");
  list.className = "capture-warning-list";
  for (const warning of warnings) {
    const item = document.createElement("li");
    item.textContent = warning.message;
    list.append(item);
  }
  details.append(summary, list);
  container.append(details);
}

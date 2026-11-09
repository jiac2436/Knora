// @ts-check

/** Stable section IDs are shared by extraction data, checkboxes, preview nodes and upload filtering. */
export function noteSections(capture) {
  const note = capture.mode === "page" && capture.site === "xiaohongshu" ? capture.noteContent : null;
  if (!note) return [];
  const sections = [];
  if (note.title) sections.push({ id: "title", label: "标题", text: `【标题】\n${note.title}` });
  if (note.author) sections.push({ id: "author", label: "作者", text: `【作者】\n${note.author}` });
  if (note.body) sections.push({ id: "body", label: "正文", text: `【正文】\n${note.body}` });
  if (note.tags?.length) sections.push({ id: "tags", label: "标签", text: `【话题标签】\n${note.tags.map(tag => "#" + tag).join(" ")}` });
  const count = note.comments?.length ?? 0;
  if (count) sections.push({ id: "comments", label: "评论", text:
    `【评论区 · 已加载 ${count} 条${Number.isInteger(note.commentTotal) ? ` / 页面显示共 ${note.commentTotal} 条` : ""}】\n仅含当前页面已加载的评论及回复，不代表完整评论区。\n\n${commentTree(note.comments)}` });
  return sections;
}

/** Parent IDs remain in the source data; present a readable tree instead of internal IDs. */
export function commentTree(comments) {
  return commentTreeLines(comments).map(line => line.text).join("\n");
}

/** Header metadata is display-only: saved names and comment text are never shortened. */
export function commentTreeLines(comments) {
  const byID = new Map(comments.map(comment => [comment.id, comment]));
  const children = new Map();
  const roots = [];
  for (const comment of comments) {
    if (comment.parentId && comment.parentId !== comment.id && byID.has(comment.parentId)) {
      if (!children.has(comment.parentId)) children.set(comment.parentId, []);
      children.get(comment.parentId).push(comment);
    } else roots.push(comment);
  }
  const visited = new Set();
  const lines = [];
  const append = (comment, depth) => {
    if (visited.has(comment.id)) return;
    visited.add(comment.id);
    const indent = "   ".repeat(Math.min(depth, 12));
    const branch = depth ? "|_ " : "";
    const target = comment.replyTo || byID.get(comment.parentId)?.author;
    lines.push({ heading: true, text: `${indent}${branch}${comment.author}${comment.isAuthor ? "（作者）" : ""}${target ? ` 回复 ${target}` : ""}：` });
    for (const line of comment.text.split("\n")) lines.push({ heading: false, text: `${indent}${depth ? "   " : ""}${line}` });
    for (const child of children.get(comment.id) ?? []) append(child, depth + 1);
  };
  for (const root of roots) { append(root, 0); lines.push({ heading: false, text: "" }); }
  // Retain malformed/orphaned threads without guessing or looping on parent cycles.
  for (const comment of comments) if (!visited.has(comment.id)) { append(comment, 0); lines.push({ heading: false, text: "" }); }
  while (lines.length && !lines.at(-1).text.trim()) lines.pop();
  if (lines.length) lines.at(-1).text = lines.at(-1).text.trimEnd();
  return lines;
}

/** One representation for preview and upload; excluded fields never enter saved text. */
export function captureText(capture, limit = 100000) {
  const available = noteSections(capture);
  if (!capture.noteContent || capture.mode !== "page" || capture.site !== "xiaohongshu") {
    const text = (capture.text ?? "").trim();
    return { text: text.slice(0, limit), truncated: text.length > limit, sections: [] };
  }
  const selected = Array.isArray(capture.selectedSections) ? new Set(capture.selectedSections) : null;
  const sections = available.filter(section => !selected || selected.has(section.id));
  const text = sections.map(section => section.text).join("\n\n");
  const truncated = text.length > limit;
  const marker = "\n\n[本次保存文字超过上限，后续内容未包含。]";
  return { text: truncated ? text.slice(0, Math.max(0, limit - marker.length)) + marker : text, truncated, sections };
}

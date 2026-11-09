"""Local conversion of supported non-PDF files with anydoc."""
from __future__ import annotations

from pathlib import Path

MAX_DOCUMENT_CHARS = 200_000


def extract_document(path: Path, filename: str) -> str:
    if Path(filename).suffix.lower() in {".md", ".txt"}:
        try:
            text = path.read_bytes().decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise ValueError("文本文件须使用 UTF-8 编码") from exc
        if "\x00" in text:
            raise ValueError("Markdown 文件包含无效的二进制内容")
        if not text.strip():
            raise ValueError("文档没有可提取的文字")
        if len(text) > MAX_DOCUMENT_CHARS:
            raise ValueError(f"文档文字超过 {MAX_DOCUMENT_CHARS} 字上限")
        return text
    from app.runtime_extensions import require
    require("documents")
    try:
        import anydoc
    except ImportError as exc:
        raise ValueError("Worker 未安装 firecrawl-anydoc") from exc

    data = path.read_bytes()
    try:
        if Path(filename).suffix.lower() == ".csv":
            markdown = anydoc.to_markdown_bytes(data, "csv")
        else:
            markdown = anydoc.to_markdown_bytes(data)
    except anydoc.ConvertError as exc:
        raise ValueError(f"文档解析失败：{exc}") from exc
    text = markdown.strip()
    if not text:
        raise ValueError("文档没有可提取的文字")
    if len(text) > MAX_DOCUMENT_CHARS:
        raise ValueError(f"文档文字超过 {MAX_DOCUMENT_CHARS} 字上限")
    return text

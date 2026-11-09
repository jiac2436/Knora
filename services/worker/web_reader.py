"""Bounded, text-only extraction of public HTML pages."""
from __future__ import annotations

import datetime
import ipaddress
import json
import logging
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

MAX_HTML_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_MARKDOWN_CHARS = 1_000_000
MAX_CRAWL_RESPONSE_BYTES = 16 * 1024 * 1024
# The local server has a 300s wall-clock deadline. Allow its response to arrive
# instead of abandoning a healthy crawl after the old 60s transport timeout.
CRAWL4AI_READ_TIMEOUT_SECONDS = 330
MIN_USEFUL_TEXT_CHARS = 80
WECHAT_VERIFICATION_ERROR = "微信公众号要求人工验证，无法自动读取正文；请在浏览器完成验证后使用网页摘录保存"
WECHAT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
WECHAT_HEADERS = {
    "User-Agent": WECHAT_USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def is_wechat_url(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    return host == "mp.weixin.qq.com" or host.endswith(".mp.weixin.qq.com")

FAKE_IP_NETWORKS = (ipaddress.ip_network("198.18.0.0/15"), ipaddress.ip_network("2001:2::/48"))
GENERAL_CLEANING_CONFIG = {
    "type": "CrawlerRunConfig",
    "params": {
        # Select content during scraping, after metadata extraction. Browser
        # css_selector would discard the page title before this stage.
        "target_elements": ["article", "body:not(:has(article))"],
        "excluded_tags": ["nav", "footer", "aside", "form"],
        "remove_forms": True,
        "markdown_generator": {
            "type": "DefaultMarkdownGenerator",
            "params": {
                "content_filter": {
                    "type": "PruningContentFilterLXML",
                    "params": {"threshold": 0.48, "threshold_type": "dynamic"},
                },
            },
        },
    },
}
BLOCK_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre"}
SKIP_TAGS = {"script", "style", "noscript", "template", "nav", "footer", "header", "aside", "form"}
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class _PageText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.og_title = ""
        self.in_title = False
        self.skip_depth = 0
        self.article_depth = 0
        self.main_depth = 0
        self.body_depth = 0
        self.loose_parts: list[str] = []
        self.block: str | None = None
        self.block_parts: list[str] = []
        self.blocks: list[tuple[bool, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skip_depth:
            if tag not in VOID_TAGS:
                self.skip_depth += 1
            return
        if tag in SKIP_TAGS:
            self.skip_depth = 1
            return
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            values = dict(attrs)
            if values.get("property") == "og:title" and values.get("content"):
                self.og_title = values["content"] or ""
        if tag == "article":
            self.article_depth += 1
        if tag == "main":
            self.main_depth += 1
        if tag == "body":
            self.body_depth += 1
        if tag in BLOCK_TAGS:
            self._flush()
            self.block = tag
        elif tag == "br" and self.block:
            self.block_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.skip_depth:
            self.skip_depth -= 1
            return
        if tag == "title":
            self.in_title = False
        if tag == self.block:
            self._flush()
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)
        if tag == "main":
            self.main_depth = max(0, self.main_depth - 1)
        if tag == "body":
            self.body_depth = max(0, self.body_depth - 1)

    def handle_data(self, data: str) -> None:
        if self.skip_depth:
            return
        if self.in_title:
            self.title_parts.append(data)
        if self.block:
            self.block_parts.append(data)
        if self.body_depth:
            self.loose_parts.append(data)

    def _flush(self) -> None:
        if self.block:
            value = re.sub(r"[\t \r\f\v]+", " ", "".join(self.block_parts)).strip()
            if value:
                self.blocks.append((bool(self.article_depth or self.main_depth), value))
        self.block = None
        self.block_parts = []


def _require_public_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("仅支持公开的 HTTP/HTTPS 网页")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise ValueError("网页端口无效") from exc
    if port not in {80, 443}:
        raise ValueError("仅支持标准 HTTP/HTTPS 端口")
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        pass
    else:
        raise ValueError("仅支持域名，不允许直接读取 IP 地址")
    try:
        addresses = socket.getaddrinfo(parts.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("无法解析网页地址") from exc
    def allowed(address: str) -> bool:
        ip = ipaddress.ip_address(address)
        return ip.is_global or (settings.web_allow_fake_ip and parts.scheme == "https"
                                and any(ip in network for network in FAKE_IP_NETWORKS))

    denied = [ipaddress.ip_address(item[4][0]) for item in addresses if not allowed(item[4][0])]
    if denied and parts.scheme == "https" and all(
            any(ip in network for network in FAKE_IP_NETWORKS) for ip in denied):
        raise ValueError("域名被代理解析为虚拟 IP；请在设置→网页读取中开启“代理网络兼容”，重启客户端后重试")
    if not addresses or denied:
        raise ValueError("不允许读取内网或本机地址")


class _WeChatArticleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.og_title = ""
        self.og_desc = ""
        self.account = ""
        self.author = ""

        self.in_title = False
        self.in_account = False
        self.in_author = False

        self.content_depth = 0
        self.skip_depth = 0

        self.current_tag_stack: list[str] = []
        self.current_block: list[str] = []
        self.items: list[tuple[str, str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_dict = {k.lower(): (v or "") for k, v in attrs}
        elem_id = attr_dict.get("id", "")
        elem_class = attr_dict.get("class", "")

        if tag == "meta":
            prop = attr_dict.get("property", "").lower()
            name = attr_dict.get("name", "").lower()
            content = attr_dict.get("content", "").strip()
            if prop == "og:title":
                self.og_title = content
            elif prop == "og:description":
                self.og_desc = content
            elif prop in {"og:article:author", "author"} or name == "author":
                if not self.author:
                    self.author = content
            return

        if (tag == "h1" and elem_id == "activity-name") or "rich_media_title" in elem_class:
            self.in_title = True
            self.title = ""
            return
        elif elem_id == "js_name":
            self.in_account = True
            self.account = ""
            return
        elif elem_id == "js_author_name":
            self.in_author = True
            self.author = ""
            return

        if elem_id == "js_content" or "rich_media_content" in elem_class:
            self.content_depth = 1
            return

        if self.content_depth > 0:
            self.content_depth += 1

            if tag in {"script", "style", "noscript", "iframe"}:
                self.skip_depth += 1
                return

            if self.skip_depth > 0:
                return

            if tag == "img":
                img_url = attr_dict.get("data-src") or attr_dict.get("src") or ""
                img_url = img_url.strip()
                if img_url.startswith(("https://", "http://")):
                    self._flush_block()
                    alt = attr_dict.get("alt", "").strip() or "图片"
                    escaped = re.sub(r'([\\\[\]])', r'\\\1', alt.replace('\n', ' '))
                    destination = quote(img_url, safe=":/?#[]@!$&'()*+,;=%")
                    if '(' in destination or ')' in destination:
                        destination = '<' + destination + '>'
                    self.items.append(("img", f"![{escaped}]({destination})", f"[图片: {alt}]"))
                return

            if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "section", "br", "hr"}:
                if tag != "br":
                    self._flush_block()
                    self.current_tag_stack.append(tag)
                else:
                    self.current_block.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if self.in_title and (tag == "h1" or tag == "span"):
            self.in_title = False
        elif self.in_account and (tag == "a" or tag == "span"):
            self.in_account = False
        elif self.in_author and (tag == "span" or tag == "em"):
            self.in_author = False

        if self.content_depth > 0:
            if tag in {"script", "style", "noscript", "iframe"}:
                self.skip_depth = max(0, self.skip_depth - 1)
            elif self.skip_depth == 0:
                if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "blockquote", "section"}:
                    self._flush_block()
                    if self.current_tag_stack and self.current_tag_stack[-1] == tag:
                        self.current_tag_stack.pop()

            self.content_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title += data
            return
        if self.in_account:
            self.account += data
            return
        if self.in_author:
            self.author += data
            return

        if self.content_depth > 0 and self.skip_depth == 0:
            self.current_block.append(data)

    def _flush_block(self) -> None:
        text = "".join(self.current_block).strip()
        self.current_block = []
        if not text:
            return

        current_tag = self.current_tag_stack[-1] if self.current_tag_stack else "p"
        if current_tag in {"h1", "h2", "h3"}:
            self.items.append(("h2", f"## {text}", text))
        elif current_tag in {"h4", "h5", "h6"}:
            self.items.append(("h3", f"### {text}", text))
        elif current_tag == "blockquote":
            self.items.append(("quote", f"> {text}", text))
        else:
            self.items.append(("p", text, text))


def _extract_wechat_html(html: str, url: str) -> tuple[str, str, str]:
    if "环境异常" in html and ("完成验证后即可继续访问" in html or "去验证" in html):
        raise ValueError(WECHAT_VERIFICATION_ERROR)

    parser = _WeChatArticleParser()
    parser.feed(html)
    parser.close()
    parser._flush_block()

    title = re.sub(r"\s+", " ", parser.title or parser.og_title).strip()
    account = re.sub(r"\s+", " ", parser.account).strip()
    author = re.sub(r"\s+", " ", parser.author).strip()
    desc = re.sub(r"\s+", " ", parser.og_desc).strip()

    ts_match = re.search(r'\bct\s*=\s*"(\d+)"', html) or re.search(r'ori_create_time\s*:\s*[\'"]?(\d+)', html)
    publish_time = ""
    if ts_match:
        try:
            publish_time = datetime.datetime.fromtimestamp(int(ts_match.group(1))).strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, OSError):
            pass

    plain_header_parts = []
    if title:
        plain_header_parts.append(title)
    meta_parts = []
    if account:
        meta_parts.append(f"公众号: {account}")
    if author:
        meta_parts.append(f"作者: {author}")
    if publish_time:
        meta_parts.append(f"发布时间: {publish_time}")
    if meta_parts:
        plain_header_parts.append(" | ".join(meta_parts))
    if desc:
        plain_header_parts.append(desc)

    body_plain_parts = [item[2] for item in parser.items if item[2]]
    full_plain = "\n\n".join(plain_header_parts + body_plain_parts).strip()
    if not full_plain or len(body_plain_parts) == 0:
        if "环境异常" in html or "sec.weixin.qq.com" in html:
            raise ValueError(WECHAT_VERIFICATION_ERROR)
        raise ValueError("网页未提取到可读文字")
    if len(full_plain) > MAX_TEXT_CHARS:
        raise ValueError("网页正文超过读取上限")

    md_lines = []
    if title:
        md_lines.append(f"# {title}\n")
    if account:
        md_lines.append(f"- **来源公众号**：{account}")
    if author:
        md_lines.append(f"- **作者**：{author}")
    if publish_time:
        md_lines.append(f"- **发布时间**：{publish_time}")
    md_lines.append(f"- **原文链接**：{url}\n")

    if desc:
        md_lines.append(f"> {desc}\n")

    for item in parser.items:
        md_lines.append(item[1] + "\n")

    full_markdown = "\n".join(md_lines).strip()
    return (title or url)[:500], full_plain, full_markdown


def extract_wechat_page(url: str, client_factory=httpx.Client) -> tuple[str, str, str]:
    """Extract a WeChat official account article using User-Agent spoofing."""
    headers = dict(WECHAT_HEADERS)
    with client_factory(timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
                        trust_env=False, headers=headers) as client:
        for _ in range(5):
            _require_public_url(url)
            with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("网页跳转缺少地址")
                    url = urljoin(url, location)
                    continue
                if response.status_code >= 400:
                    raise ValueError(f"网页返回 HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").lower()
                if content_type and "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                    raise ValueError("链接不是 HTML 网页")
                body = bytearray()
                started = time.monotonic()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_HTML_BYTES:
                        raise ValueError("网页内容超过读取上限")
                    if time.monotonic() - started > 30:
                        raise ValueError("网页读取超时")
                charset = re.search(rb"<meta[^>]+charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", body[:4096], re.I)
                encoding = charset.group(1).decode("ascii") if charset else response.encoding or "utf-8"
                try:
                    html = body.decode(encoding, errors="replace")
                except LookupError:
                    html = body.decode("utf-8", errors="replace")

                return _extract_wechat_html(html, url)
    raise ValueError("网页跳转次数过多")


def extract_page(url: str, client_factory=httpx.Client) -> tuple[str, str]:
    """Return (title, readable text); failures leave the original URL intact."""
    if is_wechat_url(url):
        title, text, _ = extract_wechat_page(url, client_factory=client_factory)
        return title, text
    headers = {"User-Agent": "PersonalKnowledgeAgent/0.1 (+public-page-reader)",
               "Accept": "text/html,application/xhtml+xml"}
    with client_factory(timeout=httpx.Timeout(15, connect=5), follow_redirects=False,
                        trust_env=False, headers=headers) as client:
        for _ in range(5):
            _require_public_url(url)
            with client.stream("GET", url) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise ValueError("网页跳转缺少地址")
                    url = urljoin(url, location)
                    continue
                if response.status_code >= 400:
                    raise ValueError(f"网页返回 HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").lower()
                if content_type and "text/html" not in content_type and "application/xhtml+xml" not in content_type:
                    raise ValueError("链接不是 HTML 网页")
                body = bytearray()
                started = time.monotonic()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_HTML_BYTES:
                        raise ValueError("网页内容超过读取上限")
                    if time.monotonic() - started > 30:
                        raise ValueError("网页读取超时")
                charset = re.search(rb"<meta[^>]+charset\s*=\s*['\"]?([a-zA-Z0-9_-]+)", body[:4096], re.I)
                encoding = charset.group(1).decode("ascii") if charset else response.encoding or "utf-8"
                try:
                    html = body.decode(encoding, errors="replace")
                except LookupError:
                    html = body.decode("utf-8", errors="replace")
                return _extract_html_page(html, url)
    raise ValueError("网页跳转次数过多")


def _extract_html_page(html: str, url: str) -> tuple[str, str]:
    parser = _PageText()
    parser.feed(html)
    parser.close()
    parser._flush()
    preferred = [text for in_main, text in parser.blocks if in_main]
    blocks = preferred if len("".join(preferred)) >= 80 else [text for _, text in parser.blocks]
    body_text = "\n\n".join(blocks).strip()
    if not body_text:
        body_text = re.sub(r"\s+", " ", " ".join(parser.loose_parts)).strip()
    if not body_text:
        raise ValueError("网页未提取到可读文字")
    if len(body_text) > MAX_TEXT_CHARS:
        raise ValueError("网页正文超过读取上限")
    title = re.sub(r"\s+", " ", parser.og_title or "".join(parser.title_parts)).strip()
    return (title or url)[:500], body_text


def extract_browser_page_with_markdown(url: str, client_factory=httpx.Client,
                                       cleaning_enabled: bool = False) -> tuple[str, str, str | None]:
    """Read a public page through the configured local Crawl4AI browser service."""
    _require_public_url(url)
    if not settings.crawl4ai_url or not settings.crawl4ai_token:
        raise ValueError("Crawl4AI 本地服务未配置")
    endpoint = settings.crawl4ai_url.rstrip("/") + "/crawl"
    started = time.monotonic()
    host = urlsplit(url).hostname
    logger.info("Crawl4AI 开始：站点 %s，通用清洗 %s", host, cleaning_enabled)
    try:
        with client_factory(timeout=httpx.Timeout(CRAWL4AI_READ_TIMEOUT_SECONDS, connect=5), follow_redirects=False,
                            trust_env=False) as client:
            with client.stream("POST", endpoint,
                               headers={"Authorization": f"Bearer {settings.crawl4ai_token}"},
                               json={"urls": [url], "browser_config": {},
                                     "crawler_config": GENERAL_CLEANING_CONFIG if cleaning_enabled else {}}) as response:
                if response.status_code != 200:
                    raise ValueError(f"Crawl4AI 服务返回 HTTP {response.status_code}")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > MAX_CRAWL_RESPONSE_BYTES:
                        raise ValueError("Crawl4AI 返回内容超过读取上限")
    except httpx.ReadTimeout as exc:
        logger.warning("Crawl4AI 等待结果超时：站点 %s，耗时 %.2fs", host, time.monotonic() - started)
        raise ValueError("Crawl4AI 等待抓取结果超时，请稍后重试") from exc
    except httpx.HTTPError as exc:
        logger.warning("Crawl4AI 连接失败：站点 %s，类型 %s，耗时 %.2fs",
                       host, type(exc).__name__, time.monotonic() - started)
        raise ValueError("Crawl4AI 本地服务不可用") from exc
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Crawl4AI 返回内容无效") from exc
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        raise ValueError("Crawl4AI 返回内容无效")
    result = results[0]
    if not result.get("success"):
        upstream_error = str(result.get("error_message") or "")
        logger.warning("Crawl4AI 抓取失败：站点 %s，状态码 %s，耗时 %.2fs",
                       host, result.get("status_code"), time.monotonic() - started)
        # Keep upstream details bounded in logs without exposing them in UI.
        reason = re.sub(r"https?://\S+", "[链接]", upstream_error)
        logger.warning("Crawl4AI 失败原因：%s", reason[:500] or "未提供")
        if "blocked by anti-bot protection" in upstream_error.lower():
            raise ValueError("目标网站触发反爬拦截，Crawl4AI 无法自动读取，请稍后重试或使用网页摘录")
        raise ValueError("Crawl4AI 未能读取网页")
    markdown = result.get("markdown")
    if isinstance(markdown, dict):
        text = markdown.get("fit_markdown") or markdown.get("raw_markdown") or ""
        raw_markdown = markdown.get("raw_markdown") or text
    elif isinstance(markdown, str):
        text = markdown
        raw_markdown = markdown
    else:
        text = ""
        raw_markdown = ""
    if not isinstance(text, str) or not text.strip():
        raise ValueError("网页未提取到可读文字")
    text = text.strip()
    if text.startswith("Crawl4AI Error:"):
        reason = re.search(r"Error Message:\s*([^\n]+)", text)
        html = result.get("html")
        logger.error("Crawl4AI 解析失败：站点 %s，原因 %s，HTML 字符 %s，耗时 %.2fs；尝试 HTML 回退",
                     host, reason.group(1)[:180] if reason else "错误 Markdown",
                     len(html) if isinstance(html, str) else 0, time.monotonic() - started)
        if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise ValueError("Crawl4AI 未能读取网页")
        try:
            title, fallback_text = _extract_html_page(html, url)
        except ValueError as exc:
            raise ValueError("Crawl4AI 未能读取网页") from exc
        if len(fallback_text) < MIN_USEFUL_TEXT_CHARS:
            raise ValueError("Crawl4AI 未能读取网页")
        return title, fallback_text, None
    if not isinstance(raw_markdown, str) or len(raw_markdown) > MAX_MARKDOWN_CHARS:
        raise ValueError("Crawl4AI Markdown 超过读取上限")
    if len(text) > MAX_TEXT_CHARS:
        # Markdown may be much longer than the article because URLs and image
        # sources are repeated. Keep the full native Markdown for preview and
        # use readable HTML text for indexing, without truncating the article.
        html = result.get("html")
        if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_HTML_BYTES:
            raise ValueError("网页正文超过读取上限")
        _, text = _extract_html_page(html, url)
        logger.info("Crawl4AI 正文转换：站点 %s，Markdown 超过正文上限，使用 HTML 可读文字；原始 Markdown 完整保留", host)
    metadata = result.get("metadata")
    title = metadata.get("title") if isinstance(metadata, dict) else None
    title = re.sub(r"\s+", " ", title).strip() if isinstance(title, str) else ""
    logger.info("Crawl4AI 完成：站点 %s，状态码 %s，正文字符 %s，原始 Markdown 字符 %s，耗时 %.2fs",
                host, result.get("status_code"), len(text), len(raw_markdown),
                time.monotonic() - started)
    return (title or url)[:500], text, raw_markdown.strip()


def extract_browser_page(url: str, client_factory=httpx.Client) -> tuple[str, str]:
    title, text, _ = extract_browser_page_with_markdown(url, client_factory)
    return title, text


def extract_legacy_page(url: str) -> tuple[str, str]:
    title, text = extract_page(url)
    if _is_wechat_verification(url, title, text):
        raise ValueError(WECHAT_VERIFICATION_ERROR)
    return title, text


def extract_page_with_browser(url: str) -> tuple[str, str]:
    """Keep the lightweight reader first; use the local browser for sparse pages."""
    try:
        title, text = extract_page(url)
    except ValueError as exc:
        if str(exc) != "网页未提取到可读文字" or not settings.crawl4ai_url:
            raise
        browser_title, browser_text = extract_browser_page(url)
        if _is_wechat_verification(url, browser_title, browser_text):
            raise ValueError(WECHAT_VERIFICATION_ERROR)
        return browser_title, browser_text
    verification_page = _is_wechat_verification(url, title, text)
    if not verification_page and (len(text.strip()) >= MIN_USEFUL_TEXT_CHARS or not settings.crawl4ai_url):
        return title, text
    if not settings.crawl4ai_url:
        raise ValueError(WECHAT_VERIFICATION_ERROR)
    try:
        browser_title, browser_text = extract_browser_page(url)
    except ValueError as exc:
        if verification_page:
            raise ValueError(WECHAT_VERIFICATION_ERROR) from exc
        return title, text
    if _is_wechat_verification(url, browser_title, browser_text):
        raise ValueError(WECHAT_VERIFICATION_ERROR)
    if verification_page:
        return browser_title, browser_text
    if len(browser_text) > len(text):
        return browser_title, browser_text
    return title, text


def _is_wechat_verification(url: str, title: str, text: str) -> bool:
    if urlsplit(url).hostname != "mp.weixin.qq.com" or len(text) > 500:
        return False
    page = f"{title}\n{text}"
    return "环境异常" in page and ("完成验证后即可继续访问" in page or "去验证" in page)

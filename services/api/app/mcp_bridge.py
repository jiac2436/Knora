"""Optional stdio-to-local-API adapter; no database access in this mode."""
from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .article_links import browser_article_url
from .platform_support import get_platform


def bridge_configured() -> bool:
    """Configuration presence selects bridge mode even while the client is stopped."""
    return bool(os.getenv("PKA_API_URL", "").strip() or os.getenv("PKA_API_FILE", "").strip())


def _validate_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
                 and parsed.port is not None and 1 <= parsed.port <= 65535
                 and not parsed.username and not parsed.password and not parsed.query
                 and not parsed.fragment and parsed.path in {"", "/"})
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("PKA_API_URL 必须是带端口的本机回环 HTTP 地址")
    return value.rstrip("/")


def _file_url(path: str) -> str:
    message = "本地服务端点文件不可用或无效，请启动客户端并检查 PKA_API_FILE 配置"
    try:
        if not os.path.isabs(path):
            raise ValueError(message)
        raw = get_platform().files.read(Path(path), maximum=4096)
        if raw is None:
            raise ValueError(message)
        data = json.loads(raw)
        if (not isinstance(data, dict) or not {"api_url", "library_id"} <= set(data)
                or not set(data) <= {"api_url", "library_id", "instance_id"}
                or not isinstance(data["api_url"], str)
                or not isinstance(data["library_id"], str) or not data["library_id"].strip()):
            raise ValueError(message)
        if "instance_id" in data and (not isinstance(data["instance_id"], str) or not data["instance_id"].strip()):
            raise ValueError(message)
        return _validate_url(data["api_url"].strip())
    except (OSError, ValueError, TypeError, UnicodeError):
        raise ValueError(message) from None


def api_url() -> str | None:
    value = os.getenv("PKA_API_URL", "").strip()
    if value:
        return _validate_url(value)
    path = os.getenv("PKA_API_FILE", "").strip()
    return _file_url(path) if path else None


def call_api(tool: str, payload: dict) -> dict:
    address = api_url()
    token = os.getenv("PKA_AGENT_TOKEN", "").strip()
    if not address or not token:
        raise ValueError("缺少本地 API 地址或 Agent 连接令牌")
    try:
        with httpx.Client(timeout=30, trust_env=False, follow_redirects=False) as client:
            response = client.post(f"{address}/v1/mcp/tools/{tool}",
                headers={"Authorization": f"Bearer {token}"}, json=payload)
        if response.status_code == 401:
            raise ValueError("Agent 连接无效、已撤销或已过期")
        if response.status_code == 404:
            raise ValueError("请求的知识不存在或当前连接无权访问")
        if response.status_code != 200:
            raise ValueError("本地知识服务未能完成请求，请检查服务状态和参数")
        try:
            result = response.json()
        except ValueError:
            raise ValueError("本地知识服务返回无效响应") from None
        if not isinstance(result, dict):
            raise ValueError("本地知识服务返回无效响应")
        # Keep the native URI for hosts that support it. A normal HTTP link is
        # also needed for chat renderers that strip custom URL schemes.
        targets = [result]
        if isinstance(result.get('items'), list):
            targets.extend(result['items'])
        for item in targets:
            if isinstance(item, dict) and (link := browser_article_url(address, item.get('app_url'))):
                item['citation_url'] = link
        return result
    except httpx.HTTPError:
        raise ValueError("无法访问本地知识服务，请启动客户端并检查 API 地址") from None

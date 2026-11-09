from __future__ import annotations

import json
import pytest
import httpx
from types import SimpleNamespace
from sqlalchemy import select

from app.database import SessionLocal
from app.models import WebReadJob
from services.worker.main import run_once, run_web_once
from services.worker.web_reader import (_PageText, _require_public_url, extract_page,
                                        extract_browser_page, extract_browser_page_with_markdown,
                                        extract_page_with_browser)
from services.worker import web_reader


def _save_link(client, account, url="https://example.com/article"):
    response = client.post("/v1/captures", json={
        "capture_type": "url", "source_url": url,
        "content": {"text": ""}, "coverage": "link_only",
        "context": {"domain_id": account["domain"]["id"]},
        "client": {"kind": "desktop", "version": "0.1.0"},
    }, headers={**account["headers"], "Idempotency-Key": "web-read-test"})
    assert response.status_code in {200, 201}, response.text
    return response.json()


def test_public_link_is_read_as_new_version(client, account):
    source = _save_link(client, account)
    assert source["processing_status"] == "pending"
    assert source["web_read"]["status"] == "pending"
    with SessionLocal() as db:
        assert run_web_once(db, extractor=lambda url: ("网页标题", "正文第一段\n\n正文第二段"))
        assert not run_web_once(db, extractor=lambda url: ("x", "y"))
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["title"] == "网页标题"
    assert detail["extracted_text"] == "正文第一段\n\n正文第二段"
    assert detail["processing_status"] == "usable"
    assert detail["coverage"] == "full_text"
    assert detail["revision"] == 2
    assert detail["evidence"][0]["locator"]["kind"] == "text"
    revisions = client.get(f"/v1/sources/{source['id']}/revisions", headers=account["headers"]).json()
    assert [item["version"] for item in revisions["items"]] == [2, 1]


def test_web_preference_snapshots_jobs_and_exposes_saved_markdown(client, account, monkeypatch):
    headers = account["headers"]
    assert client.get("/v1/web/preferences", headers=headers).json() == {
        "engine": "legacy", "cleaning_enabled": False}
    assert client.put("/v1/web/preferences", headers=headers,
                      json={"engine": "crawl4ai", "cleaning_enabled": True}).json() == {
                          "engine": "crawl4ai", "cleaning_enabled": True}
    source = _save_link(client, account)
    assert source["web_read"]["engine"] == "crawl4ai"
    assert source["web_read"]["cleaning_enabled"] is True
    assert client.put("/v1/web/preferences", headers=headers,
                      json={"engine": "legacy"}).status_code == 200
    assert client.get("/v1/web/preferences", headers=headers).json()["cleaning_enabled"] is True
    monkeypatch.setattr("services.worker.main.extract_browser_page_with_markdown",
                        lambda _, cleaning_enabled: ("浏览器标题", "正文", "# 原始正文\n\n链接"))
    with SessionLocal() as db:
        assert run_web_once(db)
    detail = client.get(f"/v1/sources/{source['id']}", headers=headers).json()
    assert detail["extracted_text"] == "正文"
    assert detail["crawl4ai_markdown_available"] is True
    assert detail["crawl4ai_markdown_origin"] == "crawl4ai"
    markdown_path = f"/v1/sources/{source['id']}/crawl4ai-markdown"
    markdown_result = client.get(markdown_path, headers=headers).json()
    assert markdown_result["markdown"] == "# 原始正文\n\n链接"
    assert markdown_result["origin"] == "crawl4ai"
    supplement = client.post(f"/v1/sources/{source['id']}/supplements", headers=headers,
                             json={"text": "我的补充"})
    assert supplement.status_code == 201
    assert client.get(f"/v1/sources/{source['id']}", headers=headers).json()[
        "crawl4ai_markdown_available"] is True
    assert client.get(markdown_path, headers=headers).json()["markdown"] == "# 原始正文\n\n链接"
    other = client.post("/v1/dev/sessions", json={"email": "other@example.com", "name": "Other"}).json()
    assert client.get(markdown_path, headers={"Authorization": f'Bearer {other["access_token"]}'}).status_code == 404
    retry = client.post(f"/v1/sources/{source['id']}/web-read", headers=headers)
    assert retry.json()["status"] == "pending"
    with SessionLocal() as db:
        job = db.get(WebReadJob, retry.json()["id"])
        assert job.engine == "legacy"
        assert job.cleaning_enabled is False
        assert run_web_once(db, extractor=lambda _: ("轻量标题", "轻量正文"))
    assert client.get(markdown_path, headers=headers).status_code == 404
    assert client.get(f"/v1/sources/{source['id']}", headers=headers).json()[
        "crawl4ai_markdown_available"] is False


def test_retry_requires_owner_and_idempotent_capture_keeps_one_job(client, account):
    source = _save_link(client, account)
    repeated = _save_link(client, account)
    assert repeated["id"] == source["id"]
    with SessionLocal() as db:
        assert len(db.scalars(select(WebReadJob).where(WebReadJob.source_id == source["id"])).all()) == 1
    other = client.post("/v1/dev/sessions", json={"email": "other@example.com", "name": "Other"}).json()
    headers = {"Authorization": f'Bearer {other["access_token"]}'}
    assert client.post(f"/v1/sources/{source['id']}/web-read", headers=headers).status_code == 404


def test_deleting_link_removes_pending_read_job(client, account):
    source = _save_link(client, account)
    assert client.delete(f"/v1/sources/{source['id']}", headers=account["headers"]).status_code == 202
    with SessionLocal() as db:
        assert run_once(db)
        assert db.scalar(select(WebReadJob).where(WebReadJob.source_id == source["id"])) is None


def test_failed_read_retains_url_and_can_retry(client, account):
    source = _save_link(client, account)
    with SessionLocal() as db:
        assert run_web_once(db, extractor=lambda url: (_ for _ in ()).throw(ValueError("网页不可访问")))
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["processing_status"] == "failed"
    assert detail["extracted_text"] == "https://example.com/article"
    assert detail["web_read"]["last_error"] == "网页不可访问"
    retry = client.post(f"/v1/sources/{source['id']}/web-read", headers=account["headers"])
    assert retry.status_code == 202
    assert client.post(f"/v1/sources/{source['id']}/web-read", headers=account["headers"]).status_code == 409
    with SessionLocal() as db:
        assert db.scalar(select(WebReadJob).where(WebReadJob.id == retry.json()["id"])).status == "pending"
        assert run_web_once(db, extractor=lambda url: ("恢复访问", "现在有正文"))
    assert client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()["revision"] == 2


def test_user_supplement_survives_initial_and_repeated_reads(client, account):
    source = _save_link(client, account)
    supplement = client.post(f"/v1/sources/{source['id']}/supplements",
                             headers=account["headers"], json={"text": "我的补充"})
    assert supplement.status_code == 201
    assert supplement.json()["processing_status"] == "pending"
    with SessionLocal() as db:
        assert run_web_once(db, extractor=lambda url: ("标题", "网页正文"))
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["extracted_text"] == "网页正文\n\n我的补充"
    assert detail["coverage"] == "partial_text"
    retry = client.post(f"/v1/sources/{source['id']}/web-read", headers=account["headers"])
    assert retry.status_code == 202
    with SessionLocal() as db:
        assert run_web_once(db, extractor=lambda url: ("新标题", "更新正文"))
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["extracted_text"] == "更新正文\n\n我的补充"


def test_supplement_added_during_fetch_is_not_lost(client, account):
    source = _save_link(client, account)

    def extract(url):
        response = client.post(f"/v1/sources/{source['id']}/supplements",
                               headers=account["headers"], json={"text": "抓取时补充"})
        assert response.status_code == 201
        return "网页标题", "网页正文"

    with SessionLocal() as db:
        assert run_web_once(db, extractor=extract)
    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["extracted_text"] == "网页正文\n\n抓取时补充"


def test_html_extraction_prefers_article_and_ignores_navigation():
    parser = _PageText()
    parser.feed("<title>页面标题</title><nav><p>菜单</p></nav><p>侧边推荐</p>"
                "<article><h1>正文标题</h1><p>第一段 <strong>重点</strong><br>第二行</p>"
                "<script><img src='x'>bad</script><p>末段</p></article>")
    preferred = [text for in_main, text in parser.blocks if in_main]
    assert "".join(parser.title_parts) == "页面标题"
    assert preferred == ["正文标题", "第一段 重点\n第二行", "末段"]


def test_fetches_public_html_and_follows_public_redirect(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("93.184.215.14", 443))])
    requests = []

    def respond(request):
        requests.append(str(request.url))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/article"})
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"},
                              text="<title>普通标题</title><main><h1>正文标题</h1>"
                                   "<p>这是一段公开网页正文。</p></main>")

    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond), **kwargs)
    title, text = extract_page("https://example.com/start", client_factory=factory)
    assert title == "普通标题"
    assert text == "正文标题\n\n这是一段公开网页正文。"
    assert requests == ["https://example.com/start", "https://example.com/article"]


def test_redirect_to_private_address_is_not_requested(monkeypatch):
    def resolve(host, *args, **kwargs):
        address = "127.0.0.1" if host == "localhost" else "93.184.215.14"
        return [(None, None, None, None, (address, 443))]

    monkeypatch.setattr("socket.getaddrinfo", resolve)
    requests = []

    def respond(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://localhost/private"})

    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond), **kwargs)
    with pytest.raises(ValueError, match="内网"):
        extract_page("https://example.com/start", client_factory=factory)
    assert requests == ["https://example.com/start"]


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://localhost/", "file:///etc/passwd",
                                     "http://user:pass@example.com/", "https://example.com:8080/"])
def test_private_or_unsupported_urls_are_rejected(url, monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 80))])
    with pytest.raises(ValueError):
        _require_public_url(url)


@pytest.mark.parametrize("addresses", [
    ["198.18.0.52"], ["2001:2::10"], ["198.18.0.6", "2001:2::10"],
    ["2001:2::10", "93.184.215.14"],
])
def test_fake_ip_requires_explicit_https_opt_in(monkeypatch, addresses):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, (address, 443)) for address in addresses])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(web_allow_fake_ip=False))
    with pytest.raises(ValueError, match="域名被代理解析为虚拟 IP"):
        _require_public_url("https://zh.wikipedia.org/wiki/test")
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(web_allow_fake_ip=True))
    _require_public_url("https://zh.wikipedia.org/wiki/test")
    with pytest.raises(ValueError, match="内网"):
        _require_public_url("http://zh.wikipedia.org/wiki/test")
    with pytest.raises(ValueError, match="直接读取 IP"):
        _require_public_url("https://198.18.0.52/")
    with pytest.raises(ValueError, match="直接读取 IP"):
        _require_public_url("https://[2001:2::10]/")


@pytest.mark.parametrize("denied_address", ["127.0.0.1", "::1", "fd00::1", "fe80::1", "2001:db8::1", "2001:2:1::1"])
def test_mixed_fake_and_private_dns_is_not_reported_as_proxy_compatibility(monkeypatch, denied_address):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, (address, 443)) for address in ("198.18.0.52", "2001:2::10", denied_address)])
    for enabled in (False, True):
        monkeypatch.setattr(web_reader, "settings", SimpleNamespace(web_allow_fake_ip=enabled))
        with pytest.raises(ValueError, match="^不允许读取内网或本机地址$"):
            _require_public_url("https://mp.weixin.qq.com/s/example")


def test_browser_service_extracts_title_and_markdown(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(
        web_allow_fake_ip=False, crawl4ai_url="http://crawl4ai:11235", crawl4ai_token="test-token"))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"success": True, "results": [{
            "success": True, "metadata": {"title": " 渲染后标题 "},
            "markdown": {"fit_markdown": "# 页面正文\n\n浏览器渲染内容", "raw_markdown": "原文"}}]})

    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond), **kwargs)
    assert extract_browser_page("https://example.com/article", client_factory=factory) == (
        "渲染后标题", "# 页面正文\n\n浏览器渲染内容")
    assert str(requests[0].url) == "http://crawl4ai:11235/crawl"
    assert requests[0].headers["authorization"] == "Bearer test-token"
    assert json.loads(requests[0].content)["urls"] == ["https://example.com/article"]


def test_browser_general_cleaning_requests_fit_markdown(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(
        web_allow_fake_ip=False, crawl4ai_url="http://crawl4ai:11235", crawl4ai_token="test-token"))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"results": [{"success": True, "metadata": {"title": "文章"},
            "markdown": {"fit_markdown": "# 正文", "raw_markdown": "菜单\n\n# 正文"}}]})

    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond), **kwargs)
    assert extract_browser_page_with_markdown("https://example.com/article", factory, True) == (
        "文章", "# 正文", "菜单\n\n# 正文")
    config = json.loads(requests[0].content)["crawler_config"]
    assert config["params"]["target_elements"] == ["article", "body:not(:has(article))"]
    assert "css_selector" not in config["params"]
    assert config["params"]["markdown_generator"]["params"]["content_filter"]["type"] == (
        "PruningContentFilterLXML")


@pytest.mark.parametrize("error, message", [
    (httpx.ReadTimeout, "等待抓取结果超时"),
    (httpx.ConnectError, "本地服务不可用"),
])
def test_browser_distinguishes_slow_crawl_from_unreachable_service(monkeypatch, error, message):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(
        web_allow_fake_ip=False, crawl4ai_url="http://crawl4ai:11235", crawl4ai_token="test-token"))

    def respond(request):
        # The server can legitimately run for 300 seconds; transport must not
        # cancel a healthy crawl at the former 60-second boundary.
        assert request.extensions["timeout"]["read"] > 300
        assert request.extensions["timeout"]["connect"] == 5
        raise error("test transport failure", request=request)

    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(respond), **kwargs)
    with pytest.raises(ValueError, match=message):
        extract_browser_page("https://example.com/article", client_factory=factory)


def test_browser_embedded_error_is_rejected(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(
        web_allow_fake_ip=False, crawl4ai_url="http://crawl4ai:11235", crawl4ai_token="test-token"))
    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(
        200, json={"results": [{"success": True, "markdown": {
            "raw_markdown": "Crawl4AI Error: This page is not fully supported."}}]})), **kwargs)
    with pytest.raises(ValueError, match="Crawl4AI 未能读取网页"):
        extract_browser_page_with_markdown("https://example.com/article", factory, True)


def test_browser_long_markdown_keeps_native_artifact_and_readable_body(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(
        web_allow_fake_ip=False, crawl4ai_url="http://crawl4ai:11235", crawl4ai_token="test-token"))
    monkeypatch.setattr(web_reader, "MAX_TEXT_CHARS", 200)
    article = "这是完整的文章正文。" * 10
    raw = f"# 标题\n\n[{article}](https://example.com/" + "x" * 300 + ")"
    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": [{
            "success": True, "metadata": {"title": "标题"},
            "html": f"<article><p>{article}</p></article>",
            "markdown": {"raw_markdown": raw}}]})), **kwargs)
    assert extract_browser_page_with_markdown("https://example.com/article", factory) == (
        "标题", article, raw)


def test_browser_markdown_error_falls_back_to_captured_html(monkeypatch, caplog):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [
        (None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(
        web_allow_fake_ip=False, crawl4ai_url="http://crawl4ai:11235", crawl4ai_token="test-token"))
    article = "这是浏览器成功抓到的文章正文。" * 10
    html = ("<title>真实标题</title><nav><p>菜单入口</p></nav>"
            f"<main><article><p>{article}</p></article></main>")
    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"results": [{
            "success": True, "html": html,
            "markdown": {"fit_markdown": "Crawl4AI Error: This page is not fully supported.",
                         "raw_markdown": "Crawl4AI Error: This page is not fully supported."}}]})), **kwargs)
    assert extract_browser_page_with_markdown("https://example.com/article", factory, True) == (
        "真实标题", article, None)
    assert "Crawl4AI 解析失败" in caplog.text
    assert "test-token" not in caplog.text


def test_browser_html_fallback_does_not_claim_markdown(client, account, monkeypatch):
    headers = account["headers"]
    assert client.put("/v1/web/preferences", headers=headers,
                      json={"engine": "crawl4ai", "cleaning_enabled": True}).status_code == 200
    source = _save_link(client, account)
    monkeypatch.setattr("services.worker.main.extract_browser_page_with_markdown",
                        lambda _, cleaning_enabled: ("恢复的标题", "恢复的正文", None))
    with SessionLocal() as db:
        assert run_web_once(db)
    detail = client.get(f"/v1/sources/{source['id']}", headers=headers).json()
    assert detail["processing_status"] == "usable"
    assert detail["extracted_text"] == "恢复的正文"
    assert detail["crawl4ai_markdown_available"] is True
    assert detail["crawl4ai_markdown_origin"] == "html_fallback"
    markdown_path = f"/v1/sources/{source['id']}/crawl4ai-markdown"
    assert client.get(markdown_path, headers=headers).json() == {
        "revision": 2, "markdown": "# 恢复的标题\n\n恢复的正文", "origin": "html_fallback"}
    assert client.post(f"/v1/sources/{source['id']}/supplements", headers=headers,
                       json={"text": "后来添加的笔记"}).status_code == 201
    assert client.get(markdown_path, headers=headers).json()["markdown"] == (
        "# 恢复的标题\n\n恢复的正文")


def test_browser_fallback_for_sparse_or_empty_page(monkeypatch):
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(crawl4ai_url="http://crawl4ai:11235"))
    monkeypatch.setattr(web_reader, "extract_page", lambda url: ("静态标题", "加载中"))
    monkeypatch.setattr(web_reader, "extract_browser_page", lambda url: ("渲染标题", "渲染后的完整正文" * 10))
    assert extract_page_with_browser("https://example.com/")[0] == "渲染标题"
    monkeypatch.setattr(web_reader, "extract_page", lambda url: (_ for _ in ()).throw(
        ValueError("网页未提取到可读文字")))
    assert extract_page_with_browser("https://example.com/")[1].startswith("渲染后")


def test_browser_failure_keeps_available_lightweight_text(monkeypatch):
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(crawl4ai_url="http://crawl4ai:11235"))
    monkeypatch.setattr(web_reader, "extract_page", lambda url: ("静态标题", "简短正文"))
    monkeypatch.setattr(web_reader, "extract_browser_page", lambda url: (_ for _ in ()).throw(
        ValueError("Crawl4AI 本地服务不可用")))
    assert extract_page_with_browser("https://example.com/") == ("静态标题", "简短正文")


def test_wechat_verification_page_is_not_saved_as_article(monkeypatch):
    url = "https://mp.weixin.qq.com/s/example"
    challenge = "## 环境异常\n当前环境异常，完成验证后即可继续访问。\n去验证"
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(crawl4ai_url="http://crawl4ai:11235"))
    monkeypatch.setattr(web_reader, "extract_page", lambda _: ("微信公众平台", challenge))
    monkeypatch.setattr(web_reader, "extract_browser_page", lambda _: ("微信公众平台", challenge))
    with pytest.raises(ValueError, match="微信公众号要求人工验证"):
        extract_page_with_browser(url)


def test_wechat_verification_uses_article_if_browser_can_read_it(monkeypatch):
    url = "https://mp.weixin.qq.com/s/example"
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(crawl4ai_url="http://crawl4ai:11235"))
    monkeypatch.setattr(web_reader, "extract_page", lambda _: (
        "微信公众平台", "环境异常\n当前环境异常，完成验证后即可继续访问。"))
    monkeypatch.setattr(web_reader, "extract_browser_page", lambda _: ("文章标题", "真实文章正文"))
    assert extract_page_with_browser(url) == ("文章标题", "真实文章正文")


def test_wechat_verification_without_browser_fails_clearly(monkeypatch):
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(crawl4ai_url=""))
    monkeypatch.setattr(web_reader, "extract_page", lambda _: (
        "微信公众平台", "环境异常\n当前环境异常，完成验证后即可继续访问。"))
    with pytest.raises(ValueError, match="微信公众号要求人工验证"):
        extract_page_with_browser("https://mp.weixin.qq.com/s/example")


def test_browser_does_not_receive_private_url_or_oversized_response(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("127.0.0.1", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(
        web_allow_fake_ip=False, crawl4ai_url="http://crawl4ai:11235", crawl4ai_token="test-token"))
    factory = lambda **kwargs: pytest.fail("不应调用浏览器服务")
    with pytest.raises(ValueError, match="内网"):
        extract_browser_page("https://example.com/", client_factory=factory)
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr(web_reader, "MAX_CRAWL_RESPONSE_BYTES", 32)
    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"x" * 33)), **kwargs)
    with pytest.raises(ValueError, match="超过读取上限"):
        extract_browser_page("https://example.com/", client_factory=factory)


@pytest.mark.parametrize("upstream_error, message", [
    ("internal details", "Crawl4AI 未能读取网页"),
    ("Blocked by anti-bot protection: Structural: minimal_text", "目标网站触发反爬拦截"),
])
def test_browser_failure_rejects_empty_result(monkeypatch, upstream_error, message):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("93.184.215.14", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(
        web_allow_fake_ip=False, crawl4ai_url="http://crawl4ai:11235", crawl4ai_token="test-token"))
    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"success": True, "results": [
            {"success": False, "error_message": upstream_error}]})), **kwargs)
    with pytest.raises(ValueError, match=message):
        extract_browser_page("https://example.com/", client_factory=factory)


def test_is_wechat_url():
    assert web_reader.is_wechat_url("https://mp.weixin.qq.com/s/pL-yzT2u8C-Oh6h6Di2GSA")
    assert web_reader.is_wechat_url("http://mp.weixin.qq.com/s?__biz=xxx")
    assert web_reader.is_wechat_url("https://sub.mp.weixin.qq.com/article")
    assert not web_reader.is_wechat_url("https://weixin.qq.com/")
    assert not web_reader.is_wechat_url("https://qq.com/")
    assert not web_reader.is_wechat_url("https://example.com/wechat")
    assert not web_reader.is_wechat_url("not a url")


def test_extract_wechat_page_uses_spoofed_ua_and_parses_content(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("182.254.116.116", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(web_allow_fake_ip=False))

    wechat_html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta property="og:title" content="测试微信文章标题" />
        <meta property="og:description" content="这是一篇用于单元测试的微信文章导语。" />
    </head>
    <body>
        <h1 class="rich_media_title" id="activity-name">测试微信文章标题</h1>
        <div class="rich_media_meta_list">
            <a class="rich_media_meta_nickname" id="js_name">测试公众号</a>
            <span class="rich_media_meta_text" id="js_author_name">测试作者</span>
        </div>
        <div class="rich_media_content" id="js_content">
            <p>9月20日晚，启元机器人在上海召开发布会，彭志辉站上舞台。</p>
            <section>
                <h2>核心技术突破</h2>
                <p>支撑这套产品定义的底层是自研鸡蛋关节，峰值扭矩密度极高。</p>
                <img data-src="https://mmbiz.qpic.cn/mmbiz_jpg/test_img_1/640?wx_fmt=jpeg" alt="关节细节" />
            </section>
            <blockquote>这是引用的专家评价。</blockquote>
            <p>机器人真正走向个人，让它进入每个人的生活与创造之中。</p>
        </div>
        <script>
            var ct = "1727768560";
        </script>
    </body>
    </html>
    """

    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return httpx.Response(200, headers={"Content-Type": "text/html; charset=utf-8"}, text=wechat_html)

    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handler), **kwargs)

    title, plain, markdown = web_reader.extract_wechat_page("https://mp.weixin.qq.com/s/sample", client_factory=factory)

    # 1. 验证使用了现代桌面浏览器/微信伪装 User-Agent，而非默认机器人 UA
    assert "PersonalKnowledgeAgent" not in captured_headers.get("user-agent", "")
    assert "Mozilla/5.0" in captured_headers.get("user-agent", "")

    # 2. 验证提取的元数据
    assert title == "测试微信文章标题"
    assert "测试公众号" in plain
    assert "测试作者" in plain
    assert "2024-10-01" in plain or "1727768560" in wechat_html

    # 3. 验证 Markdown 格式与图片 data-src 规范化
    assert "# 测试微信文章标题" in markdown
    assert "- **来源公众号**：测试公众号" in markdown
    assert "- **作者**：测试作者" in markdown
    assert "> 这是一篇用于单元测试的微信文章导语。" in markdown
    assert "## 核心技术突破" in markdown
    assert "![关节细节](https://mmbiz.qpic.cn/mmbiz_jpg/test_img_1/640?wx_fmt=jpeg)" in markdown
    assert "> 这是引用的专家评价。" in markdown


def test_extract_wechat_page_handles_verification_challenge(monkeypatch):
    monkeypatch.setattr("socket.getaddrinfo", lambda *args, **kwargs: [(None, None, None, None, ("182.254.116.116", 443))])
    monkeypatch.setattr(web_reader, "settings", SimpleNamespace(web_allow_fake_ip=False))

    challenge_html = """
    <!DOCTYPE html>
    <html>
    <head><title>微信公众平台</title></head>
    <body>
        <div id="js_content">
            <h3>环境异常</h3>
            <p>当前环境异常，完成验证后即可继续访问。去验证</p>
        </div>
    </body>
    </html>
    """

    factory = lambda **kwargs: httpx.Client(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, headers={"Content-Type": "text/html"}, text=challenge_html)), **kwargs)

    with pytest.raises(ValueError, match="微信公众号要求人工验证"):
        web_reader.extract_wechat_page("https://mp.weixin.qq.com/s/sample", client_factory=factory)


def test_wechat_url_saves_with_wechat_markdown_and_version(client, account, monkeypatch):
    import uuid
    wechat_url = "https://mp.weixin.qq.com/s/pL-yzT2u8C-Oh6h6Di2GSA"
    key = f"wechat-test-{uuid.uuid4()}"
    resp = client.post("/v1/captures", json={
        "capture_type": "url", "source_url": wechat_url,
        "content": {"text": ""}, "coverage": "link_only",
        "context": {"domain_id": account["domain"]["id"]},
        "client": {"kind": "desktop", "version": "0.1.0"},
    }, headers={**account["headers"], "Idempotency-Key": key})
    assert resp.status_code in {200, 201}
    source = resp.json()

    monkeypatch.setattr("services.worker.main.extract_wechat_page",
                        lambda url: ("微信文章标题", "微信文章纯文本正文", "# 微信文章标题\n\n正文Markdown"))

    with SessionLocal() as db:
        assert run_web_once(db)

    detail = client.get(f"/v1/sources/{source['id']}", headers=account["headers"]).json()
    assert detail["title"] == "微信文章标题"
    assert detail["extracted_text"] == "微信文章纯文本正文"
    assert detail["crawl4ai_markdown_available"] is True
    assert detail["crawl4ai_markdown_origin"] == "wechat_ua"

    markdown_path = f"/v1/sources/{source['id']}/crawl4ai-markdown"
    markdown_result = client.get(markdown_path, headers=account["headers"]).json()
    assert markdown_result["markdown"] == "# 微信文章标题\n\n正文Markdown"
    assert markdown_result["origin"] == "wechat_ua"



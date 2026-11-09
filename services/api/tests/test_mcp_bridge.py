"""The stdio bridge calls only the loopback API, with safe credential handling."""
import json

import httpx
import pytest

from app import mcp_bridge, mcp_server


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("PKA_API_URL", "http://127.0.0.1:8090/")
    monkeypatch.setenv("PKA_AGENT_TOKEN", "synthetic-private-agent-token")
    return monkeypatch


def mock_client(monkeypatch, handler):
    real_client = httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(mcp_bridge.httpx, "Client", client)
    return options


def test_bridge_sends_correct_bearer_payload_and_avoids_proxies_redirects(bridge):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [{"source_id": "synthetic-source"}]})

    options = mock_client(bridge, handler)
    payload = {"source_id": "synthetic-source"}
    assert mcp_bridge.call_api("get_tags", payload) == {"items": [{"source_id": "synthetic-source"}]}
    assert len(seen) == 1
    assert str(seen[0].url) == "http://127.0.0.1:8090/v1/mcp/tools/get_tags"
    assert seen[0].headers["Authorization"] == "Bearer synthetic-private-agent-token"
    assert json.loads(seen[0].content) == payload
    assert options == [{"timeout": 30, "trust_env": False, "follow_redirects": False}]


@pytest.mark.parametrize('tool', ['get_evidence', 'get_source', 'get_summary', 'get_tags',
                                 'search_knowledge', 'list_knowledge'])
def test_bridge_adds_clickable_citations_using_its_verified_endpoint(bridge, tool):
    from uuid import uuid4
    library_id, source_id = uuid4(), uuid4()
    native_url = f'knowledge-inbox://library/{library_id}/source/{source_id}'
    article = {'app_url': native_url, 'source_id': str(source_id), 'title': '测试资料'}
    listing = tool in {'search_knowledge', 'list_knowledge'}
    mock_client(bridge, lambda _: httpx.Response(200, json={'items': [article]} if listing else article))
    result = mcp_bridge.call_api(tool, {})
    item = result['items'][0] if listing else result
    assert item['app_url'] == native_url and item['source_id'] == str(source_id)
    assert item['citation_url'] == f'http://127.0.0.1:8090/open/library/{library_id}/source/{source_id}'


def test_bridge_does_not_create_a_handoff_for_an_untrusted_app_link(bridge):
    mock_client(bridge, lambda _: httpx.Response(200, json={'app_url': 'https://example.test/redirect'}))
    assert mcp_bridge.call_api('get_source', {}) == {'app_url': 'https://example.test/redirect'}


@pytest.mark.parametrize("status,message", [
    (401, "Agent 连接无效、已撤销或已过期"),
    (404, "请求的知识不存在或当前连接无权访问"),
    (500, "本地知识服务未能完成请求，请检查服务状态和参数"),
    (302, "本地知识服务未能完成请求，请检查服务状态和参数"),
])
def test_http_errors_hide_server_content_and_tokens(bridge, status, message):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, text="synthetic-private-agent-token server-sensitive-details",
                              headers={"Location": "https://example.test/leak"})

    mock_client(bridge, handler)
    with pytest.raises(ValueError) as error:
        mcp_bridge.call_api("get_summary", {"source_id": "source"})
    assert str(error.value) == message
    assert len(requests) == 1
    assert "synthetic-private-agent-token" not in str(error.value)
    assert "server-sensitive-details" not in str(error.value)


def test_network_error_is_safe_and_does_not_expose_token(bridge):
    def handler(request):
        raise httpx.ConnectError("synthetic-private-agent-token diagnostic", request=request)

    mock_client(bridge, handler)
    with pytest.raises(ValueError) as error:
        mcp_bridge.call_api("get_tags", {"source_id": "source"})
    assert str(error.value) == "无法访问本地知识服务，请启动客户端并检查 API 地址"
    assert "synthetic-private-agent-token" not in str(error.value)
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("address", [
    "https://127.0.0.1:8090", "http://example.test:8090", "http://localhost:8090",
    "http://0.0.0.0:8090", "http://127.0.0.1", "http://[::1]",
    "http://user:password@127.0.0.1:8090", "http://127.0.0.1:8090/path",
    "http://127.0.0.1:8090?token=private", "http://127.0.0.1:8090#fragment",
    "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:not-a-port",
])
def test_api_url_rejects_non_loopback_or_ambiguous_addresses(monkeypatch, address):
    monkeypatch.setenv("PKA_API_URL", address)
    with pytest.raises(ValueError, match="带端口的本机回环 HTTP 地址"):
        mcp_bridge.api_url()


@pytest.mark.parametrize("address,expected", [
    ("http://127.0.0.1:8090", "http://127.0.0.1:8090"),
    (" http://127.0.0.1:8090/ ", "http://127.0.0.1:8090"),
    ("http://[::1]:8090/", "http://[::1]:8090"),
])
def test_api_url_accepts_explicit_loopback_addresses(monkeypatch, address, expected):
    monkeypatch.setenv("PKA_API_URL", address)
    assert mcp_bridge.api_url() == expected


def test_api_url_unset_preserves_database_mode(monkeypatch):
    monkeypatch.delenv("PKA_API_URL", raising=False)
    assert mcp_bridge.api_url() is None


@pytest.mark.parametrize("tool,payload", [
    ("get_summary", {"source_id": "source"}),
    ("get_tags", {"source_id": "source"}),
    ("get_source", {"source_id": "source"}),
    ("get_evidence", {"evidence_id": "evidence", "context_window": 1}),
    ("search_knowledge", {"query": "检索", "task_context": "上下文", "project_id": None,
                          "domain_ids": None, "limit": 3, "tag_ids": None, "tag_query": "标签"}),
])
def test_all_tools_use_api_bridge_without_database(bridge, tool, payload):
    calls = []

    def call_api(name, body):
        calls.append((name, body))
        return {"bridge_result": name}

    bridge.setattr(mcp_server, "call_api", call_api)
    bridge.setattr(mcp_server, "SessionLocal", lambda: pytest.fail("Bridge mode must not open a database session"))
    assert getattr(mcp_server, tool)(**payload) == {"bridge_result": tool}
    assert calls == [(tool, payload)]


def test_bridge_main_runs_stdio_without_schema_or_database(bridge):
    calls = []
    bridge.setattr(mcp_server, "create_schema", lambda: pytest.fail("Bridge mode must not initialize the schema"))
    bridge.setattr(mcp_server, "SessionLocal", lambda: pytest.fail("Bridge mode must not open a database session"))
    bridge.setattr(mcp_server.mcp, "run", lambda **options: calls.append(options))
    mcp_server.main()
    assert calls == [{"transport": "stdio"}]

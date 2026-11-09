"""Stable endpoint discovery follows client restarts without touching its database."""
import asyncio
import json
import os
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app import mcp_bridge, mcp_server


@pytest.fixture
def endpoint(monkeypatch, tmp_path):
    monkeypatch.delenv("PKA_API_URL", raising=False)
    monkeypatch.setenv("PKA_AGENT_TOKEN", "synthetic-private-agent-token")
    path = tmp_path / "agent-api.json"
    write_endpoint(path)
    monkeypatch.setenv("PKA_API_FILE", str(path))
    return path


def write_endpoint(path, port=8090):
    path.write_text(json.dumps({"api_url": f"http://127.0.0.1:{port}", "library_id": "synthetic-library"}))
    path.chmod(0o600)


def mock_client(monkeypatch, handler):
    real_client = httpx.Client
    options = []

    def client(**kwargs):
        options.append(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(mcp_bridge.httpx, "Client", client)
    return options


def test_reads_private_endpoint_and_explicit_url_takes_priority(endpoint, monkeypatch):
    assert mcp_bridge.api_url() == "http://127.0.0.1:8090"
    assert mcp_bridge.bridge_configured()
    endpoint.unlink()
    monkeypatch.setenv("PKA_API_URL", "http://[::1]:8092/")
    assert mcp_bridge.api_url() == "http://[::1]:8092"


def test_endpoint_allows_publisher_instance_id(endpoint):
    payload = json.loads(endpoint.read_text())
    payload["instance_id"] = "synthetic-instance"
    endpoint.write_text(json.dumps(payload))
    assert mcp_bridge.api_url() == "http://127.0.0.1:8090"


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o610])
def test_rejects_group_or_other_access(endpoint, mode):
    endpoint.chmod(mode)
    with pytest.raises(ValueError, match="端点文件不可用或无效"):
        mcp_bridge.api_url()


@pytest.mark.parametrize("problem", ["missing", "symlink", "directory", "relative", "oversize", "foreign_owner"])
def test_rejects_unsafe_files(endpoint, monkeypatch, problem):
    if problem == "missing":
        endpoint.unlink()
    elif problem == "symlink":
        target = endpoint.with_name("target.json")
        endpoint.rename(target)
        endpoint.symlink_to(target)
    elif problem == "directory":
        endpoint.unlink()
        endpoint.mkdir(mode=0o700)
    elif problem == "relative":
        monkeypatch.setenv("PKA_API_FILE", "agent-api.json")
    elif problem == "oversize":
        endpoint.write_bytes(b"x" * 4097)
    else:
        real_fstat = os.fstat

        def foreign_fstat(fd):
            info = real_fstat(fd)
            return SimpleNamespace(st_mode=info.st_mode, st_uid=os.getuid() + 1,
                                   st_size=info.st_size, st_dev=info.st_dev, st_ino=info.st_ino)

        monkeypatch.setattr(mcp_bridge.os, "fstat", foreign_fstat)
    with pytest.raises(ValueError, match="端点文件不可用或无效") as error:
        mcp_bridge.api_url()
    assert str(endpoint) not in str(error.value)
    assert error.value.__suppress_context__


@pytest.mark.parametrize("data", [
    b"{bad private diagnostic", b"\xff", b"[]", b"null",
    b'{"api_url": "http://127.0.0.1:8090"}',
    b'{"api_url": 123, "library_id": "library"}',
    b'{"api_url": "http://127.0.0.1:8090", "library_id": ""}',
    b'{"api_url": "http://external.test:8090", "library_id": "library"}',
    b'{"api_url": "http://127.0.0.1:8090", "library_id": "library", "token": "private"}',
])
def test_malformed_content_is_safe(endpoint, data):
    endpoint.write_bytes(data)
    with pytest.raises(ValueError, match="端点文件不可用或无效") as error:
        mcp_bridge.api_url()
    assert "private" not in str(error.value)
    assert "external.test" not in str(error.value)


def test_same_process_rereads_endpoint_after_atomic_replacement(endpoint, monkeypatch):
    ports = []

    def handler(request):
        ports.append(request.url.port)
        assert request.headers["Authorization"] == "Bearer synthetic-private-agent-token"
        return httpx.Response(200, json={"available": True})

    options = mock_client(monkeypatch, handler)
    assert mcp_server.get_summary("source") == {"available": True}
    updated = endpoint.with_name("next.json")
    write_endpoint(updated, 8093)
    updated.replace(endpoint)
    assert mcp_server.get_summary("source") == {"available": True}
    assert ports == [8090, 8093]
    assert all(option == {"timeout": 30, "trust_env": False, "follow_redirects": False} for option in options)


@pytest.mark.parametrize("tool,payload", [
    ("search_knowledge", {"query": "资料"}),
    ("get_summary", {"source_id": "source"}),
    ("get_tags", {"source_id": "source"}),
    ("get_source", {"source_id": "source"}),
    ("get_evidence", {"evidence_id": "evidence"}),
])
def test_stopped_client_does_not_fall_back_to_database(endpoint, monkeypatch, tool, payload):
    endpoint.unlink()
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: pytest.fail("Must not fall back to the database"))
    with pytest.raises(ValueError, match="端点文件不可用或无效"):
        getattr(mcp_server, tool)(**payload)


def test_stopped_service_network_error_is_safe(endpoint, monkeypatch):
    def handler(request):
        raise httpx.ConnectError("synthetic-private-agent-token diagnostics", request=request)

    mock_client(monkeypatch, handler)
    with pytest.raises(ValueError, match="无法访问本地知识服务") as error:
        mcp_server.get_source("source")
    assert "synthetic-private-agent-token" not in str(error.value)


def test_missing_endpoint_still_starts_and_discovers_all_tools(endpoint, monkeypatch):
    endpoint.unlink()
    monkeypatch.setattr(mcp_server, "create_schema", lambda: pytest.fail("Must not create schema"))
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: pytest.fail("Must not access database"))
    names = []

    def run(**options):
        assert options == {"transport": "stdio"}
        names.extend(tool.name for tool in asyncio.run(mcp_server.mcp.list_tools()))

    monkeypatch.setattr(mcp_server.mcp, "run", run)
    mcp_server.main()
    assert set(names) == {"search_knowledge", "get_evidence", "get_source", "get_summary", "get_tags", "list_knowledge"}
    # The bundled entrypoint must also defer endpoint parsing until a tool call.
    runpy.run_path(str(Path(mcp_server.__file__).with_name("local_mcp.py")), run_name="__main__")
    assert len(names) == 12


def test_only_unconfigured_bridge_preserves_database_mode(monkeypatch):
    monkeypatch.delenv("PKA_API_URL", raising=False)
    monkeypatch.delenv("PKA_API_FILE", raising=False)
    assert not mcp_bridge.bridge_configured()
    assert mcp_bridge.api_url() is None


def test_real_stdio_discovers_tools_when_client_has_not_started(endpoint):
    import sys

    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    endpoint.unlink()
    database = endpoint.with_name("must-not-create.db")
    environment = os.environ.copy()
    environment["PKA_DATABASE_URL"] = f"sqlite:///{database}"
    environment["PKA_ENVIRONMENT"] = "test"
    server = StdioServerParameters(
        command=sys.executable,
        args=[str(Path(mcp_server.__file__).with_name("local_mcp.py"))],
        env=environment,
    )

    async def check():
        async with stdio_client(server) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} == {
                    "search_knowledge", "get_evidence", "get_source", "get_summary", "get_tags", "list_knowledge",
                }
                result = await session.call_tool("get_source", {"source_id": "source"})
                assert result.isError
                assert "端点文件不可用或无效" in result.content[0].text
                assert "synthetic-private-agent-token" not in result.content[0].text
                assert str(endpoint) not in result.content[0].text

    asyncio.run(asyncio.wait_for(check(), timeout=20))
    assert not database.exists()

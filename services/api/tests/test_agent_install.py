import json
from pathlib import Path
import stat
import subprocess
import sys
import uuid

import pytest
import tomli

from app import agent_install as installer


def test_isolated_cli_can_load_platform_adapter_without_modifying_config(setup):
    request, _ = setup
    value = request(action='status')
    script = Path(installer.__file__).resolve()
    result = subprocess.run([sys.executable, '-I', '-B', str(script)], input=json.dumps(value).encode(),
                            capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)['installed'] is False
    assert not Path(value['config_path']).exists()


@pytest.fixture
def setup(tmp_path, monkeypatch):
    async def check(entry):
        assert entry["env"]["PKA_AGENT_TOKEN"] == "synthetic-secret"
    monkeypatch.setattr(installer, "_check", check)
    tmp_path = tmp_path.resolve()
    identifier = str(uuid.uuid4())
    name = "knowledge_" + identifier.replace("-", "_")
    entry = {"command": "/fixture/python", "args": ["-I", "/fixture/local_mcp.py"], "env": {"PKA_AGENT_TOKEN": "synthetic-secret", "PKA_API_FILE": "/fixture/agent-api.json"}}
    def request(client="codex", action="install"):
        configuration = (f'[mcp_servers.{name}]\ncommand = "/fixture/python"\nargs = ["-I", "/fixture/local_mcp.py"]\n[mcp_servers.{name}.env]\nPKA_AGENT_TOKEN = "synthetic-secret"\nPKA_API_FILE = "/fixture/agent-api.json"\n' if client == "codex" else json.dumps({"mcpServers": {name: entry}}))
        return {"action": action, "client": client, "connection_id": identifier, "configuration": configuration, "config_path": str(tmp_path / ("config.toml" if client == "codex" else ".claude.json"))}
    return request, name


@pytest.mark.parametrize("client", ["codex", "claudeCode"])
def test_install_preserves_other_settings_backup_and_repeat(setup, client):
    request, name = setup
    data = request(client)
    path = Path(data["config_path"])
    original = b'model = "fixture"\n[mcp_servers.other]\ncommand = "other"\n' if client == "codex" else b'{"theme":"dark","mcpServers":{"other":{"command":"other"}}}'
    path.write_bytes(original)
    result = installer.execute(data)
    assert result["installed"] and result["checked"]
    backup = Path(result["backup_path"])
    assert backup.read_bytes() == original
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    parser = tomli.loads if client == "codex" else json.loads
    value = parser(path.read_text())
    assert value["mcp_servers" if client == "codex" else "mcpServers"]["other"] == {"command": "other"}
    after = path.read_bytes()
    installer.execute(data)
    assert path.read_bytes() == after
    assert installer.execute(request(client, "status"))["installed"]
    assert installer.execute(request(client, "check"))["checked"]
    assert not installer.execute(request(client, "remove"))["installed"]
    removed = path.read_bytes()
    installer.execute(request(client, "remove"))
    assert path.read_bytes() == removed
    assert not installer.execute(request(client, "status"))["installed"]


@pytest.mark.parametrize("client", ["codex", "claudeCode"])
def test_failure_restores_original(setup, monkeypatch, client):
    request, _ = setup
    data = request(client)
    path = Path(data["config_path"])
    original = b'model = "fixture"\n' if client == "codex" else b'{"theme":"dark"}'
    path.write_bytes(original)
    async def fail(entry):
        raise RuntimeError("synthetic-secret")
    monkeypatch.setattr(installer, "_check", fail)
    with pytest.raises(installer.InstallError, match="已恢复原配置"):
        installer.execute(data)
    assert path.read_bytes() == original


def test_failed_new_install_removes_file(setup, monkeypatch):
    request, _ = setup
    async def fail(entry):
        raise RuntimeError()
    monkeypatch.setattr(installer, "_check", fail)
    with pytest.raises(installer.InstallError):
        installer.execute(request())
    assert not Path(request()["config_path"]).exists()


def test_failure_does_not_overwrite_concurrent_change(setup, monkeypatch):
    request, _ = setup
    data = request()
    path = Path(data["config_path"])
    async def fail(entry):
        path.write_text('model = "external-change"\n')
        raise RuntimeError()
    monkeypatch.setattr(installer, "_check", fail)
    with pytest.raises(installer.InstallError, match="未覆盖其更改"):
        installer.execute(data)
    assert path.read_text() == 'model = "external-change"\n'


@pytest.mark.parametrize("client", ["codex", "claudeCode"])
def test_malformed_and_conflict_untouched(setup, client):
    request, name = setup
    data = request(client)
    path = Path(data["config_path"])
    for original in (b'{INVALID', (f'[mcp_servers.{name}]\ncommand="foreign"\n' if client == "codex" else json.dumps({"mcpServers": {name: {"command": "foreign"}}})).encode()):
        path.write_bytes(original)
        with pytest.raises(installer.InstallError):
            installer.execute(data)
        assert path.read_bytes() == original


def test_symlink_and_fifo_rejected(setup, tmp_path):
    request, _ = setup
    data = request()
    path = Path(data["config_path"])
    target = tmp_path / "target"
    target.write_text("untouched")
    path.symlink_to(target)
    with pytest.raises(installer.InstallError):
        installer.execute(data)
    assert target.read_text() == "untouched"
    path.unlink()
    import os
    os.mkfifo(path)
    with pytest.raises(installer.InstallError):
        installer.execute(data)


def test_parent_symlink_rejected(setup, tmp_path):
    request, _ = setup
    directory = tmp_path / "real"
    directory.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(directory)
    data = request()
    data["config_path"] = str(alias / "config.toml")
    with pytest.raises(installer.InstallError):
        installer.execute(data)
    assert not (directory / "config.toml").exists()


def test_codex_inline_settings_rejected_if_merge_changes_scope(setup):
    request, _ = setup
    data = request()
    path = Path(data["config_path"])
    original = b'mcp_servers = { other = { command = "other" } }\n'
    path.write_bytes(original)
    with pytest.raises(installer.InstallError):
        installer.execute(data)
    assert path.read_bytes() == original


def test_changed_before_write_is_not_overwritten(setup, monkeypatch):
    request, _ = setup
    data = request()
    path = Path(data["config_path"])
    path.write_text('model = "old"\n')
    original_read = installer._read
    count = 0
    def read(candidate):
        nonlocal count
        count += 1
        if count == 2:
            path.write_text('model = "new"\n')
        return original_read(candidate)
    monkeypatch.setattr(installer, "_read", read)
    with pytest.raises(installer.InstallError, match="其他程序修改"):
        installer.execute(data)
    assert path.read_text() == 'model = "new"\n'


@pytest.mark.parametrize("action", ["install", "check"])
def test_successful_check_detects_concurrent_change(setup, monkeypatch, action):
    request, _ = setup
    installer.execute(request())
    data = request(action=action)
    path = Path(data["config_path"])
    async def change(entry):
        path.write_text('model = "external"\n')
    monkeypatch.setattr(installer, "_check", change)
    with pytest.raises(installer.InstallError, match="其他程序修改"):
        installer.execute(data)
    assert path.read_text() == 'model = "external"\n'


def test_lock_is_nonblocking(setup):
    request, _ = setup
    data = request()
    path = Path(data["config_path"])
    with installer._lock(path):
        with pytest.raises(installer.InstallError, match="正在进行"):
            installer.execute(data)


def test_codex_modified_managed_entry_not_removed(setup):
    request, _ = setup
    installer.execute(request())
    path = Path(request()["config_path"])
    value = path.read_bytes().replace(b"synthetic-secret", b"external-secret")
    path.write_bytes(value)
    with pytest.raises(installer.InstallError, match="未移除"):
        installer.execute(request(action="remove"))
    assert path.read_bytes() == value


@pytest.mark.parametrize("client", ["codex", "claudeCode"])
def test_owned_removal_needs_no_credential_and_is_repeatable(setup, client):
    request, _ = setup
    data = request(client)
    installer.execute(data)
    removal = {key: value for key, value in data.items() if key != "configuration"}
    removal.update(action="remove_owned", runtime_path="/fixture", endpoint_path="/fixture/agent-api.json")
    # Match the production launcher shape; the fixture intentionally used shorter args.
    path = Path(data["config_path"])
    if client == "codex":
        path.write_text(path.read_text().replace('/fixture/python', '/fixture/python/bin/python3').replace('["-I", "/fixture/local_mcp.py"]', '["-I", "-B", "/fixture/backend/app/local_mcp.py"]'))
    else:
        document = json.loads(path.read_text())
        entry = next(iter(document["mcpServers"].values()))
        entry["args"] = ["-I", "-B", "/fixture/backend/app/local_mcp.py"]
        entry["type"] = "stdio"
        entry["command"] = "/fixture/python/bin/python3"
        path.write_text(json.dumps(document))
    result = installer.execute(removal)
    assert not result["installed"]
    assert "synthetic-secret" not in path.read_text()
    assert installer.execute(removal)["message"] == "此连接配置已经不存在"


@pytest.mark.parametrize("change", ["endpoint", "runtime", "unmanaged"])
def test_owned_removal_rejects_foreign_scope_and_unmanaged_config(setup, change):
    request, _ = setup
    data = request()
    installer.execute(data)
    path = Path(data["config_path"])
    text = path.read_text().replace('/fixture/python', '/fixture/python/bin/python3').replace('["-I", "/fixture/local_mcp.py"]', '["-I", "-B", "/fixture/backend/app/local_mcp.py"]')
    if change == "unmanaged":
        text = "\n".join(line for line in text.splitlines() if not line.startswith("# BEGIN PKA") and not line.startswith("# END PKA")) + "\n"
    path.write_text(text)
    removal = dict(data, action="remove_owned", runtime_path="/foreign" if change == "runtime" else "/fixture",
                   endpoint_path="/foreign/agent-api.json" if change == "endpoint" else "/fixture/agent-api.json")
    with pytest.raises(installer.InstallError):
        installer.execute(removal)
    assert path.read_text() == text


def test_plugin_migration_preflight_is_read_only_and_checks_ownership(setup):
    request, _ = setup
    data = request()
    installer.execute(data)
    path = Path(data["config_path"])
    text = path.read_text().replace('/fixture/python', '/fixture/python/bin/python3').replace(
        '["-I", "/fixture/local_mcp.py"]', '["-I", "-B", "/fixture/backend/app/local_mcp.py"]')
    path.write_text(text)
    preflight = dict(data, action="migration_check", runtime_path="/fixture", endpoint_path="/fixture/agent-api.json")
    preflight.pop("configuration")
    assert installer.execute(preflight)["installed"]
    assert path.read_text() == text
    with pytest.raises(installer.InstallError):
        installer.execute(dict(preflight, endpoint_path="/foreign/agent-api.json"))
    assert path.read_text() == text
    installer.execute(dict(preflight, action="remove_owned"))
    empty = path.read_bytes()
    assert not installer.execute(preflight)["installed"]
    assert path.read_bytes() == empty


def test_connection_check_uses_base_exact_search_without_semantic_extension(monkeypatch):
    import asyncio
    from contextlib import asynccontextmanager
    from types import SimpleNamespace
    import mcp
    import mcp.client.stdio
    from app.agent_install import _check, TOOLS

    class Session:
        def __init__(self, *_): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): pass
        async def initialize(self): pass
        async def list_tools(self): return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in TOOLS])
        async def call_tool(self, name, payload):
            assert name == 'search_knowledge' and payload['mode'] == 'exact'
            return SimpleNamespace(isError=False)

    @asynccontextmanager
    async def transport(*_, **__):
        yield (None, None)

    monkeypatch.setattr(mcp, 'ClientSession', Session)
    monkeypatch.setattr(mcp.client.stdio, 'stdio_client', transport)
    asyncio.run(_check({'command': 'synthetic', 'args': [], 'env': {}}))

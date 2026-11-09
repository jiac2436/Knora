"""Reference opt-in changes only owned skill files and the exact routing block."""
import json
from pathlib import Path
import stat
import uuid

import pytest

from app import agent_install, agent_reference as reference


@pytest.fixture
def setup(tmp_path):
    root = tmp_path.resolve()
    identifier = str(uuid.uuid4())
    server = "knowledge_" + identifier.replace("-", "_")
    def request(client="codex", action="reference_enable"):
        config = root / ("config.toml" if client == "codex" else ".claude.json")
        config.write_text(f'[mcp_servers.{server}]\ncommand="fixture"\n' if client == "codex"
                          else json.dumps({"mcpServers": {server: {"command": "fixture"}}}))
        return {"action": action, "client": client, "connection_id": identifier,
                "config_path": str(config), "skills_root": str(root / client / "skills"),
                "instructions_path": str(root / client / ("AGENTS.md" if client == "codex" else "CLAUDE.md"))}
    return request


@pytest.mark.parametrize("client", ["codex", "claudeCode"])
def test_enable_repeat_disable_preserves_original_bytes_and_no_credentials(setup, client):
    request = setup(client)
    instruction = Path(request["instructions_path"])
    instruction.parent.mkdir(parents=True)
    original = "# 自定义规则\r\n保留用户配置，不要覆盖。".encode()
    instruction.write_bytes(original)
    result = agent_install.execute(request)
    assert result["auto_reference_enabled"]
    first = instruction.read_bytes()
    assert first.startswith(original)
    directory = Path(request["skills_root"]) / ("pka-knowledge-" + request["connection_id"])
    skill = directory / "SKILL.md"
    assert stat.S_IMODE(skill.stat().st_mode) == 0o600
    assert "knowledge_" + request["connection_id"].replace("-", "_") in skill.read_text()
    assert all(word not in skill.read_text() for word in ["PKA_AGENT_TOKEN", "杰克", "synthetic-secret"])
    assert agent_install.execute(setup(client, "reference_status"))["reference_status"] == "enabled"
    agent_install.execute(request)
    assert instruction.read_bytes() == first
    agent_install.execute(setup(client, "reference_disable"))
    assert instruction.read_bytes() == original
    assert not skill.exists()
    assert agent_install.execute(setup(client, "reference_disable"))["auto_reference_enabled"] is False
    assert agent_install.execute(setup(client, "reference_status"))["reference_status"] == "disabled"
    assert any(Path(path).read_bytes() == original for path in result["backup_paths"])


def test_new_instruction_removed_when_disabled_and_directory_edits_preserved(setup):
    request = setup()
    instruction = Path(request["instructions_path"])
    agent_install.execute(request)
    directory = Path(request["skills_root"]) / ("pka-knowledge-" + request["connection_id"])
    extra = directory / "personal-extra.txt"
    extra.write_text("user-owned")
    agent_install.execute(setup(action="reference_disable"))
    assert not instruction.exists()
    assert extra.read_text() == "user-owned"


@pytest.mark.parametrize("target", ["skill", "block", "manifest"])
def test_user_modified_owned_content_is_not_overwritten_or_removed(setup, target):
    request = setup()
    agent_install.execute(request)
    directory = Path(request["skills_root"]) / ("pka-knowledge-" + request["connection_id"])
    path = directory / ("SKILL.md" if target == "skill" else ".pka-managed.json")
    if target == "block":
        path = Path(request["instructions_path"])
        path.write_bytes(path.read_bytes().replace("优先使用".encode(), "用户修改".encode()))
    else:
        path.write_bytes(path.read_bytes() + b"user-edit")
    before = path.read_bytes()
    assert agent_install.execute(setup(action="reference_status"))["reference_status"] == "modified"
    for action in ["reference_enable", "reference_disable"]:
        with pytest.raises(agent_install.InstallError, match="未覆盖"):
            agent_install.execute(setup(action=action))
        assert path.read_bytes() == before


def test_disable_keeps_edits_outside_managed_block(setup):
    request = setup()
    instruction = Path(request["instructions_path"])
    agent_install.execute(request)
    instruction.write_bytes(b"# New user rule\n" + instruction.read_bytes() + b"\nMore user rules\n")
    agent_install.execute(setup(action="reference_disable"))
    assert instruction.read_bytes() == b"# New user rule\n\nMore user rules\n"


def test_multiple_connections_are_independent(setup):
    first = setup()
    agent_install.execute(first)
    second = dict(first, connection_id=str(uuid.uuid4()))
    config = Path(second["config_path"])
    config.write_text(config.read_text() + '[mcp_servers.knowledge_' + second["connection_id"].replace("-", "_") + ']\ncommand="fixture"\n')
    agent_install.execute(second)
    agent_install.execute(dict(first, action="reference_disable"))
    assert agent_install.execute(dict(second, action="reference_status"))["auto_reference_enabled"]
    assert second["connection_id"] in Path(second["instructions_path"]).read_text()
    assert first["connection_id"] not in Path(second["instructions_path"]).read_text()


def test_missing_mcp_and_readonly_status_do_not_create_files(setup):
    request = setup()
    Path(request["config_path"]).write_text('model="fixture"\n')
    assert agent_install.execute(dict(request, action="reference_status"))["reference_status"] == "disabled"
    assert not Path(request["skills_root"]).exists()
    with pytest.raises(agent_install.InstallError, match="先接入"):
        agent_install.execute(request)
    assert not Path(request["skills_root"]).exists()


@pytest.mark.parametrize("target", ["skill_parent", "instructions"])
def test_symlink_rejected(setup, tmp_path, target):
    request = setup()
    destination = tmp_path.resolve() / "untouched"
    destination.mkdir()
    if target == "skill_parent":
        root = Path(request["skills_root"])
        root.parent.mkdir(parents=True)
        root.symlink_to(destination)
    else:
        instructions = Path(request["instructions_path"])
        instructions.parent.mkdir(parents=True)
        original = destination / "file"
        original.write_text("untouched")
        instructions.symlink_to(original)
    with pytest.raises(agent_install.InstallError):
        agent_install.execute(request)
    assert list(destination.iterdir()) == ([] if target == "skill_parent" else [original])


def test_partial_install_detected_without_silently_replacing_user_file(setup):
    request = setup()
    directory = Path(request["skills_root"]) / ("pka-knowledge-" + request["connection_id"])
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("existing user skill")
    assert agent_install.execute(dict(request, action="reference_status"))["reference_status"] == "partial"
    with pytest.raises(agent_install.InstallError):
        agent_install.execute(request)
    assert (directory / "SKILL.md").read_text() == "existing user skill"


def test_write_failure_restores_prior_instruction_and_skill(setup, monkeypatch):
    request = setup()
    instruction = Path(request["instructions_path"])
    instruction.parent.mkdir(parents=True)
    instruction.write_bytes(b"# user rules\n")
    real_atomic = reference._atomic
    def fail_manifest(path, value):
        if path.name == ".pka-managed.json":
            raise OSError("fixture write failure")
        real_atomic(path, value)
    monkeypatch.setattr(reference, "_atomic", fail_manifest)
    with pytest.raises(OSError):
        agent_install.execute(request)
    assert instruction.read_bytes() == b"# user rules\n"
    assert not list(Path(request["skills_root"]).glob("*/SKILL.md"))


def test_concurrent_user_change_not_overwritten_by_rollback(setup, monkeypatch):
    request = setup()
    instruction = Path(request["instructions_path"])
    real_atomic = reference._atomic
    def change_then_fail(path, value):
        if path.name == ".pka-managed.json":
            instruction.write_bytes(b"concurrent user edit")
            raise OSError("fixture write failure")
        real_atomic(path, value)
    monkeypatch.setattr(reference, "_atomic", change_then_fail)
    with pytest.raises(OSError):
        agent_install.execute(request)
    assert instruction.read_bytes() == b"concurrent user edit"


def test_codex_instruction_limit_is_respected(setup):
    request = setup()
    config = Path(request["config_path"])
    config.write_text('project_doc_max_bytes=100\n' + config.read_text())
    with pytest.raises(agent_install.InstallError, match="加载长度"):
        agent_install.execute(request)
    assert not Path(request["instructions_path"]).exists()


def test_isolated_standalone_helper_success_and_errors_are_visible(setup):
    import subprocess
    import sys
    script = Path(agent_install.__file__).resolve()
    request = setup()
    for action, enabled in [("reference_enable", True), ("reference_status", True), ("reference_disable", False)]:
        process = subprocess.run([sys.executable, "-I", "-B", str(script)],
                                 input=json.dumps(dict(request, action=action)), text=True, capture_output=True, timeout=10)
        assert process.returncode == 0, process.stdout
        assert json.loads(process.stdout)["auto_reference_enabled"] == enabled
    Path(request["config_path"]).write_text('model="fixture"\n')
    process = subprocess.run([sys.executable, "-I", "-B", str(script)],
                             input=json.dumps(request), text=True, capture_output=True, timeout=10)
    assert process.returncode == 1
    assert "请先接入" in json.loads(process.stdout)["message"]

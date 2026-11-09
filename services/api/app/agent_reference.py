"""Install connection-scoped skills and reversible, user-scope routing instructions."""
from __future__ import annotations

from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import uuid

# This module is also loaded by the isolated standalone installer.
try:
    from .agent_install import InstallError, _atomic, _lock, _parse, _read
except ImportError:
    from agent_install import InstallError, _atomic, _lock, _parse, _read


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _paths(request: dict):
    try:
        identifier = str(uuid.UUID(request["connection_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise InstallError("连接标识无效") from None
    client = request.get("client")
    if client not in {"codex", "claudeCode"}:
        raise InstallError("该 Agent 暂不支持自动参考知识库")
    name = "pka-knowledge-" + identifier
    root = Path(request["skills_root"])
    instructions = Path(request["instructions_path"])
    if not root.is_absolute() or not instructions.is_absolute():
        raise InstallError("Skill 和指令路径必须是绝对路径")
    if instructions.name not in ({"AGENTS.md", "AGENTS.override.md"} if client == "codex" else {"CLAUDE.md"}):
        raise InstallError("持久指令文件名无效")
    directory = root / name
    return identifier, name, directory / "SKILL.md", directory / ".pka-managed.json", instructions


def _template() -> str:
    bundled = Path(__file__).with_name("agent_skill.md")
    source = Path(__file__).resolve().parents[3] / "skills/personal-knowledge/SKILL.md"
    path = bundled if bundled.exists() else source
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise InstallError("未找到有效知识库 Skill，请使用新版打包客户端") from None


def _content(identifier: str, name: str):
    server = "knowledge_" + identifier.replace("-", "_")
    skill = _template().replace("name: personal-knowledge\n", f"name: {name}\n", 1)
    skill += (f"\n## 当前连接\n\n此 Skill 仅使用 MCP 服务 `{server}` 的工具。"
              "不要用其他知识库连接代替。连接不可用时说明无法查询，不把鉴权失败当作没有资料。\n")
    marker = f"PKA AUTO REFERENCE {identifier}"
    block = (f"\n<!-- BEGIN {marker} -->\n"
             f"涉及用户本人信息、已保存资料、历史决策或需要已有项目经验时，优先使用 `{name}` Skill "
             f"查询 MCP 服务 `{server}`，再根据原文回答，无需用户明确说查询知识库。"
             "仅在资料与当前任务相关时检索；普通闲聊、纯翻译和无需个人背景的常识通常不检索。"
             "用户明确要求不查询时遵从；候选和总结不能代替原文证据，无依据时明确说明。\n"
             f"<!-- END {marker} -->\n").encode()
    return skill.encode(), block


def _state(identifier, name, skill_path, manifest_path, instructions):
    values = {path: _read(path) for path in (skill_path, manifest_path, instructions)}
    skill, raw, text = values[skill_path], values[manifest_path], values[instructions] or b""
    marker = f"PKA AUTO REFERENCE {identifier}".encode()
    if raw is None:
        status = "disabled" if skill is None and marker not in text else "partial"
        return status, values, None
    try:
        manifest = json.loads(raw)
        block = manifest["block"].encode()
        if (manifest["schema_version"] != 1 or manifest["skill_name"] != name
                or manifest["instructions_path"] != str(instructions)
                or not isinstance(manifest["instructions_created"], bool)
                or not isinstance(manifest["skill_sha256"], str)
                or marker not in block or not block.startswith(b"\n<!-- BEGIN ")
                or not block.endswith(b" -->\n")):
            raise ValueError()
    except (ValueError, KeyError, TypeError, AttributeError, UnicodeError):
        return "modified", values, None
    if skill is None or marker not in text:
        return "partial", values, manifest
    if (_digest(skill) != manifest["skill_sha256"] or text.count(block) != 1
            or text.count(marker) != 2):
        return "modified", values, manifest
    return "enabled", values, manifest


def _write_changes(changes: dict, originals: dict) -> list[str]:
    backups, applied = [], []
    try:
        # Validate every target before the first mutation.
        for path in changes:
            if _read(path) != originals[path]:
                raise InstallError("安装期间文件被其他程序修改，未覆盖其更改")
        for path, updated in changes.items():
            original = originals[path]
            if original == updated:
                continue
            if original is not None:
                backup = path.with_name(path.name + ".pka-backup-" + uuid.uuid4().hex)
                _atomic(backup, original)
                backups.append(str(backup))
            if _read(path) != original:
                raise InstallError("安装期间文件被其他程序修改，未覆盖其更改")
            if updated is None:
                path.unlink()
            else:
                _atomic(path, updated)
            applied.append(path)
        if any(_read(path) != updated for path, updated in changes.items()):
            raise InstallError("安装期间文件被其他程序修改，状态未确认")
    except Exception:
        for path in reversed(applied):
            # Only undo our own bytes; preserve concurrent edits.
            if _read(path) == changes[path]:
                if originals[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic(path, originals[path])
        raise
    return backups


def execute_reference(request: dict) -> dict:
    action = request.get("action")
    if action not in {"reference_enable", "reference_disable", "reference_status"}:
        raise InstallError("不支持的自动参考操作")
    identifier, name, skill, manifest_path, instructions = _paths(request)
    state, originals, manifest = _state(identifier, name, skill, manifest_path, instructions)
    if action == "reference_status":
        return {"auto_reference_enabled": state == "enabled", "reference_status": state,
                "message": {"enabled": "已安装自动参考规则，请在 Agent 中开启新会话", "disabled": "自动参考未开启",
                            "modified": "Skill 或指令已被修改，请手动检查", "partial": "自动参考安装不完整，请手动检查"}[state]}
    if state in {"modified", "partial"}:
        raise InstallError("Skill 或受管指令已修改或安装不完整，未覆盖；请先手动检查")
    if action == "reference_disable" and state == "disabled":
        return {"auto_reference_enabled": False, "reference_status": "disabled", "message": "自动参考已关闭"}
    if action == "reference_enable":
        config_path = Path(request["config_path"])
        config = _parse(_read(config_path), request["client"])
        server = "knowledge_" + identifier.replace("-", "_")
        if server not in config.get("mcp_servers" if request["client"] == "codex" else "mcpServers", {}):
            raise InstallError("请先接入此连接，再开启自动参考知识库")
        skill_text, block = _content(identifier, name)
    with ExitStack() as locks:
        # Serialize connections sharing global instructions and a skills directory.
        locks.enter_context(_lock(instructions))
        locks.enter_context(_lock(skill.parent.parent / ".pka-reference"))
        state, originals, manifest = _state(identifier, name, skill, manifest_path, instructions)
        if state in {"modified", "partial"}:
            raise InstallError("Skill 或受管指令已修改，未覆盖；请先手动检查")
        text = originals[instructions] or b""
        if action == "reference_enable":
            try:
                text.decode("utf-8")
            except UnicodeError:
                raise InstallError("持久指令不是有效 UTF-8，原文件未修改") from None
            if manifest:
                text = text.replace(manifest["block"].encode(), block, 1)
            else:
                text += block
            if request["client"] == "codex":
                maximum = config.get("project_doc_max_bytes", 32768)
                if not isinstance(maximum, int) or maximum <= 0 or len(text) > maximum:
                    raise InstallError("全局指令超过 Codex 加载长度，请先精简或手动配置")
            metadata = {"schema_version": 1, "skill_name": name,
                        "skill_sha256": _digest(skill_text), "block": block.decode(),
                        "instructions_path": str(instructions),
                        "instructions_created": manifest["instructions_created"] if manifest else originals[instructions] is None}
            changes = {skill: skill_text, instructions: text,
                       manifest_path: (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode()}
        else:
            text = text.replace(manifest["block"].encode(), b"", 1)
            changes = {instructions: None if not text and manifest["instructions_created"] else text,
                       skill: None, manifest_path: None}
        backups = _write_changes(changes, originals)
    enabled = action == "reference_enable"
    return {"auto_reference_enabled": enabled, "reference_status": "enabled" if enabled else "disabled",
            "message": "自动参考已开启，Skill 和触发规则已安装；请在 Agent 中开启新会话" if enabled
                       else "自动参考已关闭，已移除本连接 Skill 和触发规则；请在 Agent 中开启新会话",
            "backup_paths": backups}

"""User-triggered, conservative installation of a private local MCP configuration."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import sys
import uuid

import tomli

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.platform_support import get_platform
from app.platform_support.contracts import UnsafeFile, UnsupportedPlatform

MAX_FILE = 2 * 1024 * 1024
TOOLS = {"search_knowledge", "get_source", "get_evidence", "get_summary", "get_tags"}


class InstallError(Exception):
    pass


def _safe_path(path: Path, create: bool = False) -> None:
    try:
        get_platform().files.safe_path(path, create=create)
    except UnsafeFile:
        raise InstallError("配置文件或目录不安全，请使用高级设置手动接入") from None


def _read(path: Path) -> bytes | None:
    _safe_path(path)
    try:
        return get_platform().files.read(path, maximum=MAX_FILE, private=False, parents=True)
    except UnsafeFile:
        raise InstallError("配置文件不安全或过大") from None


def _atomic(path: Path, value: bytes) -> None:
    _safe_path(path, create=True)
    try:
        get_platform().files.atomic_write(path, value)
    except UnsafeFile:
        raise InstallError("配置文件或目录不安全，原文件未修改") from None


@contextmanager
def _lock(path: Path):
    _safe_path(path, create=True)
    lock_path = path.with_name(path.name + ".pka.lock")
    _safe_path(lock_path)
    with ExitStack() as stack:
        try:
            stack.enter_context(get_platform().files.lock(lock_path, exclusive=True))
        except BlockingIOError:
            raise InstallError("接入操作正在进行，请稍后重试") from None
        except UnsafeFile:
            raise InstallError("配置锁文件不安全") from None
        yield


def _parse(value: bytes | None, client: str) -> dict:
    try:
        text = (value or b"").decode("utf-8")
        result = tomli.loads(text) if client == "codex" else json.loads(text or "{}")
        if not isinstance(result, dict) or not isinstance(result.get("mcp_servers" if client == "codex" else "mcpServers", {}), dict):
            raise ValueError()
        return result
    except (ValueError, UnicodeError):
        raise InstallError("配置格式无法解析，原文件未修改") from None


def _without_entry(document: dict, key: str, name: str) -> dict:
    result = dict(document)
    servers = dict(result.get(key, {}))
    servers.pop(name, None)
    if servers:
        result[key] = servers
    else:
        result.pop(key, None)
    return result


def _edit(original: bytes | None, client: str, name: str, configuration: str, remove: bool) -> tuple[bytes, dict, bool]:
    key = "mcp_servers" if client == "codex" else "mcpServers"
    requested = _parse(configuration.encode(), client)
    if set(requested) != {key} or set(requested[key]) != {name}:
        raise InstallError("连接配置与当前连接不匹配")
    entry = requested[key][name]
    if not isinstance(entry, dict) or not isinstance(entry.get("command"), str) or not isinstance(entry.get("args"), list) or not isinstance(entry.get("env"), dict):
        raise InstallError("连接配置不完整")
    old = _parse(original, client)
    existing = old.get(key, {}).get(name)
    installed = existing == entry
    if remove and existing is not None and not installed:
        raise InstallError("当前配置与本连接不同，未移除；请使用高级设置检查")
    if client == "codex":
        begin = f"# BEGIN PKA MANAGED {name}\n"
        end = f"# END PKA MANAGED {name}\n"
        text = (original or b"").decode()
        if text.count(begin) != text.count(end) or text.count(begin) > 1:
            raise InstallError("受管配置边界异常，原文件未修改")
        if begin in text:
            start, finish = text.index(begin), text.index(end) + len(end)
            if finish <= start:
                raise InstallError("受管配置边界异常，原文件未修改")
            remainder = text[:start] + text[finish:]
            if _parse(remainder.encode(), client) != _without_entry(old, key, name):
                raise InstallError("无法安全修改受管配置，请使用高级设置")
        else:
            if existing is not None:
                raise InstallError("已有同名配置，请使用高级设置处理")
            remainder = text
        updated = remainder if remove else remainder.rstrip("\n") + "\n\n" + begin + configuration.rstrip("\n") + "\n" + end
        value = updated.encode()
    else:
        if existing is not None and not installed:
            raise InstallError("已有同名配置与本连接不同，原文件未修改")
        updated = dict(old)
        servers = dict(old.get(key, {}))
        if remove:
            servers.pop(name, None)
        else:
            servers[name] = entry
        if servers or key in old:
            updated[key] = servers
        value = (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode()
    new = _parse(value, client)
    if _without_entry(new, key, name) != _without_entry(old, key, name):
        raise InstallError("无法保证保留其他配置，原文件未修改")
    if not remove and new.get(key, {}).get(name) != entry:
        raise InstallError("连接配置无法安全合并")
    return value, entry, installed


async def _check(entry: dict) -> None:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    parameters = StdioServerParameters(command=entry["command"], args=entry["args"], env=entry["env"])
    async def inspect():
        # Child diagnostics never escape to the UI or reveal private configuration.
        with open(os.devnull, "w") as errors:
            async with stdio_client(parameters, errlog=errors) as (reader, writer):
                async with ClientSession(reader, writer) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    if not TOOLS.issubset({tool.name for tool in tools.tools}):
                        raise InstallError("知识库工具不完整")
                    result = await session.call_tool("search_knowledge", {"query": "知识库连接检查", "limit": 1, "mode": "exact"})
                    if result.isError:
                        raise InstallError("知识库读取检查失败")

    await asyncio.wait_for(inspect(), timeout=25)



def _owned_removal_configuration(original: bytes | None, client: str, name: str, request: dict) -> str | None:
    key = "mcp_servers" if client == "codex" else "mcpServers"
    entry = _parse(original, client).get(key, {}).get(name)
    if entry is None:
        return None
    runtime = Path(request["runtime_path"])
    endpoint = Path(request["endpoint_path"])
    if not runtime.is_absolute() or not endpoint.is_absolute():
        raise InstallError("运行环境和知识库路径必须是绝对路径")
    if (not isinstance(entry, dict) or (set(entry) != {"command", "args", "env"}
                and not (client == "claudeCode" and set(entry) == {"command", "args", "env", "type"} and entry["type"] == "stdio"))
            or entry["command"] != str(get_platform().runtime_layout(runtime).python)
            or entry["args"] != ["-I", "-B", str(get_platform().runtime_layout(runtime).backend / "app/local_mcp.py")]
            or not isinstance(entry["env"], dict)
            or set(entry["env"]) != {"PKA_API_FILE", "PKA_AGENT_TOKEN"}
            or entry["env"]["PKA_API_FILE"] != str(endpoint)
            or not isinstance(entry["env"]["PKA_AGENT_TOKEN"], str)):
        raise InstallError("该配置已修改或不属于当前知识库，未移除；请手动检查")
    if client == "codex":
        begin = f"# BEGIN PKA MANAGED {name}\n"
        end = f"# END PKA MANAGED {name}\n"
        text = (original or b"").decode()
        if text.count(begin) != 1 or text.count(end) != 1:
            raise InstallError("配置不是客户端受管配置，未移除；请手动检查")
        start, finish = text.index(begin) + len(begin), text.index(end)
        if finish <= start:
            raise InstallError("受管配置边界异常，未移除")
        return text[start:finish]
    return json.dumps({key: {name: entry}})

def execute(request: dict) -> dict:
    action, client = request.get("action"), request.get("client")
    if isinstance(action, str) and action.startswith("plugin_"):
        if __package__:
            from .agent_plugin import execute as execute_plugin
        else:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            sys.modules.setdefault("agent_install", sys.modules[__name__])
            from agent_plugin import execute as execute_plugin
        return execute_plugin(request)
    if action in {"reference_enable", "reference_disable", "reference_status"}:
        if __package__:
            from .agent_reference import execute_reference
        else:
            # The -I launcher trusts only its own bundled source directory.
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            sys.modules.setdefault("agent_install", sys.modules[__name__])
            from agent_reference import execute_reference
        return execute_reference(request)
    if action not in {"install", "status", "remove", "remove_owned", "migration_check", "check"} or client not in {"codex", "claudeCode"}:
        raise InstallError("不支持的接入操作")
    try:
        identifier = str(uuid.UUID(request["connection_id"]))
    except (KeyError, ValueError, TypeError, AttributeError):
        raise InstallError("连接标识无效") from None
    name = "knowledge_" + identifier.replace("-", "_")
    configuration = request.get("configuration")
    if action not in {"remove_owned", "migration_check"} and not isinstance(configuration, str):
        raise InstallError("缺少连接配置")
    default = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "config.toml" if client == "codex" else Path(os.environ.get("CLAUDE_CONFIG_DIR", str(Path.home()))) / ".claude.json"
    path = Path(request.get("config_path", str(default)))
    if action == "migration_check":
        original = _read(path)
        legacy = _owned_removal_configuration(original, client, name, request)
        if legacy is not None:
            _edit(original, client, name, legacy, True)
        return {"installed": legacy is not None, "message": "旧连接可安全迁移"}
    if action == "status":
        _, _, installed = _edit(_read(path), client, name, configuration, False)
        return {"installed": installed, "checked": False, "message": "已写入本连接配置，尚未检查服务" if installed else "尚未接入"}
    with _lock(path):
        original = _read(path)
        if action == "remove_owned":
            configuration = _owned_removal_configuration(original, client, name, request)
            if configuration is None:
                return {"installed": False, "checked": False, "message": "此连接配置已经不存在"}
            action = "remove"
        updated, entry, installed = _edit(original, client, name, configuration, action == "remove")
        if action == "check":
            if not installed:
                raise InstallError("当前配置未安装本连接")
            asyncio.run(_check(entry))
            if _read(path) != original:
                raise InstallError("检查期间配置被其他程序修改，请刷新后重新检查")
            return {"installed": True, "checked": True, "message": "本地 MCP 工具发现和知识检索通过；必要时请重启 Agent 加载配置"}
        if action == "remove" and not installed and _parse(original, client).get("mcp_servers" if client == "codex" else "mcpServers", {}).get(name) is None:
            return {"installed": False, "checked": False, "message": "连接配置已移除"}
        if _read(path) != original:
            raise InstallError("配置已被其他程序修改，请重试")
        backup = None
        if original is not None and original != updated:
            backup = path.with_name(path.name + ".pka-backup-" + uuid.uuid4().hex)
            _atomic(backup, original)
        if _read(path) != original:
            raise InstallError("配置已被其他程序修改，请重试")
        _atomic(path, updated)
        if action == "install":
            try:
                asyncio.run(_check(entry))
            except Exception:
                if _read(path) == updated:
                    if original is None:
                        path.unlink()
                    else:
                        _atomic(path, original)
                    raise InstallError("连接检查失败，已恢复原配置；请保持知识库客户端运行后重试") from None
                raise InstallError("连接检查失败；配置随后被其他程序修改，未覆盖其更改") from None
        if action == "install" and _read(path) != updated:
            raise InstallError("检查期间配置被其他程序修改，连接状态未确认；请刷新后检查")
        result = {"installed": action == "install", "checked": action == "install", "message": "已接入，本地 MCP 工具发现和知识检索通过；必要时请重启 Agent" if action == "install" else "连接配置已移除"}
        if backup:
            result["backup_path"] = str(backup)
        return result


def main() -> int:
    try:
        value = sys.stdin.buffer.read(65537)
        if len(value) > 65536:
            raise InstallError("接入请求过大")
        request = json.loads(value)
        if not isinstance(request, dict):
            raise InstallError("接入请求无效")
        result = execute(request)
    except Exception as error:
        message = str(error) if isinstance(error, (InstallError, UnsupportedPlatform)) else "接入操作未确认，请刷新连接状态后检查"
        print(json.dumps({"message": message}, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

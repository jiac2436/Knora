"""Private, connection-scoped Codex plugin packages; never edit AGENTS.md."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import uuid

from app.platform_support import get_platform

try:
    from .agent_install import InstallError, _atomic, _check, _lock, _parse, _read
except ImportError:
    from agent_install import InstallError, _atomic, _check, _lock, _parse, _read


def _apply(changes, originals, backup_root):
    """Backups stay outside the distributable package, including credential backups."""
    applied, backups = [], []
    try:
        for path in changes:
            if _read(path) != originals[path]:
                raise InstallError("文件被其他程序修改，未覆盖")
        for path, value in changes.items():
            old = originals[path]
            if old == value:
                continue
            if old is not None:
                backup = backup_root / (uuid.uuid4().hex + ".backup")
                _atomic(backup, old)
                backups.append(str(backup))
            if _read(path) != old:
                raise InstallError("文件被其他程序修改，未覆盖")
            if value is None:
                path.unlink()
            else:
                _atomic(path, value)
            applied.append(path)
        if any(_read(path) != value for path, value in changes.items()):
            raise InstallError("文件状态无法确认")
    except Exception:
        for path in reversed(applied):
            if _read(path) == changes[path]:
                if originals[path] is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic(path, originals[path])
        raise
    return backups


def _json(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()


def _hash(value):
    return hashlib.sha256(value).hexdigest()


def _paths(request):
    if request.get("client") != "codex":
        raise InstallError("目前仅支持 Codex 本地插件")
    try:
        identifier = str(uuid.UUID(request["connection_id"]))
        paths = {key: Path(request[key]) for key in
                 ("plugin_root", "marketplace_path", "config_path", "runtime_path", "endpoint_path")}
    except (KeyError, ValueError, TypeError, AttributeError):
        raise InstallError("插件接入参数无效") from None
    if any(not path.is_absolute() for path in paths.values()):
        raise InstallError("插件接入路径必须是绝对路径")
    marketplace = paths["marketplace_path"]
    if marketplace.parts[-3:] != (".agents", "plugins", "marketplace.json"):
        raise InstallError("请选择受支持的本地插件市场路径")
    home = marketplace.parents[2]
    root = paths["plugin_root"]
    try:
        relative = root.relative_to(home)
    except ValueError:
        raise InstallError("插件目录必须位于本地插件市场根目录内") from None
    if ".." in relative.parts or root == home or root == marketplace.parent:
        raise InstallError("插件目录不安全")
    _read(root / ".pka-path-check")
    name = "pka-knowledge-" + identifier
    directory = root / name
    private = root.parent / (root.name + "-credentials")
    paths.update(identifier=identifier, name=name, directory=directory,
                 credential=private / (identifier + ".json"), launcher=private / (identifier + ".py"),
                 manifest=private / (identifier + ".managed.json"), home=home)
    return paths


def _catalog(raw):
    try:
        value = json.loads(raw) if raw is not None else {"name": "pka-personal", "interface": {"displayName": "个人知识库"}, "plugins": []}
        if (not isinstance(value, dict) or not isinstance(value.get("name"), str)
                or not value["name"] or not isinstance(value.get("plugins"), list)
                or any(not isinstance(item, dict) or not isinstance(item.get("name"), str) for item in value["plugins"])):
            raise ValueError()
        names = [item["name"] for item in value["plugins"]]
        if len(names) != len(set(names)):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise InstallError("插件市场文件格式无效，未修改") from None


def _entry(paths):
    relative = paths["directory"].relative_to(paths["home"]).as_posix()
    return {"name": paths["name"], "source": {"source": "local", "path": "./" + relative},
            "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Productivity"}


def _block(paths, catalog, enabled):
    key = paths["name"] + "@" + catalog["name"]
    return (f'# BEGIN PKA PLUGIN {paths["identifier"]}\n'
            f'[plugins.{json.dumps(key)}]\nenabled = {str(enabled).lower()}\n'
            f'# END PKA PLUGIN {paths["identifier"]}\n').encode()


def _config_edit(raw, old_block, new_block, paths, catalog):
    text = raw or b""
    old = _parse(text, "codex")
    key = paths["name"] + "@" + catalog["name"]
    if old_block:
        if text.count(old_block) != 1:
            raise InstallError("插件启用配置已修改，未覆盖")
        remainder = text.replace(old_block, b"", 1)
    else:
        if key in old.get("plugins", {}) or ("PKA PLUGIN " + paths["identifier"]).encode() in text:
            raise InstallError("已有同名插件配置，未覆盖")
        remainder = text
    value = remainder if new_block is None else remainder.rstrip(b"\n") + b"\n\n" + new_block
    new = _parse(value, "codex")
    def without(document):
        result = dict(document)
        plugins = dict(result.get("plugins", {}))
        plugins.pop(key, None)
        if plugins:
            result["plugins"] = plugins
        else:
            result.pop("plugins", None)
        return result
    if without(old) != without(new):
        raise InstallError("无法保证保留其他 Codex 设置")
    return value


def _state(paths, catalog, config, *, allow_runtime_update=False, allow_detached_config=False):
    _read(paths["directory"] / ".pka-path-check")
    raw = _read(paths["manifest"])
    entry = next((item for item in catalog["plugins"] if item["name"] == paths["name"]), None)
    if raw is None:
        if ("PKA PLUGIN " + paths["identifier"]).encode() in config or entry is not None or any(path.is_file() or path.is_symlink() for path in paths["directory"].rglob("*")) or _read(paths["credential"]) is not None or _read(paths["launcher"]) is not None:
            raise InstallError("存在未受管或不完整的插件文件，请手动检查")
        return None
    try:
        manifest = json.loads(raw)
        if (manifest["schema_version"] != 1 or manifest["name"] != paths["name"]
                or manifest["marketplace_path"] != str(paths["marketplace_path"])
                or manifest["config_path"] != str(paths["config_path"])
                or (not allow_runtime_update and manifest["runtime_path"] != str(paths["runtime_path"]))
                or manifest["endpoint_path"] != str(paths["endpoint_path"])
                or manifest["entry"] != entry or entry != _entry(paths)
                or manifest["marketplace_name"] != catalog["name"]
                or not isinstance(manifest["enabled"], bool)):
            raise ValueError()
        expected = {str(paths["directory"] / item) for item in
                    _LEGACY_PACKAGE_FILES}
        metadata = str(paths["directory"] / "skills/knowledge/agents/openai.yaml")
        if metadata in manifest["files"]:
            expected.add(metadata)
        expected.update((str(paths["credential"]), str(paths["launcher"])))
        contents = list(paths["directory"].rglob("*"))
        if any(path.is_symlink() for path in contents):
            raise ValueError()
        actual = {str(path) for path in contents if path.is_file()}
        package_expected = {path for path in expected if path.startswith(str(paths["directory"]) + "/")}
        if actual != package_expected:
            raise ValueError()
        if set(manifest["files"]) != expected:
            raise ValueError()
        if any(_hash(get_platform().files.read(Path(path), maximum=2 * 1024 * 1024, parents=True, permissions=0o600) or b"")
               != digest for path, digest in manifest["files"].items()):
            raise ValueError()
        plugin_key = paths["name"] + "@" + catalog["name"]
        setting = _parse(config, "codex").get("plugins", {}).get(plugin_key)
        if (allow_detached_config and setting is None and
                ("# BEGIN PKA PLUGIN " + paths["identifier"]).encode() not in config):
            enabled = False
        else:
            if not isinstance(setting, dict) or set(setting) != {"enabled"} or not isinstance(setting["enabled"], bool):
                raise ValueError()
            enabled = setting["enabled"]
            if config.count(_block(paths, catalog, enabled)) != 1:
                raise ValueError()
        manifest = dict(manifest, enabled=enabled)
    except (ValueError, KeyError, TypeError, AttributeError, OSError):
        raise InstallError("插件或配置已被修改，未覆盖；请手动检查") from None
    return manifest


def _saved_configuration(paths, manifest):
    """Relocate a verified private binding without displaying/retrieving a token."""
    if manifest is None:
        raise InstallError("缺少已验证的插件凭证，请从客户端创建接入")
    value = json.loads(_read(paths["credential"]))
    old = get_platform().runtime_layout(Path(manifest["runtime_path"]))
    if (set(value) != {"command", "args", "env"} or value["command"] != str(old.python)
            or value["args"] != ["-I", "-B", str(old.backend / "app/local_mcp.py")]
            or set(value["env"]) != {"PKA_API_FILE", "PKA_AGENT_TOKEN"}
            or value["env"]["PKA_API_FILE"] != str(paths["endpoint_path"])
            or not isinstance(value["env"]["PKA_AGENT_TOKEN"], str) or not value["env"]["PKA_AGENT_TOKEN"]):
        raise InstallError("已保存的插件凭证不属于当前知识库，未修改")
    layout = get_platform().runtime_layout(paths["runtime_path"])
    quote = lambda item: json.dumps(item, ensure_ascii=False)
    return (f'[mcp_servers.knowledge_{paths["identifier"].replace("-", "_")}]\n'
            f'command = {quote(str(layout.python))}\n'
            f'args = {quote(["-I", "-B", str(layout.backend / "app/local_mcp.py")])}\n'
            'env = { ' + ', '.join(key + ' = ' + quote(item) for key, item in value['env'].items()) + ' }\n')


def _consolidate(paths, catalog, config):
    """Retire only verified same-library catalog entries; keep files recoverable."""
    retired, skipped = [], []
    for entry in catalog["plugins"]:
        if entry["name"] == paths["name"] or not entry["name"].startswith("pka-knowledge-"):
            continue
        try:
            other = _paths(dict(client="codex", connection_id=entry["name"].removeprefix("pka-knowledge-"),
                **{key: str(paths[key]) for key in
                   ("plugin_root", "marketplace_path", "config_path", "runtime_path", "endpoint_path")}))
            # Another library is outside this operation's scope.
            metadata = _read(other["manifest"])
            if metadata is None or json.loads(metadata).get("endpoint_path") != str(paths["endpoint_path"]):
                continue
            manifest = _state(other, catalog, config, allow_runtime_update=True, allow_detached_config=True)
            if manifest is None:
                continue
            if manifest["enabled"]:
                config = _config_edit(config, _block(other, catalog, True), _block(other, catalog, False), other, catalog)
            retired.append(entry["name"])
        except (InstallError, ValueError, KeyError, TypeError, OSError):
            skipped.append(entry["name"])
    return dict(catalog, plugins=[entry for entry in catalog["plugins"] if entry["name"] not in retired]), config, retired, skipped


def _files(paths, request):
    server = "knowledge_" + paths["identifier"].replace("-", "_")
    document = _parse(request.get("configuration", "").encode(), "codex")
    if set(document) != {"mcp_servers"} or set(document["mcp_servers"]) != {server}:
        raise InstallError("插件连接配置不匹配")
    entry = document["mcp_servers"][server]
    runtime = paths["runtime_path"]
    layout = get_platform().runtime_layout(runtime)
    if (not isinstance(entry, dict) or set(entry) != {"command", "args", "env"}
            or not isinstance(entry.get("args"), list) or not isinstance(entry.get("env"), dict)
            or entry.get("command") != str(layout.python)
            or entry.get("args") != ["-I", "-B", str(layout.backend / "app/local_mcp.py")]
            or set(entry.get("env", {})) != {"PKA_API_FILE", "PKA_AGENT_TOKEN"}
            or entry["env"]["PKA_API_FILE"] != str(paths["endpoint_path"])
            or not isinstance(entry["env"]["PKA_AGENT_TOKEN"], str) or not entry["env"]["PKA_AGENT_TOKEN"]):
        raise InstallError("插件连接配置不是当前知识库的受管连接")
    credential = _json(entry)
    # This private launcher is outside the package and carries no credential bytes.
    launcher = ("import sys\nfrom pathlib import Path\n"
                f"sys.path.insert(0, {str(layout.backend)!r})\n"
                "from app.agent_launcher import launch\n"
                f"launch(Path({str(runtime)!r}), Path({str(paths['credential'])!r}))\n").encode()
    template_path = Path(request["skill_template_path"])
    template = _read(template_path)
    if not template:
        raise InstallError("知识库 Skill 模板不可用")
    metadata_path = Path(request.get("skill_metadata_path") or template_path.parent / "agents/openai.yaml")
    metadata = _read(metadata_path)
    if not metadata:
        raise InstallError("知识库 Skill 调用策略模板不可用，请使用新版客户端")
    metadata += (f'\ndependencies:\n  tools:\n    - type: "mcp"\n      value: "{server}"\n      description: "当前授权连接的本地知识检索服务"\n').encode()
    skill = template.decode().replace("name: personal-knowledge\n", "name: knowledge\n", 1)
    skill += f"\n## 当前连接\n\n仅使用本插件的 `{server}` MCP 服务；服务不可用时明确说明，不使用其他连接代替。\n"
    interface = {"displayName": "个人知识库", "shortDescription": "检索本地资料、原文问答与知识归纳",
                 "developerName": "Knora", "category": "Productivity", "capabilities": ["Read"]}
    portable = {"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json",
                "name": paths["name"], "version": "1.0.5", "description": "检索授权资料，依据原文回答问题并归纳知识",
                "extensions": {"com.openai": {"interface": interface}}}
    compat = {"name": paths["name"], "version": "1.0.5", "description": portable["description"],
              "skills": "./skills/", "mcpServers": "./.mcp.json", "interface": interface}
    mcp = {"$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
           "mcpServers": {server: get_platform().plugin_command(runtime, paths["launcher"])}}
    files = {paths["directory"] / "plugin.json": _json(portable),
             paths["directory"] / ".codex-plugin/plugin.json": _json(compat),
             paths["directory"] / "mcp.json": _json(mcp), paths["directory"] / ".mcp.json": _json(mcp),
             paths["directory"] / "skills/knowledge/SKILL.md": skill.encode(),
             paths["directory"] / "skills/knowledge/agents/openai.yaml": metadata,
             paths["credential"]: credential, paths["launcher"]: launcher}
    return files, dict(mcp["mcpServers"][server], env={})


_LEGACY_PACKAGE_FILES = ("plugin.json", ".codex-plugin/plugin.json", "mcp.json", ".mcp.json", "skills/knowledge/SKILL.md")


def _host_cache(paths, catalog, manifest):
    """Read the official host cache only; never install through experimental RPC."""
    if manifest is None:
        return None
    market_name = catalog["name"]
    if Path(market_name).name != market_name or market_name in {".", ".."}:
        raise InstallError("插件市场名称不能用于安全的宿主缓存检查")
    cache = paths["config_path"].parent / "plugins/cache" / market_name / paths["name"]
    _read(cache / ".pka-cache-path-check")
    if not cache.exists():
        return None
    versions = []
    for directory in cache.iterdir():
        if directory.is_symlink():
            raise InstallError("Codex 插件缓存路径不安全，请在插件页重新安装")
        if directory.is_dir():
            versions.append(directory)
            if len(versions) > 30:
                raise InstallError("Codex 插件缓存版本过多，请在插件页重新安装")
    portable = json.loads(_read(paths["directory"] / "plugin.json"))
    allowed_versions = {portable["version"], "local"}
    for directory in versions:
        if directory.name not in allowed_versions:
            continue
        matches = True
        package_files = list(_LEGACY_PACKAGE_FILES)
        if str(paths["directory"] / "skills/knowledge/agents/openai.yaml") in manifest["files"]:
            package_files.append("skills/knowledge/agents/openai.yaml")
        for relative in package_files:
            value = _read(directory / relative)
            digest = manifest["files"][str(paths["directory"] / relative)]
            if value is None or _hash(value) != digest:
                matches = False
                break
        if matches:
            value = json.loads(_read(directory / "mcp.json"))
            server = "knowledge_" + paths["identifier"].replace("-", "_")
            return dict(value["mcpServers"][server], env={})
    return None


def _host_installed(paths, catalog, manifest):
    try:
        return _host_cache(paths, catalog, manifest) is not None
    except (InstallError, OSError, ValueError, KeyError, TypeError):
        return False


def execute(request):
    action = request.get("action")
    if action not in {"plugin_install", "plugin_status", "plugin_remove", "plugin_check", "plugin_host_check", "plugin_enable", "plugin_disable", "plugin_consolidate"}:
        raise InstallError("不支持的插件操作")
    paths = _paths(request)
    with ExitStack() as locks:
        if action != "plugin_status":
            for path in (paths["marketplace_path"], paths["config_path"], paths["manifest"]):
                locks.enter_context(_lock(path))
        market_raw = _read(paths["marketplace_path"])
        config = _read(paths["config_path"]) or b""
        catalog = _catalog(market_raw)
        try:
            # An explicit update may move to a newly packaged App. Continue to
            # verify every old managed file, connection and library endpoint;
            # the new runtime is checked before publishing the replacement.
            manifest = _state(paths, catalog, config, allow_runtime_update=True)
        except InstallError as error:
            if action != "plugin_status":
                raise
            return {"installed": False, "plugin_status": "modified", "reference_status": "modified",
                    "auto_reference_enabled": False, "host_installed": False, "plugin_path": str(paths["directory"]), "message": str(error)}
        installed = manifest is not None
        enabled = bool(manifest and manifest["enabled"])
        host_installed = _host_installed(paths, catalog, manifest)
        update_required = bool(manifest and manifest['runtime_path'] != str(paths['runtime_path']))
        result = {"host_installed": host_installed, "installed": installed, "plugin_status": "enabled" if enabled else "disabled" if installed else "not_installed",
                  "plugin_path": str(paths["directory"]), "plugin_name": paths["name"], "marketplace_name": catalog["name"],
                  "auto_reference_enabled": enabled, "reference_status": "enabled" if enabled else "disabled", "message": "插件已生成，请先在 Codex 插件页安装个人知识库插件" if installed and not host_installed else "插件已启用" if enabled else "插件已停用" if installed else "插件未安装"}
        result['update_required'] = update_required
        if update_required:
            result.update(plugin_status='needs_update', auto_reference_enabled=False,
                          message='App 位置已更新，请点击“创建/更新插件”修复接入路径；原凭证和授权范围保持不变')
        if action == "plugin_status":
            return result
        if update_required and action in {'plugin_enable', 'plugin_check', 'plugin_host_check'}:
            raise InstallError('App 位置已更新，请先点击“创建/更新插件”修复接入路径')
        if action == "plugin_consolidate":
            if not installed:
                raise InstallError("请先生成当前知识库插件")
            updated_catalog, updated_config, retired, skipped = _consolidate(paths, catalog, config)
            changes = {paths['marketplace_path']: _json(updated_catalog), paths['config_path']: updated_config}
            backups = _apply(changes, {path: _read(path) for path in changes}, paths['manifest'].parent / 'backups')
            return dict(result, retired_plugins=retired, retained_plugins=skipped, backups=backups,
                        message=f'已整理 {len(retired)} 个历史插件登记，保留当前插件；请刷新 Codex 插件页' +
                        (f'；另有 {len(skipped)} 个已修改条目保持原样' if skipped else ''))
        if action in {"plugin_host_check", "plugin_check"}:
            if not installed:
                raise InstallError("请先生成知识库插件")
            entry = _host_cache(paths, catalog, manifest)
            if entry is None:
                raise InstallError("请先在 Codex 插件页安装个人知识库插件，再完成接入")
            asyncio.run(_check(entry))
            latest_catalog = _catalog(_read(paths["marketplace_path"]))
            latest_config = _read(paths["config_path"]) or b""
            latest = _state(paths, latest_catalog, latest_config)
            if latest is None or latest["enabled"] != enabled or _block(paths, latest_catalog, latest["enabled"]) != _block(paths, catalog, enabled):
                raise InstallError("检查期间插件启用状态发生变化，请重新完成接入")
            if _host_cache(paths, latest_catalog, latest) != entry:
                raise InstallError("检查期间 Codex 插件缓存发生变化，请重新完成接入")
            return dict(result, host_installed=True, checked=True, message="Codex 已安装插件，知识库工具和读取检查通过")
        retired, skipped = [], []
        if action in {"plugin_enable", "plugin_disable"}:
            if not installed:
                raise InstallError("请先安装知识库插件")
            enabled = action == "plugin_enable"
            updated = dict(manifest, enabled=enabled)
            changes = {paths["config_path"]: _config_edit(config, _block(paths, catalog, manifest["enabled"]), _block(paths, catalog, enabled), paths, catalog), paths["manifest"]: _json(updated)}
        elif action == "plugin_remove":
            if not installed:
                return dict(result, message="插件已经不存在")
            changes = {Path(path): None for path in manifest["files"]}
            changes[paths["manifest"]] = None
            updated_catalog = dict(catalog, plugins=[item for item in catalog["plugins"] if item["name"] != paths["name"]])
            changes[paths["marketplace_path"]] = _json(updated_catalog)
            changes[paths["config_path"]] = _config_edit(config, _block(paths, catalog, enabled), None, paths, catalog)
        else:
            if not request.get('configuration'):
                request = dict(request, configuration=_saved_configuration(paths, manifest))
            files, check_entry = _files(paths, request)
            changes = dict(files)
            consolidated, updated_config, retired, skipped = _consolidate(paths, catalog, config)
            updated_catalog = dict(consolidated, plugins=[item for item in consolidated["plugins"] if item["name"] != paths["name"]] + [_entry(paths)])
            changes[paths["marketplace_path"]] = _json(updated_catalog)
            changes[paths["config_path"]] = _config_edit(updated_config, _block(paths, catalog, manifest["enabled"]) if manifest else None, _block(paths, catalog, True), paths, catalog)
            changes[paths["manifest"]] = _json({"schema_version": 1, "name": paths["name"], "marketplace_path": str(paths["marketplace_path"]),
                "config_path": str(paths["config_path"]), "runtime_path": str(paths["runtime_path"]), "endpoint_path": str(paths["endpoint_path"]), "marketplace_name": catalog["name"], "entry": _entry(paths), "enabled": True,
                "files": {str(path): _hash(value) for path, value in files.items()}})
            enabled = True
        originals = {path: _read(path) for path in changes}
        backup_root = paths["manifest"].parent / "backups"
        if action == "plugin_install":
            private = {path: changes[path] for path in (paths["credential"], paths["launcher"])}
            private_originals = {path: originals[path] for path in private}
            _apply(private, private_originals, backup_root)
            try:
                asyncio.run(_check(check_entry))
                after_private = dict(originals)
                after_private.update(private)
                backups = _apply(changes, after_private, backup_root)
            except Exception:
                _apply(private_originals, private, backup_root)
                raise InstallError("插件知识库检查或发布失败，已回退本次修改") from None
        else:
            backups = _apply(changes, originals, backup_root)
        return {"host_installed": action != "plugin_remove" and _host_installed(paths, catalog, _state(paths, _catalog(_read(paths["marketplace_path"])), _read(paths["config_path"]) or b"", allow_runtime_update=True)), "installed": action != "plugin_remove", "plugin_status": "not_installed" if action == "plugin_remove" else "enabled" if enabled else "disabled",
                "retired_plugins": retired, "retained_plugins": skipped,
                "auto_reference_enabled": action != "plugin_remove" and enabled,
                "reference_status": "enabled" if action != "plugin_remove" and enabled else "disabled",
                "plugin_path": str(paths["directory"]), "plugin_name": paths["name"], "marketplace_name": catalog["name"], "backups": backups, "checked": action == "plugin_install",
                "message": ("本地接入已移除，请在 Codex 插件页卸载" if action == "plugin_remove" else "插件已生成，请先在 Codex 插件页安装个人知识库插件，再完成接入" if action == "plugin_install" else "插件已启用，请在 Codex 中重新加载插件" if enabled else "插件已停用，请在 Codex 中重新加载插件") +
                    (f'；已整理 {len(retired)} 个历史插件登记' if retired else '') +
                    (f'；另有 {len(skipped)} 个已修改条目保持原样' if skipped else '')}

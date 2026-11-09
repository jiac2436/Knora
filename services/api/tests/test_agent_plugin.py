"""Plugin bundles exclude credentials and preserve unrelated host configuration."""
import json
from pathlib import Path
import stat
import shutil
import subprocess
import sys
import uuid

import pytest

from app import agent_install, agent_plugin as plugin


def test_isolated_plugin_status_keeps_private_configuration_unchanged(setup):
    plugin.execute(setup)
    config = Path(setup['config_path'])
    before = config.read_bytes()
    value = dict(setup, action='plugin_status')
    result = subprocess.run([sys.executable, '-I', '-B', str(Path(agent_install.__file__).resolve())],
        input=json.dumps(value).encode(), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert json.loads(result.stdout)['installed'] is True
    assert config.read_bytes() == before


def test_generated_launcher_executes_only_bound_mcp_with_clean_environment(setup):
    plugin.execute(setup)
    paths = plugin._paths(setup)
    runtime = Path(setup['runtime_path'])
    backend = runtime / 'backend/app'
    backend.mkdir(parents=True)
    source = Path(agent_install.__file__).parent
    (backend / '__init__.py').write_text('')
    shutil.copyfile(source / 'agent_launcher.py', backend / 'agent_launcher.py')
    shutil.copytree(source / 'platform_support', backend / 'platform_support', ignore=shutil.ignore_patterns('__pycache__'))
    binary = runtime / 'python/bin/python3'
    binary.parent.mkdir(parents=True)
    binary.write_text('#!/bin/sh\nif [ -n "$UNRELATED_SECRET" ]; then exit 42; fi\n'
                      'if [ "$PKA_AGENT_TOKEN" != "synthetic-secret" ]; then exit 43; fi\n'
                      'printf "%s\\n" "$@"\n')
    binary.chmod(0o700)
    result = subprocess.run([sys.executable, '-I', '-B', str(paths['launcher'])],
        env={'UNRELATED_SECRET': 'synthetic-unrelated', 'PATH': '/usr/bin:/bin'},
        capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode()
    assert result.stdout.decode().splitlines() == ['-I', '-B', str(backend / 'local_mcp.py')]


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path.resolve()
    identifier = str(uuid.uuid4())
    runtime = home / "runtime"
    endpoint = home / "library/agent-api.json"
    config = home / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text('model = "existing"\n[plugins."other@elsewhere"]\nenabled = false\n')
    marketplace = home / ".agents/plugins/marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text(json.dumps({"name": "existing-personal", "interface": {"displayName": "用户市场"},
                                       "plugins": [{"name": "other", "source": {"source": "local", "path": "./other"}, "custom": True}]}))
    template = home / "SKILL.md"
    template.write_text("---\nname: personal-knowledge\ndescription: 检索已有资料\n---\n检索后读取原文。\n")
    metadata = home / "agents/openai.yaml"
    metadata.parent.mkdir()
    metadata.write_text((Path(__file__).resolve().parents[3] / "skills/personal-knowledge/agents/openai.yaml").read_text())
    server = "knowledge_" + identifier.replace("-", "_")
    configuration = (f'[mcp_servers.{server}]\ncommand={json.dumps(str(runtime / "python/bin/python3"))}\n'
                     f'args={json.dumps(["-I", "-B", str(runtime / "backend/app/local_mcp.py")])}\n'
                     f'[mcp_servers.{server}.env]\nPKA_API_FILE={json.dumps(str(endpoint))}\nPKA_AGENT_TOKEN="synthetic-secret"\n')
    request = {"client": "codex", "action": "plugin_install", "connection_id": identifier, "runtime_path": str(runtime),
               "endpoint_path": str(endpoint), "plugin_root": str(home / ".codex/pka-knowledge-plugins"),
               "marketplace_path": str(marketplace), "config_path": str(config), "configuration": configuration,
               "skill_template_path": str(template)}
    async def check(entry):
        assert "synthetic-secret" not in json.dumps(entry)
        launcher = Path(entry["args"][-1])
        assert launcher.is_file()
    monkeypatch.setattr(plugin, "_check", check)
    return request


def test_install_repeat_disable_remove_without_credentials_preserves_other_settings(setup):
    config = Path(setup["config_path"])
    original = config.read_bytes()
    result = plugin.execute(setup)
    directory = Path(result["plugin_path"])
    metadata = json.loads((directory / "plugin.json").read_text())
    assert metadata["extensions"]["com.openai"]["interface"]["developerName"] == "Knora"
    assert metadata["version"] == "1.0.5"
    assert result["auto_reference_enabled"]
    assert not any("synthetic-secret" in file.read_text() or "PKA_AGENT_TOKEN" in file.read_text()
                   for file in directory.rglob("*") if file.is_file())
    private = plugin._paths(setup)["credential"]
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert private.parent != directory
    assert json.loads(Path(setup["marketplace_path"]).read_text())["plugins"][0]["custom"] is True
    assert "./.codex/pka-knowledge-plugins/" in Path(setup["marketplace_path"]).read_text()
    first = config.read_bytes()
    plugin.execute(setup)
    assert config.read_bytes() == first
    assert len(json.loads(Path(setup["marketplace_path"]).read_text())["plugins"]) == 2
    for action, expected in [("plugin_disable", False), ("plugin_enable", True)]:
        assert plugin.execute(dict(setup, action=action))["auto_reference_enabled"] is expected
    bare = {key: value for key, value in setup.items() if key != "configuration"}
    _copy_host_cache(setup)
    assert plugin.execute(dict(bare, action="plugin_check"))["checked"]
    assert not plugin.execute(dict(bare, action="plugin_remove"))["installed"]
    assert config.read_bytes().startswith(original)
    assert not private.exists()
    assert not any(file.is_file() for file in directory.rglob("*"))
    assert plugin.execute(dict(bare, action="plugin_status"))["plugin_status"] == "not_installed"
    assert not plugin.execute(dict(bare, action="plugin_remove"))["installed"]
    assert not (config.parent / "AGENTS.md").exists()


@pytest.mark.parametrize("target", ["skill", "config", "catalog", "credential", "extra"])
def test_modified_managed_state_is_rejected(setup, target):
    result = plugin.execute(setup)
    paths = plugin._paths(setup)
    file = {"skill": Path(result["plugin_path"]) / "skills/knowledge/SKILL.md",
            "config": Path(setup["config_path"]), "catalog": Path(setup["marketplace_path"]),
            "credential": paths["credential"], "extra": paths["directory"] / "user-owned.txt"}[target]
    if target == "catalog":
        value = json.loads(file.read_text())
        value["plugins"][-1]["source"]["path"] = "./foreign"
        file.write_text(json.dumps(value))
    elif target == "config":
        file.write_bytes(file.read_bytes().replace(b"enabled = true", b"enabled = true\nextra = true"))
    else:
        file.write_bytes((file.read_bytes() if file.exists() else b"") + b"user edit")
    before = file.read_bytes()
    assert plugin.execute(dict(setup, action="plugin_status"))["reference_status"] == "modified"
    for action in ("plugin_install", "plugin_remove", "plugin_disable"):
        with pytest.raises(agent_install.InstallError):
            plugin.execute(dict(setup, action=action))
        assert file.read_bytes() == before


def test_check_failure_never_publishes_and_cleans_private_credentials(setup, monkeypatch):
    async def fail(entry):
        raise RuntimeError("not reachable")
    monkeypatch.setattr(plugin, "_check", fail)
    before = Path(setup["config_path"]).read_bytes()
    market = Path(setup["marketplace_path"]).read_bytes()
    with pytest.raises(agent_install.InstallError, match="回退"):
        plugin.execute(setup)
    assert Path(setup["config_path"]).read_bytes() == before
    assert Path(setup["marketplace_path"]).read_bytes() == market
    assert not plugin._paths(setup)["credential"].exists()
    assert not plugin._paths(setup)["directory"].exists()


def test_foreign_same_name_and_symlink_are_rejected(setup):
    paths = plugin._paths(setup)
    paths["directory"].parent.mkdir(parents=True)
    paths["directory"].symlink_to(Path(setup["runtime_path"]).parent)
    with pytest.raises(agent_install.InstallError):
        plugin.execute(setup)


def test_plugin_root_cannot_escape_marketplace_root(setup):
    with pytest.raises(agent_install.InstallError, match="根目录"):
        plugin.execute(dict(setup, plugin_root="/outside/plugin"))


def test_unmanaged_config_entry_is_not_adopted_or_private_written(setup):
    paths = plugin._paths(setup)
    config = Path(setup["config_path"])
    config.write_text(config.read_text() + f'\n[plugins."{paths["name"]}@existing-personal"]\nenabled=true\n')
    with pytest.raises(agent_install.InstallError):
        plugin.execute(setup)
    assert not paths["credential"].exists()


def test_unrelated_config_and_catalog_updates_survive_removal(setup):
    plugin.execute(setup)
    config = Path(setup["config_path"])
    config.write_text('# new user note\n' + config.read_text())
    market = Path(setup["marketplace_path"])
    document = json.loads(market.read_text())
    document["plugins"].append({"name": "new-other", "source": {"source": "local", "path": "./new"}})
    market.write_text(json.dumps(document))
    plugin.execute(dict(setup, action="plugin_remove"))
    assert config.read_text().startswith('# new user note\n')
    assert [item["name"] for item in json.loads(market.read_text())["plugins"]] == ["other", "new-other"]


@pytest.mark.parametrize("target", ["credential", "skill"])
def test_insecure_file_permissions_are_not_accepted(setup, target):
    plugin.execute(setup)
    paths = plugin._paths(setup)
    file = paths["credential"] if target == "credential" else paths["directory"] / "skills/knowledge/SKILL.md"
    file.chmod(0o644)
    assert plugin.execute(dict(setup, action="plugin_status"))["plugin_status"] == "modified"
    with pytest.raises(agent_install.InstallError):
        plugin.execute(dict(setup, action="plugin_remove"))


def test_other_library_cannot_remove_plugin(setup):
    plugin.execute(setup)
    with pytest.raises(agent_install.InstallError):
        plugin.execute(dict(setup, action="plugin_remove", endpoint_path=str(Path(setup["endpoint_path"]).parent / "other.json")))


def test_empty_symlink_directory_inside_package_is_rejected(setup, tmp_path):
    result = plugin.execute(setup)
    (Path(result["plugin_path"]) / "empty-link").symlink_to(tmp_path)
    with pytest.raises(agent_install.InstallError):
        plugin.execute(dict(setup, action="plugin_remove"))


def test_publish_failure_restores_existing_private_bridge(setup, monkeypatch):
    plugin.execute(setup)
    paths = plugin._paths(setup)
    original_credential = paths["credential"].read_bytes()
    original_config = paths["config_path"].read_bytes()
    original_manifest = paths["manifest"].read_bytes()
    changed = dict(setup, configuration=setup["configuration"].replace("synthetic-secret", "replacement-secret"))
    real_atomic = plugin._atomic
    def fail_manifest(path, value):
        if path == paths["manifest"] and value != original_manifest:
            raise OSError("fixture publication failure")
        real_atomic(path, value)
    monkeypatch.setattr(plugin, "_atomic", fail_manifest)
    with pytest.raises(agent_install.InstallError, match="回退"):
        plugin.execute(changed)
    assert paths["credential"].read_bytes() == original_credential
    assert paths["config_path"].read_bytes() == original_config
    assert paths["manifest"].read_bytes() == original_manifest
    assert plugin.execute(dict(setup, action="plugin_status"))["auto_reference_enabled"]


def test_codex_native_enabled_toggle_is_reflected_and_can_be_removed(setup):
    plugin.execute(setup)
    config = Path(setup["config_path"])
    config.write_bytes(config.read_bytes().replace(b"enabled = true", b"enabled = false"))
    assert not plugin.execute(dict(setup, action="plugin_status"))["auto_reference_enabled"]
    assert plugin.execute(dict(setup, action="plugin_status"))["plugin_status"] == "disabled"
    assert plugin.execute(dict(setup, action="plugin_enable"))["auto_reference_enabled"]
    config.write_bytes(config.read_bytes().replace(b"enabled = true", b"enabled = false"))
    assert not plugin.execute(dict(setup, action="plugin_remove"))["installed"]


def test_plugin_skill_namespace_fits_codex_limit(setup):
    result = plugin.execute(setup)
    skill = Path(result["plugin_path"]) / "skills/knowledge/SKILL.md"
    assert "name: knowledge\n" in skill.read_text()
    assert "name: personal-knowledge\n" not in skill.read_text()
    assert len(result["plugin_name"] + ":knowledge") <= 64


def _copy_host_cache(request):
    paths = plugin._paths(request)
    catalog = json.loads(paths["marketplace_path"].read_bytes())
    cache = paths["config_path"].parent / "plugins/cache" / catalog["name"] / paths["name"] / "local"
    shutil.copytree(paths["directory"], cache)
    return cache


def test_host_check_rejects_source_only_and_status_explains_install_step(setup):
    result = plugin.execute(setup)
    assert not result["host_installed"]
    status = plugin.execute(dict(setup, action="plugin_status"))
    assert status["installed"] and not status["host_installed"]
    assert "Codex 插件页安装" in status["message"]
    for action in ("plugin_host_check", "plugin_check"):
        with pytest.raises(agent_install.InstallError, match="先在 Codex 插件页安装"):
            plugin.execute(dict(setup, action=action))


def test_matching_host_cache_is_read_only_verified_and_checked(setup):
    plugin.execute(setup)
    cache = _copy_host_cache(setup)
    before = {str(path): path.read_bytes() for path in cache.rglob("*") if path.is_file()}
    assert plugin.execute(dict(setup, action="plugin_status"))["host_installed"]
    for action in ("plugin_host_check", "plugin_check"):
        result = plugin.execute(dict(setup, action=action))
        assert result["host_installed"] and result["checked"]
    assert before == {str(path): path.read_bytes() for path in cache.rglob("*") if path.is_file()}


def test_modified_host_cache_is_not_accepted(setup):
    plugin.execute(setup)
    cache = _copy_host_cache(setup)
    file = cache / "skills/knowledge/SKILL.md"
    file.write_bytes(file.read_bytes() + b"foreign modification")
    assert not plugin.execute(dict(setup, action="plugin_status"))["host_installed"]
    with pytest.raises(agent_install.InstallError, match="Codex 插件页安装"):
        plugin.execute(dict(setup, action="plugin_host_check"))


def test_host_cache_symlink_is_rejected(setup, tmp_path):
    plugin.execute(setup)
    cache = _copy_host_cache(setup)
    (cache.parent / "unexpected").symlink_to(tmp_path)
    assert not plugin.execute(dict(setup, action="plugin_status"))["host_installed"]
    with pytest.raises(agent_install.InstallError, match="路径不安全"):
        plugin.execute(dict(setup, action="plugin_host_check"))


def test_historical_matching_cache_does_not_confirm_current_install(setup):
    plugin.execute(setup)
    cache = _copy_host_cache(setup)
    cache.rename(cache.with_name("0.9.0"))
    assert not plugin.execute(dict(setup, action="plugin_status"))["host_installed"]
    with pytest.raises(agent_install.InstallError, match="Codex 插件页安装"):
        plugin.execute(dict(setup, action="plugin_host_check"))


def test_current_manifest_version_cache_is_accepted(setup):
    plugin.execute(setup)
    cache = _copy_host_cache(setup)
    cache.rename(cache.with_name("1.0.5"))
    assert plugin.execute(dict(setup, action="plugin_host_check"))["host_installed"]


def test_host_check_rejects_concurrent_native_disable(setup, monkeypatch):
    plugin.execute(setup)
    _copy_host_cache(setup)
    async def disable_during_check(entry):
        config = Path(setup["config_path"])
        config.write_bytes(config.read_bytes().replace(b"enabled = true", b"enabled = false"))
    monkeypatch.setattr(plugin, "_check", disable_during_check)
    with pytest.raises(agent_install.InstallError, match="启用状态发生变化"):
        plugin.execute(dict(setup, action="plugin_host_check"))
    assert not plugin.execute(dict(setup, action="plugin_status"))["auto_reference_enabled"]


def test_explicit_skill_policy_and_knowledge_product_description(setup):
    result = plugin.execute(setup)
    directory = Path(result['plugin_path'])
    metadata = (directory / 'skills/knowledge/agents/openai.yaml').read_text()
    assert 'allow_implicit_invocation: false' in metadata
    assert 'default_prompt' not in metadata
    assert 'value: "knowledge_' + setup['connection_id'].replace('-', '_') + '"' in metadata
    manifest = json.loads((directory / 'plugin.json').read_text())
    server = 'knowledge_' + setup['connection_id'].replace('-', '_')
    entry = json.loads((directory / 'mcp.json').read_text())['mcpServers'][server]
    assert entry['command'] == 'env'
    assert entry['args'][0] == str(Path(setup['runtime_path']) / 'python/bin/python3')
    assert entry['args'][1:3] == ['-I', '-B']
    source = Path(__file__).resolve().parents[3] / 'skills/personal-knowledge/SKILL.md'
    text = source.read_text()
    assert '文档、笔记和已保存资料' in text
    assert all(word not in text for word in ('姓名', '职业', '何时使用', '无需用户点名'))
    assert json.loads((directory / 'plugin.json').read_text())['version'] == '1.0.5'


def test_upgrade_replaces_id_citation_rules_and_preserves_host_cache(setup):
    template = Path(setup['skill_template_path'])
    template.write_text('---\nname: personal-knowledge\ndescription: 知识检索\n---\n引用写明证据 ID。\n')
    result = plugin.execute(setup)
    directory = Path(result['plugin_path'])
    paths = plugin._paths(setup)
    # An older, legitimately managed package must remain upgradeable.
    managed = json.loads(paths['manifest'].read_text())
    for relative in ('plugin.json', '.codex-plugin/plugin.json'):
        file = directory / relative
        value = json.loads(file.read_text()); value['version'] = '1.0.2'
        file.write_bytes(plugin._json(value))
        managed['files'][str(file)] = plugin._hash(file.read_bytes())
    paths['manifest'].write_bytes(plugin._json(managed))
    cache = _copy_host_cache(setup)
    cache_snapshot = {str(p): p.read_bytes() for p in cache.rglob('*') if p.is_file()}
    source = Path(__file__).resolve().parents[3] / 'skills/personal-knowledge/SKILL.md'
    template.write_text(source.read_text())
    updated = plugin.execute(setup)
    skill = (directory / 'skills/knowledge/SKILL.md').read_text()
    assert '引用只展示资料标题和可读位置' in skill
    assert '不显示完整或缩写 UUID' in skill
    assert '引用写明证据 ID' not in skill
    assert 'citation_url' in skill
    assert json.loads((directory / 'plugin.json').read_text())['version'] == '1.0.5'
    assert json.loads((directory / '.codex-plugin/plugin.json').read_text())['version'] == '1.0.5'
    assert not updated['host_installed'], 'Old host cache must require the supported host refresh'
    assert cache_snapshot == {str(p): p.read_bytes() for p in cache.rglob('*') if p.is_file()}
    config = plugin._parse(Path(setup['config_path']).read_bytes(), 'codex')
    assert config['plugins']['other@elsewhere'] == {'enabled': False}
    assert not (paths['home'] / 'AGENTS.md').exists()


def test_explicit_plugin_update_can_move_to_new_app_runtime(setup):
    plugin.execute(setup)
    previous_runtime = setup['runtime_path']
    next_runtime = str(Path(previous_runtime).with_name('new-runtime'))
    updated = dict(setup, runtime_path=next_runtime,
                   configuration=setup['configuration'].replace(previous_runtime, next_runtime))
    # Status/check do not silently redirect a live connection to a new process.
    with pytest.raises(agent_install.InstallError):
        plugin.execute(dict(updated, action='plugin_check'))
    plugin.execute(updated)
    state = plugin.execute(dict(updated, action='plugin_status'))
    assert state['installed'] and state['plugin_status'] == 'enabled'
    paths = plugin._paths(updated)
    manifest = json.loads(paths['manifest'].read_text())
    assert manifest['runtime_path'] == next_runtime
    entry = json.loads(paths['credential'].read_text())
    assert entry['command'] == str(Path(next_runtime) / 'python/bin/python3')
    assert entry['env']['PKA_API_FILE'] == setup['endpoint_path']
    with pytest.raises(agent_install.InstallError):
        plugin.execute(dict(updated, endpoint_path=str(Path(setup['endpoint_path']).with_name('other.json'))))


def test_failed_runtime_update_keeps_original_plugin_and_connection(setup, monkeypatch):
    plugin.execute(setup)
    paths = plugin._paths(setup)
    files = [paths['credential'], paths['launcher'], paths['manifest'],
             paths['directory'] / 'plugin.json', paths['config_path']]
    original = {path: path.read_bytes() for path in files}
    previous_runtime = setup['runtime_path']
    next_runtime = str(Path(previous_runtime).with_name('new-runtime'))
    updated = dict(setup, runtime_path=next_runtime,
                   configuration=setup['configuration'].replace(previous_runtime, next_runtime))
    async def unavailable(_):
        raise RuntimeError('synthetic new runtime unavailable')
    monkeypatch.setattr(plugin, '_check', unavailable)
    with pytest.raises(agent_install.InstallError, match='回退'):
        plugin.execute(updated)
    assert original == {path: path.read_bytes() for path in files}
    assert plugin.execute(dict(setup, action='plugin_status'))['installed']


def test_legacy_package_can_be_inspected_upgraded_and_removed(setup):
    plugin.execute(setup)
    paths = plugin._paths(setup)
    metadata = paths['directory'] / 'skills/knowledge/agents/openai.yaml'
    metadata.unlink()
    manifest = json.loads(paths['manifest'].read_text())
    manifest['files'].pop(str(metadata))
    for relative in ('plugin.json', '.codex-plugin/plugin.json'):
        file = paths['directory'] / relative
        value = json.loads(file.read_text())
        value['version'] = '1.0.0'
        file.write_bytes(plugin._json(value))
        manifest['files'][str(file)] = plugin._hash(file.read_bytes())
    paths['manifest'].write_bytes(plugin._json(manifest))
    cache = _copy_host_cache(setup)
    cache.rename(cache.with_name('1.0.0'))
    assert plugin.execute(dict(setup, action='plugin_host_check'))['host_installed']
    plugin.execute(setup)
    assert metadata.is_file()
    assert not plugin.execute(dict(setup, action='plugin_status'))['host_installed']
    plugin.execute(dict(setup, action='plugin_remove'))
    assert not metadata.exists()


def test_relocated_runtime_status_is_update_required_not_modified(setup):
    plugin.execute(setup)
    moved = dict(setup, action='plugin_status', runtime_path=str(Path(setup['runtime_path']).with_name('new-runtime')))
    before = Path(setup['config_path']).read_bytes()
    result = plugin.execute(moved)
    assert result['installed'] and result['update_required'] and result['plugin_status'] == 'needs_update'
    assert result['auto_reference_enabled'] is False and '路径' in result['message']
    assert Path(setup['config_path']).read_bytes() == before


def test_runtime_repair_reuses_verified_private_token_without_configuration(setup):
    installed = plugin.execute(setup)
    moved = dict(setup, runtime_path=str(Path(setup['runtime_path']).with_name('new-runtime')))
    moved.pop('configuration')
    result = plugin.execute(moved)
    assert result['plugin_name'] == installed['plugin_name']
    paths = plugin._paths(moved)
    credential = json.loads(paths['credential'].read_text())
    assert credential['env']['PKA_AGENT_TOKEN'] == 'synthetic-secret'
    assert credential['command'] == str(Path(moved['runtime_path']) / 'python/bin/python3')
    assert not plugin.execute(dict(moved, action='plugin_status'))['update_required']
    assert len(json.loads(paths['marketplace_path'].read_text())['plugins']) == 2  # current plus unrelated plugin


def _legacy_duplicate(setup, *, endpoint=None):
    plugin.execute(setup)
    current = plugin._paths(setup)
    original_entry = plugin._entry(current)
    identifier = str(uuid.uuid4())
    other = dict(setup, connection_id=identifier,
                 configuration=setup['configuration'].replace(setup['connection_id'], identifier)
                    .replace(setup['connection_id'].replace('-', '_'), identifier.replace('-', '_')))
    if endpoint:
        other['endpoint_path'] = endpoint
        other['configuration'] = other['configuration'].replace(setup['endpoint_path'], endpoint)
    plugin.execute(other)
    catalog = json.loads(current['marketplace_path'].read_text())
    if not any(entry['name'] == current['name'] for entry in catalog['plugins']):
        catalog['plugins'].append(original_entry)
        current['marketplace_path'].write_bytes(plugin._json(catalog))
    return other, plugin._paths(other)


def test_consolidation_retires_same_library_registration_and_preserves_files_and_unrelated_plugin(setup):
    old, old_paths = _legacy_duplicate(setup)
    before = {path: path.read_bytes() for path in old_paths['directory'].rglob('*') if path.is_file()}
    result = plugin.execute(dict(setup, action='plugin_consolidate'))
    assert result['retired_plugins'] == [old_paths['name']] and not result['retained_plugins']
    catalog = json.loads(Path(setup['marketplace_path']).read_text())
    assert {entry['name'] for entry in catalog['plugins']} == {'other', plugin._paths(setup)['name']}
    config = plugin._parse(Path(setup['config_path']).read_bytes(), 'codex')
    assert config['plugins'][old_paths['name'] + '@existing-personal']['enabled'] is False
    assert before == {path: path.read_bytes() for path in old_paths['directory'].rglob('*') if path.is_file()}
    assert plugin.execute(dict(setup, action='plugin_consolidate'))['retired_plugins'] == []


def test_consolidation_handles_detached_config_and_orphan_comment(setup):
    old, paths = _legacy_duplicate(setup)
    config = Path(setup['config_path'])
    catalog = json.loads(Path(setup['marketplace_path']).read_text())
    config.write_bytes(plugin._config_edit(config.read_bytes(), plugin._block(paths, catalog, True), None, paths, catalog)
                       + ('\n# END PKA PLUGIN ' + old['connection_id'] + '\n').encode())
    before = config.read_bytes()
    result = plugin.execute(dict(setup, action='plugin_consolidate'))
    assert result['retired_plugins'] == [paths['name']]
    assert config.read_bytes() == before


def test_consolidation_preserves_other_library_and_modified_package(setup):
    old, paths = _legacy_duplicate(setup, endpoint=str(Path(setup['endpoint_path']).with_name('other-api.json')))
    assert plugin.execute(dict(setup, action='plugin_consolidate'))['retired_plugins'] == []
    (paths['directory'] / 'user-notes.txt').write_text('keep this user content')
    # Same-library registration with an extra file must be left for explicit inspection.
    manifest = json.loads(paths['manifest'].read_text())
    manifest['endpoint_path'] = setup['endpoint_path']
    paths['manifest'].write_bytes(plugin._json(manifest))
    result = plugin.execute(dict(setup, action='plugin_consolidate'))
    assert result['retained_plugins'] == [paths['name']] and result['retired_plugins'] == []
    assert (paths['directory'] / 'user-notes.txt').read_text() == 'keep this user content'


def test_new_connection_install_automatically_keeps_one_same_library_listing(setup):
    first = plugin.execute(setup)
    identifier = str(uuid.uuid4())
    second = dict(setup, connection_id=identifier,
        configuration=setup['configuration'].replace(setup['connection_id'], identifier)
            .replace(setup['connection_id'].replace('-', '_'), identifier.replace('-', '_')))
    installed = plugin.execute(second)
    assert installed['retired_plugins'] == [first['plugin_name']]
    listed = json.loads(Path(setup['marketplace_path']).read_text())['plugins']
    assert {entry['name'] for entry in listed} == {'other', installed['plugin_name']}

import json
from types import SimpleNamespace

import pytest

from app import agent_launcher
from app.platform_support import get_platform


def test_launcher_binds_runtime_and_filters_environment(tmp_path, monkeypatch):
    runtime = tmp_path / 'runtime'
    files = get_platform().files
    credential = tmp_path / 'private/connection.json'
    entry = {'command': str(runtime / 'python/bin/python3'),
             'args': ['-I', '-B', str(runtime / 'backend/app/local_mcp.py')],
             'env': {'PKA_API_FILE': str(tmp_path / 'endpoint.json'), 'PKA_AGENT_TOKEN': 'synthetic-agent'}}
    files.atomic_write(credential, json.dumps(entry).encode())
    calls = []
    platform = SimpleNamespace(files=files, runtime_layout=get_platform().runtime_layout,
        processes=SimpleNamespace(exec=lambda *arguments: calls.append(arguments)))
    monkeypatch.setattr(agent_launcher, 'get_platform', lambda: platform)
    monkeypatch.setenv('PKA_AI_PROVIDER_KEY', 'synthetic-provider')
    monkeypatch.setenv('UNRELATED_SECRET', 'synthetic-other')
    monkeypatch.setenv('PKA_DATA_DIR', '/unrelated-library')
    agent_launcher.launch(runtime, credential)
    assert len(calls) == 1
    command, arguments, environment = calls[0]
    assert command == entry['command'] and arguments == entry['args']
    assert environment['PKA_AGENT_TOKEN'] == 'synthetic-agent'
    assert all(key not in environment for key in ('PKA_AI_PROVIDER_KEY', 'UNRELATED_SECRET', 'PKA_DATA_DIR'))
    for altered in (dict(entry, command='/usr/bin/python3'), dict(entry, args=['other.py']),
                    dict(entry, env={**entry['env'], 'EXTRA': 'synthetic-secret'})):
        files.atomic_write(credential, json.dumps(altered).encode())
        with pytest.raises(SystemExit, match='凭证不可用或不安全') as error:
            agent_launcher.launch(runtime, credential)
        assert 'synthetic-secret' not in str(error.value)
        assert len(calls) == 1
    files.atomic_write(credential, json.dumps(entry).encode())
    credential.chmod(0o644)
    with pytest.raises(SystemExit):
        agent_launcher.launch(runtime, credential)
    assert len(calls) == 1

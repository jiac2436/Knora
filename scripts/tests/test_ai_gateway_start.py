"""Test startup wiring without credentials, ports or model calls."""
import os
from pathlib import Path
import shutil
import subprocess


def project(tmp_path):
    root = tmp_path / 'project'
    target = root / 'services/ai_gateway/start.sh'
    target.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[2] / 'services/ai_gateway/start.sh', target)
    executable = root / '.venv/bin/python'
    executable.parent.mkdir(parents=True)
    executable.write_text('#!/bin/bash\nprintf "%s\\n" "$@" > "$PKA_TEST_ARGS"\n')
    executable.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith('PKA_AI_')}
    env['PKA_TEST_ARGS'] = str(tmp_path / 'args.txt')
    return target, env


def test_script_fails_with_variable_name_only(tmp_path):
    target, env = project(tmp_path)
    result = subprocess.run(['bash', str(target)], env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert 'PKA_AI_PROVIDER_KEY' in result.stderr
    assert not Path(env['PKA_TEST_ARGS']).exists()


def test_script_passes_factory_and_loopback_to_existing_python(tmp_path):
    target, env = project(tmp_path)
    env.update(PKA_AI_PROVIDER_KEY='synthetic-test-key', PKA_AI_MODEL='synthetic-model',
               PKA_AI_GATEWAY_TOKEN='synthetic-test-token')
    result = subprocess.run(['bash', str(target)], env=env, capture_output=True, text=True)
    assert result.returncode == 0
    args = Path(env['PKA_TEST_ARGS']).read_text().splitlines()
    assert args == ['-m', 'uvicorn', 'services.ai_gateway.main:create_app', '--factory',
                    '--host', '127.0.0.1', '--port', '8091', '--workers', '1']
    assert 'synthetic-test-key' not in result.stdout + result.stderr

import json
import os
from pathlib import Path
import select
import subprocess
import sys
import time
import pytest
from urllib.request import Request, urlopen


HOST = Path(__file__).resolve().parents[1] / 'app/local_host.py'


def environment(root):
    values = {key: value for key, value in os.environ.items() if not key.startswith('PKA_')}
    values.update(PKA_ENVIRONMENT='local', PKA_DATA_DIR=str(root / 'data'),
                  PKA_CACHE_DIR=str(root / 'cache'), PKA_LOG_DIR=str(root / 'logs'), PKA_SEMANTIC_ENABLED='0',
                  PKA_WORKER_ROOT=str(Path(__file__).resolve().parents[3]))
    return values


def start(root, credential='a' * 48, ai=None):
    log = (root / f'host-{time.time_ns()}.log').open('wb')
    process = subprocess.Popen([sys.executable, '-I', '-B', str(HOST)], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=log, env=environment(root))
    if ai is None:
        process.stdin.write((credential + '\n').encode())
    else:
        process.stdin.write((json.dumps(dict(version=1, owner_credential=credential, ai=ai)) + '\n').encode())
    process.stdin.flush()
    return process, log


def event(process):
    assert select.select([process.stdout], [], [], 20)[0], '宿主没有及时返回启动状态'
    return json.loads(process.stdout.readline())


def close(process, log):
    if not process.stdin.closed:
        process.stdin.close()
    try:
        process.wait(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        process.stdout.close()
        log.close()


def test_host_ready_auth_duplicate_and_parent_eof(tmp_path):
    first, first_log = start(tmp_path)
    address = None
    try:
        ready = event(first)
        assert ready['event'] == 'ready', ready
        address = ready['address']
        assert address.startswith('http://127.0.0.1:')
        assert 'a' * 48 not in json.dumps(ready)
        assert ready['kernel']['contract_version'] == 1
        assert ready['kernel']['schema_version'] == 16
        assert ready['kernel']['platform'] == 'macos'
        assert ready['kernel']['capabilities']['owned_processes']
        with urlopen(address + '/health', timeout=3) as response:
            assert json.load(response)['kernel'] == ready['kernel']
        request = Request(address + '/v1/me', headers={'Authorization': 'Bearer ' + 'a' * 48})
        with urlopen(request, timeout=3) as response:
            assert json.load(response)['id'] == ready['owner_id']
        second, second_log = start(tmp_path, 'b' * 48)
        try:
            assert event(second)['event'] == 'failed'
            assert second.wait(timeout=5) == 1
            # Duplicate start cannot rotate the first client's credential.
            with urlopen(request, timeout=3) as response:
                assert response.status == 200
        finally:
            close(second, second_log)
    finally:
        close(first, first_log)
    assert first.returncode == 0
    if address:
        try:
            urlopen(address + '/health', timeout=1)
            raise AssertionError('宿主退出后端口仍在提供服务')
        except OSError:
            pass
    restarted, restarted_log = start(tmp_path)
    try:
        again = event(restarted)
        assert again['event'] == 'ready'
        assert again['device_id'] == ready['device_id']
        assert again['library_id'] == ready['library_id']
    finally:
        close(restarted, restarted_log)


def wait_for_worker_exit(pid):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError('宿主停止后 Worker 仍在运行')


@pytest.mark.parametrize('contract,schema', [(2, 16), (1, 999)])
def test_incompatible_desktop_rejected_before_database_changes(tmp_path, contract, schema):
    with (tmp_path / 'diagnostics').open('wb') as log:
        host = subprocess.Popen([sys.executable, '-I', '-B', str(HOST)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=log, env=environment(tmp_path))
        try:
            value = dict(version=1, owner_credential='a' * 48,
                         client_contract_version=contract, client_schema_version=schema)
            host.stdin.write((json.dumps(value) + '\n').encode())
            host.stdin.flush()
            result = event(host)
            assert result['event'] == 'failed' and '不兼容' in result['message']
            assert host.wait(timeout=5) == 1
            assert not (tmp_path / 'data/knowledge.db').exists()
        finally:
            close(host, log)


def test_killed_host_does_not_leave_worker(tmp_path):
    import signal
    host, log = start(tmp_path)
    try:
        ready = event(host)
        assert ready['event'] == 'ready', ready
        os.kill(host.pid, signal.SIGKILL)
        host.wait(timeout=5)
        wait_for_worker_exit(ready['worker_pid'])
    finally:
        close(host, log)


def test_worker_failure_stops_api_and_reports_failure(tmp_path):
    import signal
    host, log = start(tmp_path)
    try:
        ready = event(host)
        assert ready['event'] == 'ready', ready
        os.kill(ready['worker_pid'], signal.SIGKILL)
        failed = event(host)
        assert failed['event'] == 'failed'
        assert host.wait(timeout=10) == 1
        try:
            urlopen(ready['address'] + '/health', timeout=1)
            raise AssertionError('Worker 异常后 API 未停止')
        except OSError:
            pass
    finally:
        close(host, log)


@pytest.mark.parametrize('state', ['ready', 'missing', 'denied', 'unavailable', 'invalid', 'disabled'])
def test_private_model_startup_keeps_basic_api_available(tmp_path, state):
    from uuid import uuid4
    value = dict(profile=dict(profile_id=str(uuid4()), config_revision=1, mode='direct', status=state,
        provider='openai-compatible', base_url='https://synthetic.example/v1', model='synthetic-model',
        credential_ref='synthetic.model', credential_generation=str(uuid4())))
    if state == 'ready':
        value.update(provider_key='synthetic-provider-secret', gateway_token='synthetic-internal-secret')
    host, log = start(tmp_path, ai=value)
    try:
        ready = event(host)
        assert ready['event'] == 'ready', ready
        headers = {'Authorization': 'Bearer ' + 'a' * 48}
        with urlopen(Request(ready['address'] + '/v1/me', headers=headers), timeout=3) as response:
            assert json.load(response)['id'] == ready['owner_id']
        with urlopen(Request(ready['address'] + '/v1/ai/config', headers=headers), timeout=3) as response:
            assert json.load(response)['configured'] == (state == 'ready')
        with urlopen(Request(ready['address'] + '/v1/ai/configuration/state', headers=headers), timeout=3) as response:
            status = json.load(response)
            assert status['profile']['status'] == state
            assert 'synthetic-provider-secret' not in json.dumps(status)
            assert 'synthetic-internal-secret' not in json.dumps(status)
    finally:
        close(host, log)
    for path in (tmp_path / 'data').rglob('*'):
        if path.is_file():
            content = path.read_bytes()
            assert b'synthetic-provider-secret' not in content
            assert b'synthetic-internal-secret' not in content
    assert b'synthetic-provider-secret' not in (tmp_path / log.name).read_bytes()


def test_invalid_switch_file_is_preserved_without_blocking_basic_library(tmp_path):
    from test_model_configuration import startup
    cache = tmp_path / 'cache'
    cache.mkdir()
    gate = cache / 'model-switch-v1.json'
    gate.write_text('{invalid device state', encoding='utf8')
    gate.chmod(0o600)
    host, log = start(tmp_path, ai=startup().model_dump(mode='json'))
    try:
        ready = event(host)
        assert ready['event'] == 'ready', ready
        headers = {'Authorization': 'Bearer ' + 'a' * 48}
        with urlopen(Request(ready['address'] + '/v1/me', headers=headers), timeout=3) as response:
            assert response.status == 200
        with urlopen(Request(ready['address'] + '/v1/ai/config', headers=headers), timeout=3) as response:
            assert json.load(response)['configured'] is False
        assert gate.read_text() == '{invalid device state'
    finally:
        close(host, log)

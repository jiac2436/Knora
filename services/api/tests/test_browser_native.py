import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
from uuid import uuid4

import pytest
from app import browser_native as bridge


@pytest.fixture()
def connection(tmp_path):
    root = tmp_path.resolve()
    path = root / 'connection.json'
    value = {'api_url': 'http://127.0.0.1:54321', 'token': 'x' * 64, 'library_id': str(uuid4()),
             'runtime_id': 'fixture-runtime', 'host_pid': os.getpid(), 'instance_id': str(uuid4())}
    path.write_text(json.dumps(value)); path.chmod(0o600)
    return path, value


def test_native_discovery_never_returns_owner_token(connection, monkeypatch):
    path, value = connection
    calls = []
    def http(record, route, *args, **kwargs):
        calls.append((route, kwargs))
        return {'kernel': {'runtime_id': 'fixture-runtime'}} if route == '/health' else {'library_id': value['library_id']}
    monkeypatch.setattr(bridge, 'http', http)
    result = bridge.handle({'action': 'connect'}, path, Path('/fixture.app'))
    assert result['accountId'] == value['library_id']
    assert 'token' not in result and value['token'] not in json.dumps(result)
    assert calls[0][1]['authenticated'] is False


def test_bridge_library_and_route_isolation(connection, monkeypatch):
    path, value = connection
    monkeypatch.setattr(bridge, 'http', lambda *a, **k: pytest.fail('Must reject before network'))
    for request in [{'action': 'request', 'library_id': str(uuid4()), 'path': '/v1/captures', 'method': 'POST'},
                    {'action': 'request', 'library_id': value['library_id'], 'path': '/v1/agent-connections', 'method': 'POST'},
                    {'action': 'request', 'library_id': value['library_id'], 'path': '/v1/sources/../../me'},
                    {'action': 'request', 'library_id': value['library_id'], 'path': '/v1/captures', 'method': 'POST'}]:
        with pytest.raises(bridge.BridgeError): bridge.handle(request, path, Path('/fixture.app'))


def test_private_endpoint_and_offline(connection):
    path, value = connection
    assert bridge.endpoint(path)['library_id'] == value['library_id']
    path.chmod(0o644)
    with pytest.raises(ValueError): bridge.endpoint(path)
    path.unlink()
    with pytest.raises(bridge.BridgeError, match='未启动'): bridge.endpoint(path)


def test_native_proxy_projects_minimal_capture_and_source_response(connection, monkeypatch):
    path, value = connection
    monkeypatch.setattr(bridge, 'validate_service', lambda _: None)
    monkeypatch.setattr(bridge, 'http', lambda *a: {'id': 'source-id', 'title': '资料', 'raw_input': 'private', 'extracted_text': '正文', 'coverage': 'full_text'})
    base = {'action': 'request', 'library_id': value['library_id']}
    assert bridge.handle({**base, 'path': '/v1/captures', 'method': 'POST', 'key': 'capture-key', 'json': {'content': {'text': '正文'}}}, path, Path('/fixture.app')) == {'id': 'source-id'}
    assert 'raw_input' not in bridge.handle({**base, 'path': '/v1/sources/source-id'}, path, Path('/fixture.app'))


def test_native_framing_and_origin_check(connection):
    path, _ = connection
    script = Path(bridge.__file__)
    request = json.dumps({'action': 'connect'}).encode()
    # Missing endpoint must yield a framed error, never diagnostics or credentials on stdout.
    args = [sys.executable, '-B', str(script), '--connection', str(path.parent / 'absent.json'), '--application', '/fixture.app']
    result = subprocess.run(args + [bridge.ORIGIN], input=struct.pack('=I', len(request)) + request, capture_output=True, timeout=10)
    size = struct.unpack('=I', result.stdout[:4])[0]
    assert size == len(result.stdout) - 4
    assert json.loads(result.stdout[4:])['status'] == 503
    assert subprocess.run(args + ['chrome-extension://wrong/'], input=struct.pack('=I', len(request)) + request, capture_output=True, timeout=10).stdout == b''


def test_install_registers_specific_extension_and_clear_preserves_other_instance(tmp_path):
    home = tmp_path.resolve()
    runtime = home / 'runtime'
    for relative in ('python/bin/python3', 'backend/app/browser_native.py'):
        target = runtime / relative; target.parent.mkdir(parents=True, exist_ok=True); target.write_text('fixture')
    (runtime / 'backend/app/platform_support').mkdir()
    request = {'home': str(home), 'runtime': str(runtime), 'application': str(home / '知识收集箱.app'),
               'api_url': 'http://127.0.0.1:54321', 'token': 'x' * 64, 'library_id': str(uuid4()),
               'runtime_id': 'fixture', 'host_pid': os.getpid()}
    result = bridge.install(request)
    for browser in ('Google/Chrome', 'Microsoft Edge'):
        manifest = home / 'Library/Application Support' / browser / 'NativeMessagingHosts' / (bridge.HOST + '.json')
        data = json.loads(manifest.read_text())
        assert data['allowed_origins'] == [bridge.ORIGIN]
        assert request['token'] not in manifest.read_text()
        assert request['token'] not in Path(data['path']).read_text()
    endpoint = home / 'Library/Application Support/local.pka.knowledge-inbox-state/browser/connection.json'
    bridge.clear({'home': str(home), 'instance_id': 'another-instance'})
    assert endpoint.exists()
    bridge.clear({'home': str(home), **result})
    assert not endpoint.exists()


def test_launch_when_app_offline(connection, monkeypatch):
    path, value = connection
    path.unlink()
    launched = []
    def run(args, **kwargs):
        launched.append(args)
        path.write_text(json.dumps(value)); path.chmod(0o600)
    monkeypatch.setattr(bridge.subprocess, 'run', run)
    monkeypatch.setattr(bridge, 'validate_service', lambda _: None)
    monkeypatch.setattr(bridge, 'http', lambda *a: {'library_id': value['library_id']})
    assert bridge.handle({'action': 'connect', 'launch': True}, path, Path('/fixture.app'))['kind'] == 'local'
    assert launched == [['/usr/bin/open', '/fixture.app']]


def test_reinstall_after_app_rename_preserves_host_identity_and_library(tmp_path):
    home = tmp_path.resolve()
    old_app = home / '知识收集箱.app'
    runtime = old_app / 'Contents/Resources/runtime'
    python = runtime / 'python/bin/python3'
    python.parent.mkdir(parents=True)
    python.write_text('fixture')
    (runtime / 'backend/app/platform_support').mkdir(parents=True)
    extension = home / 'extension'
    extension.mkdir(); (extension / 'manifest.json').write_text('{}')
    request = {'home': str(home), 'runtime': str(runtime), 'application': str(old_app),
               'extension_path': str(extension)}
    library_file = home / 'Library/Application Support/local.pka.knowledge-inbox/keep'
    library_file.parent.mkdir(parents=True); library_file.write_text('existing library')
    bridge.install(request)
    manifests = [home / 'Library/Application Support' / browser / 'NativeMessagingHosts' / (bridge.HOST + '.json')
                 for browser in ('Google/Chrome', 'Microsoft Edge')]
    original = [json.loads(path.read_text()) for path in manifests]
    new_app = home / 'Knora.app'
    old_app.rename(new_app)
    new_runtime = new_app / 'Contents/Resources/runtime'
    bridge.install(dict(request, runtime=str(new_runtime), application=str(new_app)))
    for path, previous in zip(manifests, original):
        assert json.loads(path.read_text()) == previous
        launcher = Path(previous['path']).read_text()
        assert str(new_runtime / 'python/bin/python3') in launcher
        assert str(old_app) not in launcher
    state = home / 'Library/Application Support/local.pka.knowledge-inbox-state/browser/connection.json'
    configuration = json.loads(state.read_text())
    assert configuration['application'] == str(new_app)
    assert configuration['runtime'] == str(new_runtime)
    assert 'token' not in configuration
    assert library_file.read_text() == 'existing library'


def test_real_local_service_capture_readback_restart_and_offline(tmp_path):
    from test_local_host import start, event, close
    from urllib.request import Request, urlopen
    root = tmp_path.resolve()
    path = root / 'browser-connection.json'
    credential = 'a' * 48
    def publish(ready, process):
        value = {'api_url': ready['address'], 'token': credential, 'library_id': ready['library_id'],
                 'runtime_id': ready['kernel']['runtime_id'], 'host_pid': process.pid}
        path.write_text(json.dumps(value)); path.chmod(0o600)
    process, log = start(root, credential)
    try:
        ready = event(process); assert ready['event'] == 'ready'
        publish(ready, process)
        binding = bridge.handle({'action': 'connect'}, path, Path('/fixture.app'))
        assert binding['accountId'] == ready['library_id']
        headers = {'Authorization': 'Bearer ' + credential, 'Content-Type': 'application/json'}
        with urlopen(Request(ready['address'] + '/v1/domains', data=json.dumps({'name': '浏览器合成验证'}).encode(), headers=headers), timeout=5) as response:
            domain = json.load(response)
        request = {'action': 'request', 'library_id': ready['library_id'], 'method': 'POST', 'path': '/v1/captures',
                   'key': 'browser-e2e-unique', 'json': {'capture_type': 'browser_page', 'title': '桥接测试',
                   'source_url': 'https://example.com/article', 'content': {'text': '仅勾选的正文'}, 'coverage': 'partial_text',
                   'context': {'domain_id': domain['id']}, 'client': {'kind': 'browser_extension', 'version': '0.1'}}}
        saved = bridge.handle(request, path, Path('/fixture.app'))
        assert bridge.handle(request, path, Path('/fixture.app')) == saved
        result = bridge.handle({'action': 'request', 'library_id': ready['library_id'], 'path': '/v1/sources/' + saved['id']}, path, Path('/fixture.app'))
        assert result['extracted_text'] == '仅勾选的正文'
        assert credential not in json.dumps(result)
    finally:
        close(process, log)
    with pytest.raises(bridge.BridgeError): bridge.handle({'action': 'connect'}, path, Path('/fixture.app'))
    restarted, log = start(root, credential)
    try:
        again = event(restarted); assert again['event'] == 'ready'
        assert again['library_id'] == ready['library_id']
        publish(again, restarted)
        assert bridge.handle({'action': 'connect'}, path, Path('/fixture.app'))['accountId'] == binding['accountId']
    finally:
        close(restarted, log)


@pytest.mark.parametrize('identifier', [None, '11111111-1111-4111-8111-111111111111'])
def test_managed_discovery_reuses_existing_client_files_without_persisting_token(tmp_path, identifier):
    import hashlib
    home = tmp_path.resolve()
    state = home / 'Library/Application Support/local.pka.knowledge-inbox-state'
    credentials = state / 'device-credentials'; credentials.mkdir(parents=True)
    if identifier:
        pointer = state / 'active-library.json'
        pointer.write_text(json.dumps({'version': 1, 'id': identifier})); pointer.chmod(0o600)
    suffix = '-libraries/' + identifier if identifier else ''
    data = home / ('Library/Application Support/local.pka.knowledge-inbox' + suffix); data.mkdir(parents=True)
    library = str(uuid4())
    endpoint = data / 'agent-api.json'
    endpoint.write_text(json.dumps({'api_url': 'http://127.0.0.1:54321', 'library_id': library})); endpoint.chmod(0o600)
    service = 'local.pka.knowledge-inbox.local-device' + ('.library.' + identifier if identifier else '')
    token = credentials / (hashlib.sha256(service.encode()).hexdigest() + '.token')
    token.write_text('x' * 64); token.chmod(0o600)
    result = bridge.managed_endpoint({'home': str(home), 'runtime': str(home / 'runtime')})
    assert result['token'] == 'x' * 64 and result['library_id'] == library
    endpoint.unlink()
    with pytest.raises(bridge.BridgeError, match='未启动'):
        bridge.managed_endpoint({'home': str(home), 'runtime': str(home / 'runtime')})

"""Account-free Chrome/Edge bridge. Owner credentials never leave the native process."""
from __future__ import annotations
import argparse
from contextlib import closing
import json
import hashlib
import os
from pathlib import Path
import re
import shlex
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request
from uuid import UUID, uuid4

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.platform_support import get_platform

HOST = 'local.pka.knowledge_inbox'
ORIGIN = 'chrome-extension://djlajagkmlkmelpdnbkmaphmhfhnhleh/'
MAXIMUM = 900_000


def private_json(path):
    raw = get_platform().files.read(path, maximum=8192, parents=True)
    return json.loads(raw) if raw else None


def install(request):
    home, runtime, application = (Path(request[key]) for key in ('home', 'runtime', 'application'))
    files = get_platform().files
    directory = home / 'Library/Application Support/local.pka.knowledge-inbox-state/browser'
    files.directory(directory)
    connection = directory / 'connection.json'
    launcher = directory / 'native-host'
    python = runtime / 'python/bin/python3'
    script = directory / 'browser_native.py'
    if not python.is_file() or not (runtime / 'backend/app/platform_support').is_dir():
        raise ValueError('当前运行包未包含浏览器桥接，请更新完整客户端')
    files.atomic_write(script, Path(__file__).read_bytes())
    bootstrap = ('import runpy,sys;sys.path.insert(0,' + repr(str(runtime / 'backend')) + ');'
                 'sys.argv=[' + repr(str(script)) + ',"--connection",' + repr(str(connection))
                 + ',"--application",' + repr(str(application)) + ']+sys.argv[1:];'
                 'runpy.run_path(' + repr(str(script)) + ',run_name="__main__")')
    command = '#!/bin/sh\nexec ' + ' '.join(shlex.quote(str(value)) for value in (python, '-I', '-B', '-c', bootstrap)) + ' "$@"\n'
    files.atomic_write(launcher, command.encode())
    launcher.chmod(0o700)
    origins = [ORIGIN]
    if request.get('extension_path'):
        extension = Path(request['extension_path']).resolve(strict=True)
        if not (extension / 'manifest.json').is_file(): raise ValueError('扩展目录无效')
        # Chromium derives unpacked-extension IDs from their absolute path.
        digest = hashlib.sha256(str(extension).encode()).digest()[:16]
        extension_id = ''.join(chr(97 + part) for byte in digest for part in (byte >> 4, byte & 15))
        origins.append('chrome-extension://' + extension_id + '/')
    manifest = {'name': HOST, 'description': 'Knora local knowledge capture', 'type': 'stdio',
                'path': str(launcher), 'allowed_origins': origins}
    for browser in ('Google/Chrome', 'Microsoft Edge'):
        destination = home / 'Library/Application Support' / browser / 'NativeMessagingHosts' / (HOST + '.json')
        previous = private_json(destination)
        if previous and (previous.get('name') != HOST or previous.get('path') != str(launcher) or ORIGIN not in previous.get('allowed_origins', [])):
            raise ValueError('浏览器桥接配置冲突，未覆盖现有配置')
        files.atomic_write(destination, json.dumps(manifest).encode())
    instance = str(uuid4())
    value = {'kind': 'managed', 'home': str(home), 'runtime': str(runtime),
             'application': str(application), 'instance_id': instance, 'allowed_origins': origins}
    files.atomic_write(connection, json.dumps(value).encode())
    return {'instance_id': instance}


def clear(request):
    path = Path(request['home']) / 'Library/Application Support/local.pka.knowledge-inbox-state/browser/connection.json'
    previous = private_json(path)
    if previous and previous.get('instance_id') == request['instance_id']:
        path.unlink()
    return {'cleared': True}


class BridgeError(Exception):
    def __init__(self, message, status=503):
        self.status = status
        super().__init__(message)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def managed_endpoint(configuration):
    """Derive only the currently selected managed library; never scan ports or read browser cookies."""
    home, runtime = Path(configuration['home']), Path(configuration['runtime'])
    state = home / 'Library/Application Support/local.pka.knowledge-inbox-state'
    pointer = private_json(state / 'active-library.json')
    identifier = pointer.get('id') if pointer else None
    if pointer and (pointer.get('version') != 1 or set(pointer) != {'version', 'id'}):
        raise BridgeError('活动知识库配置无效')
    if identifier is not None:
        identifier = str(UUID(identifier)).lower()
    suffix = '-libraries/' + identifier if identifier else ''
    data = home / ('Library/Application Support/local.pka.knowledge-inbox' + suffix)
    published = private_json(data / 'agent-api.json')
    if not published:
        raise BridgeError('本地 App 未启动，请打开 Knora 后重试')
    service = 'local.pka.knowledge-inbox.local-device' + ('.library.' + identifier if identifier else '')
    filename = hashlib.sha256(service.encode()).hexdigest() + '.token'
    raw = get_platform().files.read(state / 'device-credentials' / filename,
                                    maximum=256, parents=True, permissions=0o600)
    if not raw: raise BridgeError('未找到本机访问凭证，请先启动客户端')
    credential = raw.decode('ascii')
    if any(char.isspace() for char in credential): raise BridgeError('本机访问凭证不可用')
    # A packaged client uses the complete runtime manifest as its service identity.
    manifest = runtime / 'manifest.json'
    runtime_id = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else 'development'
    return {'api_url': published['api_url'], 'library_id': published['library_id'],
            'token': credential, 'runtime_id': runtime_id}


def endpoint(path):
    value = private_json(path)
    if not value:
        raise BridgeError('本地 App 未启动，请打开 Knora 后重试')
    if value.get('kind') == 'managed':
        value = managed_endpoint(value)
    from urllib.parse import urlsplit
    url = urlsplit(value.get('api_url', ''))
    if (url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.username
            or url.password or url.path or url.query or url.fragment):
        raise BridgeError('本地连接信息无效')
    UUID(value['library_id'])
    if not isinstance(value.get('token'), str) or not 32 <= len(value['token']) <= 256:
        raise BridgeError('本地连接凭证不可用')
    try:
        if value.get('host_pid'): os.kill(int(value['host_pid']), 0)
    except (OSError, ValueError):
        raise BridgeError('本地 App 未启动，请打开 Knora 后重试') from None
    return value


def http(value, path, method='GET', body=None, key=None, authenticated=True):
    headers = {'Accept': 'application/json'}
    if authenticated: headers['Authorization'] = 'Bearer ' + value['token']
    if key: headers['Idempotency-Key'] = key
    data = json.dumps(body, ensure_ascii=False).encode() if body is not None else None
    if data is not None: headers['Content-Type'] = 'application/json'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(value['api_url'] + path, data=data, headers=headers, method=method)
    try:
        with closing(opener.open(request, timeout=30)) as response:
            raw = response.read(12_000_001)
            if len(raw) > 12_000_000: raise BridgeError('本地响应过大，请缩小采集内容', 413)
            return json.loads(raw)
    except urllib.error.HTTPError as error:
        # Do not echo response bodies or credentials from an unexpected local process.
        raise BridgeError(f'本地服务返回 {error.code}，请检查客户端状态后重试', error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise BridgeError('本地服务暂不可用，内容会保留在插件队列中') from None


def validate_service(value):
    health = http(value, '/health', authenticated=False)
    if health.get('kernel', {}).get('runtime_id') != value['runtime_id']:
        raise BridgeError('本地服务版本已改变，请重新连接客户端', 409)


def handle(request, path, application):
    action = request.get('action')
    if action == 'connect':
        if request.get('launch'):
            subprocess.run(['/usr/bin/open', str(application)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + (80 if request.get('launch') else 0)
        while True:
            try:
                value = endpoint(path)
                validate_service(value)
                me = http(value, '/v1/me')
                if me.get('library_id') != value['library_id']: raise BridgeError('本地知识库身份已改变', 409)
                return {'kind': 'local', 'accountId': value['library_id'], 'displayName': '本地知识库', 'apiBaseUrl': value['api_url']}
            except BridgeError:
                if time.monotonic() >= deadline: raise
                time.sleep(.25)
    if action != 'request': raise BridgeError('不支持的桥接操作', 400)
    value = endpoint(path)
    if request.get('library_id') != value['library_id']:
        raise BridgeError('当前 App 已切换知识库，原资料仍保留在原知识库的待同步队列中', 409)
    route, method = request.get('path', ''), request.get('method', 'GET')
    allowed = (method == 'GET' and (route in ('/v1/me', '/v1/domains', '/v1/projects')
               or re.fullmatch(r'/v1/sources/[a-zA-Z0-9-]{1,80}', route))) or (method == 'POST' and route == '/v1/captures')
    if not allowed: raise BridgeError('不支持的本地采集接口', 400)
    key = request.get('key')
    if method == 'POST' and (not isinstance(key, str) or not 1 <= len(key) <= 200):
        raise BridgeError('缺少有效的采集提交编号', 400)
    validate_service(value)
    result = http(value, route, method, request.get('json'), key)
    if route == '/v1/captures': return {'id': result['id']}
    if route.startswith('/v1/sources/'):
        view = {key: result.get(key) for key in ('id', 'title', 'source_url', 'extracted_text', 'coverage', 'revision', 'browser_image_urls')}
        view['images'] = [{key: image.get(key) for key in ('ordinal', 'status', 'failure_reason')}
                          for image in result.get('images', [])]
        return view
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--install', action='store_true')
    parser.add_argument('--clear', action='store_true')
    parser.add_argument('--connection', type=Path)
    parser.add_argument('--application', type=Path)
    parser.add_argument('origin', nargs='?')
    args = parser.parse_args()
    if args.install or args.clear:
        request = json.loads(sys.stdin.buffer.read(16384))
        print(json.dumps(install(request) if args.install else clear(request)))
        return
    if not args.connection or not args.application: return
    configuration = private_json(args.connection)
    allowed_origins = configuration.get('allowed_origins', [ORIGIN]) if configuration else [ORIGIN]
    if args.origin not in allowed_origins: return
    header = sys.stdin.buffer.read(4)
    if len(header) != 4: return
    length = struct.unpack('=I', header)[0]
    if length > MAXIMUM: return
    try:
        raw = sys.stdin.buffer.read(length)
        if len(raw) != length: return
        request = json.loads(raw)
        if not isinstance(request, dict): raise BridgeError('请求格式无效', 400)
        result = {'ok': True, 'data': handle(request, args.connection, args.application)}
    except BridgeError as error:
        result = {'ok': False, 'message': str(error), 'status': error.status}
    except Exception:
        result = {'ok': False, 'message': '本地桥接不可用，请打开新版客户端后重试', 'status': 503}
    data = json.dumps(result, ensure_ascii=False).encode()
    if len(data) > MAXIMUM:
        data = json.dumps({'ok': False, 'message': '本地响应超过浏览器消息上限', 'status': 413}).encode()
    sys.stdout.buffer.write(struct.pack('=I', len(data)) + data)
    sys.stdout.buffer.flush()


if __name__ == '__main__': main()

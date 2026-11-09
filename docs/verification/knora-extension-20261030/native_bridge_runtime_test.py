"""Exercise the public installer and native framing with a disposable home/library."""
import hashlib
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'apps/macos/scripts'))
from packaged_kernel_test import PackagedHost


def main():
    runtime = ROOT / 'apps/macos/dist/Knora.app/Contents/Resources/runtime'
    with tempfile.TemporaryDirectory(prefix='knora-browser-') as temporary:
        home = Path(temporary).resolve()
        command = [sys.executable, '-I', '-B', str(ROOT / 'apps/extension/scripts/install-native-bridge.py'),
                   '--home', str(home)]
        subprocess.run(command, check=True, capture_output=True, timeout=40)
        state = home / 'Library/Application Support/local.pka.knowledge-inbox-state'
        connection = state / 'browser/connection.json'
        value = json.loads(connection.read_text())
        assert value['application'] == str(runtime.parents[2])
        assert value['runtime'] == str(runtime)
        assert 'token' not in value
        launcher = state / 'browser/native-host'
        assert '知识收集箱.app' not in launcher.read_text()
        assert launcher.stat().st_mode & 0o777 == 0o700
        extension = ROOT / 'apps/extension/dist'
        digest = hashlib.sha256(str(extension).encode()).digest()[:16]
        origin = 'chrome-extension://' + ''.join(chr(97 + n) for b in digest for n in (b >> 4, b & 15)) + '/'
        manifests = [home / 'Library/Application Support' / browser / 'NativeMessagingHosts/local.pka.knowledge_inbox.json'
                     for browser in ['Google/Chrome', 'Microsoft Edge']]
        original = [path.read_bytes() for path in manifests]
        for path in manifests:
            item = json.loads(path.read_text())
            assert item['name'] == 'local.pka.knowledge_inbox'
            assert item['description'] == 'Knora local knowledge capture'
            assert origin in item['allowed_origins']
            assert item['path'] == str(launcher)
        subprocess.run(command, check=True, capture_output=True, timeout=40)
        assert [path.read_bytes() for path in manifests] == original
        print('PASS: default Knora.app installer, private launcher, stable host/origin and repeat registration', flush=True)

        def native(request, expected=200):
            raw = json.dumps(request).encode()
            process = subprocess.run([str(launcher), origin], input=struct.pack('=I', len(raw)) + raw,
                                     capture_output=True, timeout=40)
            assert process.returncode == 0, process.stderr.decode()
            assert len(process.stdout) >= 4
            size = struct.unpack('=I', process.stdout[:4])[0]
            assert size == len(process.stdout) - 4
            response = json.loads(process.stdout[4:])
            assert response['ok'] == (expected == 200), response
            if expected != 200:
                assert response['status'] == expected, response
            return response.get('data', response)

        assert 'Knora' in native({'action': 'connect'}, 503)['message']
        host = PackagedHost(runtime, home, 'missing')
        data = home / 'Library/Application Support/local.pka.knowledge-inbox'
        host.env.update(PKA_DATA_DIR=str(data), PKA_CACHE_DIR=str(home / 'cache'), PKA_LOG_DIR=str(home / 'logs'))
        credentials = state / 'device-credentials'
        credentials.mkdir(mode=0o700)
        token = credentials / (hashlib.sha256(b'local.pka.knowledge-inbox.local-device').hexdigest() + '.token')
        token.write_text(host.credential); token.chmod(0o600)
        try:
            host.start()
            binding = native({'action': 'connect'})
            assert host.credential not in json.dumps(binding)
            domain = host.call('/v1/domains', {'name': '合成浏览器验收'}, expected=201)['id']
            payload = {'capture_type': 'browser_page', 'title': 'Knora桥接合成测试',
                       'source_url': 'https://example.invalid/synthetic', 'coverage': 'partial_text',
                       'content': {'text': '只保存在临时知识库的合成文字。'}, 'context': {'domain_id': domain},
                       'client': {'kind': 'browser_extension', 'version': '0.1.3'}}
            request = {'action': 'request', 'library_id': binding['accountId'], 'path': '/v1/captures',
                       'method': 'POST', 'key': str(uuid4()), 'json': payload}
            saved = native(request)
            assert native(request) == saved
            source = native({'action': 'request', 'library_id': binding['accountId'], 'path': '/v1/sources/' + saved['id']})
            assert source['extracted_text'] == payload['content']['text']
            assert host.credential not in json.dumps(source)
            native(dict(request, library_id=str(uuid4())), 409)
            print('PASS: real packaged service native discovery, capture/readback, idempotency and library isolation; no token returned', flush=True)
            host.stop()
            native({'action': 'connect'}, 503)
            host.start()
            assert native({'action': 'connect'})['accountId'] == binding['accountId']
            assert host.count() == 0
            print('PASS: offline error, restart with stable library identity and 0 model calls; no real browser/user library writes', flush=True)
        finally:
            host.stop()


if __name__ == '__main__':
    main()

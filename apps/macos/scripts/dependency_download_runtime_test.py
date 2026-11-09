"""Real public-source installation using the candidate's independent Python."""
from pathlib import Path
import json
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from packaged_kernel_test import PackagedHost, capture, wait


def exercise(runtime):
    python = runtime / 'python/bin/python3'
    with tempfile.TemporaryDirectory(prefix='pka-public-dependency-test-') as temporary:
        root = Path(temporary).resolve()
        home = root / 'home'
        home.mkdir()
        host = PackagedHost(runtime, home, 'missing')
        device = root / 'extensions'
        host.env.update(PKA_EXTENSIONS_ROOT=str(device), PKA_SEMANTIC_ENABLED='1')
        def helper(operation, key, expected=0):
            progress = root / 'progress.json'
            process = subprocess.run([str(python), '-I', '-B', str(runtime / 'backend/app/extension_install.py'),
                '--root', str(device), '--progress', str(progress), operation, key],
                env=host.env, capture_output=True, text=True, timeout=1800)
            assert process.returncode == expected, process.stdout + process.stderr
            return json.loads(process.stdout)
        try:
            host.start()
            domain = host.call('/v1/domains', {'name': '国内下载验收'}, expected=201)['id']
            source = capture(host, domain, '国内依赖安装验证：保留资料，精确搜索和语义搜索引用同一原文。')
            assert all(value['status'] == 'not_installed' for value in host.call('/v1/runtime-extensions')['extensions'].values())
            for key in ('documents', 'semantic'):
                result = helper('download', key)
                installed = next(package for package in result['packages'] if package['id'] == key)
                assert installed['installed'] and installed['enabled'] and installed['online_available']
                assert json.loads((root / 'progress.json').read_text())['phase'] == 'completed'
                print('PASS: real domestic dependency download, exact manifest, native/model health, atomic install:', key, flush=True)
            packages = json.loads((runtime / 'extension-catalog.json').read_text())['packages']
            ocr_titles = {item['title'] for item in packages['ocr']['dependency_download']['artifacts']}
            assert {'paddleocr', 'paddlepaddle', 'Mac 运行支持组件', '文字检测模型', '文字识别模型'} <= ocr_titles
            assert host.call('/v1/runtime-extensions')['extensions']['semantic']['status'] == 'not_installed'
            assert host.call('/v1/search', {'query': '国内依赖安装验证', 'mode': 'exact'})['items']
            print('PASS: install preserves active host, exact search and capture; OCR component plan available', flush=True)
            host.stop()
            host.start()
            status = host.call('/v1/runtime-extensions')['extensions']
            assert status['semantic']['status'] == status['documents']['status'] == 'ready', status
            assert status['ocr']['status'] == 'not_installed'
            result = wait(lambda: host.call('/v1/search', {'query': '国内依赖安装验证', 'mode': 'semantic'})['items'], 'semantic indexing')
            assert source in {item['source_id'] for item in result}, result
            assert host.count() == 0
            print('PASS: restart activates downloaded engines, real semantic query retrieves preserved source; no cloud calls', flush=True)
            selected = json.loads((device / 'active.json').read_text())['packages']
            packages = json.loads((runtime / 'extension-catalog.json').read_text())['packages']
            assert all(selected[key] == packages[key]['sha256'] for key in ('documents', 'semantic'))
            print('PASS: active identities match previously shipped offline extension hashes', flush=True)
        finally:
            host.stop()


if __name__ == '__main__':
    exercise(Path(sys.argv[1]).resolve(strict=True))

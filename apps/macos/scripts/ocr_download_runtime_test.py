"""Real OCR component downloads, cancellation, activation and PDF worker test."""
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from packaged_kernel_test import PackagedHost, capture, wait


def exercise(runtime):
    python = runtime / 'python/bin/python3'
    with tempfile.TemporaryDirectory(prefix='pka-ocr-components-') as temporary:
        root = Path(temporary).resolve()
        home = root / 'home'
        home.mkdir()
        device, progress = root / 'extensions', root / 'progress.json'
        host = PackagedHost(runtime, home, 'missing')
        host.env['PKA_EXTENSIONS_ROOT'] = str(device)
        command = [str(python), '-I', '-B', str(runtime / 'backend/app/extension_install.py'),
                   '--root', str(device), '--progress', str(progress), 'download', 'ocr']
        process = None
        try:
            host.start()
            domain = host.call('/v1/domains', {'name': 'OCR逐项下载验收'}, expected=201)['id']
            source = capture(host, domain, '下载期间仍可保存和精确搜索的合成正文。')
            process = subprocess.Popen(command, env=host.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            cancelled, deadline = False, time.monotonic() + 120
            while process.poll() is None:
                if progress.exists():
                    state = json.loads(progress.read_text())
                    if any(row['state'] == 'downloading' for row in state.get('components', [])):
                        (root / 'cancel').write_bytes(b'')
                        cancelled = True
                        break
                assert time.monotonic() < deadline, 'download progress unavailable'
                time.sleep(0.05)
            stdout, stderr = process.communicate(timeout=60)
            assert cancelled and process.returncode == 1 and '取消' in json.loads(stdout)['error'], stderr
            assert not (device / 'active.json').exists()
            assert list((device / 'staging').iterdir()) == []
            (root / 'cancel').unlink()
            print('PASS: native-style component progress appears; cancellation cleans staged files without activation', flush=True)
            process = subprocess.Popen(command, env=host.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            deadline, observed = time.monotonic() + 1800, set()
            while process.poll() is None:
                if progress.exists():
                    state = json.loads(progress.read_text())
                    observed.update(row['id'] for row in state.get('components', []) if row['state'] in {'downloading', 'verified'})
                assert time.monotonic() < deadline, 'OCR install timeout'
                time.sleep(0.1)
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stdout + stderr
            snapshot = json.loads(stdout)
            package = next(row for row in snapshot['packages'] if row['id'] == 'ocr')
            assert package['installed'] and package['enabled'] and package['online_available']
            state = json.loads(progress.read_text())
            expected_components = json.loads((runtime / 'extension-catalog.json').read_text())['packages']['ocr']['dependency_download']['artifacts']
            assert state['phase'] == 'completed' and len(state['components']) == len(expected_components)
            assert all(row['state'] == 'completed' for row in state['components'])
            assert observed
            assert host.call('/v1/runtime-extensions')['extensions']['ocr']['status'] == 'not_installed'
            assert host.call('/v1/search', {'query': '下载期间', 'mode': 'exact'})['items'][0]['source_id'] == source
            print(f'PASS: {len(expected_components)} real public artifact downloads, pinned native repair, exact manifest and offline OCR health; host remains usable', flush=True)
            host.stop()
            host.start()
            assert host.call('/v1/runtime-extensions')['extensions']['ocr']['status'] == 'ready'
            from PIL import Image, ImageDraw, ImageFont
            image = Image.new('RGB', (800, 180), 'white')
            ImageDraw.Draw(image).text((30, 50), 'LOCAL OCR 2026', fill='black', font=ImageFont.load_default(size=48))
            pdf = BytesIO()
            image.save(pdf, format='PDF', resolution=96)
            request = Request(host.address + '/v1/file-captures?' + urlencode({'domain_id': domain, 'filename': 'synthetic-scan.pdf'}),
                data=pdf.getvalue(), headers={'Authorization': 'Bearer ' + host.credential,
                    'Content-Type': 'application/octet-stream', 'Idempotency-Key': str(uuid4())})
            with urlopen(request, timeout=30) as response:
                assert response.status == 201
                uploaded = json.load(response)
            def complete():
                result = host.call('/v1/sources/' + uploaded['id'])
                status = result.get('ocr', {}).get('status')
                assert status not in {'failed', 'cancelled'}, result.get('ocr')
                return result if status == 'completed' else None
            detail = wait(complete, 'real PDF OCR', timeout=180)
            assert '2026' in detail['extracted_text'] and host.count() == 0, detail.get('extracted_text')
            sys.path.insert(0, str(runtime / 'backend'))
            from app.runtime_extensions import verify_package, package_directory
            item = json.loads((runtime / 'extension-catalog.json').read_text())['packages']['ocr']
            verify_package(package_directory(device, item), item)
            assert json.loads((device / 'active.json').read_text())['packages']['ocr'] == item['sha256']
            print('PASS: restart activates OCR; real worker recognizes synthetic scanned PDF; accepted files unchanged; 0 cloud calls', flush=True)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                process.communicate(timeout=30)
            host.stop()


if __name__ == '__main__':
    exercise(Path(sys.argv[1]).resolve(strict=True))

"""Copy the disk image App to a fresh path and exercise an extension-free home."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'apps/macos/scripts'))
from packaged_kernel_test import PackagedHost, capture


def main():
    package = json.loads((Path(__file__).parent / 'package.json').read_text())
    image = ROOT / package['dmg']
    with tempfile.TemporaryDirectory(prefix='knora-dmg-') as temporary:
        work = Path(temporary).resolve()
        mount = work / 'mounted'
        mount.mkdir()
        subprocess.run(['hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mount), str(image)],
                       check=True, capture_output=True)
        try:
            installed = work / 'Applications Test/Knora.app'
            installed.parent.mkdir()
            subprocess.run(['ditto', str(mount / 'Knora.app'), str(installed)], check=True)
        finally:
            subprocess.run(['hdiutil', 'detach', str(mount)], check=True, capture_output=True)
        runtime = installed / 'Contents/Resources/runtime'
        home = work / 'fresh-home'
        home.mkdir()
        host = PackagedHost(runtime, home, 'missing')
        host.env['PKA_EXTENSIONS_ROOT'] = str(home / 'extensions')
        try:
            host.start()
            extensions = host.call('/v1/runtime-extensions')['extensions']
            assert set(extensions) == {'semantic', 'ocr', 'documents'}
            assert all(item['status'] == 'not_installed' for item in extensions.values()), extensions
            print('PASS: copied App runtime starts after DMG ejection, with no system Python or installed extensions', flush=True)
            domain = host.call('/v1/domains', {'name': '合成打包验收'}, expected=201)['id']
            source = capture(host, domain, '打包验收合成笔记，只写入临时知识库。', title='合成安装检查')
            detail = host.call('/v1/sources/' + source)
            assert detail['extracted_text'] == '打包验收合成笔记，只写入临时知识库。'
            found = host.call('/v1/search', {'query': '打包验收合成笔记', 'mode': 'exact', 'domain_ids': [domain]})
            assert source in {item['source_id'] for item in found['items']}, found
            identity = host.call('/v1/me')['library_id']
            host.stop()
            host.start()
            assert host.call('/v1/me')['library_id'] == identity
            assert host.call('/v1/sources/' + source)['title'] == '合成安装检查'
            assert host.count() == 0
            print('PASS: capture/readback, exact search and restart persistence; 0 model calls and no real user-data writes', flush=True)
        finally:
            host.stop()


if __name__ == '__main__':
    main()

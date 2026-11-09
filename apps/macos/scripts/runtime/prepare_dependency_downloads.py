"""Embed audited public artifact pins; preserve existing extension identities."""
import argparse
import json
from pathlib import Path
import zipfile

from verify_runtime import digest

ROOT = Path(__file__).resolve().parents[4]


def prepare(runtime, audit, archive_directory=None):
    if not runtime.resolve().is_relative_to(ROOT / 'apps/macos/.build'):
        raise ValueError('只能修改独立构建目录')
    wheels = {item['name']: item for item in json.loads((audit / 'wheel-audit.json').read_text())['wheels']}
    models = json.loads((audit / 'model-audit.json').read_text())
    path = runtime / 'extension-catalog.json'
    value = json.loads(path.read_text())
    for key, item in value['packages'].items():
        if key == 'ocr':
            item['online_reason'] = '在线安装暂未开放，请先导入离线扩展包。'
            continue
        directory = runtime / 'dependency-manifests'
        directory.mkdir(exist_ok=True)
        manifest = directory / f'{key}.json'
        with zipfile.ZipFile((archive_directory or ROOT / 'apps/macos/dist/扩展包') / item['filename']) as archive:
            manifest.write_bytes(archive.read('manifest.json'))
        if digest(manifest) != item['manifest_sha256']:
            raise ValueError('原扩展清单不匹配')
        artifacts = []
        for name, version in item['dependencies'].items():
            wheel = wheels[name]
            if not wheel.get('download_verified') or wheel['version'] != version:
                raise ValueError('未验证依赖：' + name)
            artifacts.append(dict(kind='wheel', filename=wheel['filename'], bytes=wheel['bytes'], sha256=wheel['sha256'],
                urls=[mirror['url'] for mirror in wheel['mirrors'] if mirror.get('listed')]))
        if key == 'semantic':
            for model in models:
                if not model['path'].startswith('model/'):
                    continue
                if not model['verified']:
                    raise ValueError('模型未验证')
                artifacts.append(dict(kind='file', path=model['path'], bytes=model['bytes'],
                    sha256=model['sha256'], urls=[model['url']]))
        item['dependency_download'] = dict(version=1, manifest=manifest.relative_to(runtime).as_posix(), artifacts=artifacts)
        item['online_reason'] = ''
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    print('PASS: audited semantic/documents plans embedded; OCR stays offline; original identities/manifests retained')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path, help='Directory containing the matching offline extension packages')
    args = parser.parse_args()
    prepare(args.runtime, args.audit, args.artifacts)

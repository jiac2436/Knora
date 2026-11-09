"""Prepare OCR public artifact pins and small, fixed native compatibility recipes."""
import argparse
import base64
import hashlib
import json
from pathlib import Path, PurePosixPath
import tarfile
import zipfile

from verify_runtime import digest
from vendor_paddle import GCC_LIBS

ROOT = Path(__file__).resolve().parents[4]
CACHE = ROOT / 'apps/macos/.build/dependency-downloads'


def prepare(runtime, source_audit, gcc_audit, archive_directory=None, cache=None):
    cache = cache or CACHE
    if not runtime.resolve().is_relative_to(ROOT / 'apps/macos/.build'):
        raise ValueError('只能修改独立构建目录')
    catalog_file = runtime / 'extension-catalog.json'
    catalog = json.loads(catalog_file.read_text())
    item = catalog['packages']['ocr']
    archive_file = (archive_directory or ROOT / 'apps/macos/dist/扩展包') / item['filename']
    if digest(archive_file) != item['sha256']:
        raise ValueError('已验收离线扩展包不匹配')
    wheel_audit = {value['name']: value for value in json.loads((source_audit / 'wheel-audit.json').read_text())['wheels']}
    gcc = json.loads(gcc_audit.read_text())
    if not gcc['verified'] or digest(cache / 'gcc.tar.gz') != gcc['sha256']:
        raise ValueError('原生运行库来源尚未通过验证')
    directory = runtime / 'dependency-manifests'
    directory.mkdir(exist_ok=True)
    manifest_file = directory / 'ocr.json'
    with zipfile.ZipFile(archive_file) as archive:
        manifest_file.write_bytes(archive.read('manifest.json'))
        entries = {entry['path']: entry for entry in json.loads(manifest_file.read_text())['files']}
        patches, raw = {}, {}
        paddle = wheel_audit['paddlepaddle']
        with zipfile.ZipFile(cache / paddle['filename']) as wheel:
            for name in ('libblas.dylib', 'liblapack.dylib', 'libgcc_s.1.dylib'):
                raw['packages/paddle/libs/' + name] = wheel.read('paddle/libs/' + name)
        with tarfile.open(cache / 'gcc.tar.gz') as source:
            for name in GCC_LIBS:
                raw['packages/paddle/libs/' + name] = source.extractfile('gcc/16.2.0/lib/gcc/current/' + name).read()
        for path, original in raw.items():
            target = archive.read(path)
            if hashlib.sha256(target).hexdigest() != entries[path]['sha256']:
                raise ValueError('已验收原生组件不匹配')
            blocks = []
            for offset in range(0, len(target), 4096):
                data = target[offset:offset + 4096]
                if original[offset:offset + len(data)] != data:
                    blocks.append(dict(offset=offset, data=base64.b64encode(data).decode()))
            patches[path] = dict(source_bytes=len(original), source_sha256=hashlib.sha256(original).hexdigest(),
                target_bytes=len(target), target_sha256=entries[path]['sha256'], blocks=blocks)
    if digest(manifest_file) != item['manifest_sha256']:
        raise ValueError('OCR文件清单不匹配')
    compatibility = directory / 'ocr-compatibility.json'
    compatibility.write_text(json.dumps(dict(version=1, files=patches), separators=(',', ':')) + '\n')
    artifacts = []
    for name, version in item['dependencies'].items():
        wheel = wheel_audit[name]
        if not wheel.get('download_verified') or wheel['version'] != version or digest(cache / wheel['filename']) != wheel['sha256']:
            raise ValueError('依赖未验证：' + name)
        artifact = dict(kind='wheel', title=name, version=version, filename=wheel['filename'],
            bytes=wheel['bytes'], sha256=wheel['sha256'],
            urls=[mirror['url'] for mirror in wheel['mirrors'] if mirror.get('listed')])
        if name == 'paddlepaddle':
            artifact['record_additions'] = ['paddle/libs/' + library for library in GCC_LIBS]
            artifact['replaced_files'] = ['paddle/libs/' + library for library in GCC_LIBS]
        artifacts.append(artifact)
    artifacts.append(dict(kind='tar', title='Mac 运行支持组件', version='16.2.0', filename='gcc.tar.gz',
        bytes=gcc['bytes'], sha256=gcc['sha256'], urls=gcc['urls'], members=[
            dict(member='gcc/16.2.0/lib/gcc/current/' + name, path='packages/paddle/libs/' + name) for name in GCC_LIBS]))
    for model in json.loads((source_audit / 'model-audit.json').read_text()):
        if model['path'] not in {'ocr-det.tar', 'ocr-rec.tar'}:
            continue
        file = cache / model['path']
        if not model['verified'] or digest(file) != model['sha256']:
            raise ValueError('OCR模型未验证')
        members = []
        with tarfile.open(file) as source:
            for entry in source:
                target = 'ocr-models/' + str(PurePosixPath(entry.name))
                if entry.isfile() and target in entries:
                    members.append(dict(member=str(PurePosixPath(entry.name)), path=target))
        if len(members) != 3:
            raise ValueError('OCR模型清单不完整')
        artifacts.append(dict(kind='tar', title='文字检测模型' if model['path'] == 'ocr-det.tar' else '文字识别模型',
            version='PP-OCRv6 Small', filename=model['path'], bytes=model['bytes'], sha256=model['sha256'],
            urls=[model['url']], members=members))
    priority = {'paddleocr': 0, 'paddlepaddle': 1, 'opencv-contrib-python': 2, 'numpy': 3, 'paddlex': 4,
                'Mac 运行支持组件': 5, '文字检测模型': 6, '文字识别模型': 7}
    artifacts.sort(key=lambda value: (priority.get(value['title'], 8), value['title']))
    item['dependency_download'] = dict(version=1, manifest=manifest_file.relative_to(runtime).as_posix(),
        compatibility=dict(file=compatibility.relative_to(runtime).as_posix(), sha256=digest(compatibility)), artifacts=artifacts)
    item['online_reason'] = ''
    catalog_file.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n')
    print('PASS: OCR plan:', len(artifacts), 'public downloads; compatibility bytes:', compatibility.stat().st_size)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--source-audit', type=Path, required=True)
    parser.add_argument('--gcc-audit', type=Path, required=True)
    parser.add_argument('--artifacts', type=Path)
    parser.add_argument('--cache', type=Path, help='Combined verified wheel and archive cache')
    args = parser.parse_args()
    prepare(args.runtime, args.source_audit, args.gcc_audit, args.artifacts, args.cache)

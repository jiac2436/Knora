"""Attach verified public download recipes to a newly split local candidate."""
import argparse
import json
from pathlib import Path

from build_runtime import CONFIG, digest, tree_manifest
from prepare_dependency_downloads import prepare as prepare_standard
from prepare_ocr_downloads import prepare as prepare_ocr
from verify_runtime import verify


def finalize(runtime, artifacts, inputs, wheels, audit):
    result_path = artifacts / 'split-result.json'
    result = json.loads(result_path.read_text())
    verify(runtime, result['manifest_sha256'])
    configuration = json.loads((CONFIG / 'artifacts.json').read_text())
    models, gcc = [], None
    for item in configuration['artifacts']:
        source = inputs / item['file']
        if source.stat().st_size != item['bytes'] or digest(source) != item['sha256']:
            raise ValueError('构建输入校验失败：' + item['file'])
        if item.get('install_path', '').startswith('model/') or item['file'] in {'ocr-det.tar', 'ocr-rec.tar'}:
            models.append(dict(path=item.get('install_path', item['file']), url=item['url'],
                bytes=item['bytes'], sha256=item['sha256'], verified=True))
        if item['file'] == 'gcc.tar.gz':
            gcc = {**item, 'verified': True}
        if item['file'] in {'gcc.tar.gz', 'ocr-det.tar', 'ocr-rec.tar'}:
            target = wheels / item['file']
            if not target.exists():
                target.hardlink_to(source)
            if digest(target) != item['sha256']:
                raise ValueError('组件缓存不匹配')
    if not gcc or not models:
        raise ValueError('缺少模型或原生运行库来源')
    audit.mkdir(parents=True, exist_ok=True)
    (audit / 'model-audit.json').write_text(json.dumps(models, indent=2) + '\n')
    (audit / 'gcc-source.json').write_text(json.dumps(gcc, indent=2) + '\n')
    prepare_standard(runtime, audit, artifacts)
    prepare_ocr(runtime, audit, audit / 'gcc-source.json', artifacts, wheels)
    path = runtime / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['files'] = [entry for entry in tree_manifest(runtime) if entry['path'] != 'manifest.json']
    path.write_text(json.dumps(manifest, indent=2) + '\n')
    checksum = digest(path)
    count = verify(runtime, checksum)
    result.update(manifest_sha256=checksum, runtime_files=count,
        base_bytes=sum(p.stat().st_size for p in runtime.rglob('*') if p.is_file()),
        catalog=json.loads((runtime / 'extension-catalog.json').read_text()))
    result_path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(f'Final runtime: {count} files, manifest SHA-256 {checksum}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['runtime', 'artifacts', 'inputs', 'wheels', 'audit']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    finalize(**{name: getattr(args, name).resolve() for name in ['runtime', 'artifacts', 'inputs', 'wheels', 'audit']})

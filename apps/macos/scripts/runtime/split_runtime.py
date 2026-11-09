"""Split a verified full runtime into a base plus hash-pinned offline packages.

Uses only the already tested dependency versions, including extras. This is a
repackaging build, not a dependency resolver and never invokes pip or a network.
"""
from __future__ import annotations

import argparse
from importlib.metadata import distributions
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from build_runtime import digest, tree_manifest
from verify_runtime import verify
from license_inventory import write_inventory

REPO = Path(__file__).resolve().parents[4]
TITLES = {'semantic': ('语义搜索', '按含义检索原文和已生成的摘要，在本机运行'),
          'ocr': ('文字识别', '识别扫描版 PDF 的文字，在本机运行'),
          'documents': ('文档解析', '解析 Office 等文件及复杂 PDF 版面，在本机运行')}


def dependency_groups(site, roots, python_version=None):
    installed = {canonicalize_name(d.metadata['Name']): d for d in distributions(path=[str(site)])
                 if d.metadata.get('Name')}
    environment = default_environment()
    python_version = python_version or json.loads((REPO / 'apps/macos/runtime/artifacts.json').read_text())['python']
    python_minor = '.'.join(python_version.split('.')[:2])
    environment.update(python_version=python_minor, python_full_version=python_version,
        sys_platform='darwin', platform_system='Darwin', platform_machine='arm64')
    def closure(requested):
        seen, pending = {}, [Requirement(name) for name in requested]
        while pending:
            requirement = pending.pop()
            name = canonicalize_name(requirement.name)
            extras = {'', *requirement.extras}
            if name in seen and extras <= seen[name]:
                continue
            extras |= seen.get(name, set())
            seen[name] = extras
            if name not in installed or not requirement.specifier.contains(installed[name].version):
                raise ValueError(f'缺少经过测试的依赖：{requirement}')
            for line in installed[name].requires or []:
                child = Requirement(line)
                if child.marker is None or any(child.marker.evaluate({**environment, 'extra': extra}) for extra in extras):
                    pending.append(child)
        return set(seen)
    return installed, {key: closure(names) for key, names in roots.items() if key != 'version'}


def copy_distributions(site, destination, installed, names):
    for name in sorted(names):
        for entry in installed[name].files or []:
            source = Path(installed[name].locate_file(entry))
            # Console entry points were removed by the full-runtime builder.
            if not source.is_file() or not source.resolve().is_relative_to(site.resolve()):
                continue
            target = destination / source.relative_to(site)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                if digest(target) != digest(source):
                    raise ValueError(f'依赖文件冲突：{target.name}')
            else:
                shutil.copy2(source, target, follow_symlinks=True)


def build(source, checksum, output, backend_source):
    count = verify(source, checksum)
    if output.exists():
        raise ValueError('输出目录已存在，不能覆盖')
    inputs = json.loads((source / 'build-inputs.json').read_text())
    python_minor = '.'.join(inputs['python'].split('.')[:2])
    source_site = source / 'python/lib' / f'python{python_minor}' / 'site-packages'
    roots = json.loads((REPO / 'apps/macos/runtime/capability-roots.json').read_text())
    installed, groups = dependency_groups(source_site, roots, inputs['python'])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.split-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'output'
        stage.mkdir()
        base = stage / 'base'
        subprocess.run(['cp', '-cR', str(source), str(base)], check=True)
        for name in ['model', 'ocr-models', 'manifest.json']:
            path = base / name
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        site = base / 'python/lib' / f'python{python_minor}' / 'site-packages'
        shutil.rmtree(site)
        site.mkdir()
        copy_distributions(source_site, site, installed, groups['base'])
        write_inventory(source_site, base / 'dependency-inventory.json', installed, groups['base'])
        inventories = base / 'dependency-inventories'
        inventories.mkdir()
        # Only consume an explicitly frozen, reviewed backend, never a live
        # shared working tree that may contain concurrent features.
        if not (backend_source / 'app/runtime_extensions.py').is_file():
            raise ValueError('快照缺少扩展能力实现')
        shutil.rmtree(base / 'backend')
        shutil.copytree(backend_source, base / 'backend')
        catalog = {'version': 1, 'python': python_minor, 'architecture': 'arm64', 'packages': {}}
        for key in TITLES:
            package = Path(temporary) / key
            packages = package / 'packages'
            packages.mkdir(parents=True)
            copy_distributions(source_site, packages, installed, groups[key] - groups['base'])
            write_inventory(source_site, inventories / f'{key}.json', installed, groups[key] - groups['base'])
            if key == 'semantic':
                shutil.copytree(source / 'model', package / 'model')
            if key == 'ocr':
                shutil.copytree(source / 'ocr-models', package / 'ocr-models')
            manifest = {'version': 1, 'id': key, 'files': tree_manifest(package)}
            if any('symlink' in entry for entry in manifest['files']):
                raise ValueError('扩展包须包含实际文件，不允许符号链接')
            (package / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
            filename = f'{key}-1.1.0-macos-arm64.pkaext'
            archive = stage / filename
            with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
                for path in sorted(package.rglob('*')):
                    if path.is_file():
                        target.write(path, path.relative_to(package).as_posix())
            catalog['packages'][key] = {'id': key, 'title': TITLES[key][0], 'summary': TITLES[key][1],
                'version': '1.1.0', 'filename': filename, 'bytes': archive.stat().st_size,
                'installed_bytes': sum(p.stat().st_size for p in package.rglob('*') if p.is_file()),
                'sha256': digest(archive), 'manifest_sha256': digest(package / 'manifest.json'),
                'dependencies': {name: installed[name].version for name in sorted(groups[key] - groups['base'])}}
            print(f'Built {key}: {archive.stat().st_size / 1e6:.1f} MB download', flush=True)
        (base / 'extension-catalog.json').write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + '\n')
        (base / 'capability-roots.json').write_text(json.dumps(roots, indent=2) + '\n')
        source_manifest = json.loads((source / 'manifest.json').read_text())
        source_manifest['files'] = tree_manifest(base)
        source_manifest['profile'] = 'base-with-cloud-ai'
        source_manifest['split_source_manifest_sha256'] = checksum
        source_manifest['scope'] = 'Base runtime with optional offline capability packages; commercial distribution signing is separate'
        (base / 'manifest.json').write_text(json.dumps(source_manifest, ensure_ascii=False, indent=2) + '\n')
        manifest_sha = digest(base / 'manifest.json')
        files = verify(base, manifest_sha)
        summary = {'manifest_sha256': manifest_sha, 'runtime_files': files, 'source_runtime_files': count,
                   'base_bytes': sum(p.stat().st_size for p in base.rglob('*') if p.is_file()),
                   'base_dependencies': {name: installed[name].version for name in sorted(groups['base'])},
                   'catalog': catalog}
        (stage / 'split-result.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
        stage.rename(output)
    print(json.dumps({'output': str(output), 'manifest_sha256': manifest_sha, 'base_MB': round(summary['base_bytes'] / 1e6, 2)}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--backend-source', type=Path, required=True)
    args = parser.parse_args()
    build(args.source.resolve(), args.manifest_sha256, args.output.resolve(), args.backend_source.resolve())

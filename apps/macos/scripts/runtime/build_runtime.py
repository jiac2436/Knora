"""Build an offline L0 runtime from pinned archives and a warmed uv cache."""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tarfile
import tempfile

from vendor_paddle import GCC_LIBS

REPO = Path(__file__).resolve().parents[4]
CONFIG = REPO / 'apps/macos/runtime'


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def extract(archive: Path, prefix: str, destination: Path, names: set[str] | None = None) -> None:
    """Extract only one archive prefix, refusing traversal and special entries."""
    destination.mkdir(parents=True, exist_ok=True)
    links = []
    size = 0
    seen = set()
    with tarfile.open(archive) as source:
        for item in source:
            path = PurePosixPath(item.name)
            if path.is_absolute() or '..' in path.parts:
                raise ValueError('unsafe archive path')
            try:
                relative = path.relative_to(prefix)
            except ValueError:
                continue
            if not relative.parts:
                continue
            if names is not None and str(relative) not in names:
                continue
            if str(relative) in seen:
                raise ValueError('duplicate archive entry')
            seen.add(str(relative))
            target = destination.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(destination.resolve()):
                raise ValueError('archive path escapes destination')
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.isdir():
                target.mkdir(exist_ok=True)
            elif item.isfile():
                size += item.size
                if size > 2 * 1024 ** 3:
                    raise ValueError('archive exceeds extraction budget')
                with source.extractfile(item) as data, target.open('xb') as out:
                    shutil.copyfileobj(data, out)
                target.chmod(item.mode & 0o777)
            elif item.issym():
                link = PurePosixPath(item.linkname)
                if link.is_absolute() or not (target.parent / item.linkname).resolve().is_relative_to(destination.resolve()):
                    raise ValueError('external archive symlink')
                links.append((target, item.linkname))
            else:
                raise ValueError('unsupported archive entry')
    for target, link in links:
        target.symlink_to(link)
    if names is not None and seen != names:
        raise ValueError('archive missing required runtime libraries')


def normalize_installation(root: Path, packages: Path) -> None:
    # uv interpreter discovery can generate bytecode containing the staging path.
    for path in sorted(root.rglob('__pycache__'), reverse=True):
        if path.is_dir():
            shutil.rmtree(path)
    # Installer cache keys describe this build machine, not the distribution.
    # They are not wheel inputs and cannot be reproduced by the public-source
    # installer. Omit them before rewriting RECORD and package manifests.
    for path in packages.glob('*.dist-info/uv_cache.json'):
        path.unlink()
    for record in sorted(packages.glob('*.dist-info/RECORD')):
        with record.open(newline='') as source:
            names = {row[0] for row in csv.reader(source) if row}
        if record.parent.name.startswith('paddlepaddle-'):
            names.update('paddle/libs/' + name for name in GCC_LIBS)
        rows = []
        for name in sorted(names):
            path = packages / name
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError('distribution RECORD escapes payload')
            if not path.is_file():
                continue
            if path == record:
                rows.append([name, '', ''])
            else:
                encoded = base64.urlsafe_b64encode(bytes.fromhex(digest(path))).decode().rstrip('=')
                rows.append([name, 'sha256=' + encoded, str(path.stat().st_size)])
        with record.open('w', newline='') as output:
            csv.writer(output, lineterminator='\n').writerows(rows)


def tree_manifest(root: Path) -> list[dict]:
    files = []
    for path in sorted(root.rglob('*')):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            if not path.resolve().is_relative_to(root.resolve()) or not path.exists():
                raise ValueError('external or broken output symlink')
            files.append({'path': relative, 'symlink': os.readlink(path)})
        elif path.is_file():
            files.append({'path': relative, 'sha256': digest(path), 'bytes': path.stat().st_size,
                          'mode': path.stat().st_mode & 0o777})
    return files


def validate_wheelhouse(wheelhouse: Path, lock: Path) -> None:
    if not wheelhouse.is_dir() or wheelhouse.is_symlink():
        raise ValueError('wheelhouse must be a real directory')
    locked = lock.read_text()
    for wheel in wheelhouse.glob('*.whl'):
        if wheel.is_symlink() or 'sha256:' + digest(wheel) not in locked:
            raise ValueError('wheelhouse contains an untrusted wheel')


def build(inputs: Path, cache: Path, output: Path, uv: Path, wheelhouse: Path | None = None) -> None:
    config = json.loads((CONFIG / 'artifacts.json').read_text())
    lock = CONFIG / 'requirements-macos-arm64.lock'
    if config['schema_version'] != 1 or digest(lock) != config['lock_sha256']:
        raise ValueError('build configuration or lock checksum mismatch')
    if output.exists():
        raise ValueError('output already exists')
    for artifact in config['artifacts']:
        name = artifact['file']
        if Path(name).name != name:
            raise ValueError('invalid artifact name')
        path = inputs / name
        if not path.is_file() or path.stat().st_size != artifact['bytes'] or digest(path) != artifact['sha256']:
            raise ValueError(f'artifact checksum mismatch: {name}')
    version = subprocess.check_output([str(uv), '--version'], text=True).split()[1]
    if version != config['uv']:
        raise ValueError('uv version does not match build inputs')
    output.parent.mkdir(parents=True, exist_ok=True)
    # Only this temporary staging tree is removed on failure; existing outputs are untouched.
    with tempfile.TemporaryDirectory(prefix='.pka-runtime-build-', dir=output.parent) as temp:
        stage = Path(temp) / 'payload'
        stage.mkdir()
        extract(inputs / 'python.tar.gz', 'python', stage / 'python')
        python = stage / 'python/bin/python3'
        env = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': temp, 'TMPDIR': temp,
               'PYTHONDONTWRITEBYTECODE': '1'}
        version = subprocess.check_output([str(python), '-I', '-B', '-c',
                                          'import platform; print(platform.python_version())'], text=True, env=env).strip()
        if version != config['python']:
            raise ValueError('interpreter version mismatch')
        python_minor = '.'.join(config['python'].split('.')[:2])
        packages = stage / 'python/lib' / f'python{python_minor}' / 'site-packages'
        shutil.rmtree(packages)
        sources = ['--default-index', 'https://pypi.org/simple']
        if wheelhouse is not None:
            validate_wheelhouse(wheelhouse, lock)
            sources = ['--no-index', '--find-links', str(wheelhouse)]
        subprocess.run([str(uv), '--cache-dir', str(cache), 'pip', 'install', '--offline',
                        '--no-config', *sources,
                        '--python', str(python), '--target', str(packages),
                        '--require-hashes', '--no-deps', '--only-binary', ':all:',
                        '--requirement', str(lock)], check=True, env=env)
        for artifact in config['artifacts']:
            if 'install_path' not in artifact:
                continue
            relative = PurePosixPath(artifact['install_path'])
            if relative.is_absolute() or '..' in relative.parts or relative.parts[0] != 'model':
                raise ValueError('invalid model input path')
            target = stage.joinpath(*relative.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(inputs / artifact['file'], target)
        for name, archive in [('det', 'ocr-det.tar'), ('rec', 'ocr-rec.tar')]:
            prefix = f'PP-OCRv6_small_{name}_infer'
            extract(inputs / archive, prefix, stage / 'ocr-models' / prefix)
        gcc = Path(temp) / 'gcc'
        extract(inputs / 'gcc.tar.gz', 'gcc/16.2.0/lib/gcc/current', gcc, set(GCC_LIBS))
        shutil.copytree(REPO / 'third_party', stage / 'licenses')
        shutil.copyfile(REPO / 'LICENSE', stage / 'licenses/GPL-3.0.txt')
        extract(inputs / 'gcc.tar.gz', 'gcc/16.2.0', stage / 'licenses/gcc',
                {'COPYING', 'COPYING.LIB', 'COPYING.RUNTIME'})
        shutil.copyfile(REPO / 'LICENSE', stage / 'licenses/gcc/COPYING3')
        (stage / 'poc.json').write_text(json.dumps({
            'purpose': 'L0 relocation experiment; not a distribution artifact',
            'dependencies': 'offline install from hash-locked public wheels',
        }, indent=2) + '\n')
        subprocess.run([str(python), '-I', '-B', str(Path(__file__).with_name('vendor_paddle.py')),
                        '--candidate', str(stage), '--gcc-runtime', str(gcc)], check=True, env=env)
        # Console scripts generated during installation embed staging paths.
        # Runtime services use python -m / explicit modules, so only Python launchers remain.
        for path in (stage / 'python/bin').iterdir():
            if path.name not in {'python', 'python3', f'python{python_minor}'}:
                path.unlink()
        shutil.rmtree(packages / 'bin', ignore_errors=True)
        for name in ['probe.py', 'native_inventory.py', 'verify_runtime.py']:
            shutil.copyfile(Path(__file__).with_name(name), stage / name)
        shutil.copyfile(lock, stage / 'requirements.lock')
        shutil.copyfile(CONFIG / 'artifacts.json', stage / 'build-inputs.json')
        # Bundle source only; databases, uploads, tests and environment files are excluded.
        for source, target in [(REPO / 'services/api/app', stage / 'backend/app'),
                               (REPO / 'services/worker', stage / 'backend/services/worker'),
                               (REPO / 'services/ai_gateway', stage / 'backend/services/ai_gateway')]:
            for path in source.rglob('*.py'):
                if '__pycache__' in path.parts:
                    continue
                dest = target / path.relative_to(source)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, dest)
        shutil.copyfile(REPO / 'skills/personal-knowledge/SKILL.md', stage / 'backend/app/agent_skill.md')
        shutil.copyfile(REPO / 'skills/personal-knowledge/agents/openai.yaml', stage / 'backend/app/agent_skill.yaml')
        subprocess.run(['bash', str(Path(__file__).with_name('build_platform_helper.sh')),
                        str(stage / 'platform/macos/KnowledgePlatform')], check=True)
        normalize_installation(stage / 'python', packages)
        inventory = tree_manifest(stage)
        (stage / 'manifest.json').write_text(json.dumps({
            'schema_version': 1, 'target': config['target'], 'files': inventory,
            'lock_sha256': config['lock_sha256'], 'uv': config['uv'],
            'build_scripts_sha256': {p.name: digest(p) for p in sorted(Path(__file__).parent.iterdir())
                if p.suffix in {'.py', '.sh', '.swift'} and not p.name.startswith('test_')},
            'scope': 'Hash-locked full runtime input for the lightweight base and optional extension split',
        }, indent=2) + '\n')
        if output.exists():
            raise ValueError('output appeared while building')
        stage.rename(output)
    print(f'Runtime payload: {output}')


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, required=True)
    parser.add_argument('--uv-cache', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--uv', type=Path, required=True)
    parser.add_argument('--wheelhouse', type=Path, help='Use only hash-locked local wheels, without an index')
    args = parser.parse_args()
    try:
        build(args.inputs.resolve(), args.uv_cache.resolve(), args.output.resolve(), args.uv.resolve(),
              args.wheelhouse.resolve() if args.wheelhouse else None)
    except (ValueError, OSError, subprocess.CalledProcessError, tarfile.TarError) as exc:
        parser.exit(1, f'Build failed: {exc}\n')


if __name__ == '__main__':
    main()

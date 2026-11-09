"""Vendor GCC runtime into a marked L0 candidate; never mutate development packages."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

GCC_LIBS = ('libgfortran.5.dylib', 'libgomp.1.dylib', 'libquadmath.0.dylib', 'libgcc_s.1.1.dylib')


def load_paths(path: Path) -> list[str]:
    output = subprocess.check_output(['/usr/bin/otool', '-L', str(path)], text=True)
    return [line.strip().split(' (', 1)[0] for line in output.splitlines()[1:]
            if 'compatibility version' in line]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, required=True)
    parser.add_argument('--gcc-runtime', type=Path, required=True)
    args = parser.parse_args()
    candidate = args.candidate.resolve()
    marker = candidate / 'poc.json'
    if not marker.is_file() or json.loads(marker.read_text()).get('purpose') != 'L0 relocation experiment; not a distribution artifact':
        parser.error('requires a marked L0 candidate')
    targets = list((candidate / 'python/lib').glob('python*/site-packages/paddle/libs'))
    if len(targets) != 1 or not targets[0].resolve().is_relative_to(candidate):
        parser.error('one contained Paddle libs directory required')
    target = targets[0]
    source = args.gcc_runtime.resolve()
    # Validate all inputs before touching the experimental candidate.
    for name in GCC_LIBS:
        path = source / name
        if not path.is_file():
            parser.error(f'missing runtime library: {name}')
        architectures = subprocess.check_output(['/usr/bin/lipo', '-archs', str(path)], text=True).split()
        if 'arm64' not in architectures:
            parser.error(f'arm64 missing: {name}')
    for path in target.glob('*.dylib'):
        if not path.resolve().is_relative_to(candidate):
            parser.error('candidate contains an external library symlink')
    changed = []
    for name in GCC_LIBS:
        shutil.copyfile(source / name, target / name)
    for path in sorted(target.glob('*.dylib')):
        changes = []
        for old in load_paths(path):
            name = Path(old).name
            if name in GCC_LIBS and not old.startswith(('/usr/lib/', '/System/Library/')):
                changes.extend(['-change', old, '@loader_path/' + name])
        if path.name in GCC_LIBS:
            changes.extend(['-id', '@loader_path/' + path.name])
        if changes:
            subprocess.run(['/usr/bin/install_name_tool', *changes, str(path)], check=True, capture_output=True)
            subprocess.run(['/usr/bin/codesign', '--force', '--sign', '-', str(path)], check=True, capture_output=True)
            subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(path)], check=True, capture_output=True)
            changed.append(path.name)
    (candidate / 'vendored-gcc.json').write_text(json.dumps({
        'source_sha256': {name: hashlib.sha256((source / name).read_bytes()).hexdigest() for name in GCC_LIBS},
        'changed_and_adhoc_signed': changed,
        'scope': 'arm64 L0 experiment; distribution signing and license delivery pending',
    }, indent=2) + '\n')
    print('Vendored and signed:', ', '.join(changed))


if __name__ == '__main__':
    main()

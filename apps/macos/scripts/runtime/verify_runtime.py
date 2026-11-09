"""Verify a runtime tree against a separately trusted manifest digest."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def verify(root: Path, expected: str) -> int:
    manifest = root / 'manifest.json'
    if manifest.is_symlink() or digest(manifest) != expected:
        raise ValueError('manifest checksum mismatch')
    data = json.loads(manifest.read_text())
    if data['schema_version'] != 1:
        raise ValueError('unsupported manifest')
    paths = set()
    for item in data['files']:
        relative = PurePosixPath(item['path'])
        if relative.is_absolute() or '..' in relative.parts or not relative.parts or str(relative) in paths:
            raise ValueError('unsafe or duplicate manifest path')
        paths.add(str(relative))
        path = root.joinpath(*relative.parts)
        if not path.resolve().is_relative_to(root.resolve()):
            raise ValueError('external manifest path')
        if 'symlink' in item:
            if not path.is_symlink() or os.readlink(path) != item['symlink'] or not path.exists():
                raise ValueError(f'symlink mismatch: {relative}')
        elif path.is_symlink() or not path.is_file() or path.stat().st_size != item['bytes'] or digest(path) != item['sha256'] or path.stat().st_mode & 0o777 != item['mode']:
            raise ValueError(f'file mismatch: {relative}')
    actual = {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != paths | {'manifest.json'}:
        raise ValueError('unrecorded or missing files')
    return len(paths)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('--manifest-sha256', required=True)
    args = parser.parse_args()
    try:
        count = verify(args.runtime.resolve(), args.manifest_sha256)
    except (ValueError, KeyError, OSError) as exc:
        parser.exit(1, f'Runtime verification failed: {exc}\n')
    print(f'PASS: {count} runtime files and links verified')


if __name__ == '__main__':
    main()

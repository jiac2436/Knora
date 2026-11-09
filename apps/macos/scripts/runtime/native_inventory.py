"""Inventory macOS runtime library load paths; not a signature or rpath validator."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    root = args.runtime.resolve()
    if not root.is_dir():
        parser.error('runtime directory missing')
    findings = {}
    count = 0
    for path in root.rglob('*'):
        if not path.is_file() or (path.suffix not in {'.so', '.dylib'} and path.parent != root / 'bin'):
            continue
        result = subprocess.run(['/usr/bin/otool', '-L', str(path)], capture_output=True, text=True)
        if result.returncode:
            continue
        count += 1
        ids = subprocess.run(['/usr/bin/otool', '-D', str(path)], capture_output=True, text=True)
        own_ids = set(ids.stdout.splitlines()[1:])
        dependencies = [line.strip().split(' (', 1)[0] for line in result.stdout.splitlines()[1:]]
        external = [dep for dep in dependencies if dep not in own_ids and dep.startswith('/')
                    and not dep.startswith(('/usr/lib/', '/System/Library/', str(root) + '/'))]
        if external:
            findings[str(path.relative_to(root))] = external
    report = {'native_files': count, 'external_absolute_load_paths': findings,
              'bytes_excluding_symlinks': sum(p.stat().st_size for p in root.rglob('*')
                                              if p.is_file() and not p.is_symlink()),
              'limits': ['LC_ID_DYLIB excluded; @rpath resolution and signing not verified']}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()

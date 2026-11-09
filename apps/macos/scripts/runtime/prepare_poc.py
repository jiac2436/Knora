"""Prepare an experimental runtime, never a release bundle or a copied virtualenv."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--standalone', type=Path, required=True)
    parser.add_argument('--site-packages', type=Path, required=True)
    parser.add_argument('--model', type=Path, required=True)
    parser.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    runtime = args.standalone.resolve()
    packages = args.site_packages.resolve()
    model = args.model.resolve()
    destination = args.destination.resolve()
    for source in (runtime, packages, model):
        if not source.is_dir():
            parser.error(f'missing input directory: {source}')
        if destination == source or source in destination.parents:
            parser.error('destination must be outside inputs')
    if (runtime / 'pyvenv.cfg').exists():
        parser.error('standalone input cannot be a virtualenv')
    python = runtime / 'bin/python3'
    if not python.is_file():
        parser.error('standalone runtime requires bin/python3')
    version = subprocess.check_output(
        [str(python), '-I', '-c', 'import sys; print(f"python{sys.version_info.major}.{sys.version_info.minor}")'],
        text=True,
    ).strip()
    if packages.parent.name != version:
        parser.error('dependency Python minor version must match runtime')
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copytree(runtime, destination / 'python', symlinks=True)
    target = destination / 'python/lib' / version / 'site-packages'
    # Do not merge versions of bootstrap setuptools/pip with the tested packages.
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(packages, target, symlinks=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    shutil.copytree(model, destination / 'model', symlinks=False)
    shutil.copyfile(Path(__file__).with_name('probe.py'), destination / 'probe.py')
    # A symlink back to the original machine invalidates relocation evidence.
    for path in destination.rglob('*'):
        if path.is_symlink() and not path.resolve().is_relative_to(destination):
            raise RuntimeError(f'external symlink in candidate: {path.relative_to(destination)}')
    (destination / 'poc.json').write_text(json.dumps({
        'purpose': 'L0 relocation experiment; not a distribution artifact',
        'python_abi': version,
        'dependencies': 'installed development packages, not a reproducible release lock',
    }, indent=2) + '\n')
    print(destination)


if __name__ == '__main__':
    main()

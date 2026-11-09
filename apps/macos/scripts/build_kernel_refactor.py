"""Assemble M5 from frozen M4 sources and verified, unchanged runtime dependencies.

Creates a candidate only. Full packaged and real-provider acceptance must finish
before the ordinary publisher can promote it to the unique formal application.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(Path(__file__).parent / 'runtime'))
from build_runtime import tree_manifest
from verify_runtime import digest, verify


def frozen_sources(record):
    for group in ('source_sha256', 'build_and_test_sha256'):
        for name, checksum in record[group].items():
            if digest(ROOT / name) != checksum:
                raise ValueError('M4 frozen source changed: ' + name)


def build(output):
    baseline = json.loads((ROOT / 'docs/verification/kernel-refactor-20261020/execution-m4.json').read_text())
    frozen_sources(baseline)
    release_path = ROOT / 'apps/macos/release.json'
    if digest(release_path) != baseline['release_sha256']:
        raise ValueError('Formal release changed since M4')
    release = json.loads(release_path.read_text())
    application = ROOT / release['app']
    if digest(application / 'Contents/MacOS/KnowledgeInbox') != baseline['formal_binary_sha256']:
        raise ValueError('Formal native binary changed since M4')
    parent = application / 'Contents/Resources/runtime'
    verify(parent, release['manifest_sha256'])
    output = output.absolute()
    allowed = ROOT / 'apps/macos/.build/runtime'
    if not output.is_relative_to(allowed) or output.exists() or output.is_symlink():
        raise ValueError('Use a new runtime directory inside apps/macos/.build/runtime')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.m5-assemble-', dir=output.parent) as temporary:
        stage = Path(temporary) / 'payload'
        subprocess.run(['cp', '-cR', str(parent), str(stage)], check=True)
        (stage / 'manifest.json').unlink()
        shutil.rmtree(stage / 'backend')
        for source, destination in [('services/api/app', 'backend/app'),
                                    ('services/worker', 'backend/services/worker'),
                                    ('services/ai_gateway', 'backend/services/ai_gateway')]:
            for path in sorted((ROOT / source).rglob('*.py')):
                target = stage / destination / path.relative_to(ROOT / source)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        for source, name in [('skills/personal-knowledge/SKILL.md', 'agent_skill.md'),
                             ('skills/personal-knowledge/agents/openai.yaml', 'agent_skill.yaml')]:
            shutil.copyfile(ROOT / source, stage / 'backend/app' / name)
        subprocess.run(['bash', str(Path(__file__).parent / 'runtime/build_platform_helper.sh'),
                        str(stage / 'platform/macos/KnowledgePlatform')], check=True)
        frozen_sources(baseline)
        manifest = json.loads((parent / 'manifest.json').read_text())
        manifest.update(parent_manifest_sha256=release['manifest_sha256'],
            scope='M0-M4 shared kernel; candidate pending M5 package and provider acceptance',
            source_sha256=baseline['source_sha256'],
            assembly_script_sha256=digest(Path(__file__)),
            build_scripts_sha256={p.name: digest(p) for p in sorted((Path(__file__).parent / 'runtime').iterdir())
                                  if p.suffix in {'.py', '.sh', '.swift'} and not p.name.startswith('test_')},
            files=tree_manifest(stage))
        (stage / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + '\n')
        checksum = digest(stage / 'manifest.json')
        count = verify(stage, checksum)
        if output.exists():
            raise ValueError('Output appeared during assembly')
        stage.rename(output)
    print(json.dumps({'runtime': str(output.relative_to(ROOT)), 'manifest_sha256': checksum,
                      'runtime_files': count}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    build(parser.parse_args().output)

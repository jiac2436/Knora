"""Refresh only the authorized backend into an independently verified base tree."""
from pathlib import Path
import hashlib
import json
import shutil
import sys

ROOT = Path(__file__).resolve().parents[3]
BASE = ROOT / 'apps/macos/.build/lightweight-runtime-v1/base'
sys.path.insert(0, str(ROOT / 'apps/macos/scripts/runtime'))
from build_runtime import tree_manifest, digest
from verify_runtime import verify

release = json.loads((ROOT / 'apps/macos/release.json').read_text())
record = json.loads((ROOT / release['source_record']).read_text())
allowed = {'config.py', 'local_host.py', 'semantic.py', 'domain_node_index.py',
           'summary_retrieval.py', 'domain_retrieval.py', 'model_configuration.py',
           'services.py', 'rag.py', 'capture_queue.py', 'main.py', 'files.py', 'mcp_server.py',
           'pdf_ocr.py', 'anydoc_reader.py', 'runtime_extensions.py', 'extension_install.py'}
sources = {}
excluded = []
original_backend = ROOT / release['app'] / 'Contents/Resources/runtime/backend'
# Start from the exact verified release, preserving shared-workspace development.
shutil.rmtree(BASE / 'backend')
shutil.copytree(original_backend, BASE / 'backend')
for original, relative in [('services/api/app', 'backend/app'), ('services/worker', 'backend/services/worker'),
                           ('services/ai_gateway', 'backend/services/ai_gateway')]:
    for path in (ROOT / original).rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        key = path.relative_to(ROOT).as_posix()
        current = digest(path)
        target = BASE / relative / path.relative_to(ROOT / original)
        if current != record['backend_sources'].get(key) and path.name not in allowed:
            excluded.append(key)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
for key, wanted in record['backend_sources'].items():
    path = Path(key)
    if path.parts[1] == 'api':
        target = BASE / 'backend' / Path(*path.parts[2:])
    else:
        target = BASE / 'backend/services' / Path(*path.parts[1:])
    if target.exists():
        sources[key] = digest(target)
for original, relative in [('services/api/app', 'backend/app'), ('services/worker', 'backend/services/worker'),
                           ('services/ai_gateway', 'backend/services/ai_gateway')]:
    for path in (ROOT / original).rglob('*.py'):
        key = path.relative_to(ROOT).as_posix()
        if '__pycache__' not in path.parts and key not in excluded:
            sources[key] = digest(BASE / relative / path.relative_to(ROOT / original))
(Path(__file__).parent / 'excluded-concurrent.json').write_text(json.dumps(excluded, ensure_ascii=False, indent=2) + '\n')
manifest = BASE / 'manifest.json'
value = json.loads(manifest.read_text())
value['files'] = [item for item in tree_manifest(BASE) if item['path'] != 'manifest.json']
manifest.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
summary = BASE.parent / 'split-result.json'
result = json.loads(summary.read_text())
result.update(manifest_sha256=digest(manifest), runtime_files=verify(BASE, digest(manifest)),
              base_bytes=sum(p.stat().st_size for p in BASE.rglob('*') if p.is_file()))
summary.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
(Path(__file__).parent / 'backend-snapshot.json').write_text(json.dumps(sources, ensure_ascii=False, indent=2) + '\n')
print('PASS: source-checked base snapshot', result['manifest_sha256'])

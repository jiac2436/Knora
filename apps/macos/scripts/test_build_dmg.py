"""DMG resource boundaries checked against synthetic bundles, without mounting."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import build_dmg


def candidate(tmp_path):
    app = tmp_path / 'Knora.app'
    native = app / 'Contents/MacOS/KnowledgeInbox'
    native.parent.mkdir(parents=True); native.write_bytes(b'synthetic native binary')
    resources = app / 'Contents/Resources'
    runtime = resources / 'runtime'; runtime.mkdir(parents=True)
    (resources / 'Capture').mkdir(); (resources / 'AppIcon.icns').write_bytes(b'synthetic icon')
    manifest = runtime / 'manifest.json'
    manifest.write_text(json.dumps(dict(schema_version=1, profile='base-with-cloud-ai', files=[])))
    record = dict(native_binary_sha256=build_dmg.digest(native), manifest_sha256=build_dmg.digest(manifest))
    return app, resources, record


def legal_files(resources):
    legal = resources / 'Legal'; (legal / 'marked').mkdir(parents=True)
    for name in ['LICENSE', 'THIRD_PARTY_NOTICES.md', 'marked/LICENSE']:
        (legal / name).write_text('synthetic license')
    return legal


def test_candidate_requiring_legal_cannot_package_without_it(tmp_path):
    app, resources, record = candidate(tmp_path)
    record['requires_legal'] = True
    with patch.object(build_dmg, 'run'):
        with pytest.raises(ValueError, match='缺少开源许可'):
            build_dmg.check_app(app, record)
        legal_files(resources)
        build_dmg.check_app(app, record)


def test_legal_directory_cannot_smuggle_additional_files(tmp_path):
    app, resources, record = candidate(tmp_path)
    legal = legal_files(resources)
    (legal / 'user-config.json').write_text('synthetic unexpected data')
    with pytest.raises(ValueError, match='许可目录'):
        build_dmg.check_app(app, record)


def test_candidate_cannot_include_user_extensions_or_unexpected_resources(tmp_path):
    app, resources, record = candidate(tmp_path)
    (resources / 'user-extensions').mkdir()
    with pytest.raises(ValueError, match='资源目录'):
        build_dmg.check_app(app, record)

"""Safety and integrity regressions for runtime assembly, without user data."""
import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from build_runtime import build, extract, normalize_installation, tree_manifest, validate_wheelhouse
from verify_runtime import digest, verify


def make_archive(path, name, *, link=None):
    with tarfile.open(path, 'w') as archive:
        entry = tarfile.TarInfo(name)
        if link is not None:
            entry.type = tarfile.SYMTYPE
            entry.linkname = link
            archive.addfile(entry)
        else:
            entry.size = 4
            archive.addfile(entry, io.BytesIO(b'data'))


def test_traversal_archive_cannot_write_outside_destination(tmp_path):
    archive = tmp_path / 'bad.tar'
    make_archive(archive, 'python/../escape')
    with pytest.raises(ValueError, match='unsafe archive'):
        extract(archive, 'python', tmp_path / 'output')
    assert not (tmp_path / 'escape').exists()


def test_external_archive_symlink_rejected(tmp_path):
    archive = tmp_path / 'bad.tar'
    make_archive(archive, 'python/link', link='../../elsewhere')
    with pytest.raises(ValueError, match='external archive symlink'):
        extract(archive, 'python', tmp_path / 'output')
    assert not (tmp_path / 'output/link').exists()


def test_contained_archive_links_survive_moving(tmp_path):
    archive = tmp_path / 'valid.tar'
    with tarfile.open(archive, 'w') as out:
        entry = tarfile.TarInfo('python/bin/python3.10'); entry.size = 4; entry.mode = 0o755
        out.addfile(entry, io.BytesIO(b'data'))
        link = tarfile.TarInfo('python/bin/python3'); link.type = tarfile.SYMTYPE; link.linkname = 'python3.10'
        out.addfile(link)
    root = tmp_path / 'before'
    extract(archive, 'python', root)
    root.rename(tmp_path / 'after')
    assert (tmp_path / 'after/bin/python3').read_bytes() == b'data'
    assert (tmp_path / 'after/bin/python3.10').stat().st_mode & 0o777 == 0o755


def create_manifest(root):
    path = root / 'manifest.json'
    path.write_text(json.dumps({'schema_version': 1, 'files': tree_manifest(root)}))
    return digest(path)


def test_payload_mutation_and_extra_files_are_rejected(tmp_path):
    file = tmp_path / 'payload.py'; file.write_text('safe')
    expected = create_manifest(tmp_path)
    assert verify(tmp_path, expected) == 1
    file.write_text('evil')
    with pytest.raises(ValueError, match='file mismatch'):
        verify(tmp_path, expected)
    file.write_text('safe')
    extra = tmp_path / 'unexpected'; extra.write_text('untracked')
    with pytest.raises(ValueError, match='unrecorded'):
        verify(tmp_path, expected)


def test_replaced_manifest_rejected_with_trusted_digest(tmp_path):
    (tmp_path / 'payload.py').write_text('safe')
    expected = create_manifest(tmp_path)
    (tmp_path / 'manifest.json').write_text('{}')
    with pytest.raises(ValueError, match='manifest checksum'):
        verify(tmp_path, expected)


def test_existing_build_output_is_untouched(tmp_path):
    output = tmp_path / 'output'; output.mkdir()
    sentinel = output / 'user-data'; sentinel.write_bytes(b'keep')
    with pytest.raises(ValueError, match='already exists'):
        build(tmp_path / 'missing-inputs', tmp_path / 'cache', output, tmp_path / 'uv')
    assert sentinel.read_bytes() == b'keep'


def test_bad_artifacts_fail_before_creating_output(tmp_path):
    output = tmp_path / 'output'
    with pytest.raises(ValueError, match='checksum mismatch'):
        build(tmp_path, tmp_path / 'cache', output, tmp_path / 'uv')
    assert not output.exists()
    assert not list(tmp_path.glob('.pka-runtime-build-*'))


def test_installation_records_follow_pruned_and_modified_files(tmp_path):
    import csv
    root = tmp_path / 'python'
    packages = root / 'lib/python3.10/site-packages'
    info = packages / 'sample-1.0.dist-info'; info.mkdir(parents=True)
    payload = packages / 'sample.py'; payload.write_bytes(b'new content')
    cache = packages / '__pycache__'; cache.mkdir(); (cache / 'sample.pyc').write_bytes(b'temporary path')
    record = info / 'RECORD'
    (info / 'uv_cache.json').write_text('{"synthetic_build_cache":"local"}')
    record.write_text('sample.py,sha256=old,3\n' +
                      'bin/deleted-cli,sha256=old,3\n' +
                      '__pycache__/sample.pyc,sha256=old,3\n' +
                      'sample-1.0.dist-info/uv_cache.json,sha256=old,1\n' +
                      'sample-1.0.dist-info/RECORD,,\n')
    normalize_installation(root, packages)
    with record.open(newline='') as source:
        rows = list(csv.reader(source))
    assert {row[0] for row in rows} == {'sample.py', 'sample-1.0.dist-info/RECORD'}
    file_row = next(row for row in rows if row[0] == 'sample.py')
    assert file_row[1] != 'sha256=old' and file_row[2] == str(len(b'new content'))
    assert not cache.exists()
    assert not (info / 'uv_cache.json').exists()
    saved = record.read_bytes()
    normalize_installation(root, packages)
    assert record.read_bytes() == saved


def test_wheelhouse_checks_bytes_and_refuses_symbolic_links(tmp_path):
    wheels = tmp_path / 'wheels'; wheels.mkdir()
    wheel = wheels / 'package-1.0-py3-none-any.whl'; wheel.write_bytes(b'locked bytes')
    lock = tmp_path / 'lock'; lock.write_text('--hash=sha256:' + digest(wheel))
    validate_wheelhouse(wheels, lock)
    wheel.write_bytes(b'changed bytes')
    with pytest.raises(ValueError, match='untrusted'):
        validate_wheelhouse(wheels, lock)
    wheel.unlink(); wheel.symlink_to(lock)
    with pytest.raises(ValueError, match='untrusted'):
        validate_wheelhouse(wheels, lock)
    alias = tmp_path / 'alias'; alias.symlink_to(wheels, target_is_directory=True)
    with pytest.raises(ValueError, match='real directory'):
        validate_wheelhouse(alias, lock)


def test_download_recipes_use_explicit_archive_directory_for_each_extension(tmp_path, monkeypatch):
    import zipfile
    import prepare_dependency_downloads as recipes
    monkeypatch.setattr(recipes, 'ROOT', tmp_path)
    runtime = tmp_path / 'apps/macos/.build/base'; runtime.mkdir(parents=True)
    audit = tmp_path / 'audit'; audit.mkdir()
    archives = tmp_path / 'packages'; archives.mkdir()
    (audit / 'wheel-audit.json').write_text('{"wheels":[]}')
    (audit / 'model-audit.json').write_text('[]')
    packages = {}
    for key in ['semantic', 'ocr', 'documents']:
        manifest = json.dumps(dict(version=1, id=key, files=[])).encode()
        path = archives / (key + '.pkaext')
        with zipfile.ZipFile(path, 'w') as archive:
            archive.writestr('manifest.json', manifest)
        import hashlib
        packages[key] = dict(filename=path.name, manifest_sha256=hashlib.sha256(manifest).hexdigest(), dependencies={})
    (runtime / 'extension-catalog.json').write_text(json.dumps(dict(packages=packages)))
    recipes.prepare(runtime, audit, archives)
    result = json.loads((runtime / 'extension-catalog.json').read_text())['packages']
    for key in ['semantic', 'documents']:
        assert (runtime / result[key]['dependency_download']['manifest']).is_file()

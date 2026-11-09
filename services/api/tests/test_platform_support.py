"""Platform contracts use synthetic files only; no installed app or credentials."""
import ast
import hashlib
import os
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from app import kernel_contract
from app.platform_support import future_layout_design, get_platform
from app.platform_support.contracts import UnsafeFile, UnsupportedPlatform


@pytest.fixture
def files():
    return get_platform('darwin').files


def test_layouts_and_unimplemented_platforms(tmp_path):
    mac = get_platform('darwin')
    paths = mac.directories(tmp_path, 'bundle')
    assert paths.data == tmp_path / 'Library/Application Support/bundle'
    assert paths.configuration == tmp_path / 'Library/Application Support/bundle-state'
    assert mac.runtime_layout(tmp_path).python == tmp_path / 'python/bin/python3'
    assert not get_platform('win32').capabilities['private_files']
    assert get_platform('linux').capabilities['file_locks']  # Legacy CLI compatibility only.
    for system in ('win32', 'linux', 'unknown'):
        platform = get_platform(system)
        assert not platform.capabilities['desktop_client']
        with pytest.raises(UnsupportedPlatform):
            platform.runtime_layout(tmp_path)
        with pytest.raises(UnsupportedPlatform):
            platform.directories(tmp_path, 'bundle')
        with pytest.raises(UnsupportedPlatform):
            platform.processes.notify_ready(0)
    with pytest.raises(UnsupportedPlatform):
        get_platform('win32').files.atomic_write(tmp_path / 'refused', b'x')
    assert not (tmp_path / 'refused').exists()
    for system, home in [('win32', 'C:/Users/test'), ('linux', '/home/test')]:
        assert future_layout_design(system, home)['implemented'] is False


def test_private_atomic_write_read_and_permissions(tmp_path, files, monkeypatch):
    target = tmp_path / 'private/state.json'
    files.atomic_write(target, b'first')
    assert files.read(target, maximum=5) == b'first'
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    with pytest.raises(UnsafeFile):
        files.read(target, maximum=4)
    target.chmod(0o644)
    with pytest.raises(UnsafeFile):
        files.read(target, maximum=5)
    assert files.read(target, maximum=5, private=False) == b'first'
    files.protect(target)
    original = os.replace
    def fail_publish(*_):
        raise OSError('synthetic disk failure')
    monkeypatch.setattr(os, 'replace', fail_publish)
    with pytest.raises(OSError):
        files.atomic_write(target, b'second')
    assert files.read(target, maximum=5) == b'first'
    assert list(target.parent.iterdir()) == [target]
    monkeypatch.setattr(os, 'replace', original)
    files.atomic_write(target, b'new')
    assert files.read(target, maximum=5) == b'new'


@pytest.mark.parametrize('kind', ['symlink', 'fifo', 'directory', 'parent-link'])
def test_special_files_and_parent_links_are_rejected(tmp_path, files, kind):
    target = tmp_path / 'target'
    original = tmp_path / 'original'
    original.write_bytes(b'preserve')
    original.chmod(0o600)
    if kind == 'symlink':
        target.symlink_to(original)
    elif kind == 'fifo':
        os.mkfifo(target)
    elif kind == 'directory':
        target.mkdir()
    else:
        directory = tmp_path / 'outside'
        directory.mkdir()
        target.symlink_to(directory, target_is_directory=True)
        target /= 'state'
    for operation in (lambda: files.read(target, maximum=100, parents=True),
                      lambda: files.atomic_write(target, b'refused')):
        with pytest.raises((UnsafeFile, OSError)):
            operation()
    assert original.read_bytes() == b'preserve'


def test_owner_and_inode_are_checked_after_open(tmp_path, files, monkeypatch):
    target = tmp_path / 'credential'
    files.atomic_write(target, b'synthetic')
    original = os.fstat
    for altered in ({'st_uid': os.getuid() + 1}, {'st_ino': target.stat().st_ino + 1}):
        def changed(descriptor):
            info = original(descriptor)
            return SimpleNamespace(**{**{key: getattr(info, key) for key in
                ('st_uid', 'st_mode', 'st_size', 'st_ino', 'st_dev')}, **altered})
        with monkeypatch.context() as patch:
            patch.setattr(os, 'fstat', changed)
            with pytest.raises(UnsafeFile):
                files.read(target, maximum=100)


def test_lock_conflicts_and_release(tmp_path, files):
    lock = tmp_path / 'service.lock'
    with files.lock(lock, exclusive=False), files.lock(lock, exclusive=False):
        with pytest.raises(BlockingIOError), files.lock(lock, exclusive=True):
            pass
    with files.lock(lock, exclusive=True):
        with pytest.raises(BlockingIOError), files.lock(lock, exclusive=False):
            pass
    with pytest.raises(RuntimeError), files.lock(lock, exclusive=True):
        raise RuntimeError('synthetic transaction failure')
    with files.lock(lock, exclusive=True):
        assert stat.S_IMODE(lock.stat().st_mode) == 0o600


def test_exclusive_publication_never_overwrites(tmp_path, files):
    stage = tmp_path / 'stage'
    stage.mkdir()
    (stage / 'new').write_bytes(b'new')
    target = tmp_path / 'target'
    target.mkdir()
    (target / 'old').write_bytes(b'old')
    with pytest.raises(FileExistsError):
        files.publish_directory(stage, target)
    assert (target / 'old').read_bytes() == b'old' and stage.exists()
    destination = tmp_path / 'published'
    files.publish_directory(stage, destination)
    assert not stage.exists() and (destination / 'new').read_bytes() == b'new'
    file = tmp_path / 'archive'
    files.reserve(file)
    with pytest.raises(FileExistsError):
        files.reserve(file)
    with pytest.raises(FileExistsError):
        files.publish_file(destination / 'new', file)
    assert file.read_bytes() == b''


def test_kernel_contract_identity_and_safe_metadata(tmp_path):
    assert kernel_contract.runtime_id(tmp_path) == 'development'
    kernel_contract.runtime_id.cache_clear()
    manifest = tmp_path / 'manifest.json'
    manifest.write_bytes(b'{"files":[]}')
    assert kernel_contract.runtime_id(tmp_path) == hashlib.sha256(manifest.read_bytes()).hexdigest()
    kernel_contract.runtime_id.cache_clear()
    manifest.write_bytes(b'x' * (8 * 1024 * 1024 + 1))
    with pytest.raises(ValueError):
        kernel_contract.runtime_id(tmp_path)
    info = kernel_contract.metadata()
    assert set(info) == {'kernel_version', 'contract_version', 'schema_version',
                         'platform', 'architecture', 'runtime_id', 'capabilities'}
    kernel_contract.validate_client(info['contract_version'], info['schema_version'])
    for contract, schema in ((2, info['schema_version']), (1, 999)):
        with pytest.raises(ValueError):
            kernel_contract.validate_client(contract, schema)


def test_platform_specific_imports_are_confined_to_adapters():
    root = Path(__file__).resolve().parents[3]
    for directory in (root / 'services/api/app', root / 'services/worker'):
        for file in directory.rglob('*.py'):
            if 'platform_support' in file.parts:
                continue
            tree = ast.parse(file.read_text())
            for node in ast.walk(tree):
                modules = ([alias.name for alias in node.names] if isinstance(node, ast.Import)
                           else [node.module or ''] if isinstance(node, ast.ImportFrom) else [])
                assert not {'fcntl', 'ctypes', 'resource'} & set(modules), str(file)

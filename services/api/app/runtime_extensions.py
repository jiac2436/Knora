"""Trusted, versioned optional runtime packages. No knowledge data is stored here."""
from __future__ import annotations

import hashlib
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import platform
import sys

from .platform_support import get_platform

IDS = ('semantic', 'ocr', 'documents')
MANUAL_EDIT_PRESERVED = '已保留手动编辑，自动解析已停止；可手动重试解析'
_loaded = None
_leases = ExitStack()


class ExtensionRequired(ValueError):
    def __init__(self, capability):
        self.capability = capability
        title = {'semantic': '语义搜索', 'ocr': '文字识别', 'documents': '文档解析'}[capability]
        super().__init__(f'需要安装{title}扩展，请在设置→扩展功能中安装后重新打开客户端')


def runtime_root():
    return Path(__file__).resolve().parents[2]


def extension_root():
    return Path(os.environ.get('PKA_EXTENSIONS_ROOT',
        str(Path.home() / 'Library/Application Support/local.pka.knowledge-inbox-state/extensions')))


def safe_directory(path):
    path = Path(path).absolute()
    for item in [path, *path.parents]:
        if item.is_symlink() and str(item) not in {'/var', '/tmp', '/etc'}:
            raise ValueError('扩展目录不能包含符号链接')
    return path


def read_json(path, maximum=8_000_000):
    if path.is_symlink():
        raise ValueError('扩展配置不能是符号链接')
    with path.open('rb') as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError('扩展配置超过大小限制')
    return json.loads(data)


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def catalog(root=None):
    root = root or runtime_root()
    path = root / 'extension-catalog.json'
    if not path.exists():
        return None  # Existing full runtimes retain their original behavior.
    value = read_json(path)
    python_minor = f'{sys.version_info.major}.{sys.version_info.minor}'
    if value.get('version') != 1 or value.get('python') != python_minor or value.get('architecture') != platform.machine():
        raise ValueError('扩展目录与当前运行环境不兼容')
    if set(value.get('packages', {})) != set(IDS):
        raise ValueError('扩展目录缺少能力定义')
    for key, item in value['packages'].items():
        if (item.get('id') != key or not isinstance(item.get('sha256'), str) or len(item['sha256']) != 64
                or any(c not in '0123456789abcdef' for c in item['sha256'])):
            raise ValueError('扩展目录格式无效')
        if not isinstance(item.get('version'), str) or not item['version'].replace('.', '').replace('-', '').isalnum():
            raise ValueError('扩展版本无效')
    return value


def active(root):
    safe_directory(root)
    path = root / 'active.json'
    if not path.exists():
        return {}
    data = read_json(path, 32_000)
    if data.get('version') != 1 or not isinstance(data.get('packages'), dict):
        raise ValueError('扩展安装记录无效，原记录已保留')
    if set(data['packages']) - set(IDS):
        raise ValueError('扩展安装记录包含未知能力')
    return data['packages']


def package_directory(root, item):
    return safe_directory(root / 'packages' / item['id'] / item['sha256'])


@contextmanager
def package_lock(root, item, *, exclusive=False):
    directory = safe_directory(root / 'leases')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with get_platform().files.lock(directory.resolve() / item['sha256'], exclusive=exclusive):
        yield


def verify_package(path, item):
    safe_directory(path)
    manifest = read_json(path / 'manifest.json')
    if digest(path / 'manifest.json') != item['manifest_sha256']:
        raise ValueError('扩展清单校验失败')
    expected = set()
    for entry in manifest['files']:
        name = PurePosixPath(entry['path'])
        if name.is_absolute() or not name.parts or '..' in name.parts or str(name) in expected or '\\' in str(name):
            raise ValueError('扩展清单路径无效')
        expected.add(str(name))
        target = path.joinpath(*name.parts)
        if any(p.is_symlink() for p in [target, *target.parents]) or not target.is_file():
            raise ValueError('扩展文件不可用')
        if target.stat().st_size != entry['bytes'] or digest(target) != entry['sha256']:
            raise ValueError('扩展文件完整性校验失败')
    actual = {p.relative_to(path).as_posix() for p in path.rglob('*') if p.is_file() or p.is_symlink()}
    if actual != expected | {'manifest.json'}:
        raise ValueError('扩展目录存在未记录文件')


def activate():
    """Load one immutable selection at process start; changes require a restart."""
    global _loaded
    if _loaded is not None:
        return
    try:
        value = catalog()
    except (OSError, ValueError, KeyError, TypeError):
        _loaded = {key: {'status': 'failed', 'message': '扩展目录不可用，基础知识库仍可使用'} for key in IDS}
        os.environ['PKA_SEMANTIC_ENABLED'] = '0'
        return
    if value is None:
        _loaded = {key: {'status': 'bundled'} for key in IDS}
        return
    _loaded = {key: {'status': 'not_installed'} for key in IDS}
    try:
        from .extension_install import collect_removals
        collect_removals(runtime_root(), extension_root())
        inherited = os.environ.get('PKA_ACTIVE_EXTENSIONS')
        selected = json.loads(inherited) if inherited else active(extension_root())
        if not isinstance(selected, dict):
            raise ValueError('扩展启动快照无效')
    except (OSError, ValueError, TypeError):
        selected = {}
        _loaded = {key: {'status': 'failed', 'message': '扩展安装记录不可用，基础知识库仍可使用'} for key in IDS}
    for key, item in value['packages'].items():
        if selected.get(key) != item['sha256']:
            continue
        lease = package_lock(extension_root(), item)
        acquired = False
        try:
            lease.__enter__()
            acquired = True
            path = package_directory(extension_root(), item)
            verify_package(path, item)
            _leases.callback(lease.__exit__, None, None, None)
            acquired = False
            sys.path.append(str(path / 'packages'))
            _loaded[key] = {'status': 'ready', 'version': item['version']}
            if key == 'semantic':
                os.environ['PKA_SEMANTIC_MODEL_PATH'] = str(path / 'model')
            elif key == 'ocr':
                os.environ['PKA_OCR_MODELS_DIR'] = str(path / 'ocr-models')
        except (OSError, ValueError, KeyError, TypeError):
            _loaded[key] = {'status': 'failed', 'message': '扩展校验失败，请重新安装；基础知识库仍可使用'}
        finally:
            if acquired:
                lease.__exit__(None, None, None)
    os.environ['PKA_ACTIVE_EXTENSIONS'] = json.dumps({key: value['packages'][key]['sha256']
        for key, loaded in _loaded.items() if loaded['status'] == 'ready'})
    if _loaded['semantic']['status'] != 'ready':
        os.environ['PKA_SEMANTIC_ENABLED'] = '0'


def available(capability):
    activate()
    return _loaded[capability]['status'] in {'ready', 'bundled'}


def require(capability):
    if not available(capability):
        raise ExtensionRequired(capability)


def status():
    activate()
    return {key: dict(value) for key, value in _loaded.items()}


def job_status(job):
    return ('waiting_extension' if job.status == 'pending' and
            (job.last_error or '').startswith('extension_required:') else job.status)


def job_message(job):
    text = job.last_error
    return text.split(':', 2)[-1] if text and text.startswith('extension_required:') else text

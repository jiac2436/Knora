"""Offline/HTTPS installation of packages pinned by the signed app's catalog.

Activation changes apply to the next app launch. Existing version directories
remain intact while a running app might use them; no user documents are touched.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import sys
import tempfile
import uuid
import time
from urllib.parse import urlsplit
import zipfile

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime_extensions import (IDS, active, catalog, digest, package_directory,
    package_lock, read_json, runtime_root, safe_directory, verify_package)
from app.platform_support import get_platform


def write_json(path, value):
    safe_directory(path.parent)
    if path.is_symlink():
        raise ValueError('扩展配置不能是符号链接')
    descriptor, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def mutation(root):
    safe_directory(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = root / '.install.lock'
    try:
        with get_platform().files.lock(root.resolve() / path.name, exclusive=True):
            yield
    except BlockingIOError:
        raise ValueError('另一个扩展安装正在进行，请稍后重试') from None


def source_url(value):
    parts = urlsplit(value)
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.query or parts.fragment):
        raise ValueError('下载源须为不含凭证、查询参数和片段的 HTTPS 地址')
    return value.rstrip('/')


def snapshot(runtime, root):
    from app.dependency_install import component_list
    value = catalog(runtime)
    if value is None:
        return {'bundled': True, 'packages': [], 'source': ''}
    selected = active(root)
    pending = read_json(root / 'remove.json').get('packages', {}) if (root / 'remove.json').exists() else {}
    packages = []
    for key, item in value['packages'].items():
        path = package_directory(root, item)
        installed = path.is_dir()
        packages.append({**{k: item[k] for k in ('id', 'title', 'summary', 'version', 'bytes', 'installed_bytes')},
            'installed': installed, 'enabled': installed and selected.get(key) == item['sha256'],
            'removal_pending': pending.get(key) == item['sha256'],
            'online_available': bool(item.get('dependency_download')),
            'download_bytes': sum(file['bytes'] for file in item.get('dependency_download', {}).get('artifacts', [])) or item['bytes'],
            'online_reason': item.get('online_reason', '当前版本请导入配套离线扩展包。'),
            'components': component_list(item)})
    config = root / 'download-source.json'
    source = read_json(config).get('url', '') if config.exists() else ''
    return {'bundled': False, 'packages': packages, 'source': source}


def install(runtime, root, archive, progress=lambda *_: None):
    value = catalog(runtime)
    if value is None:
        raise ValueError('当前完整安装包尚未启用独立扩展安装')
    archive = Path(archive)
    if archive.is_symlink() or not archive.is_file():
        raise ValueError('请选择实际的官方扩展包文件')
    possible = [item for item in value['packages'].values() if item['bytes'] == archive.stat().st_size]
    if not possible:
        raise ValueError('扩展包大小不匹配，请使用与当前客户端配套的官方安装包')
    progress('checking', 0)
    checksum = digest(archive)
    item = next((item for item in possible if item['sha256'] == checksum), None)
    if item is None:
        raise ValueError('扩展包完整性或来源校验失败，原有扩展保持不变')
    with mutation(root):
        destination = package_directory(root, item)
        valid = False
        if destination.exists():
            try:
                verify_package(destination, item)
                valid = True
            except ValueError:
                pass  # Repair only after a complete replacement has validated.
        if not valid:
            needed = item['installed_bytes'] + 20 * 1024 * 1024
            if shutil.disk_usage(root).free < needed:
                raise ValueError('磁盘可用空间不足，尚未修改已安装扩展')
            staging = root / 'staging'
            staging.mkdir(exist_ok=True, mode=0o700)
            safe_directory(staging)
            with tempfile.TemporaryDirectory(prefix='install-', dir=staging) as temporary:
                stage = Path(temporary) / 'payload'
                stage.mkdir(mode=0o700)
                progress('installing', 0)
                total, names = 0, set()
                with zipfile.ZipFile(archive) as source:
                    entries = source.infolist()
                    if len(entries) > 50_000:
                        raise ValueError('扩展包文件数量超过限制')
                    for entry in entries:
                        name = PurePosixPath(entry.filename)
                        mode = entry.external_attr >> 16
                        if (name.is_absolute() or not name.parts or '..' in name.parts
                                or '\\' in entry.filename or '\x00' in entry.filename
                                or str(name) in names or entry.is_dir()
                                or stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                            raise ValueError('扩展包包含不安全的路径或文件类型')
                        names.add(str(name))
                        total += entry.file_size
                        if total > item['installed_bytes'] or len(name.parts) > 30:
                            raise ValueError('扩展包超过解压限制')
                        target = stage.joinpath(*name.parts)
                        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        with source.open(entry) as incoming, target.open('xb') as output:
                            remaining = entry.file_size
                            while remaining:
                                chunk = incoming.read(min(1024 * 1024, remaining))
                                if not chunk:
                                    raise ValueError('扩展包内容不完整')
                                output.write(chunk)
                                remaining -= len(chunk)
                                progress('installing', total / max(1, item['installed_bytes']))
                            if incoming.read(1):
                                raise ValueError('扩展包文件大小无效')
                        target.chmod(0o755 if mode & 0o111 else 0o644)
                if total != item['installed_bytes']:
                    raise ValueError('扩展包解压大小不匹配')
                verify_package(stage, item)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                safe_directory(destination.parent)
                progress('activating', 0.99)
                with package_lock(root, item, exclusive=True):
                    previous = None
                    if destination.exists():
                        previous = staging / ('previous-' + uuid.uuid4().hex)
                        destination.rename(previous)
                    try:
                        stage.rename(destination)
                    except BaseException:
                        if previous is not None:
                            previous.rename(destination)
                        raise
        progress('activating', 0.99)
        selected = active(root)
        selected[item['id']] = item['sha256']
        write_json(root / 'active.json', {'version': 1, 'packages': selected})
        cancel_removal(root, item['id'])
        progress('completed', 1)
    return item['id']


def change(runtime, root, key, enabled):
    value = catalog(runtime)
    if value is None or key not in IDS:
        raise ValueError('该扩展不可独立管理')
    with mutation(root):
        selected = active(root)
        if enabled:
            item = value['packages'][key]
            verify_package(package_directory(root, item), item)
            selected[key] = item['sha256']
            cancel_removal(root, key)
        else:
            selected.pop(key, None)
        write_json(root / 'active.json', {'version': 1, 'packages': selected})


def cancel_removal(root, key):
    path = root / 'remove.json'
    if path.exists():
        pending = read_json(path)['packages']
        pending.pop(key, None)
        write_json(path, {'version': 1, 'packages': pending})


def remove(runtime, root, key):
    value = catalog(runtime)
    if value is None or key not in IDS:
        raise ValueError('该扩展不可独立卸载')
    with mutation(root):
        selected = active(root)
        selected.pop(key, None)
        path = root / 'remove.json'
        pending = read_json(path)['packages'] if path.exists() else {}
        pending[key] = value['packages'][key]['sha256']
        write_json(path, {'version': 1, 'packages': pending})
        write_json(root / 'active.json', {'version': 1, 'packages': selected})
    collect_removals(runtime, root)


def collect_removals(runtime, root):
    """Delete only packages for which no host or worker holds a shared lease."""
    path = root / 'remove.json'
    if not path.exists():
        return
    value = catalog(runtime)
    with mutation(root):
        pending = read_json(path)['packages']
        selected = active(root)
        for key, checksum in list(pending.items()):
            if key not in IDS or checksum != value['packages'][key]['sha256']:
                continue
            if selected.get(key) == checksum:
                pending.pop(key)
                continue
            item = value['packages'][key]
            try:
                with package_lock(root, item, exclusive=True):
                    directory = package_directory(root, item)
                    if directory.exists():
                        shutil.rmtree(directory)
                    pending.pop(key)
            except BlockingIOError:
                pass  # Running processes continue with their frozen version.
        write_json(path, {'version': 1, 'packages': pending})


def download(runtime, root, key, progress=lambda *_: None, *, component_progress=None):
    import ssl
    import urllib.request
    import certifi
    value = catalog(runtime)
    if value is None or key not in IDS:
        raise ValueError('未知扩展')
    if value['packages'][key].get('dependency_download'):
        from app.dependency_install import install_dependencies
        return install_dependencies(runtime, root, key, progress, component_progress=component_progress)
    if value['packages'][key].get('online_reason'):
        raise ValueError(value['packages'][key]['online_reason'])
    with mutation(root):
        config = root / 'download-source.json'
        if not config.exists():
            raise ValueError('尚未配置下载服务器，请先导入官方离线扩展包')
        base = source_url(read_json(config)['url'])
        item = value['packages'][key]
        filename = item['filename']
        if Path(filename).name != filename:
            raise ValueError('扩展下载文件名无效')
        if shutil.disk_usage(root).free < item['bytes'] + item['installed_bytes'] + 20 * 1024 * 1024:
            raise ValueError('磁盘空间不足以下载并安装扩展')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('扩展下载发生重定向，请配置最终 HTTPS 下载地址')
        context = ssl.create_default_context(cafile=certifi.where())
        opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
        staging = root / 'staging'
        staging.mkdir(exist_ok=True, mode=0o700)
        safe_directory(staging)
        with tempfile.TemporaryDirectory(prefix='download-', dir=staging) as temporary:
            path = Path(temporary) / filename
            request = urllib.request.Request(base + '/' + filename, headers={'Accept-Encoding': 'identity'})
            deadline = time.monotonic() + 3600
            with opener.open(request, timeout=30) as response, path.open('xb') as output:
                if response.status != 200:
                    raise ValueError('下载服务器未返回完整安装包')
                total = 0
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > item['bytes'] or time.monotonic() > deadline:
                        raise ValueError('扩展下载超过大小或时间限制')
                    output.write(chunk)
                    progress('downloading', total / item['bytes'])
            if total != item['bytes'] or digest(path) != item['sha256']:
                raise ValueError('下载包大小或摘要不匹配，未安装')
            # Release the download lock before the install transaction.
            saved = root / ('download-' + uuid.uuid4().hex + '.pkaext')
            path.rename(saved)
    try:
        return install(runtime, root, saved, progress)
    finally:
        saved.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--progress', type=Path)
    parser.add_argument('operation', choices=['list', 'install', 'download', 'enable', 'disable', 'remove', 'source'])
    parser.add_argument('value', nargs='?')
    args = parser.parse_args()
    root = safe_directory(args.root)
    last_progress = [0.0]
    details = []
    latest = ['preparing', 0]
    def progress(phase, fraction):
        latest[:] = [phase, fraction]
        if args.progress:
            if phase != "completed" and (args.progress.parent / "cancel").exists():
                raise InterruptedError("操作已取消，原有扩展和资料保持不变")
            if time.monotonic() - last_progress[0] < 0.2 and phase not in {"completed", "checking"}:
                return
            last_progress[0] = time.monotonic()
            write_json(args.progress, {'phase': phase, 'fraction': fraction, 'components': details})
    def component_progress(index, state, fraction):
        details[index].update(state=state, fraction=fraction)
        progress(*latest)
    try:
        if args.operation == 'install':
            install(runtime_root(), root, args.value, progress)
        elif args.operation == 'download':
            from app.dependency_install import component_list
            value = catalog(runtime_root())
            if value and args.value in value['packages']:
                details[:] = [{**entry, 'state': 'pending', 'fraction': 0}
                              for entry in component_list(value['packages'][args.value])]
            download(runtime_root(), root, args.value, progress, component_progress=component_progress)
        elif args.operation in {'enable', 'disable'}:
            change(runtime_root(), root, args.value, args.operation == 'enable')
        elif args.operation == 'remove':
            remove(runtime_root(), root, args.value)
        elif args.operation == 'source':
            with mutation(root):
                write_json(root / 'download-source.json', {'url': source_url(args.value)})
        print(json.dumps(snapshot(runtime_root(), root), ensure_ascii=False), flush=True)
    except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
        print(json.dumps({'error': str(error)}, ensure_ascii=False), flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

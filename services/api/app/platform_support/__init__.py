"""Explicit platform selection. Unsupported capabilities fail, never no-op."""
from dataclasses import dataclass
from functools import lru_cache
import sys

from .contracts import UnsupportedPlatform


class Unsupported:
    def __init__(self, system):
        self.system = system

    def __getattr__(self, capability):
        raise UnsupportedPlatform(f'当前系统 {self.system} 尚未实现平台能力：{capability}')


@dataclass(frozen=True)
class PlatformSupport:
    system: str
    name: str
    files: object
    processes: object

    @property
    def capabilities(self):
        return {'private_files': self.system == 'darwin' or self.system.startswith('linux'),
                'file_locks': self.system == 'darwin' or self.system.startswith('linux'),
                'owned_processes': self.system == 'darwin',
                'desktop_client': self.system == 'darwin',
                'capture_queue': self.system == 'darwin'}

    def directories(self, home, bundle_id):
        if self.system != 'darwin':
            raise UnsupportedPlatform('当前系统尚未实现桌面知识库目录适配')
        from .macos import directories
        return directories(home, bundle_id)

    def runtime_layout(self, root):
        if self.system != 'darwin':
            raise UnsupportedPlatform('当前系统尚未实现桌面运行包布局适配')
        from .macos import runtime_layout
        return runtime_layout(root)

    def plugin_command(self, runtime, launcher):
        layout = self.runtime_layout(runtime)
        return {'type': 'stdio', 'command': 'env',
                'args': [str(layout.python), '-I', '-B', str(launcher)]}

    def external_capabilities(self):
        if self.system != 'darwin':
            raise UnsupportedPlatform('当前系统尚未实现桌面CLI与系统OCR适配')
        from .external_macos import MacExternalCapabilities
        return MacExternalCapabilities()

    def client_file_path(self, path):
        if self.system != 'darwin':
            raise UnsupportedPlatform('当前系统尚未实现旧桌面文件标识适配')
        from .macos import client_file_path
        return client_file_path(path)


@lru_cache(maxsize=8)
def _select(system):
    unavailable = Unsupported(system)
    if system == 'darwin':
        from .posix_files import PosixFiles
        from .macos import MacOwnedProcesses
        return PlatformSupport(system, 'macos', PosixFiles(system), MacOwnedProcesses())
    if system.startswith('linux'):
        from .posix_files import PosixFiles
        return PlatformSupport(system, 'linux', PosixFiles(system), unavailable)
    return PlatformSupport(system, 'windows' if system == 'win32' else system, unavailable, unavailable)


def get_platform(system=None):
    return _select(system or sys.platform)


def future_layout_design(system, home):
    """Documentation-only paths; does not advertise implemented file security."""
    from pathlib import PureWindowsPath, PurePosixPath
    if system == 'win32':
        root = PureWindowsPath(home) / 'AppData/Local/local.pka.knowledge-inbox'
        return {'data': str(root / 'data'), 'cache': str(root / 'cache'),
                'logs': str(root / 'logs'), 'configuration': str(root / 'device'),
                'python_relative': 'python/python.exe', 'implemented': False}
    if system.startswith('linux'):
        home = PurePosixPath(home)
        return {'data': str(home / '.local/share/local.pka.knowledge-inbox'),
                'cache': str(home / '.cache/local.pka.knowledge-inbox'),
                'logs': str(home / '.local/state/local.pka.knowledge-inbox/logs'),
                'configuration': str(home / '.config/local.pka.knowledge-inbox'),
                'python_relative': 'python/bin/python3', 'implemented': False}
    raise UnsupportedPlatform('没有该系统的后续布局设计')

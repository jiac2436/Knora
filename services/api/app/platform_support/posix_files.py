"""Existing Unix private-file semantics, used by macOS and legacy Linux tools.

This preserves the previous low-level Linux backup behavior. It is not a Linux
desktop/credential/process implementation; those capabilities remain explicit.
"""
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import tempfile

from .contracts import UnsafeFile, UnsupportedPlatform


class PosixFiles:
    def __init__(self, system):
        self.system = system

    def directory(self, path):
        # Validate ancestors through a non-existent child, so a real directory
        # is accepted while links or special files are refused.
        self.safe_path(path / '.pka-directory-check', create=True)
        info = path.lstat()
        if info.st_uid != os.getuid():
            raise UnsafeFile('私密目录不属于当前用户')
        path.chmod(0o700)

    def safe_path(self, path: Path, *, create=False):
        if not path.is_absolute():
            raise UnsafeFile('文件路径必须是绝对路径')
        for parent in reversed(path.parents):
            try:
                info = parent.lstat()
            except FileNotFoundError:
                if not create:
                    continue
                parent.mkdir(mode=0o700)
                info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
                raise UnsafeFile('文件目录不安全')
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise UnsafeFile('文件类型或所有权不安全')

    def read(self, path, *, maximum, private=True, parents=False, permissions=None):
        if not path.is_absolute() or maximum < 1:
            raise UnsafeFile('文件路径或读取上限无效')
        if parents:
            self.safe_path(path)
        try:
            before = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(before.st_mode):
            raise UnsafeFile('文件不是普通文件')
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or (private and info.st_mode & 0o077) or info.st_size > maximum
                    or (permissions is not None and stat.S_IMODE(info.st_mode) != permissions)
                    or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)):
                raise UnsafeFile('文件权限、类型或大小无效')
            with os.fdopen(descriptor, 'rb', closefd=False) as stream:
                value = stream.read(maximum + 1)
            if len(value) > maximum:
                raise UnsafeFile('文件超过读取上限')
            return value
        finally:
            os.close(descriptor)

    def atomic_write(self, path, value):
        self.safe_path(path, create=True)
        descriptor, temporary = tempfile.mkstemp(prefix='.pka-', dir=path.parent)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, 'wb') as stream:
                stream.write(value)
                stream.flush()
                os.fsync(stream.fileno())
            self.safe_path(path)
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(temporary).unlink(missing_ok=True)

    @contextmanager
    def lock(self, path, *, exclusive):
        import fcntl
        self.safe_path(path, create=True)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise UnsafeFile('锁文件类型或所有权无效')
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            yield
        finally:
            os.close(descriptor)

    def protect(self, path):
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise UnsafeFile('私密文件类型或所有权无效')
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    def reserve(self, path):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(descriptor)

    def publish_file(self, stage, destination):
        os.link(stage, destination, follow_symlinks=False)

    def publish_directory(self, stage, destination):
        import ctypes
        library = ctypes.CDLL(None, use_errno=True)
        if self.system == 'darwin':
            rename = library.renamex_np
            rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
            arguments = (os.fsencode(stage), os.fsencode(destination), 4)  # RENAME_EXCL
        elif self.system.startswith('linux') and hasattr(library, 'renameat2'):
            rename = library.renameat2
            rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
            arguments = (-100, os.fsencode(stage), -100, os.fsencode(destination), 1)
        else:
            raise UnsupportedPlatform('当前系统尚未实现独占原子目录发布')
        rename.restype = ctypes.c_int
        if rename(*arguments) != 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

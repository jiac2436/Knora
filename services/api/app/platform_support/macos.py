"""macOS directory, runtime and owned-process implementation."""
import os
from pathlib import Path
import signal
import subprocess
import threading

from .contracts import DirectoryLayout, RuntimeLayout


def directories(home, bundle_id):
    library = home / 'Library'
    return DirectoryLayout(library / 'Application Support' / bundle_id,
        library / 'Caches' / bundle_id, library / 'Logs' / bundle_id,
        library / 'Application Support' / (bundle_id + '-state'))


def runtime_layout(root):
    return RuntimeLayout(root, root / 'python/bin/python3', root / 'backend',
                         root / 'model', root / 'ocr-models')


def client_file_path(path):
    # Foundation may spell system aliases without /private. Normalize only
    # these OS aliases; do not resolve user-controlled descendants or symlinks.
    for alias in (Path('/var'), Path('/tmp')):
        physical = Path('/private') / alias.name
        if path.is_relative_to(alias) and alias.is_symlink() and alias.resolve() == physical:
            return physical / path.relative_to(alias)
    return path


class MacOwnedWorker:
    def __init__(self, process, descriptor):
        self.process = process
        self.descriptor = descriptor

    @property
    def pid(self):
        return self.process.pid

    def poll(self):
        return self.process.poll()

    def read_ready(self):
        try:
            return os.read(self.descriptor, 5)
        except BlockingIOError:
            return None

    def close_ready(self):
        if self.descriptor is not None:
            os.close(self.descriptor)
            self.descriptor = None

    def stop(self):
        self.close_ready()
        if self.process.poll() is not None:
            self.close_input()
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        finally:
            self.close_input()

    def close_input(self):
        if self.process.stdin:
            try:
                self.process.stdin.close()
            except OSError:
                pass


class MacOwnedProcesses:
    def start_worker(self, arguments, environment, startup, diagnostics):
        read_fd, write_fd = os.pipe()
        os.set_blocking(read_fd, False)
        worker = None
        try:
            process = subprocess.Popen(arguments, stdin=subprocess.PIPE, stdout=diagnostics,
                stderr=diagnostics, env={**environment, 'PKA_WORKER_READY_FD': str(write_fd)},
                pass_fds=(write_fd,))
            worker = MacOwnedWorker(process, read_fd)
            process.stdin.write(startup)
            process.stdin.flush()
            return worker
        except BaseException:
            if worker is not None:
                worker.stop()
            else:
                os.close(read_fd)
            raise
        finally:
            os.close(write_fd)

    def watch_parent(self, descriptor, on_eof):
        def watch():
            try:
                while os.read(descriptor, 1):
                    pass
            finally:
                on_eof()
        threading.Thread(target=watch, name='pka-parent-lifetime', daemon=True).start()

    def install_stop_handlers(self, stop):
        for number in (signal.SIGTERM, signal.SIGINT):
            signal.signal(number, lambda *_: stop())

    def stop_current_worker(self):
        os.kill(os.getpid(), signal.SIGTERM)

    def notify_ready(self, descriptor):
        try:
            os.write(descriptor, b'ready')
        finally:
            os.close(descriptor)

    def exec(self, command, arguments, environment):
        os.execve(command, [command, *arguments], environment)

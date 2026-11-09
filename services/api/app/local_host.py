"""Owned desktop service: inherited socket, readiness and parent pipe lifetime."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time

# Direct file execution with isolated Python does not load the source directory.
if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    # Source-tree development keeps services/ beside services/api/. A packaged
    # runtime already has both under backend/; no system Python paths are used.
    if os.getenv('PKA_WORKER_ROOT'):
        sys.path.append(os.environ['PKA_WORKER_ROOT'])

from app.runtime_extensions import activate
activate()

from app.config import settings
from app.database import engine
from app.local_runtime import data_lock, initialize_identity, validate_credential
from app.migrations import migrate
from app.agent_endpoint import published_endpoint
from app.model_configuration import (read_message, encode_message, install_startup,
                                     WorkerStartup, worker_environment)
from app.platform_support import get_platform
from app.kernel_contract import metadata, validate_client


@contextmanager
def instance_lock():
    paths = settings.local_paths
    path = paths.data.parent / f'.{paths.data.name}.instance.lock'
    with ExitStack() as stack:
        try:
            stack.enter_context(get_platform().files.lock(path, exclusive=True))
        except BlockingIOError:
            raise ValueError('此知识库已有客户端在运行') from None
        yield


def run_owned_worker():
    startup = read_message(sys.stdin.buffer, worker=True)
    install_startup(startup.ai, worker=True)
    del startup
    # A killed host cannot run finally: the inherited pipe also protects this case.
    processes = get_platform().processes
    processes.watch_parent(sys.stdin.fileno(), processes.stop_current_worker)
    from services.worker.main import main as worker_main
    worker_main()


def stop_worker(worker):
    if worker is not None:
        worker.stop()


async def serve(identity: dict, stopped: threading.Event, ai=None):
    import uvicorn
    from app.main import app
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(128)
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port,
                            log_level='warning', access_log=False, timeout_graceful_shutdown=5))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    worker = None
    try:
        deadline = time.monotonic() + 60
        while not server.started:
            if task.done():
                await task
                raise ValueError('本地 API 启动失败')
            if stopped.is_set() or time.monotonic() > deadline:
                raise ValueError('本地服务启动已取消或超时')
            await asyncio.sleep(0.05)
        backend = str(Path(__file__).resolve().parents[1])
        worker_root = os.getenv('PKA_WORKER_ROOT', backend)
        worker = get_platform().processes.start_worker([sys.executable, '-I', '-B', '-c',
                        'import sys; sys.path[:0] = sys.argv[1:3]; sys.argv = sys.argv[:1]; from app.local_host import run_owned_worker; run_owned_worker()',
                        backend, worker_root], worker_environment(os.environ),
                        encode_message(WorkerStartup(version=1, ai=ai)), sys.stderr)
        ai = None
        try:
            while True:
                ready = worker.read_ready()
                if ready == b'ready':
                    break
                if ready == b'' or worker.poll() is not None:
                    raise ValueError('本地 Worker 启动失败')
                if stopped.is_set() or time.monotonic() > deadline:
                    raise ValueError('本地 Worker 启动已取消或超时')
                await asyncio.sleep(0.05)
        finally:
            worker.close_ready()
        address = f'http://127.0.0.1:{port}'
        with published_endpoint(settings.local_paths.data, address, identity['library_id']):
            print(json.dumps({'event': 'ready', 'address': address, 'worker_pid': worker.pid,
                              'kernel': metadata(), **identity}), flush=True)
            while not stopped.is_set() and not task.done():
                if worker.poll() is not None:
                    raise ValueError('本地 Worker 已退出，请重新启动客户端')
                await asyncio.sleep(0.2)
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(task, timeout=10)
        finally:
            stop_worker(worker)
            listener.close()


def main():
    if settings.environment != 'local':
        raise ValueError('本地服务须使用 local 运行模式')
    startup = read_message(sys.stdin.buffer)
    # Refuse an incompatible desktop before migration or identity changes.
    validate_client(startup.client_contract_version, startup.client_schema_version)
    credential = startup.owner_credential
    validate_credential(credential)
    install_startup(startup.ai)
    stopped = threading.Event()
    # EOF also handles a crash or forced quit of the owning desktop process.
    get_platform().processes.watch_parent(sys.stdin.fileno(), stopped.set)
    get_platform().processes.install_stop_handlers(stopped.set)
    with instance_lock():
        with data_lock(settings.local_paths, exclusive=True):
            migrate(engine, settings.local_paths.backups)
            identity = initialize_identity(engine, credential)
            from app.model_switch import clear_startup_gate
            from app.model_configuration import ModelStartup
            from app.errors import ApiError
            try:
                clear_startup_gate()
            except (ApiError, ValueError, OSError):
                # Preserve a suspicious gate and prevent model dispatch while
                # still opening the authenticated library's basic features.
                unavailable = (ModelStartup(profile=startup.ai.profile.model_copy(update={'status': 'invalid'}))
                               if startup.ai else None)
                startup = startup.model_copy(update={'ai': unavailable})
                install_startup(unavailable)
        try:
            owned_service = serve(identity, stopped, startup.ai)
            del startup, credential
            asyncio.run(owned_service)
        finally:
            engine.dispose()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as error:
        try:
            print(json.dumps({'event': 'failed', 'message': str(error)}), flush=True)
        except BrokenPipeError:
            pass
        sys.exit(1)

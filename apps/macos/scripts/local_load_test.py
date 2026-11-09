"""Bounded desktop-runtime acceptance using synthetic data in a temporary library."""
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import select
import sqlite3
import subprocess
import tempfile
import threading
import time
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    parser.add_argument('--duration', type=int, default=300)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not 10 <= args.duration <= 1800:
        parser.error('duration 必须为 10～1800 秒')
    runtime = args.runtime.resolve(strict=True)
    python = runtime / 'python/bin/python3'
    host = runtime / 'backend/app/local_host.py'
    if not python.is_file() or not host.is_file():
        parser.error('缺少独立 Python 或本地宿主')
    record = {'status': 'running', 'duration_requested_seconds': args.duration,
              'semantic_enabled': True, 'concurrent_clients': 4,
              'real_user_data_used': False, 'legacy_data_migrated': False,
              'started_at_utc': datetime.now(timezone.utc).isoformat(),
              'runtime_manifest_sha256': hashlib.sha256((runtime / 'manifest.json').read_bytes()).hexdigest()}
    credential = secrets.token_hex(24)
    latencies = []
    operations = Counter()
    created = set()
    guard = threading.Lock()
    stopped = threading.Event()
    memory = []
    with tempfile.TemporaryDirectory(prefix='pka-load-test.') as temporary:
        root = Path(temporary)
        data = root / 'data'
        environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'HOME': str(root),
                       'TMPDIR': temporary, 'PKA_ENVIRONMENT': 'local',
                       'PKA_DATA_DIR': str(data), 'PKA_CACHE_DIR': str(root / 'cache'),
                       'PKA_LOG_DIR': str(root / 'logs'),
                       'PKA_SEMANTIC_MODEL_PATH': str(runtime / 'model'),
                       'PKA_OCR_MODELS_DIR': str(runtime / 'ocr-models')}
        with (root / 'host.log').open('wb') as log:
            process = subprocess.Popen([str(python), '-I', '-B', str(host)], cwd=runtime,
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                       stderr=log, env=environment)
            address = None
            ready = None
            try:
                process.stdin.write((credential + '\n').encode())
                process.stdin.flush()
                if not select.select([process.stdout], [], [], 75)[0]:
                    raise AssertionError('本地宿主就绪超时')
                ready = json.loads(process.stdout.readline())
                assert ready['event'] == 'ready', '本地宿主启动失败'
                address = ready['address']
                assert address.startswith('http://127.0.0.1:')

                def call(path, payload=None, key=None, binary=None):
                    headers = {'Authorization': 'Bearer ' + credential}
                    body = binary
                    if payload is not None:
                        body = json.dumps(payload).encode()
                        headers['Content-Type'] = 'application/json'
                    elif binary is not None:
                        headers['Content-Type'] = 'application/octet-stream'
                    if key:
                        headers['Idempotency-Key'] = key
                    started = time.monotonic()
                    with urlopen(Request(address + path, data=body, headers=headers), timeout=30) as response:
                        content = response.read()
                        result = (json.loads(content) if 'application/json' in response.headers.get('Content-Type', '')
                                  else content)
                        status = response.status
                    with guard:
                        latencies.append(time.monotonic() - started)
                        operations[path.split('?')[0]] += 1
                    return status, result

                _, domain = call('/v1/domains', {'name': '持续并发验收'})
                domain_id = domain['id']
                deadline = time.monotonic() + args.duration

                def writer(number):
                    index = 0
                    while not stopped.is_set() and time.monotonic() < deadline:
                        key = f'load-note-{number}-{index}'
                        payload = {'capture_type': 'text', 'title': '笔记', 'coverage': 'full_text',
                                   'content': {'text': f'合成并发验收资料 {key}，用于验证本地知识库。'},
                                   'context': {'domain_id': domain_id},
                                   'client': {'kind': 'desktop', 'version': 'load-test'}}
                        status, source = call('/v1/captures', payload, key=key)
                        assert status == 201
                        status, replay = call('/v1/captures', payload, key=key)
                        assert status == 200 and replay['id'] == source['id']
                        with guard:
                            created.add(source['id'])
                        if index % 10 == 0:
                            body = f'name,value\nsynthetic-{number}-{index},1\n'.encode()
                            path = '/v1/file-captures?' + urlencode({'domain_id': domain_id,
                                                                  'filename': f'table-{number}-{index}.csv'})
                            status, file_source = call(path, key=f'load-csv-{number}-{index}', binary=body)
                            assert status == 201
                            status, replay = call(path, key=f'load-csv-{number}-{index}', binary=body)
                            assert status == 200 and replay['id'] == file_source['id']
                            _, downloaded = call(file_source['file']['download_path'])
                            assert downloaded == body
                            with guard:
                                created.add(file_source['id'])
                        index += 1
                        stopped.wait(min(4, max(0, deadline - time.monotonic())))

                def reader(number):
                    index = number
                    while not stopped.is_set() and time.monotonic() < deadline:
                        route = index % 4
                        if route == 0:
                            status, _ = call('/health')
                        elif route == 1:
                            status, me = call('/v1/me')
                            assert me['id'] == ready['owner_id']
                        elif route == 2:
                            status, _ = call('/v1/sources?limit=10')
                        else:
                            status, result = call('/v1/search', {'query': '合成并发验收资料', 'limit': 5})
                            assert 'items' in result
                        assert status == 200
                        index += 1
                        stopped.wait(min(1, max(0, deadline - time.monotonic())))

                def guarded(function, number):
                    try:
                        function(number)
                    except Exception:
                        stopped.set()
                        raise

                started = time.monotonic()
                with ThreadPoolExecutor(max_workers=4) as executor:
                    futures = [executor.submit(guarded, function, number)
                               for function in (writer, reader) for number in (0, 1)]
                    try:
                        while not all(future.done() for future in futures):
                            assert process.poll() is None, '负载期间宿主退出'
                            sample = subprocess.check_output(['/bin/ps', '-o', 'pid=,rss=', '-p',
                                                              f'{process.pid},{ready["worker_pid"]}'], text=True)
                            rows = [line.split() for line in sample.splitlines() if line.strip()]
                            assert len(rows) == 2, '负载期间宿主或 Worker 丢失'
                            memory.append({int(pid): int(rss) for pid, rss in rows})
                            stopped.wait(5)
                        for future in futures:
                            future.result()
                    except BaseException:
                        stopped.set()
                        raise
                record['load_elapsed_seconds'] = round(time.monotonic() - started, 2)

                def database_status():
                    with sqlite3.connect(f'file:{data / "knowledge.db"}?mode=ro', uri=True, timeout=10) as db:
                        jobs = {table: dict(db.execute(f'SELECT status, COUNT(*) FROM {table} GROUP BY status'))
                                for table in ('document_jobs', 'semantic_index_jobs')}
                        ids = {row[0] for row in db.execute('SELECT id FROM sources')}
                        integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
                    return jobs, ids, integrity

                drain_deadline = time.monotonic() + 120
                while True:
                    jobs, ids, integrity = database_status()
                    assert not any(counts.get('failed', 0) for counts in jobs.values()), '后台任务失败'
                    if not any(counts.get(state, 0) for counts in jobs.values() for state in ('pending', 'running')):
                        break
                    assert time.monotonic() < drain_deadline, '后台任务未在 120 秒内排空'
                    assert process.poll() is None
                    stopped.wait(2)
                assert ids == created and integrity == 'ok'
                assert jobs['semantic_index_jobs'].get('completed', 0) == len(created)
                # Load metrics exclude the separate final search-readiness assertion below.
                record.update(source_count=len(ids), jobs=jobs, sqlite_integrity=integrity,
                              idempotent_replays_preserved=True,
                              requests=sum(operations.values()), operations=dict(operations),
                              request_latency_p95_ms=round(sorted(latencies)[int((len(latencies) - 1) * .95)] * 1000, 2),
                              host_peak_rss_mib=round(max(row[process.pid] for row in memory) / 1024, 2),
                              worker_peak_rss_mib=round(max(row[ready['worker_pid']] for row in memory) / 1024, 2),
                              memory_samples=len(memory))
                _, search_result = call('/v1/search', {'query': '合成并发验收资料', 'limit': 5})
                assert search_result['items'] and search_result['semantic_index'] == 'ready'
                record['final_search_index_status'] = search_result['semantic_index']
                record['status'] = 'passed'
            except Exception as error:
                record.update(status='failed', error_type=type(error).__name__)
                raise
            finally:
                stopped.set()
                process.stdin.close()
                try:
                    process.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
                    record['status'] = 'failed'
                process.stdout.close()
                record['host_exit_code'] = process.returncode
                if ready and ready.get('event') == 'ready':
                    for name in ('.data.service.lock', '.data.instance.lock'):
                        with (root / name).open('rb') as descriptor:
                            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    record['exit_releases_locks'] = True
                    try:
                        urlopen(address + '/health', timeout=1).close()
                        record['status'] = 'failed'
                        record['exit_closes_port'] = False
                    except URLError:
                        record['exit_closes_port'] = True
                if process.returncode != 0:
                    record['status'] = 'failed'
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(record, ensure_ascii=False, indent=2))
    if record['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    main()

"""Packaged library-update mode and usage acceptance, temporary library only."""
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_domain_runtime_test import Gateway, wait
from packaged_kernel_test import PackagedHost


class ExternalHost(PackagedHost):
    def __init__(self, runtime, root, gateway):
        profile = dict(profile_id=str(uuid4()), config_revision=1, mode='external', status='ready',
            provider='openai-compatible', model='synthetic-runtime', external_url=gateway,
            credential_generation=str(uuid4()))
        super().__init__(runtime, root, fixture=False,
            startup=dict(profile=profile, gateway_token='synthetic-runtime-gateway'))


def exercise(host):
    call = host.call
    domain = call('/v1/domains', {'name': '更新知识库合成验收'}, expected=201)['id']
    base = f'/v1/domains/{domain}/analysis/full'
    ids = []
    for n in range(2):
        ids.append(call('/v1/captures', dict(capture_type='text', title=f'合成文章{n}',
            content={'text': '这是合成知识加工正文，检索与分析均需引用原文证据。'}, coverage='full_text',
            context={'domain_id': domain}, client={'kind': 'desktop', 'version': 'update-test'}), expected=201)['id'])

    def update(force=False):
        preview = call(base + '/preview', {'reanalyze': force})
        assert preview['reanalyze'] is force and not preview['blocked_sources'], preview
        before = sum(Gateway.calls.values())
        body = dict(request_id=str(uuid4()), preview_hash=preview['preview_hash'], confirmed=True, reanalyze=force)
        job = call(base + '/jobs', body, expected=202)
        assert call(base + '/jobs', body, expected=202)['id'] == job['id']
        def complete():
            result = call(base + '/jobs/' + job['id'])
            assert result['status'] in ('pending', 'running', 'completed'), result
            return result if result['status'] == 'completed' else None
        done = wait(complete, 'library update')
        count = sum(Gateway.calls.values()) - before
        assert done['token_usage'] == dict(input_tokens=count*10, output_tokens=count*10, total_tokens=count*20), done
        return preview, done

    first, done = update()
    assert first['analyze_sources'] == 2 and done['analyzed_sources'] == 2
    call('/v1/sources/' + ids[0] + '/revisions', {'text': '已修改的合成正文，旧分析已经过期。'}, expected=201)
    preview, skipped = update()
    assert preview['stale_sources'] == preview['skipped_sources'] == 1
    assert preview['analyze_sources'] == 0 and skipped['reused_sources'] == 1
    assert skipped['overview_coverage']['excluded'] == {ids[0]: 'skipped_stale_analysis'}
    navigation = call(f'/v1/domains/{domain}/analysis/navigation', {})
    assert navigation['overview']['available'], navigation
    preview, forced = update(True)
    assert preview['stale_sources'] == 1 and preview['skipped_sources'] == 0
    assert forced['analyzed_sources'] == 2 and forced['reused_sources'] == 0
    usage = forced['token_usage']
    before = dict(Gateway.calls)
    host.stop(); host.start()
    latest = call(base + '/latest')['job']
    assert latest['id'] == forced['id'] and latest['token_usage'] == usage and latest['reanalyze']
    assert dict(Gateway.calls) == before and not Gateway.errors
    print('PASS: 随包默认仅补未分析→过期跳过且概览排除→勾选全部重新分析→精确模型回执→重启保留且不重复调用', flush=True)


if __name__ == '__main__':
    runtime = Path(sys.argv[1]).resolve(strict=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='pka-library-update-test.') as directory:
            host = ExternalHost(runtime, Path(directory), f'http://127.0.0.1:{server.server_port}')
            try:
                host.start(); exercise(host)
            finally:
                host.stop()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)

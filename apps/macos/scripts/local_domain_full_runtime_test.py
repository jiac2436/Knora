"""Whole-domain acceptance using the actual bundled host and a synthetic gateway."""
import argparse
from http.server import ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import time
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_domain_runtime_test import Gateway, Host, wait


def exercise(host):
    call = host.call
    domain = call('/v1/domains', {'name': '全量分析合成知识域'}, expected=201)['id']
    base = f'/v1/domains/{domain}/analysis'
    ids = []
    for ordinal in range(3):
        source = call('/v1/captures', dict(capture_type='text', title=f'全量合成资料 {ordinal}',
            coverage='full_text', content={'text': f'合成知识加工资料{ordinal}：原文保留于本地，摘要和标签帮助查找，主题与知识域概览帮助组织资料，具体判断仍需核对原文。'},
            context={'domain_id': domain}, client={'kind': 'desktop', 'version': 'full-runtime-test'}), expected=201)
        ids.append(source['id'])
    payload = dict(source_id=ids[0], kind='analyze', contract_version=3)
    plan = call('/v1/ai/plan', payload)
    child = call('/v1/ai/jobs', {**payload, 'request_key': str(uuid4()), 'plan_hash': plan['plan_hash']}, expected=202)
    wait(lambda: call('/v1/ai/jobs/' + child['id'])['status'] == 'completed', 'existing single analysis')
    initial_calls = dict(Gateway.calls)
    assert call(base + '/full/latest')['job'] is None
    preview = call(base + '/full/preview', {'scope': 'app'})
    assert preview['total_sources'] == 3 and preview['reused_sources'] == 1 and preview['analyze_sources'] == 2
    assert not preview['blocked_sources']
    assert dict(Gateway.calls) == initial_calls, 'preview or history triggered a model request'
    body = dict(request_id=str(uuid4()), preview_hash=preview['preview_hash'], confirmed=True)
    queued = call(base + '/full/jobs', body, expected=202)
    replayed = call(base + '/full/jobs', body, expected=202)
    assert replayed['id'] == queued['id'], 'lost create response made another workflow'

    def complete():
        job = call(base + '/full/jobs/' + queued['id'])
        assert job['status'] not in {'failed', 'unknown', 'stale', 'cancelled', 'partial'}, job
        return job if job['status'] == 'completed' else None

    finished = wait(complete, 'full domain workflow', timeout=180)
    assert finished['completed_sources'] == 3 and finished['reused_sources'] == 1 and finished['analyzed_sources'] == 2
    assert finished['budget']['used_calls'] <= finished['budget']['calls']
    assert finished['budget']['used_input_tokens'] <= finished['budget']['input_token_reservation']
    assert finished['indexing']['semantic_enabled'] is False and finished['indexing']['original'] == 'disabled'
    assert all(count == 1 for count in Gateway.calls.values()), 'a model request was sent more than once'
    assert all(Gateway.calls[key] == count for key, count in initial_calls.items()), 'reused single analysis was sent again'
    information = call(base + '/information', {'scope': 'app'})
    assert information['documents']['analyzed'] == information['documents']['total'] == 3
    assert information['overview_coverage']['processed_source_count'] == 3
    assert information['rag']['keyword_available']
    navigation = call(base + '/navigation', {'scope': 'app'})
    assert navigation['overview']['available'] and navigation['topics']['items']
    for source_id in ids:
        tags = call(f'/v1/sources/{source_id}/tags')['tag_details']
        assert any('ai' in tag['origins'] for tag in tags)
    print('PASS: 随包全量入口→1篇复用+2篇补分析→AI标签→单次域概览→关键词索引', flush=True)

    before_restart = dict(Gateway.calls)
    host.stop(); host.start()
    assert call(base + '/full/latest')['job']['id'] == finished['id']
    assert call(base + '/full/jobs/' + finished['id'])['status'] == 'completed'
    assert call(base + '/navigation', {})['overview']['available']
    assert dict(Gateway.calls) == before_restart
    print('PASS: 关闭和重新启动实际宿主后保留任务、结果及索引，不重复收费调用', flush=True)

    # With every document already current, only the actual overview plan is
    # authorized. A synthetic failed provider request must stop the new workflow.
    # An identical direct overview now reuses the root with zero calls. Change
    # only navigation structure to exercise a genuinely fresh overview failure.
    call(f'/v1/domains/{domain}/topics', {'name': '合成验收分类'}, expected=201)
    Gateway.fail_node_once = True
    next_preview = call(base + '/full/preview', {'scope': 'app'})
    assert next_preview['reused_sources'] == 3 and next_preview['analyze_sources'] == 0
    next_job = call(base + '/full/jobs', dict(request_id=str(uuid4()),
        preview_hash=next_preview['preview_hash'], confirmed=True), expected=202)

    def stopped():
        state = call(base + '/full/jobs/' + next_job['id'])
        assert state['status'] not in {'failed', 'stale', 'cancelled', 'partial'}, state
        return state if state['status'] == 'unknown' else None

    wait(stopped, 'unknown full workflow', timeout=120)
    before_unknown_restart = dict(Gateway.calls)
    host.stop(); host.start()
    for _ in range(4):
        assert call(base + '/full/jobs/' + next_job['id'])['status'] == 'unknown'
        call(base + '/full/latest')
        time.sleep(.3)
    assert dict(Gateway.calls) == before_unknown_restart
    assert not Gateway.errors, Gateway.errors
    print('PASS: 全量后续概览模拟503→结果未知→查询与重启均不自动重播', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    runtime = parser.parse_args().runtime.resolve(strict=True)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='pka-domain-full-runtime.') as temporary:
            host = Host(runtime, Path(temporary), f'http://127.0.0.1:{server.server_port}')
            try:
                host.start(); exercise(host)
            except BaseException:
                try:
                    host.stop()
                finally:
                    log = Path(temporary) / 'host.log'
                    if log.exists():
                        print(log.read_text(errors='replace')[-12000:], file=sys.stderr)
                raise
            finally:
                host.stop()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    print('PASS: 全量功能随包验收完成，未调用真实模型或读取用户库', flush=True)


if __name__ == '__main__':
    main()

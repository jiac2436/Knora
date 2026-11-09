"""Explicit paid acceptance through packaged API/Worker; synthetic library only.

The forwarding guard caps sends and conservative input reservation across all
routes. A failed/unknown response closes the guard; no provider retry occurs.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4
import copy

REPO = Path(__file__).resolve().parents[1]
SAMPLES = [
    ('索引失败处理', '索引恢复', '决定：标签保存后自动更新本地检索索引。索引失败仅重建本地数据，不重新调用大模型。'),
    ('索引发布与恢复', '索引恢复', '知识加工使用SQLite保存原文与AI产物。正文版本、总结和AI标签在一个发布事务中校验，冲突就整体回滚。索引在发布后由本地worker建立。只有产物生效且索引就绪，任务才算完成。模型返回成功不能单独代表完成。恢复时复用有效产物，不能再次发送已完成的付费请求。'),
    ('缓存方案A：单机', '缓存边界', '方案A适用于单机只读资料。采用进程内缓存，缓存有效期为60秒。修改原文后必须立即失效，不能等有效期结束。此方案没有多节点一致性保障，不适用于多端并发修改。60秒是有效期，不是请求超时。'),
    ('缓存方案B：多端', '缓存边界', '方案B用于多端并发编辑，通过服务端版本号校验提交顺序。离线编辑必须在同步时校验版本，不能保证立即一致。此方案不要求取消单机缓存，也不能证明方案A错误，两者适用条件不同。测试预算为每月120元，这只是合成方案的预算，不是模型服务报价。'),
]


def check_build_budget(preview, calls, reserved):
    """Check both production plan reservation and the stricter forwarding cap."""
    if calls + preview['calls'] > 12:
        raise ValueError('分层计划会超过本轮12次调用上限；未创建任务')
    required = max(preview['input_token_reservation'], preview['calls'] * 100000)
    if reserved + required > 350000:
        raise ValueError('分层计划会超过本轮350000输入token预留；未创建任务')


def replay_response(previous, body):
    """Restore synthetic results, rebinding IDs only; never regenerate facts."""
    old = previous['input']
    if [e['text'] for e in old['evidence']] != [e['text'] for e in body['evidence']]:
        raise ValueError('合成原文与已完成请求不一致，禁止复用')
    identities = {a['id']: b['id'] for a, b in zip(old['evidence'], body['evidence'])}
    def rebind(value):
        if isinstance(value, dict): return {key: rebind(item) for key, item in value.items()}
        if isinstance(value, list): return [rebind(item) for item in value]
        return identities.get(value, value) if isinstance(value, str) else value
    response = rebind(copy.deepcopy(previous['response']))
    for tag in response['result']['tag_suggestions']:
        catalog = next((item for item in body.get('tags', [])
            if item['name'] == tag['name'] and item['dimension'] == tag['dimension']), None)
        tag['tag_id'] = catalog['id'] if catalog else None
    return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--real', action='store_true')
    parser.add_argument('--gateway-url', default='http://127.0.0.1:8091')
    parser.add_argument('--keychain-service')
    parser.add_argument('--prior-report', type=Path)
    parser.add_argument('--replay-only', action='store_true', help='Free restoration and plan preview, no provider requests')
    args = parser.parse_args()
    if args.report.exists():
        raise SystemExit('报告已存在；禁止覆盖已记录请求，请使用新路径并携带前次报告')
    prior = json.loads(args.prior_report.read_text()) if args.prior_report else None
    if prior:
        if args.report.resolve() == args.prior_report.resolve():
            raise SystemExit('禁止覆盖原验收报告')
        if len(prior['calls']) != 4 or any(c['status'] != 'completed' or c['route'] != '/v1/analyze' for c in prior['calls']):
            raise SystemExit('前次报告不是四篇已完成的合成分析')
        if prior['samples'] != [dict(title=t, topic=k, text=x) for t, k, x in SAMPLES]:
            raise SystemExit('前次合成样本不一致')
    report = dict(protocol='domain-deepseek-quality-v1', real_model=args.real,
        max_calls=12, max_input_token_reservation=350000, calls=[], stages=[],
        samples=[dict(title=t, topic=k, text=x) for t, k, x in SAMPLES],
        real_user_library_accessed=False, status='prepared')
    report['restorations'] = []
    if prior:
        report['calls'] = copy.deepcopy(prior['calls'])
        report['prior_report'] = str(args.prior_report)
    lock = threading.Lock()
    def save():
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    save()
    if not args.real and not args.replay_only:
        print('PREPARED: zero provider calls'); return
    token = os.getenv('PKA_AI_GATEWAY_TOKEN', '')
    if not token and args.keychain_service:
        result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s',
            args.keychain_service, '-a', 'session', '-w'], capture_output=True, timeout=30)
        if result.returncode == 0:
            token = result.stdout.decode().strip()
    if not token and not args.replay_only:
        raise SystemExit('网关凭证不可用；未发送模型请求')
    headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
    # Deliberately invalid request cannot pass the gateway contract or call LLM.
    probe = Request(args.gateway_url.rstrip('/') + '/v1/analyze', data=b'{}', headers=headers)
    if not args.replay_only:
        try:
            with urlopen(probe, timeout=10) as reply:
                code = reply.status
        except HTTPError as error:
            code = error.code
        if code != 422:
            raise SystemExit(f'网关鉴权/契约检查失败 HTTP {code}；未发送有效模型请求')
    spec = importlib.util.spec_from_file_location('domain_runtime_acceptance',
        REPO / 'apps/macos/scripts/local_domain_runtime_test.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)

    class Guard(BaseHTTPRequestHandler):
        stopped = False
        reserved = sum(c['reservation'] for c in report['calls'])
        def log_message(self, *_):
            pass
        def do_POST(self):
            if self.headers.get('Authorization') != 'Bearer synthetic-runtime-gateway':
                self.send_error(401); return
            raw = self.rfile.read(int(self.headers['Content-Length']))
            body = json.loads(raw)
            if self.path not in {'/v1/analyze', '/v1/summarize-node'}:
                self.send_error(404); return
            # Replay completed short synthetic documents without contacting the
            # gateway. The fourth document is the one targeted paid retest.
            previous = next((c for c in prior['calls'] if c['input']['evidence'][0]['text'] == body.get('evidence', [{}])[0].get('text')), None) if prior and self.path == '/v1/analyze' else None
            if previous and (args.replay_only or previous is not prior['calls'][-1]):
                response = replay_response(previous, body)
                data = json.dumps(response, ensure_ascii=False).encode()
                with lock:
                    report['restorations'].append(dict(request_id=body['request_id'],
                        original_request_id=previous['request_id'], provider_calls=0)); save()
                self.send_response(200); self.send_header('Content-Length', str(len(data)))
                self.end_headers(); self.wfile.write(data); return
            if args.replay_only:
                self.send_error(503, 'free preview cannot send provider requests'); return
            # UTF-8 bytes + 8192 covers fixed prompts and JSON overhead. Parent
            # nodes reserve 100k in the production planner; retain that ceiling.
            reserve = max(len(raw) + 8192, 100000 if self.path == '/v1/summarize-node' else 0)
            with lock:
                if self.stopped or len(report['calls']) >= 12 or self.reserved + reserve > 350000:
                    self.stopped = True; self.send_error(503); return
                type(self).reserved += reserve
                row = dict(route=self.path, request_id=body['request_id'],
                    reservation=reserve, input=body, status='sending')
                report['calls'].append(row); save()
            start = time.monotonic()
            try:
                request = Request(args.gateway_url.rstrip('/') + self.path, data=raw, headers=headers)
                try:
                    reply = urlopen(request, timeout=185)
                except HTTPError as error:
                    reply = error
                with reply:
                    data = reply.read(256 * 1024 + 1); code = reply.status
                if len(data) > 256 * 1024:
                    raise ValueError('response too large')
                parsed = json.loads(data)
                row.update(http_status=code, response=parsed, status='completed' if code == 200 else 'failed')
                if code == 200 and self.path == '/v1/analyze' and prior:
                    if parsed.get('prompt_version') != 'compact-knowledge-v3.1':
                        raise ValueError('running gateway has not loaded compact-knowledge-v3.1')
                    if any(t['name'] == '索引更新' for t in parsed['result']['tag_suggestions']):
                        raise ValueError('标签复测仍错误关联索引更新，停止后续调用')
                if code != 200:
                    type(self).stopped = True
            except Exception as error:
                type(self).stopped = True
                row.update(status='unknown', error_type=type(error).__name__)
                code, data = 504, b'{"detail":{"code":"provider_result_unknown"}}'
            row['seconds'] = round(time.monotonic() - start, 2)
            with lock: save()
            self.send_response(code); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data))); self.end_headers()
            try: self.wfile.write(data)
            except BrokenPipeError: pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Guard)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    report['status'] = 'running'; save()
    try:
        with tempfile.TemporaryDirectory(prefix='pka-real-domain-quality.') as folder:
            host = module.Host(args.runtime.resolve(), Path(folder), f'http://127.0.0.1:{server.server_port}')
            try:
                host.start(); call = host.call
                domain = call('/v1/domains', {'name': '合成知识加工与缓存决策'}, expected=201)['id']
                base = '/v1/domains/' + domain
                topics, ids = {}, []
                for title, topic_name, text in SAMPLES:
                    capture = call('/v1/captures', dict(capture_type='text', title=title, coverage='full_text',
                        content={'text': text}, context={'domain_id': domain},
                        client={'kind': 'desktop', 'version': 'real-quality-test'}), expected=201)
                    source = capture['id']; ids.append(source)
                    payload = dict(source_id=source, kind='analyze', contract_version=3)
                    preview = call('/v1/ai/plan', payload)
                    assert preview['calls'] == 1, '本轮仅授权每篇单次分析'
                    queued = call('/v1/ai/jobs', {**payload, 'request_key': str(uuid4()),
                        'plan_hash': preview['plan_hash']}, expected=202)
                    def complete():
                        state = call('/v1/ai/jobs/' + queued['id'])
                        assert state['status'] not in {'failed', 'unknown', 'cancelled'}, state
                        return state if state['status'] == 'completed' else None
                    completed = module.wait(complete, 'real single analysis', timeout=200)
                    artifact = call('/v1/ai/artifacts/' + completed['artifact_id'])
                    assert not artifact['stale'] and artifact['result']['contract_version'] == 3
                    report['stages'].append(dict(kind='single', title=title, artifact=artifact)); save()
                    print('PASS single', title, flush=True)
                    if topic_name not in topics:
                        topics[topic_name] = call(base + '/topics', {'name': topic_name}, expected=201)['id']
                    call(base + f'/topics/{topics[topic_name]}/members/{source}', {'included': True}, method='PUT')
                preview = call(base + '/analysis/build/preview', {})
                report['unrestricted_build_preview'] = preview; save()
                # Automatic candidates can overlap the manually frozen test
                # groups. Exclude only in this disposable library, via Owner API.
                offset = 0
                while True:
                    page = call(base + f'/topics?limit=100&offset={offset}')
                    for topic in page['items']:
                        if topic['id'] not in topics.values():
                            for source in ids:
                                call(base + f'/topics/{topic["id"]}/members/{source}', {'included': False}, method='PUT')
                    if page.get('next_offset') is None: break
                    offset = page['next_offset']
                preview = call(base + '/analysis/build/preview', {})
                report['build_preview'] = preview; save()
                # Guard also caps the conservative actual sends across singles/nodes.
                # A replay-only plan includes the old B result; allow one future
                # single retest with a conservative 12000 reservation.
                check_build_budget(preview, len(report['calls']) + (1 if args.replay_only else 0),
                    Guard.reserved + (12000 if args.replay_only else 0))
                if args.replay_only:
                    report['status'] = 'free_restoration_and_plan_passed'; save(); return
                call(base + '/analysis/authorization', dict(authorization_id=str(uuid4()),
                    policy=dict(enabled=True, max_calls=12-len(report['calls']),
                        max_input_tokens=350000-Guard.reserved)), method='PUT')
                queued = call(base + '/analysis/build/jobs', {'request_id': str(uuid4()),
                    'preview_hash': preview['preview_hash']})
                def build_complete():
                    state = call(base + '/analysis/build/jobs/' + queued['id'])
                    assert state['status'] not in {'failed', 'unknown', 'cancelled', 'stale'}, state
                    return state if state['status'] == 'completed' else None
                report['build'] = module.wait(build_complete, 'real hierarchical build', timeout=600)
                report['navigation'] = call(base + '/analysis/navigation', {})
                assert report['navigation']['overview']['available']
                report['topics'] = {name: call(base + '/analysis/navigation', {'topic_id': topic})
                    for name, topic in topics.items()}
                assert all(view['overview']['available'] for view in report['topics'].values())
                report['retrieval'] = {query: call('/v1/search', {'query': query, 'limit': 10}) for query in
                    ['索引失败是否需要重新调用模型', '60秒缓存适用什么条件', '多端是否立即一致', '120元是模型报价吗']}
                count = len(report['calls']); host.stop(); host.start()
                assert call(base + '/analysis/navigation', {})['overview']['available']
                assert len(report['calls']) == count
                report['status'] = 'engineering_passed_pending_semantic_review'
            finally:
                host.stop()
    except Exception as error:
        report.update(status='stopped', failure_type=type(error).__name__, failure=str(error)[:1800])
        print('STOP', type(error).__name__, flush=True)
    finally:
        Guard.stopped = True; server.shutdown(); server.server_close(); thread.join(timeout=5)
        report['reserved_input_tokens'] = Guard.reserved
        report['actual_usage'] = {name: sum(row.get('response', {}).get('usage', {}).get(name, 0)
            for row in report['calls']) for name in ['prompt_tokens', 'completion_tokens']}
        save()
    print('REPORT', args.report, report['status'], flush=True)
    if report['status'] == 'stopped': raise SystemExit(1)


if __name__ == '__main__':
    main()

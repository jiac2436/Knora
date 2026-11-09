"""Isolated packaged-runtime acceptance; synthetic loopback models, never user data.

Run with the assembled runtime's Python:
  runtime/python/bin/python3 -I -B local_domain_runtime_test.py runtime
"""
import argparse
import asyncio
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import select
import subprocess
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


class Gateway(BaseHTTPRequestHandler):
    calls = Counter()
    fail_node_once = False
    errors = []

    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            assert self.headers.get('Authorization') == 'Bearer synthetic-runtime-gateway'
            assert self.path in {'/v1/analyze', '/v1/summarize-node'}
            size = int(self.headers['Content-Length'])
            assert 0 < size < 400000
            body = json.loads(self.rfile.read(size))
            type(self).calls[body['request_id']] += 1
            refs = [body['evidence'][0]['id']]
            result = dict(contract_version=1 if self.path == '/v1/summarize-node' else body.get('contract_version', 1),
                overview=dict(text='合成知识加工概览。', citations=refs), insights=[], limitations=[])
            if self.path == '/v1/summarize-node':
                if type(self).fail_node_once:
                    type(self).fail_node_once = False
                    self.send_error(503, 'synthetic unknown result')
                    return
            else:
                known = next((tag for tag in body.get('tags', []) if tag['name'] == '合成知识加工'), None)
                result.update(summary='合成资料说明本地知识加工。', summary_citations=refs,
                    organizing_suggestion=None, warnings=[], tag_suggestions=[dict(
                        tag_id=known['id'] if known else None, name='合成知识加工', dimension='topic',
                        reason='合成原文主题', citations=refs)])
            response = dict(result=result, provider='synthetic',
                model='mock-no-fees', prompt_version='runtime-test',
                usage=dict(prompt_tokens=10, completion_tokens=10))
            if self.path == '/v1/summarize-node':
                response['request_id'] = body['request_id']
            data = json.dumps(response, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as error:
            type(self).errors.append(type(error).__name__ + ': ' + str(error))
            self.send_error(500, 'synthetic fixture failure')


def wait(check, label, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(.3)
    raise AssertionError(label + ' timeout')


class Host:
    def __init__(self, runtime, root, gateway):
        self.runtime, self.root = runtime, root
        self.credential = secrets.token_hex(24)
        self.env = dict(PATH='/usr/bin:/bin:/usr/sbin:/sbin', HOME=str(root), TMPDIR=str(root),
            PKA_ENVIRONMENT='local', PKA_DATA_DIR=str(root / 'data'),
            PKA_CACHE_DIR=str(root / 'cache'), PKA_LOG_DIR=str(root / 'logs'),
            PKA_SEMANTIC_ENABLED='0', PKA_AI_GATEWAY_URL=gateway,
            PKA_AI_GATEWAY_TOKEN='synthetic-runtime-gateway')
        self.process = self.log = None

    def start(self):
        self.log = (self.root / 'host.log').open('ab')
        self.process = subprocess.Popen([str(self.runtime / 'python/bin/python3'), '-I', '-B',
            str(self.runtime / 'backend/app/local_host.py')], cwd=self.runtime,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, env=self.env)
        self.process.stdin.write((self.credential + '\n').encode()); self.process.stdin.flush()
        assert select.select([self.process.stdout], [], [], 75)[0], 'host readiness timeout'
        ready = json.loads(self.process.stdout.readline())
        assert ready['event'] == 'ready', ready
        self.address = ready['address']
        assert self.address.startswith('http://127.0.0.1:')

    def stop(self):
        if self.process:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                self.process.kill(); self.process.wait(timeout=10)
            self.process.stdout.close()
            self.log.close()
            exit_code = self.process.returncode
            self.process = None
            assert exit_code == 0, f'host shutdown failed ({exit_code})'

    def call(self, path, body=None, method=None, token=None, expected=200):
        assert self.process.poll() is None, 'host exited'
        headers = {'Authorization': 'Bearer ' + (token or self.credential)}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        if path == '/v1/captures':
            headers['Idempotency-Key'] = str(uuid4())
        request = Request(self.address + path, data=data, headers=headers, method=method)
        try:
            response = urlopen(request, timeout=30)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            assert response.status == expected, f'{path}: HTTP {response.status} {raw[:1200]!r}'
            return json.loads(raw) if raw else None


async def mcp_check(host, agent_token, source_ids):
    # The test itself uses the runtime SDK, while the server runs separately in
    # isolated Python and accesses only the authenticated loopback HTTP bridge.
    sys.path.insert(0, str(host.runtime / 'backend'))
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = {**host.env, 'PKA_API_URL': host.address, 'PKA_AGENT_TOKEN': agent_token}
    params = StdioServerParameters(command=str(host.runtime / 'python/bin/python3'),
        args=['-I', '-B', '-c',
              'import sys; sys.path.insert(0, sys.argv[1]); from app.mcp_server import main; main()',
              str(host.runtime / 'backend')], env=env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {'search_knowledge', 'get_evidence'} <= {tool.name for tool in tools.tools}
            found = await session.call_tool('search_knowledge', {'query': '合成知识加工', 'limit': 10})
            assert not found.isError, found
            body = found.structuredContent or json.loads(found.content[0].text)
            assert source_ids <= {item['source_id'] for item in body['items']}
            evidence = await session.call_tool('get_evidence', {'evidence_id': body['items'][0]['evidence_id']})
            assert not evidence.isError, evidence
            original = evidence.structuredContent or json.loads(evidence.content[0].text)
            assert '合成知识加工' in original['text']


def exercise(host):
    call = host.call
    domain = call('/v1/domains', {'name': '合成运行时领域'}, expected=201)['id']
    base = '/v1/domains/' + domain
    ids = []

    def analyze(source):
        payload = dict(source_id=source, kind='analyze', contract_version=3)
        plan = call('/v1/ai/plan', payload)
        job = call('/v1/ai/jobs', {**payload, 'request_key': str(uuid4()), 'plan_hash': plan['plan_hash']}, expected=202)
        def complete():
            state = call('/v1/ai/jobs/' + job['id'])
            assert state['status'] not in {'failed', 'unknown', 'cancelled'}, state
            return state if state['status'] == 'completed' else None
        completed = wait(complete, 'single analysis')
        artifact = call('/v1/ai/artifacts/' + completed['artifact_id'])
        assert artifact['result']['contract_version'] == 3 and not artifact['stale']
        tags = call('/v1/sources/' + source + '/tags')['tag_details']
        assert any(tag['name'] == '合成知识加工' and 'ai' in tag['origins'] for tag in tags)

    for number in range(2):
        capture = call('/v1/captures', dict(capture_type='text', title='合成资料' + str(number),
            coverage='full_text', content={'text': f'合成知识加工测试资料{number}：本地知识库保留原文，标签辅助查找，概览用于导航，事实需要原文证据。'},
            context={'domain_id': domain}, client={'kind': 'desktop', 'version': 'synthetic-runtime'}), expected=201)
        ids.append(capture['id'])
        call('/v1/sources/' + ids[-1] + '/context', {'visibility': 'app_and_agents'}, method='PATCH')
        analyze(ids[-1])
    print('PASS: HTTP采集→单篇v3加工与自动标签', flush=True)
    topic = call(base + '/topics', {'name': '合成主题'}, expected=201)['id']
    for source in ids:
        call(base + f'/topics/{topic}/members/{source}', {'included': True}, method='PUT')
    call(base + '/analysis/authorization', dict(authorization_id=str(uuid4()),
        policy=dict(enabled=True, max_calls=30, max_input_tokens=3000000)), method='PUT')

    def build(topic_id=None, unknown=False):
        selection = {'topic_id': topic_id} if topic_id else {}
        preview = call(base + '/analysis/build/preview', selection)
        request = {**selection, 'request_id': str(uuid4()), 'preview_hash': preview['preview_hash']}
        queued = call(base + '/analysis/build/jobs', request)
        def done():
            state = call(base + '/analysis/build/jobs/' + queued['id'])
            if unknown:
                assert state['status'] not in {'failed', 'cancelled', 'stale'}, state
                return state if state['status'] == 'unknown' else None
            assert state['status'] not in {'failed', 'unknown', 'cancelled', 'stale'}, state
            return state if state['status'] == 'completed' else None
        return wait(done, 'domain build')

    build(topic)
    complete = build()
    nav = call(base + '/analysis/navigation', {})
    assert nav['overview']['available'] and nav['topics']['items']
    root_id = nav['overview']['id']
    processing = call(base + '/analysis/processing-nodes/' + root_id, {})
    assert processing['available'] and processing['children']
    for child in processing['children']:
        assert call(base + '/analysis/processing-nodes/' + child['artifact_id'], {})['available']
    assert call(base + '/analysis/navigation', {'topic_id': topic})['overview']['available']
    assert call(base + '/analysis/schedule-state', {})['latest_build']['id'] == complete['id']
    search = call('/v1/search', {'query': '合成知识加工', 'limit': 10})
    assert set(ids) <= {item['source_id'] for item in search['items']}
    before_reads = dict(Gateway.calls)
    library = call(base + '/analysis/library', {'scope': 'app'})
    assert library['source_count'] == 2 and library['unclassified_count'] == 0
    assert any(row['id'] == topic and row['source_count'] == 2 for row in library['items'])
    first_page = call(base + '/analysis/library/sources', {'topic_id': topic, 'limit': 1})
    assert first_page['total_count'] == 2 and first_page['next_offset'] == 1
    second_page = call(base + '/analysis/library/sources', {'topic_id': topic, 'limit': 1, 'offset': 1})
    assert {row['id'] for page in [first_page, second_page] for row in page['items']} == set(ids)
    information = call(base + '/analysis/information', {})
    assert information['documents']['total'] == information['documents']['analyzed'] == 2
    assert information['overview_coverage']['processed_source_count'] == 2
    assert information['storage']['physical_bytes'] is None
    assert information['storage']['measurement'] == 'logical_partial'
    assert not information['rag']['semantic_enabled'] and information['rag']['keyword_available']
    assert any('ai' in tag['origins'] for tag in information['tags']['items'])
    references = nav['overview']['result']['overview']['citations']
    citations = call(base + '/analysis/citations/' + root_id, {'citation_ids': references})
    assert citations['items'] and {item['source_id'] for item in citations['items']} <= set(ids)
    for citation in citations['items']:
        source = call('/v1/sources/' + citation['source_id'])
        assert citation['text'] in source['extracted_text']
    assert dict(Gateway.calls) == before_reads
    print('PASS: 随包三页只读API→分类分页→有效分析/存储/RAG统计→精确原文依据，无额外模型调用', flush=True)
    token = call('/v1/agent-connections', {'client_name': 'synthetic-runtime', 'domain_ids': [domain]}, expected=201)['access_token']
    asyncio.run(mcp_check(host, token, set(ids)))
    call(base + '/analysis/navigation', {}, token=token, expected=401)
    call(base + '/analysis/information', {}, token=token, expected=401)
    print('PASS: 主题/域加工→导航下钻→原文检索→真实MCP stdio只读调用', flush=True)
    before = dict(Gateway.calls)
    host.stop(); host.start()
    assert call(base + '/analysis/navigation', {})['overview']['available']
    assert call(base + '/analysis/processing-nodes/' + root_id, {})['available']
    assert dict(Gateway.calls) == before
    print('PASS: 重启保留概览与下钻，未重复调用模型', flush=True)
    call('/v1/sources/' + ids[0] + '/revisions', {'text': '合成知识加工修改内容：当前原文已更新，旧概览不得继续作为当前知识。'}, expected=201)
    assert not call(base + '/analysis/navigation', {})['overview']['available']
    assert not call(base + '/analysis/processing-nodes/' + root_id, {})['available']
    analyze(ids[0]); build()
    deleted = call('/v1/sources/' + ids[1], method='DELETE', expected=202)
    assert not call(base + '/analysis/navigation', {})['overview']['available']
    def deletion_complete():
        operation = call('/v1/operations/' + deleted['operation_id'])
        assert operation['status'] != 'failed', operation
        return operation['status'] in {'trashed', 'completed'}
    wait(deletion_complete, 'source deletion')
    print('PASS: 原文变更和删除均使旧概览失效', flush=True)
    Gateway.fail_node_once = True
    unknown = build(unknown=True)
    before = dict(Gateway.calls)
    host.stop(); host.start()
    for _ in range(6):
        assert call(base + '/analysis/build/jobs/' + unknown['id'])['status'] == 'unknown'
        call(base + '/analysis/schedule-state', {})
        time.sleep(.5)
    assert dict(Gateway.calls) == before
    assert not Gateway.errors, Gateway.errors
    print('PASS: 模拟503结果未知，查询和重启均不重播收费请求', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime', type=Path)
    args = parser.parse_args()
    runtime = args.runtime.resolve(strict=True)
    assert (runtime / 'python/bin/python3').is_file()
    assert (runtime / 'backend/app/local_host.py').is_file()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='pka-domain-runtime.') as temporary:
            host = Host(runtime, Path(temporary), f'http://127.0.0.1:{server.server_port}')
            try:
                host.start(); exercise(host)
            except BaseException:
                print('FAIL: runtime验收；隔离宿主日志如下', file=sys.stderr)
                try:
                    host.stop()
                except Exception as cleanup_error:
                    print('宿主清理: ' + str(cleanup_error), file=sys.stderr)
                log = Path(temporary) / 'host.log'
                if log.exists():
                    print(log.read_text(errors='replace')[-12000:], file=sys.stderr)
                raise
            finally:
                host.stop()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)
    print('PASS: 全流程模拟验收完成；semantic关闭，未使用真实模型或用户库', flush=True)


if __name__ == '__main__':
    main()

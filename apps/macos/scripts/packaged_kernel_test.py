"""M5 isolated package acceptance; only the provider transport is a test double.

Run using the candidate's isolated Python. All API, worker, executor, prompt and
validation modules load from its runtime, without repository Python or services.
"""
import asyncio
import base64
import json
import os
from pathlib import Path
import secrets
import select
import subprocess
import sys
import tempfile
import time
from urllib.request import Request, urlopen
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))
from local_domain_runtime_test import Host, wait, mcp_check


def owned_entry(runtime, events, worker):
    sys.path.insert(0, str(runtime / 'backend'))
    sys.argv = sys.argv[:1]  # The production worker owns its own CLI parser.
    if worker:
        import httpx
        from app import embedded_gateway
        from services.ai_gateway.execution import ModelExecutor

        def provider(request):
            assert request.headers['Authorization'] == 'Bearer synthetic-m5-provider'
            payload = json.loads(request.content)
            value = payload['messages'][1]['content']
            has_image = isinstance(value, list)
            value = json.loads(next(p['text'] for p in value if p['type'] == 'text') if has_image else value)
            if 'question' in value:
                kind = 'answer'
                result = dict(answer='合成知识加工保留原文，图示增长37%。',
                    citations=[value['evidence'][0]['id']], insufficient_evidence=False, warnings=[])
            elif 'summaries' in value:
                kind = 'node'
                result = dict(contract_version=1, overview=dict(text='合成知识加工围绕原文、图表与检索。',
                    citations=[value['summaries'][0]['citations'][0]]), insights=[], limitations=[])
            else:
                kind = 'image' if has_image else value.get('phase', 'analysis')
                refs = [value['evidence'][0]['id']]
                if has_image:
                    assert any(p['type'] == 'image_url' for p in payload['messages'][1]['content'])
                    refs = [value['images'][0]['evidence_id']]
                known = next((t for t in value.get('tags', []) if t['name'] == '合成知识加工'), None)
                result = dict(contract_version=value.get('contract_version', 1),
                    overview=dict(text='合成知识加工资料，图示增长37%。', citations=refs),
                    summary='合成知识加工保留原文，概览用于导航，图示增长37%。', summary_citations=refs,
                    insights=[], limitations=[], organizing_suggestion=None, warnings=[],
                    tag_suggestions=[] if value.get('phase') == 'segment' else [dict(
                        tag_id=known['id'] if known else None, name='合成知识加工', dimension='topic',
                        reason='合成主题', citations=refs)])
            with events.open('a') as output:
                output.write(json.dumps({'kind': kind}) + '\n')
            if value.get('question', '').endswith('SYNTHETIC_UNKNOWN'):
                raise httpx.ReadTimeout('synthetic result unknown')
            return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
                'message': {'content': json.dumps(result, ensure_ascii=False)}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 60}})

        embedded_gateway.create_executor = lambda configuration: ModelExecutor(configuration, transport=httpx.MockTransport(provider))
        from app.local_host import run_owned_worker
        run_owned_worker()
    else:
        from app.platform_support.macos import MacOwnedProcesses
        original = MacOwnedProcesses.start_worker

        def start(self, arguments, environment, startup, diagnostics):
            command = [arguments[0], '-I', '-B', str(Path(__file__).resolve()),
                       str(runtime), '--fixture-worker', str(events)]
            return original(self, command, environment, startup, diagnostics)

        MacOwnedProcesses.start_worker = start
        from app.local_host import main
        main()


class PackagedHost(Host):
    def __init__(self, runtime, root, state='ready', *, startup=None, fixture=True):
        super().__init__(runtime, root, '')
        self.state = state
        self.startup, self.fixture = startup, fixture
        self.profile = dict(profile_id=str(uuid4()), config_revision=1, mode='direct', status=state,
            provider='openai-compatible', base_url='https://synthetic-m5.invalid/v1', model='synthetic-m5',
            vision_model='synthetic-m5-vision', credential_ref='synthetic-memory', credential_generation=str(uuid4()))
        self.events = root / 'provider-events.jsonl'
        self.env = {k: v for k, v in self.env.items() if not k.startswith('PKA_AI_')}
        self.env.update(PKA_PLATFORM_HELPER=str(runtime / 'platform/macos/KnowledgePlatform'),
            PKA_SEMANTIC_MODEL_PATH=str(runtime / 'model'), PKA_OCR_MODELS_DIR=str(runtime / 'ocr-models'))

    def start(self):
        self.log = (self.root / 'host.log').open('ab')
        command = [str(self.runtime / 'python/bin/python3'), '-I', '-B']
        if self.state == 'ready' and self.fixture:
            command += [str(Path(__file__).resolve()), str(self.runtime), '--fixture-host', str(self.events)]
        else:
            command += [str(self.runtime / 'backend/app/local_host.py')]
        self.process = subprocess.Popen(command, cwd=self.runtime, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=self.log, env=self.env)
        ai = self.startup or dict(profile=self.profile, provider_key='synthetic-m5-provider' if self.state == 'ready' else '',
                  gateway_token='synthetic-m5-local' if self.state == 'ready' else '')
        self.process.stdin.write((json.dumps(dict(version=1, owner_credential=self.credential,
            client_contract_version=1, client_schema_version=16, ai=ai)) + '\n').encode())
        self.process.stdin.flush()
        assert select.select([self.process.stdout], [], [], 75)[0], 'packaged readiness timeout'
        line = self.process.stdout.readline()
        assert line, 'packaged host exited: ' + (self.root / 'host.log').read_text(errors='replace')[-2000:]
        ready = json.loads(line)
        assert ready['event'] == 'ready' and ready['kernel']['capabilities']['capture_queue'], ready
        self.address = ready['address']
        self.worker_pid = ready['worker_pid']

    def count(self):
        return self.events.read_text().count('\n') if self.events.exists() else 0

    def ask(self, session, question):
        request = Request(self.address + '/v1/rag/turns', data=json.dumps(dict(
            session_id=session, question=question, confirm_send=True)).encode(),
            headers={'Authorization': 'Bearer ' + self.credential, 'Content-Type': 'application/json',
                     'Idempotency-Key': str(uuid4())})
        with urlopen(request, timeout=30) as response:
            assert response.status == 202
            return json.load(response)


def capture(host, domain, text, title='合成知识加工'):
    return host.call('/v1/captures', dict(capture_type='text', title=title, coverage='full_text',
        content={'text': text}, context={'domain_id': domain}, client={'kind': 'desktop', 'version': 'M5'}), expected=201)['id']


def analyze(host, source):
    selection = dict(source_id=source, kind='analyze', contract_version=3)
    preview = host.call('/v1/ai/plan', selection)
    request = {**selection, 'request_key': str(uuid4()), 'plan_hash': preview['plan_hash']}
    queued = host.call('/v1/ai/jobs', request, expected=202)
    replay = host.call('/v1/ai/jobs', request, expected=202)
    assert replay['id'] == queued['id']
    def done():
        job = host.call('/v1/ai/jobs/' + queued['id'])
        assert job['status'] not in {'failed', 'unknown', 'cancelled'}, job
        return job if job['pipeline_status'] == 'completed' else None
    job = wait(done, 'packaged analysis', timeout=180)
    artifact = host.call('/v1/ai/artifacts/' + job['artifact_id'])
    assert not artifact['stale']
    return artifact


def main(runtime):
    with tempfile.TemporaryDirectory(prefix='pka-m5-package-') as temporary:
        root = Path(temporary)
        for state in ['missing', 'denied', 'unavailable']:
            home = root / state
            home.mkdir()
            host = PackagedHost(runtime, home, state)
            try:
                host.start()
                domain = host.call('/v1/domains', {'name': '无模型基础库'}, expected=201)['id']
                source = capture(host, domain, '合成知识加工：无模型时仍可保存和检索。')
                assert host.call('/v1/sources/' + source)['extracted_text']
                assert host.call('/v1/search', {'query': '合成知识加工', 'limit': 10})['items']
                assert host.count() == 0
            finally:
                host.stop()
        print('PASS: 随包解释器独立启动；缺失、拒绝、暂不可用模型凭证均保留基础阅读和搜索', flush=True)
        home = root / 'direct'
        home.mkdir()
        host = PackagedHost(runtime, home)
        try:
            host.start()
            domain = host.call('/v1/domains', {'name': 'M5合成知识域'}, expected=201)['id']
            source = capture(host, domain, '合成知识加工：原文留在本机，概览用于导航，图示增长37%，事实必须查原文。')
            analyze(host, source)
            long_source = capture(host, domain, ('合成知识加工：分段保留原文与上下文，检索需要可回溯依据。\n' * 1800), '合成知识加工长文')
            analyze(host, long_source)
            from PIL import Image
            from io import BytesIO
            output = BytesIO()
            Image.new('RGB', (20, 20), 'white').save(output, 'PNG')
            image = str(uuid4())
            article = host.call('/v1/captures', dict(capture_type='feishu_document', title='合成知识加工图片',
                source_url='https://synthetic.feishu.cn/docx/SyntheticDocument',
                content={'text': f'合成知识加工图片。\n![图](know-image://{image})'}, coverage='full_text',
                context={'domain_id': domain}, client={'kind': 'desktop', 'version': 'M5'},
                feishu_document={'document_id': 'SyntheticDocument', 'revision_id': 1, 'cli_version': 'M5'},
                feishu_images=[{'id': image, 'ordinal': 0, 'caption': '合成图', 'block_id': 'block'}]), expected=201)['id']
            request = Request(host.address + f'/v1/sources/{article}/images/{image}', data=output.getvalue(),
                method='PUT', headers={'Authorization': 'Bearer ' + host.credential, 'Content-Type': 'application/octet-stream'})
            with urlopen(request, timeout=30) as response:
                assert response.status == 201
            artifact = analyze(host, article)
            assert artifact['analysis_plan']['image_count'] == 1
            print('PASS: 内置执行器→真实供应商协议替身→短文、长文分段合成、图片分析与索引；重复任务不重复发送', flush=True)
            session = host.call('/v1/rag/sessions', {}, expected=201)['id']
            turn = host.ask(session, '合成知识加工说明了什么？')
            def answered():
                value = next(t for t in host.call('/v1/rag/turns?session_id=' + session)['items'] if t['id'] == turn['id'])
                assert value['status'] not in {'failed', 'unknown'}, value
                return value if value['status'] == 'succeeded' else None
            assert wait(answered, 'packaged RAG')['citations']
            base = '/v1/domains/' + domain
            topic = host.call(base + '/topics', {'name': '合成分类'}, expected=201)['id']
            for identifier in (source, article, long_source):
                host.call(base + f'/topics/{topic}/members/{identifier}', {'included': True}, method='PUT')
            host.call(base + '/analysis/authorization', dict(authorization_id=str(uuid4()),
                policy={'enabled': True, 'max_calls': 40, 'max_input_tokens': 4_000_000}), method='PUT')
            for selection in ({'topic_id': topic}, {}):
                preview = host.call(base + '/analysis/build/preview', selection)
                assert isinstance(preview['calls'], int) and preview['calls'] <= 40
                build = host.call(base + '/analysis/build/jobs', dict(**selection, request_id=str(uuid4()), preview_hash=preview['preview_hash']))
                def built():
                    value = host.call(base + '/analysis/build/jobs/' + build['id'])
                    assert value['status'] not in {'failed', 'unknown', 'cancelled', 'stale'}, value
                    return value if value['status'] == 'completed' else None
                wait(built, 'packaged node build', timeout=180)
            assert host.call(base + '/analysis/navigation', {})['overview']['available']
            assert host.call(base + '/analysis/navigation', {'topic_id': topic})['overview']['available']
            print('PASS: 内置问答、主题与知识域概览三个调用点均在随包Worker完成，无外置网关', flush=True)
            for identifier in (source, article, long_source):
                host.call('/v1/sources/' + identifier + '/context', {'visibility': 'app_and_agents'}, method='PATCH')
            token = host.call('/v1/agent-connections', {'client_name': 'M5', 'domain_ids': [domain]}, expected=201)['access_token']
            asyncio.run(mcp_check(host, token, {source, article, long_source}))
            unknown_session = host.call('/v1/rag/sessions', {}, expected=201)['id']
            unknown_turn = host.ask(unknown_session, '合成知识加工 SYNTHETIC_UNKNOWN')
            def unknown():
                value = next(t for t in host.call('/v1/rag/turns?session_id=' + unknown_session)['items'] if t['id'] == unknown_turn['id'])
                assert value['status'] != 'failed', value
                return value if value['status'] == 'unknown' else None
            wait(unknown, 'packaged unknown result')
            before = host.count()
            host.stop()
            assert not any(b'synthetic-m5-provider' in p.read_bytes() for p in home.rglob('*') if p.is_file())
            host.start()
            assert host.call('/v1/sources/' + article)['images'][0]['status'] == 'saved'
            assert unknown()['status'] == 'unknown'
            time.sleep(1)
            assert host.count() == before
            print('PASS: 随包MCP原文检索、重启保留图片与产物、未知请求不重放，临时持久文件无供应商密钥', flush=True)
        finally:
            host.stop()


if __name__ == '__main__':
    runtime = Path(sys.argv[1]).resolve(strict=True)
    if len(sys.argv) > 2:
        owned_entry(runtime, Path(sys.argv[3]), sys.argv[2] == '--fixture-worker')
    else:
        main(runtime)

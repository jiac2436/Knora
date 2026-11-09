"""Packaged API/worker image analysis with a synthetic, free gateway."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
from urllib.request import Request, urlopen
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_domain_runtime_test import Host, wait


class Gateway(BaseHTTPRequestHandler):
    calls = 0
    errors = []
    image = b''

    def log_message(self, *_):
        pass

    def do_POST(self):
        try:
            assert self.path == '/v1/analyze'
            assert self.headers['Authorization'] == 'Bearer synthetic-runtime-gateway'
            wire = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            picture = wire['images'][0]
            assert base64.b64decode(picture['data_url'].split(',', 1)[1]) == type(self).image
            evidence = next(e for e in wire['evidence'] if e['id'] == picture['evidence_id'])
            assert evidence['image_id'] == picture['id']
            type(self).calls += 1
            output = dict(result=dict(contract_version=3,
                overview=dict(text='图示增长37%。', citations=[evidence['id']]),
                summary='图示增长37%。', summary_citations=[evidence['id']], insights=[],
                limitations=[], tag_suggestions=[], organizing_suggestion=None, warnings=[]),
                provider='synthetic', model='mock-no-fees', prompt_version='visual-runtime-test',
                usage=dict(prompt_tokens=1024, completion_tokens=20))
            body = json.dumps(output).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body))); self.end_headers(); self.wfile.write(body)
        except Exception as error:
            type(self).errors.append(str(error)); self.send_error(500)


def main():
    runtime = Path(sys.argv[1]).resolve(strict=True)
    Gateway.image = Path(sys.argv[2]).read_bytes()
    server = ThreadingHTTPServer(('127.0.0.1', 0), Gateway)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix='pka-visual-runtime.') as root:
            host = Host(runtime, Path(root), f'http://127.0.0.1:{server.server_port}')
            try:
                host.start()
                domain = host.call('/v1/domains', dict(name='合成图文验收'), expected=201)['id']
                image = str(uuid4())
                captured = host.call('/v1/captures', dict(capture_type='feishu_document',
                    title='合成图文资料', source_url='https://synthetic.feishu.cn/docx/SyntheticDocument',
                    content=dict(text=f'合成资料，请结合图片理解。\n\n![配图](know-image://{image})'),
                    coverage='full_text', context=dict(domain_id=domain),
                    client=dict(kind='desktop', version='synthetic'),
                    feishu_document=dict(document_id='SyntheticDocument', revision_id=1, cli_version='synthetic'),
                    feishu_images=[dict(id=image, ordinal=0, caption='合成配图', block_id='block')]), expected=201)
                source = captured['id']
                path = f'/v1/sources/{source}/images/{image}'
                with urlopen(Request(host.address + path, data=Gateway.image, method='PUT',
                        headers={'Authorization': 'Bearer '+host.credential,'Content-Type':'application/octet-stream'}), timeout=20) as reply:
                    assert reply.status == 201
                plan = host.call('/v1/ai/plan',dict(source_id=source,kind='analyze',contract_version=3))
                assert plan['image_count'] == 1
                job = host.call('/v1/ai/jobs',dict(source_id=source,kind='analyze',contract_version=3,
                    request_key=str(uuid4()),plan_hash=plan['plan_hash']),expected=202)
                def finished():
                    state = host.call('/v1/ai/jobs/'+job['id'])
                    assert state['status'] not in ['failed','unknown','cancelled'], state
                    return state if state['pipeline_status']=='completed' else None
                done = wait(finished,'visual analysis and keyword indexing')
                artifact = host.call('/v1/ai/artifacts/'+done['artifact_id'])
                assert artifact['analysis_plan']['image_count']==1
                assert any(e.get('image_id')==image for e in artifact['evidence'])
                hits = host.call('/v1/search',dict(query='增长',limit=5))['items']
                assert any(hit['source_id']==source for hit in hits)
                host.stop(); host.start()
                restored = host.call('/v1/ai/artifacts/'+artifact['id'])
                assert not restored['stale'] and restored['analysis_plan']['image_count']==1
                assert Gateway.calls==1 and not Gateway.errors, Gateway.errors
                print('PASS: packaged image bytes → AI artifact → summary recall → restart, exactly one synthetic call')
            finally:
                host.stop()
    finally:
        server.shutdown(); server.server_close(); thread.join()


if __name__ == '__main__':
    main()

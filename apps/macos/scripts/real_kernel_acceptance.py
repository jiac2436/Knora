"""Approved M5 synthetic quality check using a private native credential relay.

Credentials are read only from the relay pipe and forwarded to the owned host
pipe. Never pass a key as an argument/environment value or print its envelope.
Stops on the first failed, unknown or unsuccessful quality check; no retries.
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from urllib.request import Request, urlopen
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).parent))
from packaged_kernel_test import PackagedHost, capture
from local_domain_runtime_test import wait


def main(runtime, relay, output, *, node_followup=False):
    report = {'status': 'in_progress', 'data': 'isolated synthetic text and chart only',
              'authorized_call_limit': 5 if node_followup else 8, 'reserved_call_upper_bound': 0,
              'checks': [], 'real_model_calls': 0, 'phase': 'node_followup' if node_followup else 'initial'}
    def save():
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    save()
    process = subprocess.run([str(relay)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if process.returncode or len(process.stdout) > 32768:
        safe_stages = ['library', 'configuration', 'options', 'provider', 'credential']
        stage = next((stage for stage in safe_stages if process.stderr == ('configuration_unavailable stage=' + stage + '\n').encode()), 'unavailable')
        report.update(status='blocked_credentials', reason='现有配置或钥匙串凭证不可用；未发起模型调用', unavailable_stage=stage)
        save()
        print('BLOCKED: 现有DeepSeek配置或钥匙串凭证不可用，未发起真实模型调用。')
        return 2
    startup = json.loads(process.stdout)
    del process
    report['provider'] = startup['profile']['provider']
    report['text_model'] = startup['profile']['model']
    report['vision_model'] = startup['profile']['vision_model']
    with tempfile.TemporaryDirectory(prefix='pka-m5-real-') as temporary:
        root = Path(temporary)
        host = PackagedHost(runtime, root, startup=startup, fixture=False)
        def reserve(count):
            assert report['reserved_call_upper_bound'] + count <= report['authorized_call_limit'], '已达到授权调用上限，停止'
            report['reserved_call_upper_bound'] += count
            save()
        def check(name, detail):
            report['checks'].append({'name': name, **detail})
            save()
            print('PASS: ' + name, flush=True)
        def analyze_one(source):
            selected = dict(source_id=source, kind='analyze', contract_version=3)
            preview = host.call('/v1/ai/plan', selected)
            assert not preview.get('requires_long_analysis'), '合成短文意外进入长文流程，停止'
            reserve(1)
            queued = host.call('/v1/ai/jobs', {**selected, 'request_key': str(uuid4()), 'plan_hash': preview['plan_hash']}, expected=202)
            def done():
                job = host.call('/v1/ai/jobs/' + queued['id'])
                assert job['status'] not in {'failed', 'unknown', 'cancelled'}, '真实分析未通过：' + str(job.get('last_error'))
                return job if job['pipeline_status'] == 'completed' else None
            job = wait(done, '真实单篇分析', timeout=180)
            report['real_model_calls'] += 1
            artifact = host.call('/v1/ai/artifacts/' + job['artifact_id'])
            assert not artifact['stale']
            return artifact
        try:
            host.start()
            domain = host.call('/v1/domains', {'name': 'M5隔离合成验收'}, expected=201)['id']
            source = capture(host, domain,
                '合成实验记录：猎户项目于2026年4月上线。在100次测试中，成功83次，失败17次；这组数据只属于猎户项目。天琴项目尚未测试，没有成功率。知识库产品必须保留原文，概览用于导航。',
                '合成猎户实验记录')
            artifact = analyze_one(source)
            result = artifact['result']
            assert '猎户' in json.dumps(result, ensure_ascii=False)
            check('真实短文分析及摘要索引', {'overview': result['overview']['text'], 'summary': result['summary']})
            from PIL import Image, ImageDraw
            from io import BytesIO
            chart = Image.new('RGB', (900, 480), 'white')
            draw = ImageDraw.Draw(chart)
            draw.text((35, 25), 'Synthetic revenue chart (USD)', fill='black', font_size=32)
            draw.rectangle((170, 105, 718, 205), fill='#2763ba')
            draw.rectangle((170, 280, 338, 380), fill='#cd7c24')
            draw.text((35, 135), 'Atlas', fill='black', font_size=28)
            draw.text((35, 310), 'Vega', fill='black', font_size=28)
            draw.text((740, 135), '137', fill='black', font_size=32)
            draw.text((365, 310), '42', fill='black', font_size=32)
            raw = BytesIO(); chart.save(raw, 'PNG')
            image = str(uuid4())
            article = host.call('/v1/captures', dict(capture_type='feishu_document', title='合成收入图表',
                source_url='https://synthetic.feishu.cn/docx/SyntheticChart',
                content={'text': f'这是一个隔离合成图表，重要数据只在图片中。请以图片为准。\n![图表](know-image://{image})'},
                coverage='full_text', context={'domain_id': domain}, client={'kind': 'desktop', 'version': 'M5'},
                feishu_document={'document_id': 'SyntheticChart', 'revision_id': 1, 'cli_version': 'synthetic'},
                feishu_images=[{'id': image, 'ordinal': 0, 'caption': '合成图表', 'block_id': 'chart'}]), expected=201)['id']
            request = Request(host.address + f'/v1/sources/{article}/images/{image}', data=raw.getvalue(), method='PUT',
                headers={'Authorization': 'Bearer ' + host.credential, 'Content-Type': 'application/octet-stream'})
            with urlopen(request, timeout=30) as response:
                assert response.status == 201
            visual = analyze_one(article)
            content = json.dumps(visual['result'], ensure_ascii=False)
            assert all(value in content for value in ['137', '42', 'Atlas', 'Vega']), '图片专有数值或对象归属未通过质量检查'
            check('真实图像数值与对象归属', {'result': visual['result'], 'expected': {'Atlas_USD': 137, 'Vega_USD': 42}})
            if not node_followup:
                session = host.call('/v1/rag/sessions', {}, expected=201)['id']
                reserve(1)
                turn = host.ask(session, '猎户项目测试成功多少次、失败多少次？天琴项目有测试结果吗？')
                def answered():
                    item = next(t for t in host.call('/v1/rag/turns?session_id=' + session)['items'] if t['id'] == turn['id'])
                    assert item['status'] not in {'failed', 'unknown'}, '真实问答失败或未知，停止'
                    return item if item['status'] == 'succeeded' else None
                answer = wait(answered, '真实问答', timeout=180)
                report['real_model_calls'] += 1
                assert all(value in answer['answer'] for value in ['83', '17', '天琴']) and answer['citations'], '真实问答事实或引用未通过'
                check('真实知识域检索问答', {'answer': answer['answer']})
            base = '/v1/domains/' + domain
            topic = host.call(base + '/topics', {'name': '合成实验与图表'}, expected=201)['id']
            for identifier in (source, article):
                host.call(base + f'/topics/{topic}/members/{identifier}', {'included': True}, method='PUT')
            host.call(base + '/analysis/authorization', dict(authorization_id=str(uuid4()),
                policy={'enabled': True, 'max_calls': 5, 'max_input_tokens': 1_000_000}), method='PUT')
            for label, selection in [('真实主题概览', {'topic_id': topic}), ('真实知识域概览', {})]:
                preview = host.call(base + '/analysis/build/preview', selection)
                calls = preview['calls']
                reserve(calls)
                queued = host.call(base + '/analysis/build/jobs', dict(**selection, request_id=str(uuid4()), preview_hash=preview['preview_hash']))
                def built():
                    value = host.call(base + '/analysis/build/jobs/' + queued['id'])
                    assert value['status'] not in {'failed', 'unknown', 'cancelled', 'stale'}, '真实概览失败或未知：' + str(value.get('last_error'))
                    return value if value['status'] == 'completed' else None
                wait(built, label, timeout=240)
                report['real_model_calls'] += calls
                navigation = host.call(base + '/analysis/navigation', selection)['overview']
                assert navigation['available']
                check(label, {'result': navigation['result']})
            host.stop()
            secret = startup['provider_key'].encode()
            assert not any(secret in path.read_bytes() for path in root.rglob('*') if path.is_file()), '凭证持久化检查未通过'
            report['status'] = 'passed_synthetic_scope'
            report['credential_persistence_scan'] = 'passed'
            save()
            print('PASS: 真实内置路径合成质量验收通过，凭证未持久化。')
            return 0
        except Exception as error:
            report.update(status='stopped_on_failure', reason=str(error)[:600])
            # Business diagnostics contain only synthetic material. Redact defensively.
            if startup['provider_key']:
                report['reason'] = report['reason'].replace(startup['provider_key'], '[redacted]')
            save()
            print('STOP: 真实验收已停止，请查看安全验收记录；不会自动重试。')
            return 1
        finally:
            host.stop()


if __name__ == '__main__':
    raise SystemExit(main(Path(sys.argv[1]).resolve(strict=True), Path(sys.argv[2]).resolve(strict=True),
                         Path(sys.argv[3]).absolute(), node_followup='--node-followup' in sys.argv[4:]))

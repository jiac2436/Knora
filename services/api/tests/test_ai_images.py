"""Actual inline pixels reach the provider transport; all model replies are mocked."""
import base64
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import httpx
from PIL import Image
import pytest
from sqlalchemy import select

from app import ai, ai_images
from app.ai_contract import AnalysisRequest
from app.ai_long_analysis import segment_request, preflight_snapshot
from app.errors import ApiError
from app.models import AIArtifact, AIJob, AIJobStep, EvidenceChunk, SourceImage, SummaryIndexJob
from app.summary_index import current_summary_artifact, summary_text
from app.summary_retrieval import summary_evidence
from services.ai_gateway import main as gateway
from services.worker import ai_analysis as worker
from test_ai_analysis import storage


@pytest.fixture
def visual(storage, tmp_path, monkeypatch):
    db, _, source, chunk, *_ = storage
    settings = replace(ai.settings, upload_dir=tmp_path / 'images-library')
    monkeypatch.setattr(ai, 'settings', settings)
    monkeypatch.setattr(worker, 'settings', settings)
    directory = settings.upload_dir / 'images'
    directory.mkdir(parents=True)
    output = BytesIO()
    Image.new('RGB', (80, 60), color='blue').save(output, format='PNG')
    body = output.getvalue()
    row = SourceImage(id=str(uuid4()), source_id=source.id, revision_id=source.current_revision_id,
        ordinal=0, caption='图表', status='saved', file_key=str(uuid4()),
        sha256=hashlib.sha256(body).hexdigest(), size_bytes=len(body), mime_type='image/png', width=80, height=60)
    (directory / row.file_key).write_bytes(body)
    db.add(row); db.commit()
    return storage, row, body, directory


def freeze(visual):
    storage = visual[0]
    return ai.freeze_snapshot(storage[0], storage[2], 'analyze', str(uuid4()), 3)


def enqueue(visual, plan_hash=None):
    storage = visual[0]
    return ai.create_job(ai.JobRequest(source_id=storage[2].id, kind='analyze',
        contract_version=3, request_key=uuid4(), plan_hash=plan_hash), storage[6], storage[0])


def output(identifier):
    return dict(contract_version=3, overview=dict(text='图示增长37%。', citations=[identifier]),
        summary='图示增长37%。', summary_citations=[identifier], insights=[], limitations=[],
        tag_suggestions=[], organizing_suggestion=None, warnings=[])


def upstream(result):
    return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(result)}}],
        'usage': {'prompt_tokens': 1040, 'completion_tokens': 20}})


def test_snapshot_binds_original_image_without_persisting_binary(visual):
    snapshot = freeze(visual)
    preflight_snapshot(snapshot)
    image = snapshot['images'][0]
    evidence = next(e for e in snapshot['evidence'] if e['id'] == image['evidence_id'])
    assert image['id'] == visual[1].id and evidence['image_id'] == image['id']
    assert evidence['original_evidence_id'] == visual[0][3].id
    assert visual[0][3].text[evidence['text_start']:evidence['text_end']] == evidence['text']
    assert snapshot['analysis_plan']['image_count'] == 1
    assert 'base64,' not in json.dumps(snapshot)
    wire = AnalysisRequest.model_validate({k:v for k,v in snapshot.items() if k in AnalysisRequest.model_fields})
    assert visual[1].file_key not in wire.model_dump_json()


def test_pixels_and_text_jointly_reach_model_then_publish_indexable_summary(visual):
    storage, image, original, _ = visual
    db = storage[0]
    seen = []
    def provider(request):
        payload = json.loads(request.content)
        seen.append(payload)
        blocks = payload['messages'][1]['content']
        metadata = json.loads(blocks[0]['text'])
        picture = next(block for block in blocks if block['type'] == 'image_url')
        assert base64.b64decode(picture['image_url']['url'].split(',', 1)[1]) == original
        assert 'data_url' not in blocks[0]['text']
        assert '不可信资料' in payload['messages'][0]['content']
        assert payload['model'] == 'deepseek-flash'
        return upstream(output(metadata['images'][0]['evidence_id']))
    app = gateway.create_app(gateway.GatewaySettings('synthetic', 'text-model', 'synthetic-secret',
        vision_model='deepseek-flash'), transport=httpx.MockTransport(provider))
    created = enqueue(visual)
    with TestClient(app) as http:
        def local(request):
            result = http.post('/v1/analyze', content=request.content, headers={'Authorization':'Bearer synthetic-secret'})
            return httpx.Response(result.status_code, json=result.json())
        assert worker.run_ai_once(db, httpx.MockTransport(local))
    artifact = db.scalar(select(AIArtifact))
    assert db.get(AIJob, created['id']).status == 'completed'
    assert artifact is not None and not ai.is_stale(db, storage[2], json.loads(artifact.snapshot_json))
    assert '37%' in summary_text(artifact) and '37%' not in storage[3].text
    assert current_summary_artifact(db, storage[2]).id == artifact.id
    assert summary_evidence(db, storage[2], artifact)[0].id == storage[3].id
    assert db.get(SummaryIndexJob, storage[2].id).artifact_id == artifact.id
    assert 'base64,' not in artifact.snapshot_json
    assert len(seen) == 1 and artifact.model == 'deepseek-flash'


@pytest.mark.parametrize('status', ['pending','running','unavailable'])
def test_incomplete_images_block_before_enqueue(visual, status):
    visual[1].status = status; visual[0][0].commit()
    with pytest.raises(ApiError, match='图片'):
        enqueue(visual)
    assert visual[0][0].scalar(select(AIJob)) is None


def test_changed_original_fails_without_http(visual):
    created = enqueue(visual)
    (visual[3] / visual[1].file_key).write_bytes(b'changed')
    assert worker.run_ai_once(visual[0][0], httpx.MockTransport(lambda _: pytest.fail('must not send')))
    job = visual[0][0].get(AIJob, created['id'])
    assert job.status == 'failed' and '尚未调用模型' in job.last_error


def test_metadata_edit_during_call_cancels_publication(visual):
    storage, image, *_ = visual
    created = enqueue(visual)
    def handler(request):
        wire = json.loads(request.content)
        with storage[1]() as concurrent:
            concurrent.get(SourceImage, image.id).sha256 = 'a' * 64
            concurrent.commit()
        return httpx.Response(200, json=dict(result=output(wire['images'][0]['evidence_id']),
            provider='synthetic', model='synthetic-vision', prompt_version='synthetic',
            usage=dict(prompt_tokens=1040, completion_tokens=20)))
    worker.run_ai_once(storage[0], httpx.MockTransport(handler))
    assert storage[0].get(AIJob, created['id']).status == 'cancelled'
    assert storage[0].scalar(select(AIArtifact)) is None


def test_many_images_split_and_synthesize_without_replaying_pixels(visual):
    storage, original, body, directory = visual
    db = storage[0]
    for ordinal in range(1, 16):
        row = SourceImage(id=str(uuid4()), source_id=original.source_id, revision_id=original.revision_id,
            ordinal=ordinal, caption='图表', status='saved', file_key=str(uuid4()), sha256=original.sha256,
            size_bytes=original.size_bytes, mime_type=original.mime_type, width=80, height=60)
        db.add(row); (directory / row.file_key).write_bytes(body)
    db.commit()
    plan = ai.preview_plan(ai.PlanRequest(source_id=storage[2].id, kind='analyze',contract_version=3),storage[6],db)
    assert plan['image_count'] == 16 and plan['calls'] == 3
    created = enqueue(visual, plan['plan_hash'])
    calls = []
    def handler(request):
        wire = json.loads(request.content); calls.append(wire)
        identifier = wire['images'][0]['evidence_id']
        return httpx.Response(200, json=dict(result=output(identifier), provider='synthetic', model='vision',
            prompt_version='synthetic', usage=dict(prompt_tokens=1040,completion_tokens=20)))
    for _ in range(3):
        assert worker.run_ai_once(db, httpx.MockTransport(handler))
    assert [wire['phase'] for wire in calls] == ['segment','segment','synthesis']
    assert len(calls[0]['images']) == 14 and len(calls[1]['images']) == 2
    assert all(image['data_url'] for wire in calls[:2] for image in wire['images'])
    assert all(image['data_url'] is None for image in calls[2]['images'])
    assert db.get(AIJob,created['id']).status == 'completed'
    assert current_summary_artifact(db,storage[2]) is not None
    assert all('base64,' not in row.request_json for row in db.scalars(select(AIJobStep)))
    assert not worker.run_ai_once(db,httpx.MockTransport(handler))


@pytest.mark.parametrize('damage', ['remote_url','digest','pixels','binding','missing_model'])
def test_gateway_rejects_invalid_image_before_provider(visual, damage):
    snapshot = freeze(visual)
    request = AnalysisRequest.model_validate({k:v for k,v in snapshot.items() if k in AnalysisRequest.model_fields})
    wire = ai_images.hydrate_images(request,snapshot['image_files'],worker.settings.upload_dir).model_dump()
    if damage == 'remote_url': wire['images'][0]['data_url']='https://example.test/private'
    if damage == 'digest': wire['images'][0]['sha256']='0'*64
    if damage == 'pixels': wire['images'][0]['width']=81
    if damage == 'binding': wire['images'][0]['evidence_id']='not-provided'
    app = gateway.create_app(gateway.GatewaySettings('synthetic','text-model','synthetic',
        vision_model='' if damage == 'missing_model' else 'deepseek-flash'),
        transport=httpx.MockTransport(lambda _: pytest.fail('must not call model')))
    with TestClient(app) as http:
        result = http.post('/v1/analyze',json=wire,headers={'Authorization':'Bearer synthetic'})
    assert result.status_code == (400 if damage == 'missing_model' else 422)
    assert 'base64,' not in result.text


def test_long_screenshot_is_tiled_without_shrinking_or_changing_original(visual):
    storage, image, _, directory = visual
    output_file = BytesIO()
    original = Image.new('RGB', (500, 10000), color='white')
    for top, color in [(0,'red'),(4000,'green'),(8000,'blue')]:
        original.paste(color,(0,top,500,min(top+2000,10000)))
    original.save(output_file,format='PNG')
    data = output_file.getvalue()
    image.height=10000; image.width=500; image.size_bytes=len(data)
    image.sha256=hashlib.sha256(data).hexdigest()
    (directory/image.file_key).write_bytes(data); storage[0].commit()
    snapshot = freeze(visual)
    request = AnalysisRequest.model_validate({k:v for k,v in snapshot.items() if k in AnalysisRequest.model_fields})
    hydrated = ai_images.hydrate_images(request,snapshot['image_files'],worker.settings.upload_dir)
    from services.ai_gateway.vision import user_content
    blocks = user_content(hydrated, hydrated.model_dump)
    pictures = [block['image_url']['url'] for block in blocks if block['type']=='image_url']
    assert len(pictures)==3
    for url, top, color in zip(pictures,[0,4000,8000],[(255,0,0),(0,128,0),(0,0,255)]):
        with Image.open(BytesIO(base64.b64decode(url.split(',',1)[1]))) as tile:
            assert tile.width==500 and tile.height==min(4096,10000-top)
            assert tile.getpixel((0,0))==color
    assert (directory/image.file_key).read_bytes()==data


def test_visual_citation_must_have_a_matching_image_binding(visual):
    snapshot=freeze(visual)
    wire={k:v for k,v in snapshot.items() if k in AnalysisRequest.model_fields}
    wire['images']=[]
    with pytest.raises(ValueError,match='image binding'):
        AnalysisRequest.model_validate(wire)


def test_visual_plan_uses_original_count_and_full_job_freezes_images(visual, monkeypatch):
    from app import domain_full_analysis as full
    storage=visual[0]
    monkeypatch.setattr(full,'settings',ai.settings)
    before=full.current_full_snapshot(storage[0],storage[6].owner_id,storage[2].domain_id)
    visual[1].sha256='f'*64; storage[0].commit()
    after=full.current_full_snapshot(storage[0],storage[6].owner_id,storage[2].domain_id)
    assert before!=after


def test_legacy_full_analysis_cannot_expand_to_images_without_new_confirmation(visual):
    from app import domain_full_analysis as full
    old=type('OldJob',(),{'snapshot_json':json.dumps({'rules':'domain-full-v1'})})()
    with pytest.raises(ApiError,match='重新预览'):
        full.ensure_inputs_current(visual[0][0],old)


def test_legacy_text_artifact_remains_readable_but_full_analysis_needs_images(visual):
    from app import domain_full_analysis as full
    from test_summary_retrieval import analyzed
    storage=visual[0]
    artifact=analyzed(storage)
    assert current_summary_artifact(storage[0],storage[2]).id==artifact.id
    assert full._valid_document(storage[0],storage[2]) is None


def test_failed_visual_call_is_not_replayed(visual):
    created=enqueue(visual); calls=[]
    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout('synthetic-private-diagnostic')
    worker.run_ai_once(visual[0][0],httpx.MockTransport(handler))
    job=visual[0][0].get(AIJob,created['id'])
    assert job.status=='unknown' and 'synthetic-private' not in job.last_error
    assert not worker.run_ai_once(visual[0][0],httpx.MockTransport(handler))
    assert len(calls)==1


def test_documented_vision_model_required_for_deepseek():
    with pytest.raises(ValueError,match='documented DeepSeek vision'):
        gateway.GatewaySettings('synthetic','text-model','synthetic',vision_model='deepseek-v4-pro')


def test_wechat_images_with_identical_captions_keep_distinct_original_positions(visual):
    storage, image, body, directory=visual
    storage[3].text='第一段 [图片: 图表]'
    second=EvidenceChunk(revision_id=image.revision_id,ordinal=1,text='第二段 [图片: 图表]',locator_end=1)
    storage[0].add(second)
    image.retrieval_method='wechat_ua'
    another=SourceImage(id=str(uuid4()),source_id=image.source_id,revision_id=image.revision_id,ordinal=1,
        caption=image.caption,status='saved',file_key=str(uuid4()),sha256=image.sha256,size_bytes=image.size_bytes,
        mime_type=image.mime_type,width=80,height=60,retrieval_method='wechat_ua')
    storage[0].add(another);(directory/another.file_key).write_bytes(body);storage[0].commit()
    snapshot=freeze(visual)
    by_id={e['id']:e for e in snapshot['evidence']}
    assert [by_id[i['evidence_id']]['original_evidence_id'] for i in snapshot['images']]==[storage[3].id,second.id]


def test_upstream_error_diagnostics_do_not_leak_provider_text(visual, caplog):
    snapshot=freeze(visual)
    request=AnalysisRequest.model_validate({k:v for k,v in snapshot.items() if k in AnalysisRequest.model_fields})
    wire=ai_images.hydrate_images(request,snapshot['image_files'],worker.settings.upload_dir).model_dump()
    app=gateway.create_app(gateway.GatewaySettings('synthetic-secret','text-model','synthetic',vision_model='deepseek-flash'),
        transport=httpx.MockTransport(lambda _:httpx.Response(400,json={'error':{
            'message':'synthetic-private-provider-message: image parameter rejected'}})))
    with TestClient(app) as http:
        reply=http.post('/v1/analyze',json=wire,headers={'Authorization':'Bearer synthetic'})
        assert http.post('/v1/analyze',json=wire,headers={'Authorization':'Bearer synthetic'}).json()==reply.json()
    assert reply.json()['detail']['upstream_status']==400
    assert reply.json()['detail']['category']=='image_input_rejected'
    assert 'synthetic-private-provider-message' not in reply.text+caplog.text
    assert 'base64,' not in reply.text+caplog.text

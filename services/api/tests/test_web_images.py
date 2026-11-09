from io import BytesIO
import hashlib

import httpx
from PIL import Image
import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app.models import Source, SourceImage, SourceRevision, utcnow
from app.web_images import image_references
from services.worker import web_images, web_reader
from services.worker.main import run_web_once
from test_web_read import _save_link
from test_local_backup import library, paths_for


def picture():
    output = BytesIO()
    Image.new('RGB', (16, 12), color='blue').save(output, format='PNG')
    return output.getvalue(), 'image/png', 16, 12


@pytest.fixture
def article(client, account, monkeypatch, tmp_path):
    configuration = type('Settings', (), {
        'upload_dir': tmp_path / 'uploads', 'max_image_bytes': 20 * 1024 * 1024})()
    monkeypatch.setattr(web_images, 'settings', configuration)
    monkeypatch.setattr('app.images.settings', configuration)
    source = _save_link(client, account, 'https://mp.weixin.qq.com/s/synthetic')
    monkeypatch.setattr('services.worker.main.extract_wechat_page', lambda _: (
        '合成微信图文', '正文前段。\n\n[图片: 测试图片]\n\n正文后段。',
        '# 合成微信图文\n\n正文前段。\n\n![测试图片](https://mmbiz.qpic.cn/image.png)\n\n正文后段。'))
    with SessionLocal() as db:
        assert run_web_once(db)
    return source['id'], account['headers']


def detail(client, article):
    source, headers = article
    response = client.get('/v1/sources/' + source, headers=headers)
    assert response.status_code == 200
    return response.json()


def test_generated_markdown_references_escape_and_preserve_repeated_images():
    html = '<h1 id="activity-name">标题</h1><div id="js_content"><p>正文</p>' \
           '<img data-src="https://mmbiz.qpic.cn/a(1).png" alt="方括号[测试]">' \
           '<img data-src="https://mmbiz.qpic.cn/a(1).png"><img data-src="https://mmbiz.qpic.cn/a.gif?wx_fmt=gif"></div>'
    _, text, markdown = web_reader._extract_wechat_html(html, 'https://mp.weixin.qq.com/s/synthetic')
    refs = image_references(markdown)
    assert len(refs) == 3 and refs[0].caption == '方括号[测试]'
    assert refs[0].url == refs[1].url == 'https://mmbiz.qpic.cn/a(1).png'
    assert '[图片:' in text
    with pytest.raises(ValueError, match='200 张'):
        image_references('![图片](https://example.test/a.png)\n' * 201)


def test_article_is_readable_before_images_and_archives_originals(client, article):
    before = detail(client, article)
    image = before['images'][0]
    assert before['processing_status'] == 'usable' and image['status'] == 'pending'
    assert 'know-image://' + image['id'] in before['reading_text']
    assert 'mmbiz.qpic.cn' not in before['reading_text']
    assert 'know-image://' not in before['extracted_text']
    with SessionLocal() as db:
        assert web_images.run_image_once(db, downloader=lambda _: picture())
        assert not web_images.run_image_once(db)
    after = detail(client, article)
    stored = after['images'][0]
    assert stored['status'] == 'saved' and stored['width'] == 16
    assert after['revision'] == before['revision'] and after['reading_text'] == before['reading_text']
    assert after['extracted_text'] == before['extracted_text']
    path = stored['download_path']
    assert client.get(path).status_code == 401
    response = client.get(path, headers=article[1])
    assert response.status_code == 200 and response.content == picture()[0]
    assert 'private' in response.headers['Cache-Control']
    retry = client.post(f'/v1/sources/{article[0]}/images/retry', headers=article[1], json={})
    assert retry.status_code == 202 and retry.json()['queued_count'] == 0


def test_failures_do_not_fail_article_and_require_explicit_retry(client, article):
    def fail(_):
        raise httpx.ConnectError('private diagnostic must not appear')
    with SessionLocal() as db:
        assert web_images.run_image_once(db, downloader=fail)
        assert not web_images.run_image_once(db, downloader=lambda _: pytest.fail('No automatic retry'))
    failed = detail(client, article)
    assert failed['processing_status'] == 'usable'
    image = failed['images'][0]
    assert image['status'] == 'unavailable' and 'private' not in image['failure_reason']
    url = f'/v1/sources/{article[0]}/images/retry'
    assert client.post(url, json={}).status_code == 401
    response = client.post(url, headers=article[1], json={'image_id': image['id']})
    assert response.status_code == 202 and response.json()['queued_count'] == 1
    assert client.post(url, headers=article[1], json={}).json()['queued_count'] == 0
    with SessionLocal() as db:
        web_images.run_image_once(db, downloader=lambda _: picture())
    assert detail(client, article)['images'][0]['status'] == 'saved'


def test_old_versions_are_not_mixed_and_retry_cannot_target_old_images(client, article):
    old = detail(client, article)
    url = f'/v1/sources/{article[0]}/web-read'
    assert client.post(url, headers=article[1]).status_code in {200, 202}
    with SessionLocal() as db:
        assert run_web_once(db)
        assert web_images.run_image_once(db, downloader=lambda _: picture())
        assert web_images.run_image_once(db, downloader=lambda _: picture())
    new = detail(client, article)
    assert new['revision'] == old['revision'] + 1
    assert len(new['images']) == 1 and new['images'][0]['id'] != old['images'][0]['id']
    assert old['images'][0]['id'] not in new['reading_text']
    response = client.post(f'/v1/sources/{article[0]}/images/retry', headers=article[1],
                           json={'image_id': old['images'][0]['id']})
    assert response.status_code == 404


def test_supplement_keeps_inline_images_and_downloads_bound_to_base(client, article):
    original = detail(client, article)
    response = client.post(f'/v1/sources/{article[0]}/supplements', headers=article[1], json={'text': '用户补充'})
    assert response.status_code in {200, 201}
    with SessionLocal() as db:
        web_images.run_image_once(db, downloader=lambda _: picture())
    current = detail(client, article)
    assert '用户补充' in current['reading_text']
    assert current['images'][0]['id'] == original['images'][0]['id']
    assert current['images'][0]['status'] == 'saved'


def test_deletion_during_download_discards_bytes(client, article):
    def removed(_):
        with SessionLocal() as another:
            another.get(Source, article[0]).deleted_at = utcnow()
            another.commit()
        return picture()
    with SessionLocal() as db:
        web_images.run_image_once(db, downloader=removed)
        image = db.scalar(select(SourceImage).where(SourceImage.source_id == article[0]))
        assert image.status == 'unavailable' and image.file_key is None
    assert not list((web_images.settings.upload_dir / 'images').glob('*'))


def test_download_validates_dns_redirects_format_and_size(monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo', lambda host, *a, **kw: [(None,None,None,None,
        ('127.0.0.1' if host == 'localhost' else '93.184.215.14', 443))])
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(200, content=picture()[0])
    factory = lambda **kw: httpx.Client(transport=httpx.MockTransport(handler), **kw)
    body, mime, width, height = web_images.download_image('https://mmbiz.qpic.cn/a', factory)
    assert body == picture()[0] and mime == 'image/png' and (width, height) == (16, 12)
    assert 'Mozilla/5.0' in seen[0].headers['User-Agent']
    assert seen[0].headers['Referer'] == 'https://mp.weixin.qq.com/'
    redirect = lambda **kw: httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(302, headers={'location': 'http://localhost/private'})), **kw)
    with pytest.raises(ValueError, match='内网'):
        web_images.download_image('https://mmbiz.qpic.cn/a', redirect)
    invalid = lambda **kw: httpx.Client(transport=httpx.MockTransport(
        lambda _: httpx.Response(200, content=b'<html>not an image</html>')), **kw)
    with pytest.raises(ValueError, match='图片文件无效'):
        web_images.download_image('https://mmbiz.qpic.cn/a', invalid)
    monkeypatch.setattr(web_images, 'settings', type('Settings', (), {'max_image_bytes': 10})())
    with pytest.raises(ValueError, match='20 MB'):
        web_images.download_image('https://mmbiz.qpic.cn/a', factory)


def test_document_image_budget_failure_does_not_publish_partial_file(client, article, monkeypatch):
    monkeypatch.setattr(web_images, 'MAX_DOCUMENT_BYTES', 1)
    with SessionLocal() as db:
        web_images.run_image_once(db, downloader=lambda _: picture())
    assert detail(client, article)['images'][0]['status'] == 'unavailable'
    assert not list((web_images.settings.upload_dir / 'images').glob('*'))


def test_legacy_articles_backfill_without_changing_text_or_revision(client, article):
    with SessionLocal() as db:
        image = db.scalar(select(SourceImage).where(SourceImage.source_id == article[0]))
        db.delete(image); db.commit()
    before = detail(client, article)
    assert before['web_images_can_archive'] and not before['images']
    response = client.post(f'/v1/sources/{article[0]}/images/retry', headers=article[1], json={})
    assert response.status_code == 202 and response.json()['queued_count'] == 1
    after = detail(client, article)
    assert len(after['images']) == 1
    assert after['revision'] == before['revision'] and after['extracted_text'] == before['extracted_text']
    assert 'know-image://' in after['reading_text']


def test_retry_requires_source_owner(client, article):
    other = client.post('/v1/dev/sessions', json={'email': 'other-images@example.test', 'name': 'Other'}).json()
    response = client.post(f'/v1/sources/{article[0]}/images/retry',
        headers={'Authorization': 'Bearer ' + other['access_token']}, json={})
    assert response.status_code == 404


@pytest.mark.parametrize('method,parser', [('wechat_ua','web-wechat-v1'), ('browser_public','browser-images-v1')])
def test_backup_restores_images_and_does_not_replay_downloads(tmp_path, library, method, parser):
    from app.database import make_engine
    from app.local_backup import export_backup, restore_backup
    from app.models import WebReadMarkdown
    from sqlalchemy.orm import Session
    paths, _, _ = library
    content = picture()[0]
    directory = paths.assets / 'images'
    directory.mkdir()
    (directory / 'image-original').write_bytes(content)
    engine = make_engine(f'sqlite:///{paths.database}')
    with Session(engine) as db:
        db.get(Source, 'source').kind = 'url'
        db.get(SourceRevision, 'revision').parser_version = parser
        db.add(WebReadMarkdown(revision_id='revision', markdown='![图片](https://mmbiz.qpic.cn/a.png)'))
        db.add(SourceImage(id='saved-image', source_id='source', revision_id='revision', ordinal=0,
            retrieval_method=method, status='saved', file_key='image-original',
            size_bytes=len(content), sha256=hashlib.sha256(content).hexdigest(), mime_type='image/png'))
        db.add(SourceImage(id='queued-image', source_id='source', revision_id='revision', ordinal=1,
            retrieval_method=method, status='pending'))
        db.commit()
    engine.dispose()
    package = tmp_path / 'images.pka'
    export_backup(paths, package)
    restored = paths_for(tmp_path / 'restored-images')
    restore_backup(package, restored, 'b' * 48)
    assert (restored.assets / 'images/image-original').read_bytes() == content
    engine = make_engine(f'sqlite:///{restored.database}')
    with Session(engine) as db:
        assert db.get(SourceImage, 'saved-image').status == 'saved'
        assert db.get(SourceImage, 'queued-image').status == 'unavailable'
        assert '手动重试' in db.get(SourceImage, 'queued-image').failure_reason
    engine.dispose()

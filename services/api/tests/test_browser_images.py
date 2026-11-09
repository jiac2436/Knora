from io import BytesIO

from PIL import Image
from sqlalchemy import select

from app.database import SessionLocal
from app.models import SourceImage
from services.worker import web_images
from conftest import capture_payload


def save(client, account, key='browser-images', urls=None, text='用户选择的正文'):
    payload = capture_payload(account['domain']['id'], capture_type='browser_page',
        source_url='https://www.xiaohongshu.com/explore/fixture',
        content={'text': text, 'image_urls': urls if urls is not None else ['https://example.com/one.png', 'https://example.com/two.png']})
    response = client.post('/v1/captures', json=payload,
        headers={**account['headers'], 'Idempotency-Key': key})
    assert response.status_code in (200, 201), response.text
    return response.json()['id']


def detail(client, account, identifier):
    response = client.get('/v1/sources/' + identifier, headers=account['headers'])
    assert response.status_code == 200
    return response.json()


def test_browser_images_are_ordered_idempotent_and_displayed(client, account, monkeypatch, tmp_path):
    configuration = type('Settings', (), {'upload_dir': tmp_path, 'max_image_bytes': 20 * 1024 * 1024})()
    monkeypatch.setattr(web_images, 'settings', configuration)
    monkeypatch.setattr('app.images.settings', configuration)
    identifier = save(client, account)
    assert save(client, account) == identifier
    first = detail(client, account, identifier)
    assert first['extracted_text'] == '用户选择的正文'
    assert len(first['images']) == 2
    assert first['browser_image_urls'] == ['https://example.com/one.png', 'https://example.com/two.png']
    assert [i['status'] for i in first['images']] == ['pending', 'pending']
    assert all('know-image://' + i['id'] in first['reading_text'] for i in first['images'])
    output = BytesIO(); Image.new('RGB', (10, 10), 'blue').save(output, format='PNG')
    seen = []
    def downloader(url):
        seen.append(url)
        return output.getvalue(), 'image/png', 10, 10
    with SessionLocal() as db:
        assert web_images.run_image_once(db, downloader)
        assert web_images.run_image_once(db, downloader)
        assert not web_images.run_image_once(db, downloader)
    current = detail(client, account, identifier)
    assert seen == first['browser_image_urls']
    for item in current['images']:
        assert item['status'] == 'saved'
        response = client.get(f'/v1/sources/{identifier}/images/{item["id"]}', headers=account['headers'])
        assert response.content == output.getvalue()
    assert client.get(f'/v1/sources/{identifier}/images/{current["images"][0]["id"]}').status_code in (401,403)


def test_browser_images_fail_explicitly_and_can_retry(client, account):
    identifier = save(client, account, urls=['https://example.com/one.png'])
    def unavailable(url): raise ValueError('图片下载返回 HTTP 403')
    with SessionLocal() as db:
        assert web_images.run_image_once(db, unavailable)
    failed = detail(client, account, identifier)['images'][0]
    assert failed['status'] == 'unavailable' and '403' in failed['failure_reason']
    response = client.post(f'/v1/sources/{identifier}/images/retry', headers=account['headers'],json={'image_id':failed['id']})
    assert response.status_code == 202 and response.json()['queued_count'] == 1


def test_only_explicit_urls_create_jobs_not_markdown_prose(client, account):
    identifier = save(client, account, text='![正文中的示例](https://example.com/not-selected.png)',
        urls=['https://example.com/one.png', 'https://example.com/one.png'])
    current = detail(client, account, identifier)
    assert len(current['images']) == 1
    assert current['browser_image_urls'] == ['https://example.com/one.png']
    assert 'not-selected.png' in current['extracted_text']


def test_limits_and_non_http_urls_are_rejected(client, account):
    for urls in (['https://example.com/a.png'] * 201, ['blob:https://example.com/a']):
        payload = capture_payload(account['domain']['id'], capture_type='browser_page',content={'text':'正文','image_urls':urls})
        assert client.post('/v1/captures',json=payload,headers={**account['headers'],'Idempotency-Key':'bad'}).status_code == 422

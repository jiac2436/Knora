import test from 'node:test';
import assert from 'node:assert/strict';
import { isXiaohongshuURL } from '../dist/site.js';
import { createCapturePayload } from '../dist/core.js';

test('recognizes only Xiaohongshu hosts', () => {
  for (const url of ['https://www.xiaohongshu.com/explore/123', 'https://xiaohongshu.com', 'https://www.xiaohongshu.com/search_result']) assert.equal(isXiaohongshuURL(url), true);
  for (const url of ['https://xiaohongshu.com.evil.example', 'https://example.com/?url=xiaohongshu.com', 'https://xiaohongshu.com@evil.example', 'file:///xiaohongshu.com', 'invalid']) assert.equal(isXiaohongshuURL(url), false);
});

test('blocked capture cannot be queued as a link-only fallback', () => {
  assert.throws(() => createCapturePayload({ mode: 'page', captureBlocked: 'xiaohongshu_detail_required', text: '', url: 'https://www.xiaohongshu.com/' }, { domainId: 'work' }), /详情/);
});


test('gallery rejects unsafe URLs and deduplicates images without reordering', async () => {
  const { galleryImages } = await import('../dist/image-gallery.js');
  assert.deepEqual(galleryImages(['https://example.com/a.png', 'javascript:alert(1)', 'https://example.com/a.png', 'https://example.com/b.png', 'https://user:secret@example.com/a', 'data:image/svg+xml,<svg/>', 'data:image/png;base64,abc']),
    ['https://example.com/a.png', 'https://example.com/b.png', 'data:image/png;base64,abc']);
  assert.equal(galleryImages(Array.from({length: 210}, (_, i) => `https://example.com/${i}.png`)).length, 200);
});

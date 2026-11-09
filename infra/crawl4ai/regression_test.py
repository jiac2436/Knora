"""Run inside the Crawl4AI image; verify its own scraping/Markdown pipeline."""
from crawl4ai.content_scraping_strategy import LXMLWebScrapingStrategy
from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
from crawl4ai.content_filter_strategy import PruningContentFilterLXML

URL = "https://example.com/article"
ARTICLE = "这是一篇用来验证网页正文提取的文章，包含正常段落与不规范属性。" * 10
for malformed in (False, True):
    broken_link = ('<a onclick="track(\'师傅那句" 中文"属性="">推荐链接</a>'
                   if malformed else '<a href="/related" class="related">推荐链接</a>')
    html = f"<html><head><title>测试文章</title></head><body><nav>{broken_link}</nav><article><h1>测试文章</h1><p>{ARTICLE}</p></article></body></html>"
    result = LXMLWebScrapingStrategy().scrap(URL, html, excluded_tags=["footer"])
    assert result.success, "HTML scraping failed"
    assert ARTICLE in result.cleaned_html
    assert "onclick" not in result.cleaned_html
    for cleaning in (False, True):
        generator = DefaultMarkdownGenerator(content_filter=(
            PruningContentFilterLXML(threshold=0.48, threshold_type="dynamic") if cleaning else None))
        markdown = generator.generate_markdown(result.cleaned_html, base_url=URL)
        assert ARTICLE in markdown.raw_markdown
        assert "# 测试文章" in markdown.raw_markdown
        if cleaning:
            assert ARTICLE in markdown.fit_markdown
# A dense recommendation outside the article must not become article content.
# Sites without article tags must explicitly select the body. Target elements
# keep full browser HTML so metadata remains available before content selection.
for tag in ("article", "main"):
    html = (f"<html><head><title>测试文章</title></head><body><{tag}><h1>测试文章</h1><p>{ARTICLE}</p></{tag}>"
            "<div><a href='/ad'>推荐广告与无关商品推荐</a></div></body></html>")
    result = LXMLWebScrapingStrategy().scrap(
        URL, html, target_elements=["article", "body:not(:has(article))"])
    assert result.success
    assert result.metadata["title"] == "测试文章"
    generator = DefaultMarkdownGenerator(content_filter=PruningContentFilterLXML(
        threshold=0.48, threshold_type="dynamic"))
    markdown = generator.generate_markdown(result.cleaned_html, base_url=URL)
    assert ARTICLE in markdown.raw_markdown
    assert ARTICLE in markdown.fit_markdown
    if tag == "article":
        assert "推荐广告与无关商品推荐" not in markdown.raw_markdown
        assert "推荐广告与无关商品推荐" not in markdown.fit_markdown
print("Crawl4AI HTML/Markdown regression checks passed")

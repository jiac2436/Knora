# 本地 Crawl4AI 修复镜像

基于官方 0.9.4，镜像名 `pka/crawl4ai:0.9.4-attrfix2`。

官方 API 接入复核、飞书复现及剩余内容质量限制见 [接入复核](INTEGRATION_REVIEW.md)。

## HTML 属性解析修复

什么值得买文章推荐区的 `onclick` 含有未转义双引号，导致 lxml 解析出不规范属性名。Crawl4AI 的 `remove_unwanted_attributes_fast()` 在 `dict(el.attrib)` 处抛出 `ValueError: Invalid tag name`；抓取响应仍可能标记 `success=true`，Markdown 却是错误说明。

补丁仅把属性读取改为 `dict(el.attrib.items())`，随后仍按 Crawl4AI 原白名单过滤属性。构建时检查上游代码位置并运行正常/不规范 HTML、清洗开/关的原生 Markdown 回归检查。上游升级或移除该语句时构建会失败，必须审查后更新补丁。

```bash
docker build -t pka/crawl4ai:0.9.4-attrfix2 infra/crawl4ai
```

若已缓存的官方镜像使用完整 registry 名，可加 `--build-arg CRAWL4AI_BASE_IMAGE=registry-1.docker.io/unclecode/crawl4ai:0.9.4`。

## 出站代理 DNS 阻塞修复

0.9.4 的 `/app/egress_proxy.py` 在异步 HTTP/HTTPS 连接处理器中同步调用 `resolve_and_pin()`，其中 DNS 查询会阻塞 API 事件循环。本机 HTTPS DNS 转发放大了多域名页面的等待，飞书抓取期间健康接口也会超时。

第二版补丁仅将这两处调用放入 `asyncio.to_thread`，仍使用原来的地址校验与固定 IP 连接规则。慢 DNS 回归检查确认事件循环能及时处理其他任务，受阻地址仍返回 403。旧镜像同一测试失败，新镜像通过。Worker 等待响应上限改为 330 秒，覆盖当前服务端 300 秒总期限；连接建立仍为 5 秒，等待结果超时与连接失败分别提示。

## 本机代理网络

部分代理会把公开域名解析到 `198.18.0.0/15` 虚拟 IP，这会被 Crawl4AI 自身的 SSRF 防护拒绝。仅在该本机环境设置 `PKA_CRAWL4AI_DOH_PROXY=http://host.docker.internal:7897`（改为实际 HTTP 代理端口）。镜像会启动只监听容器回环地址的 HTTPS DNS 转发器，让 Crawl4AI 获得真实公网地址，内网 URL 防护继续开启。未设置时沿用 Docker DNS；生产环境不设置此项。

启动过程短暂使用 root 配置容器 DNS，然后以官方原有的 appuser 运行 Crawl4AI、Redis 与 Chromium。需要已有的 `PKA_CRAWL4AI_TOKEN`，不得将真实令牌写入仓库。

首次创建容器：

```bash
docker run -d --name pka-crawl4ai --shm-size=1g \
  -p 127.0.0.1:11235:11235 \
  -e CRAWL4AI_API_TOKEN="$PKA_CRAWL4AI_TOKEN" \
  -e CRAWL4AI_ALLOW_INTERNAL_URLS=false \
  -e PKA_CRAWL4AI_DOH_PROXY="${PKA_CRAWL4AI_DOH_PROXY:-}" \
  pka/crawl4ai:0.9.4-attrfix2
```

已有同名容器需先停止并保留为回滚容器。日后 `docker restart pka-crawl4ai` 会自动恢复 DNS 设置，无需再手动运行脚本。

## 独立诊断

通用清洗开启时，使用 `target_elements=["article", "body:not(:has(article))"]` 优先限定文章区域，再由 PruningContentFilterLXML 做密度过滤。没有 `article` 的页面仍读取 `body`。浏览器保留完整 HTML，解析阶段提取元数据后再筛选正文，避免浏览器 css_selector 丢失标题或在没有 article 时返回空内容。清洗关闭时沿用整页读取；文章内部的引用链接仍保留。原始 Markdown 指这次配置下 Crawl4AI 生成的原生结果。

诊断不写数据库、不走 HTML 正文回退。每条链接分别测试清洗关闭与开启，输出状态码、HTML/Markdown 字符数、解析错误、微信验证页和耗时；任一结果不可用时退出码为 1。

```bash
PKA_CRAWL4AI_URL=http://127.0.0.1:11235 \
PYTHONPATH=services/api:. .venv/bin/python -m services.worker.crawl4ai_diagnose \
  https://post.smzdm.com/p/aomx4e2m/ https://docs.python.org/3/library/pathlib.html
```

Worker 同样需要令牌环境变量。若主机 DNS 是代理虚拟 IP，还需本机的 `PKA_WEB_ALLOW_FAKE_IP=1`。日志仅记录域名、清洗选择、状态码、正文/原始 Markdown 字符数、解析失败原因和耗时，不记录令牌或完整正文。正文上限仍为 20 万字符，原始 Markdown 单独允许 100 万字符；Markdown 因链接地址等过长时，使用同次抓取 HTML 的可读文字建立正文索引，完整原始 Markdown仍可预览。

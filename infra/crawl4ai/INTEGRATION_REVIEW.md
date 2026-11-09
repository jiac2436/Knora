# Crawl4AI 接入复核（2026-09-30）

## 依据

优先使用官方 API 文档、官方示例和本机安装的 0.9.4 源码。在线自部署文档部分示例仍标为 0.9.2，具体请求字段须与当前服务核对。

- [自部署 REST 接口、任务队列、超时](https://docs.crawl4ai.com/core/self-hosting/)
- [运行配置与页面等待](https://docs.crawl4ai.com/api/parameters/)
- [内容选择：css_selector 与 target_elements](https://docs.crawl4ai.com/core/content-selection/)
- [返回字段、重定向、Markdown](https://docs.crawl4ai.com/api/crawl-result/)
- [虚拟滚动与官方使用示例](https://docs.crawl4ai.com/advanced/virtual-scroll/)
- [0.9.4 出站代理源码](https://github.com/unclecode/crawl4ai/blob/v0.9.4/deploy/docker/egress_proxy.py)

## 当前接入核对

| 项目 | 结论 |
| --- | --- |
| 请求接口和认证 | `/crawl` + Bearer token，与当前服务一致 |
| 配置序列化 | CrawlerRunConfig / params 和嵌套 Markdown 生成器由当前 REST 接口接受 |
| 正文清洗 | target_elements 在保留完整页面上下文后选取正文，再生成 fit_markdown；原生 raw_markdown 单独保存 |
| 动态页面 | 默认 domcontentloaded 只保证初始 DOM；不代表异步或滚动内容完整 |
| 等待预算 | 原客户端 60 秒小于本机服务总期限 300 秒；已将客户端等待调为 330 秒，连接建立仍 5 秒 |
| 错误提示 | 原先所有 HTTP 传输错误均提示本地服务不可用；已区分等待结果超时与连接失败，日志补充类型与耗时；明确识别上游反爬拦截，不展示为服务故障 |
| 服务响应 | 上游 0.9.4 出站代理在异步函数同步解析 DNS；已将两个连接处理器的解析移到线程，保留同一校验与固定 IP 连接 |
| 异步任务接口 | 官方为慢任务提供 `/crawl/job`；本机实际模型支持 browser_config、crawler_config。项目已有持久化 Worker 队列，本次未迁移到另一套任务持久化协议 |

## 飞书实际复现

原镜像日志显示该链接一次 FETCH 耗时 223.24 秒；客户端会在 60 秒先失败。再次加载时，连 `/health` 的独立 10 秒请求也超时。代理源码同步 DNS 阻塞与这些现象一致，本机 HTTPS DNS 转发会放大资源域名较多的网页耗时。

慢 DNS 回归测试在旧镜像上失败（API 事件循环被阻塞），在新镜像上通过。受阻目标仍返回 403，不关闭 SSRF 地址校验。

新镜像 `pka/crawl4ai:0.9.4-attrfix2` 独立实测：15.04 秒返回，最终 HTTP 200（初始重定向 302），标题为“01-RAG基础 - 飞书云文档”，原生 Markdown 可生成。抓取期间三个健康请求均 HTTP 200，最长约 0.01 秒。这次结果包含正文开头，但不能按全文通过验收。

另按官方 VirtualScrollConfig 对该页 `.bear-web-x-container` 做 30 次有界滚动实验。正文由约 2,051 字符增至 8,602 字符，但合并结果出现多份目录和标题，不能以字符数增加当作正文完整性证据。该实验仅用于诊断，未将飞书专用规则写入项目默认配置。

## 仍需处理的内容质量问题

飞书这类文档使用内部滚动容器和虚拟化节点。默认读取不能证明全文已加载，通用文章过滤也不能自动识别文档块与目录。若要将飞书列为稳定支持站点，需要单独验证文档块合并、去重、滚动终点和加载失败的识别；需真实内容验收，不能只检查 success 或 Markdown 非空。

其他接入缺口：读取层尚未单独拒绝目标页的最终 HTTP 4xx/5xx（服务 HTTP 200 与页面 HTTP 状态不同），以及清洗输出为空时会选择 raw_markdown。后者可能保留页面杂项。这两项作为后续明确改动事项列出，本次未扩大修复范围。

登录、验证码、访问权限和图片中的文字仍不等于浏览器网页文本读取；不承诺所有站点完整读取。图片里的知识内容需要独立 OCR 路径。

## 验证与兼容性

API 测试 70 项通过；镜像构建时 HTML/Markdown、标题保留、文章外推荐过滤、无 article 页面、慢 DNS 响应、受阻目标拒绝回归均通过。只修改读取层等待/错误日志及本地修复镜像，不修改数据库格式；新镜像与 Worker 已加载。旧容器保留为 `pka-crawl4ai-before-dnsfix2` 用于回滚。

部署后普通 Python 文档实际读取正常。什么值得买的一次回归请求被上游明确判为 `Blocked by anti-bot protection`；该次未通过真实抓取验收，不以旧成功结果替代。项目现已给这类失败明确的反爬提示，既有成功版本不会因诊断请求改变。

# M1 API 与 Agent 服务

> 当前 macOS App 已内置本机服务与模型执行器，不要求用户单独启动本文件中的服务。下方命令用于外置开发或历史排查；当前 Python 3.12 开发、依赖和测试入口见 [开发与验证](../../docs/开发与验证.md)，客户端构建见 [BUILD](../../apps/macos/BUILD.md)。

## 微信双栈代理修复（2026-10-19）

网页与图片读取的共享Worker检查补齐IPv6虚拟地址 `2001:2::/48`，继续使用原有Fake-IP开关与HTTPS域名限制，无API、数据库或备份迁移。完整本地客户端需要[本次受控热修包](../../docs/verification/a1/wechat-dualstack-acceptance-20261019.md)，外置开发Worker需重启加载源码；AI网关不受此修复影响。

## 文章图文联合分析（2026-10-17）

v3 分析新增可选 `images` 与证据 `image_id`，预览返回 `image_count`。`ai_images` 绑定当前正文基础版本图片的指纹和独立原图引用；持久化快照不含二进制，Worker 发送前才加载原件。图片未保存、缺失或超限时拒绝创建图文计划；不会降级只分析正文。原文与原文证据不写入模型识图内容，图片结论保存在正式分析产物和摘要索引。schema16，无新依赖或迁移，旧纯文产物保留可读。新的全域计划需显式确认图片范围，旧任务不能扩张授权。范围、预算与兼容见 [设计](../../docs/design/文章图文联合分析-2026-10-17.md)，状态见 [验收](../../docs/verification/a1/joint-images-acceptance-20261017.md)。后文日期小节为历史说明。

当前构建、随包/外置模式、数据库和重启规则见 [BUILD.md](BUILD.md)。本说明含历史接口与迁移记录；日常完整客户端必须按 [macOS BUILD](../../apps/macos/BUILD.md) 打包，不能仅启动开发 API 代替随包验收。异步流程见 [Worker说明](../worker/README.md)，模型调用见 [AI网关构建说明](../ai_gateway/BUILD.md)。

## 微信图片归档（2026-10-17）

复用既有 `source_images` 和 `web_read_markdown`，当前 schema16，无数据库/备份格式迁移或新依赖。微信正文版本发布时按原文顺序建立图片记录，`retrieval_method=wechat_ua`；后台独立下载，正文的成功状态不依赖单张图片成功。原件仍通过带 Owner 凭证的图片 GET 接口读取，不公开远程图片代理。

- 资料详情新增兼容字段 `web_images_can_archive`。`images` 仅列当前正文基础版本的图片；历史图片原件仍保留在已有历史版本与备份中。个人补充版本继续引用原基础版本图片。
- `reading_text` 将已登记的微信图片替换为 `know-image://UUID` 本地标记；`extracted_text` 与证据内容不插入图片 ID。阅读位置映射沿用 `reading_text` 不同于索引文本时按文本定位的规则。
- `POST /v1/sources/{source_id}/images/retry`，Body `{}` 或 `{"image_id":"UUID"}`，返回202与 `queued_count`。仅 Owner 可调用，只重排当前微信基础版本的失败图片；已保存、在途图片不重复入队。旧微信文章有原始图片 Markdown 而无图片记录时，空请求可补建任务，不改变正文版本、证据或 AI 产物；不存在的旧版本图片返回404。最多200张，超限返回422。
- 备份保留图片原件与版本；快照将未完成的微信下载转为 `unavailable`，恢复后用户手动重试。恢复验证同时拒绝伪造包携带活动下载任务，不能靠重算 checksum 绕过。

必须同时更新 API、Worker 和完整客户端运行包。独立 AI 网关及插件1.0.5无需更新；本轮微信图片不做 OCR 或多模态分析。使用与验收见 [图文归档记录](../../docs/verification/a1/wechat-images-acceptance-20261017.md)。

## 插件文章导航（2026-10-17）

MCP 的本机 API 桥接在授权读取结果中添加可选 `citation_url`，保留 `app_url` 与内部工具 ID。新地址仅使用桥接已经校验的本机 API 地址及服务返回的合法原生文章 URI，不信任 Host 头拼接地址。`GET /open/library/{UUID}/source/{UUID}` 仅在 local 模式、回环客户端及回环 Host 下提供无文章内容的导航页；不读取数据库、不发放访问凭证。页面尝试打开固定的 `knowledge-inbox` 协议，并提供浏览器手动打开按钮，不接受任意重定向参数。原文章仍由 macOS 客户端验证当前本地库及 Owner 权限后读取。

这是兼容的派生字段，无数据库迁移。HTTP 地址只在当前客户端服务端口有效，重启后重新查询；不承诺历史链接永久可用。需要更新随包 API／MCP／插件 1.0.5，独立 AI 网关无改动。见 [验收说明](../../docs/verification/a1/browser-article-links-acceptance-20261017.md)。

## 桌面飞书采集契约

`POST /v1/captures` 新增 `capture_type: feishu_document`，仅允许桌面客户端提交飞书域名链接和已读取正文。`content.text` 保存 Markdown，`coverage` 为 `full_text` 或 `partial_text`，新增必填 `feishu_document: {document_id, revision_id, cli_version}`，不接受认证 Token 等额外字段。仅该类型使用 `PKA_MAX_FEISHU_CAPTURE_CHARS`（默认 1000000）上限；原有采集限制不变。幂等摘要包含文档版本信息。

正文直接保存为 `feishu-cli-v1` 版本，不建立网页读取任务，使用原有关键词和语义索引。Markdown 复用 `web_read_markdown` 归档表和 `/v1/sources/{id}/crawl4ai-markdown` 接口，`origin` 新增 `feishu_cli`；旧接口和原有返回值保持兼容。资料类型返回 `content_type: link`。详情新增可为空的 `feishu_document` 字段；个人补充后仍保留原文 Markdown 和原飞书版本元数据。附件和评论不单独归档。

飞书图片：采集请求可带 `feishu_images`（最多 200 条）：`{id: UUID, ordinal, block_id, caption, status: pending|unavailable, failure_reason, ocr_text}`。正文 Markdown 使用 `know-image://UUID` 关联原文位置。客户端 OCR 文字单独按来源标记追加到提取文本中，进入现有索引；归档 Markdown 不追加 OCR。正文及 OCR 合计受飞书文本上限约束。新增可选空图片清单不改变旧请求的幂等摘要。

`PUT /v1/sources/{source_id}/images/{image_id}` 接收已在清单声明的图片原件（原始二进制）。只允许资料所有者上传；相同内容重试返回 200，不同内容返回 409。`GET` 同路径返回已归档原件，需要 Know App 账户凭证，不公开服务图片。服务端用 Pillow 验证 PNG/JPEG/GIF/WebP，拒绝 SVG、伪造格式、超过 3000 万像素或超过 `PKA_MAX_IMAGE_BYTES`（默认 20 MB）的文件。原件保存在 `uploads/images/随机文件键`，不使用外部资源标识作磁盘路径。详情新增 `images` 清单，包含保存状态、段落 ID、顺序、OCR 及受保护的下载路径；旧客户端可忽略。

图片迁移：新增 `source_images` 表。本机重启 API 自动创建；生产先执行 `infra/postgres/feishu-images.sql`，并同时更新 API、Worker 与 Pillow 依赖。旧 Worker 无法清理新表及图片文件。删除资料立即禁止读取，Worker 随后删除原件与清单。API 验证和 Owner 权限检查保持不变，其他采集类型不接受飞书图片清单。旧资料不会自动补采图片，需要重新导入。

迁移：新增 `feishu_document_artifacts` 表（资料版本外键、飞书文档 ID、飞书版本号、CLI 版本）。本机开发重启 API 时 `create_schema()` 自动创建，无需修改旧表或旧数据。生产环境先执行 `infra/postgres/feishu-cli.sql`，再同时更新 API 和 Worker；旧 Worker 不支持新归档表的删除清理。旧客户端无需修改，只有更新后的 macOS 客户端提交新类型。移除功能时不要直接删除有数据的归档表。

## 本机启动

在仓库根目录创建 Python 环境并安装依赖：

```bash
python3 -m venv .venv
.venv/bin/pip install -r services/api/requirements.txt
PYTHONPATH=services/api .venv/bin/uvicorn app.main:app --reload
```

默认数据库固定为 `services/api/pka-dev.db`，因此从不同目录启动 API、Worker 和 MCP 时仍会访问同一开发数据库。也可通过 `PKA_DATABASE_URL` 指向 PostgreSQL。

开发环境先调用 `POST /v1/dev/sessions` 获取 App Bearer Token，再创建知识域、项目、资料和 Agent 连接。Agent Token 只在连接创建响应中显示一次，数据库只保存摘要。

## 邮箱密码账户

Mac 客户端可使用邮箱密码注册、登录与找回密码。设置 SMTP 后启动 API：

```bash
export PKA_SMTP_HOST="smtp.example.com"
export PKA_SMTP_PORT="587"
export PKA_SMTP_USER="your-smtp-user"
export PKA_SMTP_PASSWORD="your-smtp-password"
export PKA_SMTP_FROM="知识收集箱 <no-reply@example.com>"
PYTHONPATH=services/api .venv/bin/uvicorn app.main:app --reload
```

端口 587 使用 STARTTLS，465 使用 TLS 直连。不要把真实 SMTP 凭证写入仓库。未配置邮件服务时，注册和找回密码接口返回 503；已有账户仍可登录。正式部署必须使用 HTTPS 后台地址。

账户接口：`POST /v1/auth/register` 创建待验证账户并发送邮件；`POST /v1/auth/verify` 验证邮箱并创建会话；`POST /v1/auth/sessions` 密码登录；`DELETE /v1/auth/sessions/current` 退出；`POST /v1/auth/password-reset` 请求重设邮件；`POST /v1/auth/password-reset/confirm` 更新密码并撤销所有旧会话。验证与重设凭证仅存 SHA-256 摘要、30 分钟有效且只能使用一次。密码使用独立盐的 scrypt；连续五次失败锁定 15 分钟。Mac 端只把会话令牌存入系统钥匙串，不保存明文密码。

现有 SQLite / PostgreSQL 数据库启动时新增 `password_credentials`、`account_tokens` 表，不改动原有用户和资料。已有开发账户可用同一邮箱完成邮件验证后设置密码。生产环境仍需 SMTP 可用性、反滥用限流、邮件投递与多实例运行的部署验收。

## 检索与列表性能

`GET /v1/sources` 现在按页返回，默认 50 条、最多 200 条；可传 `offset`、`limit`、`domain_id`、`project_id`，响应的 `next_offset` 为下一页位置，`null` 表示结束。旧客户端若只请求一次，将只看到第一页，升级客户端时必须处理分页。列表按批次读取上下文、项目和当前版本号，不加载正文。

关键词检索新增 `source_search_terms` 倒排索引表，按账户与词项索引当前版本正文、标题和个人备注。API 启动时会为旧资料自动回填；首次升级若资料很多，启动时间会增加。采集、补充、OCR 完成和备注更新会同步刷新索引，删除资料后立即从结果中排除，Worker 随后清理索引项。升级前备份数据库；SQLite 和 PostgreSQL 均由 `create_all` 新增表，无旧表字段修改。前 64 个查询词参与索引候选筛选，仍按原有证据评分；长查询和中文检索质量需用真实语料继续评估。

语义检索使用后台本地的 `BAAI/bge-small-zh-v1.5`（FastEmbed/ONNX）。首次安装基础依赖时包含 FastEmbed；首次运行 Worker 和 API 的语义查询时会下载约 95 MB 的模型权重。默认缓存目录为 `services/api/model-cache`，可用 `PKA_SEMANTIC_CACHE_DIR` 指向共享目录；网络受限时可将完整 ONNX 模型放在本地目录，再在 API 和 Worker 中设置同一个 `PKA_SEMANTIC_MODEL_PATH`。Worker 异步为当前版本的证据按最多 480 字、重叠 80 字建立向量；旧资料启动时自动排队。搜索合并关键词与语义候选，返回 `match_type`、证据 ID、片段及定位；Mac 客户端可显示命中片段，PDF 点击结果会跳至证据页。索引尚未完成时关键词检索照常可用。

如果首次模型下载停在 Hugging Face，可改用 FastEmbed 内置的备用来源；命令会显示下载进度，并把模型放入默认缓存目录：

```bash
mkdir -p services/api/model-cache
curl -fL --progress-bar --connect-timeout 10 --max-time 300 \
  https://storage.googleapis.com/qdrant-fastembed/fast-bge-small-zh-v1.5.tar.gz \
  -o services/api/model-cache/fast-bge-small-zh-v1.5.tar.gz
tar -xzf services/api/model-cache/fast-bge-small-zh-v1.5.tar.gz -C services/api/model-cache
PYTHONPATH=services/api .venv/bin/python -c 'from app.semantic import embed_query; print(embed_query("我的爱好").shape)'
```

预期最后输出 `(512,)`；确认后可删除下载的 `.tar.gz`。程序会自动识别缓存中的 `fast-bge-small-zh-v1.5/model_optimized.onnx`，无需额外设置模型路径。

升级已有数据库前请备份。`create_all` 新增 `semantic_index_jobs` 和 `evidence_embeddings`，不修改旧表；删除资料时立即从语义结果排除，Worker 随后清除向量。此 MVP 在 SQLite/PostgreSQL 上以账户范围内的精确向量扫描工作，适合个人知识库；大量证据片段或高并发时应改为专用向量索引并压测。默认相似度下限为 `0.5`，可通过 `PKA_SEMANTIC_MIN_SIMILARITY` 调整；这是少量中文样本的初始值，正式使用前需用真实资料评估。若模型下载或索引失败，安装及网络恢复后以 `--retry-semantic` 重启 Worker 重试。`PKA_SEMANTIC_ENABLED=0` 可暂时关闭语义检索，保留关键词能力。

## MCP stdio

M1 使用官方 MCP Python SDK 的 stdio transport。创建 Agent 连接后，在启动 MCP 进程的环境中提供连接令牌：

```bash
export PKA_AGENT_TOKEN="创建连接时返回的令牌"
PYTHONPATH=services/api .venv/bin/python -m app.mcp_server
```

服务只注册 `search_knowledge`、`get_evidence`、`get_source` 三个只读工具。每次调用都会重新检查连接状态、知识域授权、资料可见性和删除状态。

Codex 配置示例见 [agent/codex-config.toml.example](../../agent/codex-config.toml.example)。令牌通过环境变量转发，不写入示例配置或仓库。Codex 的 stdio MCP 配置支持 `command`、`args`、`cwd` 和 `env_vars`，详见[官方 OpenAI 文档](https://learn.chatgpt.com/docs/extend/mcp)。

## PDF 文字识别 Worker

上传 PDF 后，API 保存原件并创建 OCR 任务。账户可通过 `GET/PUT /v1/ocr/preferences` 选择 `pdfium`（默认，现有 PDFium 文字提取）或 `pdf_inspector`（按页解析版面为 Markdown），并设置 PaddleOCR 的 `tiny`、`small` 或 `medium`（默认 `small`）模型。两个引擎都只在需要 OCR 的页面调用本地 PaddleOCR；`pdf_inspector` 不使用 Firecrawl 官方 API。任务在入队时固定引擎和模型，设置变更只影响新上传与 `POST /v1/sources/{id}/ocr` 重新扫描。结果写入新的不可变资料版本，每页一条证据，检索与 Agent 均只使用当前版本；历史证据和人工补充保留。

非 PDF 的 Word、PowerPoint、Excel、OpenDocument、RTF、EPUB、CSV 和 `.md` 上传后进入 `document_jobs` 队列。Worker 对 `.md` 直接读取 UTF-8 源文（允许 UTF-8 BOM），其他格式使用本地 `firecrawl-anydoc` 转为 Markdown；结果保存为正文版本并建立检索索引。空文件、非 UTF-8 或含 NUL 的 Markdown 会显示解析失败，但原件仍保留，可通过文档解析重试接口重试。任何路径都不启用 anydoc 的 `ocr="hosted"`，也不调用 Firecrawl 服务。PDF 根据文件内容标识走现有 PDF 任务；扩展名为 `.pdf` 但内容不是 PDF、或扩展名不在支持列表中的文件会返回 422。解析失败时保留原文件，在资料详情的 `document_parse` 字段提供状态及错误；旧的非 PDF 文件不会自动重新解析。已有数据库无需迁移；更新 API 与 Worker 后重启，随包本地模式需重新构建运行包。

API 继续使用基础依赖。要在本机运行 Worker，单独安装包含 PDF 与 anydoc 的解析依赖：

```bash
.venv/bin/pip install -r services/api/requirements-ocr.txt
PYTHONPATH=services/api:. .venv/bin/python -m services.worker.main
```

首次扫描页识别需要下载所选 PaddleOCR 模型；数字版 PDF 直接提取文字。默认使用官方 BOS 模型源，可用 `PADDLE_PDX_MODEL_SOURCE` 覆盖；若服务器不能下载模型，请按 [PaddleOCR 官方文档](https://www.paddleocr.ai/main/en/version3.x/pipeline_usage/OCR.html)预置模型。`tiny` 更省资源，`medium` 占用更多内存。处理状态、引擎和错误可从 `GET /v1/sources/{id}` 的 `ocr` 字段查看。单文件 50 MB、最多 100 页；不支持加密 PDF，页面尺寸过大时任务会失败，原文件仍保留。

Docker Compose 的 Worker 镜像单独安装 OCR 依赖，并持久化模型缓存。删除资料后读取会立即被拒绝，派生数据及 OCR 任务由 Worker 清理：

```bash
docker compose -f infra/compose.yaml up --build
```

当前队列按单 Worker 运行设计；Worker 重启后会恢复中断的 OCR 任务。不要同时运行多个 Worker 实例。
Worker 启动时会输出“等待任务”，接到 PDF 任务时会输出开始和完成或失败日志；终端持续占用且暂无新输出表示 Worker 正在等待任务。单独保存的公开网页链接也会排队，由同一 Worker 读取标题和正文，写入新的不可变版本与证据；失败保留原始链接，可通过 `POST /v1/sources/{id}/web-read` 重试。升级会给 `web_read_jobs` 增加读取方式和通用清洗字段（既有任务默认为 `legacy`、清洗关闭），给 `web_read_preferences` 增加通用清洗字段，并创建 `web_read_markdown` 表；升级前先备份数据库并重启 API/Worker。网页读取方式由账户设置选择：`legacy` 使用轻量 HTML 读取，`crawl4ai` 直接使用本地 Crawl4AI 无头浏览器。设置只影响新建的读取任务；重试会按当前设置创建新任务。Crawl4AI 的通用清洗默认关闭，开启后优先提取 article 区域，没有 article 时读取 body，再使用本地 PruningContentFilterLXML 生成正文；仍保存本次读取配置下 Crawl4AI 生成的原生 Markdown 供对照；无需大模型 API。如果 Crawl4AI 抓到完整 HTML、但生成 Markdown 时返回错误，Worker 会从这份 HTML 提取正文并保存。预览接口会为这种已有版本从保存的标题与正文生成 Markdown，并标记 `origin=html_fallback`；原始 Crawl4AI Markdown 不可恢复，正文中的原网页格式也可能丢失。HTML 不可用或正文过短时任务仍会失败。部分网页可能被站点阻挡。微信公众号返回“环境异常、去验证”页面时，任务会失败并提示使用已验证浏览器的网页摘录；验证页面不会被当作文章正文保存。只接收公开 HTTP/HTTPS 标准端口的网页；不处理登录认证、媒体或附件。轻量 HTML 上限 8 MB，Crawl4AI 返回上限 16 MB，正文上限 20 万字符。

启用本地 Crawl4AI 0.9.4 服务（不调用官方云 API）：

```bash
export PKA_CRAWL4AI_TOKEN="$(openssl rand -hex 32)"
docker compose -f infra/compose.yaml -f infra/compose.crawl4ai.yaml up --build
```

也可单独部署 Crawl4AI，并在 Worker 环境设置 `PKA_CRAWL4AI_URL`（服务根地址）和 `PKA_CRAWL4AI_TOKEN`（Bearer 令牌）。只配置 URL、不配置令牌会使浏览器读取失败；两者都不配置时继续使用原有轻量读取。浏览器运行在服务容器内，不弹出用户桌面窗口。当前网页队列仍由单个 Worker 串行消费，Crawl4AI 本身的并发能力不会让现有任务并行。运行浏览器服务请预留至少 4 GB 内存，Compose 为 Chromium 分配 1 GB 共享内存。升级 Crawl4AI 镜像前应核对其网络访问限制与 API 兼容性。

Compose 使用基于 Crawl4AI 0.9.4 的本地修复镜像，修复不规范 HTML 属性导致整页解析失败的问题。本机代理的 DNS 兼容设置、镜像构建及不走回退的独立诊断命令见 [Crawl4AI 调试文档](../../infra/crawl4ai/README.md)。Worker 会记录原始 Markdown 字符数及解析失败原因，便于区分正常转换和 HTML 回退。

本项目通过独立容器使用 [Crawl4AI](https://github.com/unclecode/crawl4ai)，其代码按 Apache-2.0 许可发布。This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).

网页读取默认拒绝非公网解析地址。本地代理若将公开域名映射到 `198.18.0.0/15`，完整客户端可在“设置→网页读取”开启“兼容代理的虚拟 IP（Fake-IP）”，自动保存后用 ⌘Q 退出重开，再重试网页；默认关闭、按物理库独立保存，不随备份迁移。外置开发 Worker 可仅在本机启动时加 `PKA_WEB_ALLOW_FAKE_IP=1`。只允许该段虚拟地址上的 HTTPS 域名，普通内网和本机仍拒绝，生产环境保持关闭。Worker 更新后需要重启，API `--reload` 不会重启独立的 Worker 进程。

本机已用 `pypdfium2 5.13.0`、`PaddlePaddle 3.3.1`、`PaddleOCR 3.7.0` 和 `small` 模型验证两页混合 PDF：第一页直接提取、第二页真实 OCR；经 API 上传和 Worker 处理后可按第二页文字检索，重新识别会保留人工补充及历史证据。首次模型下载后，可通过 `PADDLE_PDX_CACHE_HOME` 指向同一个缓存目录，避免重复下载；Docker 的模型缓存由 `pka-ocr-models` 卷持久化。`tiny`、`medium` 的实际权重尚未在本机下载验证。

`GET /v1/operations/{id}` 可查询删除清理状态。既有 PDF 不会自动回填，可在阅读器中点击“重新扫描”。

升级到双引擎版本前备份数据库，并先停止 API 与 Worker；新版本启动时会为现有 `ocr_preferences`、`ocr_jobs` 表各增加一个 `engine VARCHAR(20) NOT NULL DEFAULT 'pdfium'` 字段。旧设置及排队任务继续使用原引擎。当前版本还会通过 `create_all` 新增 `document_jobs` 表。API 和 Worker 都须安装对应版本并重启；仅 Worker 需要安装 `pdf-inspector` 和 `firecrawl-anydoc`。旧客户端只提交 `model` 时会保留已保存的引擎。生产部署若由独立迁移流程管理数据库，请先在该流程中执行这两条加列语句并创建新表，再启动新进程。

## 验证

```bash
PYTHONPATH=services/api:. .venv/bin/python -m pytest -q services/api/tests
```

测试包含跨账户、跨域、移动、仅 App 可见、删除、撤销、不可变证据、幂等采集、PDF 识别任务和版本切换，以及官方 MCP 客户端通过 stdio 启动服务并调用工具的端到端用例。

## 搜索片段兼容接口

`POST /v1/search` 可选 `mode: exact | semantic`（默认 semantic）、`source_id` 和 `result_unit: source | passage`（默认 source）。桌面搜索面板显式使用 passage，返回同篇文章的多个命中，结果总上限仍为20；原 Agent／旧调用继续按来源去重。片段响应增加 `match_id`、`match_text` 和 `text_locator`（Python Unicode scalar 范围），保留 source_id／revision／evidence_id；PDF 原 locator 仍为页码。语义／总结结果指向实际原文窗口或引用，不要求原文包含查询字面词。标题单独匹配没有虚构的正文范围。所有 owner／domain／project／visibility／tag 范围继续生效。无数据库迁移，schema16／插件1.0.5保持；客户端需随同版运行包更新。验证见 docs/verification/search-passages-20261022/README.md。


### 知识库更新的分析范围（2026-10-22）

全量任务预览与创建接口新增可选 `reanalyze: bool = false`：默认仅补做从未产生完整分析的文章，复用有效结果，跳过过期结果；`true` 重新分析全部文章。模式绑定预览摘要，创建时不得切换。响应增加更新前过期数、实际跳过数及完成后的可选模型回执用量 `token_usage`；回执不完整不显示数字，不用预算估算替代。schema16 无迁移，旧已接受任务和幂等恢复保留。见 [更新验收](../../docs/verification/library-update-20261022/README.md)。

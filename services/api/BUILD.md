# 2026-10-20 内核改造实施状态

> 当前 macOS App 已内置本机服务与模型执行器，不要求用户单独启动本文件中的服务。下方命令用于外置开发或历史排查；当前 Python 3.12 开发、依赖和测试入口见 [开发与验证](../../docs/开发与验证.md)，客户端构建见 [BUILD](../../apps/macos/BUILD.md)。

当前实施源码包含M2配置/凭证、M3 `platform_support` 适配层和M4本地采集队列。`kernel_contract.py`供启动握手与 `/health` 返回非秘密版本/能力元数据；新客户端须使用同版后台，`agent_launcher.py`、`capture_*`、`feishu_capture.py` 与整个平台子目录不可遗漏。schema16保持，不需要重建数据库。阶段状态及兼容边界见 [M4记录](../../docs/verification/kernel-refactor-20261020/M4.md)。

M5已携同版原生启动器和系统辅助程序完整构建、验收并发布，见[M5记录](../../docs/verification/kernel-refactor-20261020/M5.md)。普通用户直接打开唯一正式App，无需手动启动API。手动开发API命令保留原行为；不向API配置供应商Key，也不在日志、任务或备份写Key。下面日期小节保留历史记录。

## M4：已提交采集接管

`/v1/capture-queue`仅在local环境向Owner开放，提供提交、分页查询、终态移除及失败后的显式重试；`/migrate`读取当前库旧v1队列。MCP只读凭证无权提交。提交只引用当前库 `capture-queue/files/` 内的相对原件键与SHA，不接受任意绝对路径或符号链接。稳定条目ID保留原UUID大小写，沿用原SQL幂等键。

设备执行记录与检查点使用 `<data>/jobs/capture-queue-v1/`，私密权限、队列锁及原子写；先复制核验原件再发布回执。客户端回执持久化成功后才释放旧副本。坏记录和迁移错误保留原文件，未提交条目和草稿不执行。资料仍写原SQLite与assets；文件／图片HTTP上传和内核归档复用同一收尾事务。

设备执行目录不进备份、同步或可读导出；恢复知识库不自动执行旧设备任务。永久删除资料会联动清理其采集执行原件，保留防重复采集回执。详细契约与迁移步骤见 [M4设计](../../docs/design/M4-采集任务与旧队列交接.md)。无新增依赖、表结构或插件版本变化。

# API 构建、启动与数据库说明

## 2026-10-17 图文联合分析

当前 schema16，无新数据库、备份迁移或运行依赖。API／Worker 共用新增 `ai_images.py` 与扩展的 v3 图文契约，必须一起更新完整客户端运行包，不能仅重新编译 Swift。独立网关需同时加载图片契约、`vision.py` 和视觉模型配置。外置开发 API 若未使用代码重载需重启；Worker 始终需重启。完整本地 App 自己管理服务，不能给同一个库另启仓库 API／Worker。

历史纯文结果保留，新授权命名空间和全域规则防止旧授权自动发送图片。图片原件继续使用原有备份；模型调用时才创建内联图片字节，不保存到任务JSON。新图文产物建议只用同版程序读取。已验收运行包、构建和质量范围以 [macOS BUILD](../../apps/macos/BUILD.md) 与 [本轮记录](../../docs/verification/a1/joint-images-acceptance-20261017.md) 为准；下方旧版本说明不代表本轮需要降级数据库。

## 2026-10-17 客户端文章链接

MCP 搜索、目录、资料、总结和原文读取新增可选 `app_url`；原文读取还返回标题。只有当前所有者的有效本地安装身份可生成客户端文章链接，非本地库返回 null，不拼造本机入口；原有 Agent 认证、授权、删除和版本检查继续执行。`/v1/me` 在本地安装存在且属于当前用户时附加 `library_id`，客户端按真实库身份验证 URL，其他环境原响应保持不变。

URL 固定为 `knowledge-inbox://library/<library_id>/source/<source_id>`，不包含正文、凭证或 API 地址。数据库、备份和现有字段不变，无迁移/新依赖。完整客户端须重新打包以注册协议，同时更新 MCP/Skill；本轮无需重启模型网关。[验收与运行包](../../docs/verification/a1/article-links-acceptance-20261017.md)。

## 2026-10-17 问答来源展示

`rag.turn_view` 将历史正文中已绑定的内部 UUID 转换为与返回来源数组一致的简短编号，结构化 `evidence_id/source_id/revision_id` 保留供点击原文、权限和版本检查。普通正文中的其他 UUID、数组和日期不变；不会重写 SQLite 历史记录或触发模型。MCP 与 Skill 引用规则同步改为标题/位置，插件生成版本 1.0.3。完整客户端需重组 API／Worker 与 Skill 资源，并重启独立网关加载问答提示词；[本轮验收与产物](../../docs/verification/a1/readable-citations-acceptance-20261017.md)。schema16、备份与存储格式不变。

## 2026-10-17 概览引用兼容修复

当前 schema 仍为 **v16**，无新迁移或运行依赖。v2 概览每条支持 1～10 个合法导航引用，供应商输入仅保留文字和引用列表；原文版本、权限与依赖校验仍在本地。仅 v2 本地节点信封上限改为 2 MiB、单原文窗口最多 1,000 字符，模型输入预算仍为 48,000。完整 App 必须携新 API／Worker 运行包构建，同时重启独立网关；[本轮验收及产物](../../docs/verification/a1/navigation-citations-acceptance-20261017.md)。后续带日期或 schema 标记的小节保留历史边界，以本节和 `migrations.CURRENT_VERSION` 为当前依据。

## 2026-10-16 摘要优先概览更新

知识域更新默认按文档概览一次汇总，超出预算才分批，并显示实际步骤进度。请求新增兼容的 v2 路径，API／Worker 与独立网关均须加载本轮源码；网关重启命令仍为 `bash services/ai_gateway/start.sh`。数据库、原文和单篇结果格式不变，本轮未新增迁移。当前工作区已有回收站 schema v16，请以 `migrations.CURRENT_VERSION` 为准，不能将后续历史 v15 小节当作当前版本。

设计与边界见 [摘要优先概览设计](../../docs/design/知识域概览摘要优先优化-2026-10-16.md)。完整客户端需重新组装运行包，不能仅裸编译 Swift。真实模型质量验收仍需在切换版本后另行执行；普通回归不调用模型。


当前源码数据库版本为 **v15**，以 `app/migrations.py:CURRENT_VERSION` 为准。旧库升级须先备份并迁移，再同时更新 API 与 Worker；下面带版本的小节记录此前批次，不能作为当前部署说明。旧 v8/v13/v14 完整 App 均不能访问 v15 库。2026-10-16 全量分析新增 `domain_full_analysis_jobs` 运行任务表及其约束，保留原文、正式产物和主题格式，无新运行依赖。

核验日期：2026-10-16。命令从仓库根目录执行。API 是 Python 源码服务；开发模式无需单独编译，完整客户端则必须把它组装到内置运行包。总入口见 [项目 README](../../README.md)。

## 完整本地客户端

由 App 的 `LocalService` 启动包内 `backend/app/local_host.py`，使用包内 Python，启动动态回环端口 API 与单个 Worker。本地模式使用 `PKA_ENVIRONMENT=local`、选定知识库的 SQLite 和原件目录。不要额外启动另一组 API/Worker处理同一个本地库。

修改 `services/api/app/*.py` 后，正在运行的 App 不会读取仓库源码；按 [macOS BUILD](../../apps/macos/BUILD.md) 更新运行包并构建，退出旧 App后重新打开。Swift 编译成功不能证明后台已更新。

当前 SQLite schema 为 v15。备份及恢复走既有本地工具。若数据曾升到更高版本，回滚代码不会降级数据库；应保留数据并处理兼容问题，不能删除表或数据库。

### 2026-10-16 知识域全量分析与 v14→v15

停止旧 API/Worker 并备份知识库后，按既有本地迁移流程升级。v15 新增 `domain_full_analysis_jobs`，记录所有者、知识域、冻结输入指纹、预览授权、单篇检查点、预算和阶段进度；同版本 API 与 Worker 必须一起部署。正式资料与摘要格式不变，不会因升级自动调用模型。旧 v14 程序不能读取已升级的 v15 库，不能通过回滚源码降级数据库。

新增 Owner 且仅限 `app` 范围的接口：`POST analysis/full/preview`、`POST analysis/full/jobs`、`GET analysis/full/latest`、`GET analysis/full/jobs/{job_id}`、`POST analysis/full/jobs/{job_id}/cancel`，前缀均为 `/v1/domains/{domain_id}/`。预览和状态查询只读、不调用模型；创建总任务也不立即批量创建所有单篇任务，由 Worker 逐个推进。相同请求编号重试返回同一任务，不能借原编号更换范围或授权。全量分析与当前域普通单篇／概览任务入口互斥；已存在且兼容的单篇任务可等待复用，停止全量任务不会取消这些独立任务。

一次预览确认仅授权本次总任务，不覆盖 `DomainProcessingPreference` 或原有自动更新累计预算。有效已有结果可复用，补齐后再冻结实际主题／域加工计划，最后确认关键词与可用向量索引。正文、备注、人工标签或人工分类变化时停止；失败、未知和预算不足不自动创建替代收费请求。运行任务不随备份搬运，恢复仅保留正式产物并按原规则重建本地缓存，不恢复待执行的付费全量任务。详细契约、保守预算及规模边界见 [全量分析设计](../../docs/design/知识域全量分析-2026-10-16.md)。

迁移、恢复与任务清理有 5 项免费专项回归；全量分析 API 27、Worker 25 项通过。本轮全部使用隔离合成数据及模拟网关，没有真实模型调用；完整 App 的签名与随包验收见 [本轮验收记录](../../docs/verification/a1/domain-full-acceptance-20261016.md)。

### 2026-10-15 知识域三页与问答范围

`POST /v1/domains/{domain_id}/analysis/library`、`library/sources`、`information` 以及概览原文依据接口均为 Owner 的 `app` 范围只读查询；读取不创建索引、主题或模型任务。目录支持分页与当前文件范围，信息统计分开呈现有效分析及各层索引；逻辑存储统计不能当作域的精确物理磁盘占用。

问答 `POST /v1/rag/turns` 可传 `topic_id`，必须同时指定单个 `domain_ids`。服务端按当前用户、知识域、`app` 主题及其后代成员解析真实文件集合，再限制各检索通道。范围写入已有 `RAGTurn.result_json.scope`，成功、失败及未知结果保留；问答历史按范围读取，无范围旧记录归全库。旧客户端不传 `topic_id` 的请求哈希保持兼容，无需数据库迁移。API 与 Worker 仍须同时切换到本轮快照，详见 [范围接口说明](../../docs/design/知识域问答范围接口-2026-10-15.md)。

## 外置开发 API

```bash
python3 -m venv .venv
.venv/bin/pip install -r services/api/requirements.txt
PYTHONPATH=services/api:. .venv/bin/python -m uvicorn app.main:app \
  --reload --reload-dir services/api/app --host 127.0.0.1 --port 8000
```

已有环境时跳过创建/安装。默认环境为 `development`，数据库是 `services/api/pka-dev.db`，原件在 `services/api/uploads`。自定义时 API 与 Worker 必须使用相同的 `PKA_DATABASE_URL`、`PKA_UPLOAD_DIR`、`PKA_SEMANTIC_MODEL_PATH` / `PKA_SEMANTIC_CACHE_DIR`。配置在模块导入时读取，环境变量改变后需重启。

开发 AI 功能还要求 API 和 Worker配置相同的 `PKA_AI_GATEWAY_URL`、`PKA_AI_GATEWAY_TOKEN`；网关凭证须匹配网关端，Provider Key只在网关中。完整本地 App 的网关地址在客户端设置、访问凭证在钥匙串，不能假定终端环境会被 App继承。

API 重载仅覆盖指定源码目录，Worker及网关不会随它自动重启。共享 `ai_contract.py` / `rag_contract.py` 改动后必须协调更新调用双方。

```bash
curl -fsS http://127.0.0.1:8000/health
PYTHONPATH=services/api:. .venv/bin/python -m pytest services/api/tests -q
```

健康检查只证明 API存活；解析、AI、RAG和索引需要 [Worker](../worker/README.md)。真实模型调用不是普通测试的一部分。端口/钥匙串测试可能被执行沙箱禁止，应识别环境错误，不修改业务逻辑来使其通过。

## 数据与部署边界

开发 SQLite、App本地SQLite与Docker PostgreSQL是不同数据源，不能因看到不同资料而删库。Docker命令及共享卷见 [开发服务指南](../../docs/本地服务启动指南.md)。目前 Compose不自动配置或启动AI网关，不应宣称直接启动Compose便可完成AI分析。

基础依赖见 `requirements.txt`；文档/OCR开发依赖见 `requirements-ocr.txt`。内置运行环境使用独立锁文件 `apps/macos/runtime/requirements-macos-arm64.lock`，不是复制开发虚拟环境。迁移、原件格式或依赖变化需同步审查 Worker、备份/恢复、导出及随包兼容。


## A1域概览开发入口（源码schema v11）

当前新增Owner API预览、预算授权、显式概览任务和当前结果读取，接口顺序/范围见 [A1实施记录第6节](../../docs/A1知识库视野实施记录.md)。预览不调用模型；创建并执行任务会发送资料摘要及引用的原文片段到配置网关。不要以建任务充当健康检查。完整App仍是旧v8快照，当前不能用它打开v11数据库。API与Worker必须运行同一源码版本和同一SQLite库；改变源码并不更新随包运行环境。新版本须按现有完整构建流程重新组装、验证manifest、再构建App。


### A1多批加工（源码schema v12）

新增 `analysis/build/preview`、`analysis/build/jobs` 和计划进度查询，详见 [A1实施记录第7节](../../docs/A1知识库视野实施记录.md)。预览包含全计划上限，明确授权后才运行。变更合并自动调度已接入，但默认关闭且需要明确授权；客户端入口仍未接入。API/Worker使用当前v13源码和同一SQLite库，网关需要加载节点契约及v2提示词；旧网关不会因重启API自动更新。新完整App必须重新组装运行环境再构建，不能拿旧v8包验证v13库。

### A1变更合并自动调度（schema仍为v12）

API与Worker应一起加载本轮源码。策略JSON新增可选`automatic_updates`（默认false）、`automatic_max_sources`和`automatic_max_steps`；旧策略按默认关闭解析，无新表/迁移。开启自动更新必须显式设置两个正上限，首次域构建仍经`build/preview`与`build/jobs`手动确认。

使用现有`analysis/authorization`接口提交策略与新的授权周期ID（预算按此周期累计，不自动刷新）。`POST analysis/schedule-state`请求体`{"scope":"app"}`查看等待、暂停、预算不足、需人工确认等原因和剩余预算；此接口只允许域Owner，不能传topic选择。详见[A1实施记录第8节](../../docs/A1知识库视野实施记录.md)。本轮没有自动开启任何真实用户的策略，也没有构建新版完整App。

### A1概览辅助召回基线（schema仍为v12）

加载当前API源码后，现有搜索、RAG和MCP搜索可使用有效的v11/v12域/显式主题产物。新增返回字段`knowledge_node`及命中来源`knowledge_node`，只提供检索线索，原文证据字段不变。RAG本地证据JSON可有可选的节点来源字段，网关请求不变；本批不要求重启网关或升级数据库。该批之后已加入v13本地节点向量索引，见下节；客户端概览入口尚未实现。完整App验证仍须重新组装当前API运行包并依macOS BUILD构建，旧v8运行包不能验证此源码功能。

### A1本地节点向量与导航（历史schema v13）

先按本说明的现有`local_runtime`初始化流程准备数据库并备份，再启动同版本API/Worker。v13只新增两个可重建节点缓存表/本地任务及删除触发器，v12正式产物继续可读，备份清空新缓存；旧v8完整App不兼容v13库。本轮未在真实用户库执行迁移。

Owner API新增`POST analysis/navigation`（scope/topic_id/分页）和`POST analysis/index/rebuild`（scope/topic_id）。检索沿用当前搜索/RAG/MCP入口，向量使用既有`PKA_SEMANTIC_ENABLED`与本地BGE路径配置；模型不可用仍能关键词检索。网关请求未变，本轮无需为向量功能重启网关。完整客户端仍须重新组装当前API/Worker的运行包，不能裸build或使用旧v8包验证新功能。

### A1主题映射与中间节点（schema仍为v13，规则v2）

同时更新API/Worker源码。`bounded-tree-v2`将完整主题分支映射为真实主题概览，新计划可能增加独立域根调用，须以新预览预算为准。旧自动更新基线需重新手动预览确认；升级前等待进行中的收费任务结束，不能自动重放旧未知结果。旧v1正式根仍可读，旧中间产物不补写映射。

Owner可用`POST analysis/processing-nodes/{artifact_id}`逐层下钻，body为`{"scope":"app"}`或shareable；不创建收费任务。主题映射和依赖随正式备份保留，本地向量恢复后重建。网关字段、数据库版本均未变。详情见[A1实施记录第11节](../../docs/A1知识库视野实施记录.md)。

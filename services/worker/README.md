# 2026-10-20 内核改造实施状态

> 当前 macOS App 已内置本机服务与模型执行器，不要求用户单独启动本文件中的服务。下方命令用于外置开发或历史排查；当前 Python 3.12 开发、依赖和测试入口见 [开发与验证](../../docs/开发与验证.md)，客户端构建见 [BUILD](../../apps/macos/BUILD.md)。

M1内置执行器随Worker生命周期稳定复用；M2桌面Worker从继承的私有管道接收模型配置和凭证，禁止把模型Key恢复为命令参数或环境变量。父进程生命周期监测在读取有界启动信封后继续保持。任务冻结配置代次，配置不一致时不发送，unknown不重放。配置切换会等待当前持久任务与检查点完成，不由前端busy状态决定。

M3已将Worker就绪、父进程监测、信号与私密文件操作接到平台适配层；需携带完整 `app/platform_support/`，不可用跳过锁/权限检查的方式模拟跨平台。M4在删除任务之后接入正式本地采集队列，再沿用文档解析、网页读取、AI与索引调度。M5已同版完整验收并发布，来源见 [release.json](../../apps/macos/release.json)及[M5记录](../../docs/verification/kernel-refactor-20261020/M5.md)。普通用户无需手动启动Worker，App负责生命周期；后续构建仍须携同版API／Worker／执行器／原生启动器与系统辅助程序。

## M4：采集任务与飞书加工

`capture.py`消费设备执行目录 `jobs/capture-queue-v1/`。内核负责正文/XML同版本核对、图片清单与官方预览回退、大小限制、原件保存及逐张检查点；文件与图片复用API归档事务。重启仅恢复中断的免费采集归档，不自动创建AI任务；失败条目等待用户显式重试。读取目录失败或单条执行记录损坏时保留原文件，其他阅读、OCR与索引任务可继续。

CLI与Apple Vision通过一次性 `platform/macos/KnowledgePlatform` 调用，`PKA_PLATFORM_HELPER`只传此程序路径，由同版原生宿主设置；不传模型Key或CLI授权数据。辅助程序使用有界私有管道，父进程退出后回收自己与CLI，没有独立守护端口。裸开发环境未配置此能力时，飞书任务明确失败并保留检查点。

正文已入库、图片部分上传时从原检查点补齐，保持原条目和图片ID；SQL已提交但任务状态未写完时通过原幂等记录接续。终态移除只清设备暂存、不删知识；永久删除知识则联动清除设备原件。备份不搬运设备执行队列，不因恢复自动采集；schema16及原备份格式保持。

# Worker 构建与启动说明

## 2026-10-19 IPv4／IPv6代理虚拟地址

共享 `_require_public_url` 在显式 `PKA_WEB_ALLOW_FAKE_IP=1` 且HTTPS域名条件下，允许 `198.18.0.0/15` 和 `2001:2::/48`。同时出现IPv4／IPv6虚拟地址不会被误判；普通内网、环回、其他保留范围与包含这些地址的混合解析仍拒绝，每次重定向都重查。正文和图片下载共用该检查。完整客户端使用本次[受控热修包](../../docs/verification/a1/wechat-dualstack-acceptance-20261019.md)并退出重开；外置开发模式只需加载新版Worker，既有环境开关不变。无数据库、模型网关、插件或依赖变更。

## 2026-10-17 图文联合分析

`ai_analysis.call_gateway` 在实际发送前通过 `ai_images.hydrate_images` 读取已绑定的原图，核验文件、大小与摘要，然后临时生成内联图像；任务、分段检查点及产物不保存 Base64。缺失或变化时不调用模型。长图由网关以4096像素图块／96像素重叠保留原始细节，批次最多14原图、64图块；超出单次预算复用既有长文分段和合成流程，最终合成不重发图片。

单次发送、取消、版本检查及失败／未知不重放规则保持。全域规则改为 `domain-full-v2-images`，旧授权任务需重新预览，不能自动扩大图片发送范围。schema16，无新增依赖或迁移；必须与API／客户端一起重新打包，独立网关另行加载新版。范围及当前真实质量状态见 [本轮记录](../../docs/verification/a1/joint-images-acceptance-20261017.md)。下方日期小节保留历史能力边界。

## 2026-10-17 微信图片归档

`web_images.run_image_once` 消费 `SourceImage.retrieval_method=wechat_ua` 的待下载记录，每轮最多处理一张，排在既有 AI/RAG 调度之后。正文先发布并保持可读；单张图片失败记为 `unavailable`，仅用户明确重试时重排。普通 Worker 重启可恢复中断的免费图片下载；付费 AI 任务未知结果不自动重发的规则保持不变。

下载使用微信专用 UA/Referer、显式 timeout，不继承环境代理，每次重定向都检查公网地址与 Fake-IP 规则。复用 Pillow 验证真实 PNG/JPEG/GIF/WebP，默认单图20 MiB/3000万像素、每篇200张/200 MiB。网络等待前提交事务；下载后重新检查资料是否删除、正文基础版本是否变化，失效时丢弃下载内容。写入随机本地文件键、计算摘要；旧版本图片不混入当前详情。原有删除流程统一清理历史图片原件。

无新增依赖/迁移，需更新随包 API和Worker并重新构建完整App；外置开发 Worker 无自动代码重载，需要手动重启。独立 AI 网关无需重启。图片原件归档不等于视觉理解，本轮不对微信图片做 OCR 或模型分析。备份保留已保存原件，未完成下载在恢复后需手动重试。见 [验收与构建记录](../../docs/verification/a1/wechat-images-acceptance-20261017.md)。

## 2026-10-17 网页读取代理网络设置

微信公众号继续使用专用桌面 UA 读取。Fake-IP 地址检查失败时提供明确的代理配置提示，普通私网/本机/混合私网解析仍阻断。完整客户端在“设置→网页读取”配置开关，由 App 显式传给内置 Worker，修改后须用 ⌘Q 退出重开；不会自动重试失败文章。外置开发 Worker 继续在启动环境使用 `PKA_WEB_ALLOW_FAKE_IP=1`，不读取客户端 UserDefaults。默认关闭，无数据库迁移或网关改动。构建与验证见 [本轮验收](../../docs/verification/a1/proxy-web-settings-acceptance-20261017.md)。

## 2026-10-17 文章链接

本轮仅增加只读文章导航与 MCP 链接元数据，Worker 执行和任务契约未改，打开链接不会创建模型任务。完整 App 仍须携同一快照的 API／Worker／Skill 资源构建；模型网关不需因本轮改动重启。构建见 [macOS BUILD](../../apps/macos/BUILD.md)，边界见 [文章链接验收](../../docs/verification/a1/article-links-acceptance-20261017.md)。

## 2026-10-17 可读问答引用

Worker 仍保留结构化证据引用，权限、版本、未知结果和不自动重播规则不变；API 仅在返回问答视图时把内部 UUID 转换为来源编号。完整客户端中的 API／Worker／MCP 和 Skill 资源需使用同一快照，网关需加载 `original-evidence-answer-v1.1`；客户端与插件切换方式见 [本轮验收](../../docs/verification/a1/readable-citations-acceptance-20261017.md)。无新迁移或运行依赖。

## 2026-10-17 概览引用兼容修复

Worker 与 API 一起更新共享 `domain_contract.py`、摘要输入与加工版本 `summary-navigation-v2`，接收最多十个有效引用，避免网关通过后又被本地旧校验拒收。单篇有效结果复用；旧失败任务不自动重放，需要创建新的预览任务。当前 schema16，无新迁移或依赖。完整客户端内置服务由 App 管理，切换到[修复版运行包](../../docs/verification/a1/navigation-citations-acceptance-20261017.md)，并在原终端重启独立网关。

## 2026-10-16 摘要优先概览

新知识域计划使用 `summary-budget-v3`：有效单篇概览在预算内直接汇总，超限才稳定分批，最多三路向上合并；分类树不强制触发逐主题模型调用。Worker、API 与独立 AI 网关必须同时加载新源码（节点请求 v2，结果格式仍 v1）。单篇检查点、失败／未知不重播、原文校验和本地索引沿用既有规则。无本轮新迁移或依赖；工作区已有 schema v16。详见 [设计](../../docs/design/知识域概览摘要优先优化-2026-10-16.md)。


核验日期：2026-10-16。命令从仓库根目录执行。当前数据库为 schema15，后文按版本标注的段落保留历史实施边界，当前部署以 [API BUILD](../api/BUILD.md) 和 [macOS BUILD](../../apps/macos/BUILD.md) 为准。

Worker 消费数据库任务，执行网页读取、文档解析、PDF/OCR、AI分析、RAG回答、原文/摘要索引和删除清理。它没有独立HTTP端口，API入队成功不等于任务执行完成。

## 完整本地客户端

App通过内置 `local_host.py` 启动并管理单个Worker，退出App后相应服务停止。不要对该知识库额外启动仓库Worker。修改源码必须按 [macOS BUILD](../../apps/macos/BUILD.md) 更新运行包并重新打包；API、Worker及共享契约应来自同一源码快照。

## 外置开发模式

```bash
.venv/bin/pip install -r services/api/requirements-ocr.txt
PYTHONPATH=services/api:. .venv/bin/python -m services.worker.main
```

依赖文件包含基础 API依赖。Python环境准备见 [API BUILD](../api/BUILD.md)。API与Worker必须使用同一数据库、原件路径及模型配置；默认分别使用 `services/api/pka-dev.db`、`services/api/uploads`、`services/api/model-cache`。同一数据库只运行一个Worker。

```bash
# 处理至多一次任务，用于隔离测试；不是完整队列排空命令
PYTHONPATH=services/api:. .venv/bin/python -m services.worker.main --once
# 重启时将失败的原文语义索引任务重新排队
PYTHONPATH=services/api:. .venv/bin/python -m services.worker.main --retry-semantic
```

普通Worker默认每2秒轮询。空闲时没有新日志是正常的。Worker没有自动重载，修改代码后在对应终端 Ctrl+C，再用原环境重启；API的 `--reload`不负责Worker。

运行中的AI请求被中断时可能已产生费用，启动恢复会把相应任务标为结果未知，不自动重发。`--retry-semantic`只用于本地原文索引恢复，不能视为付费AI任务重试选项。完整App修改网关配置后的重启行为以客户端流程为准。

## AI网关与可选服务

AI分析/RAG调用独立 [AI网关](../ai_gateway/BUILD.md)。开发模式给API及Worker安全配置一致的 `PKA_AI_GATEWAY_URL` 和 `PKA_AI_GATEWAY_TOKEN`；Provider Key不配置在Worker中。缺网关时基础采集、阅读和本地索引仍可使用。

普通网页读取无需额外服务；选择Crawl4AI时需独立配置其服务/令牌，见 `infra/crawl4ai/README.md`。飞书CLI由客户端调用，当前没有打包进Worker。开发OCR首次可能下载模型；随包模式使用运行包内的small模型，不应依赖联网下载来证明包完整。

## 日志及验证

开发日志在启动Worker的终端，Docker用 `docker compose -f infra/compose.yaml logs -f worker`。随包API/Worker日志默认合并到 `~/Library/Logs/local.pka.knowledge-inbox/local-service.log`，切库后路径可能不同。

通过API查看具体任务及资料状态；验收应确认任务产物已保存、索引就绪和原文引用有效。依赖回归执行API测试，随包生命周期用 `apps/macos/scripts/LocalServiceTest.swift`；步骤见macOS BUILD。模拟网关测试不等于真实模型质量验收。

## 2026-10-16 知识域全量分析

主循环接入 `domain_full_analysis.run_full_analysis_once`，推进用户明确创建的 `domain_full_analysis_jobs`。总调度本身不发送 HTTP：每次只登记一篇单篇分析检查点，由既有 AI Worker 执行；等待时让本地索引和其他任务继续工作。资料补齐及摘要索引就绪后，按最新标签维护本地候选主题，再创建既有 `DomainBuild` 加工主题与知识域概览，最后检查当前原文／摘要／主题／知识域索引状态。普通“更新概览”和自动合并更新仍不补做缺失单篇分析。

单次预览授权通过总任务 ID 关联子任务，不改写原有自动更新策略及累计用量。调用前持久化预留、检查总授权和整域输入指纹；发送及发布时继续沿用原文版本、上下文和范围校验。新 AI 标签、摘要和自动候选主题属于正常加工产物，人工备注、分类、标签语义或资料集合变化则停止后续。采用已有兼容单篇任务时仅等待其结果，取消总任务不取消独立任务。

首次启动恢复保留已确认的单篇长文检查点；只有能够证明尚未发送的步骤才继续。中断的在途请求标记未知，不自动重发；任一单篇、概览失败或未知都会停止后续收费步骤。取消在途任务后也保留未知及可能产生用量的提示，阻止后续长文段落。索引失败显示部分完成，不能把模型生成成功当作检索就绪。`PKA_SEMANTIC_ENABLED=0` 时完成当前关键词索引即可结束，但状态明确为向量停用，不宣称向量完成。

v15 新增的总任务表不随备份搬运，恢复不会继续执行历史全量请求。API、Worker 与完整 App 运行包须一起更新；没有新运行依赖或网关契约改动，无需因本次调度重启独立网关。25 项免费 Worker 专项覆盖失败、未知、取消、重启、长文、索引开关与真实主循环；全部使用合成资料和模拟网关。契约与限制见 [全量分析设计](../../docs/design/知识域全量分析-2026-10-16.md)，完整随包结果以 [本轮验收记录](../../docs/verification/a1/domain-full-acceptance-20261016.md) 为准。

## 2026-10-15 域／主题问答范围重验

三页目录和信息读取不创建 Worker 任务；问答入队仍沿用显式确认和原文冻结机制。范围存入已有 `RAGTurn.result_json.scope`，没有数据库迁移。Worker 在发送到网关前和发布回答前，除原有正文版本、Owner 与删除状态外，再校验原文仍属于指定知识域，以及当前主题及子分类的有效文件集合。文件移域、从主题排除或相关范围失效后，尚未发送的任务取消；已发送但范围已改变的回答不发布，并保留可能产生用量的提示。

成功、失败和未知状态均保留本地范围，历史回答的引用也重新校验范围；不自动重试未知任务。网关仍只接收原有问题与原文证据，不增加目录、用户备注或凭证。API 与 Worker 必须来自同一个本轮快照；独立网关契约未变化，不需要因这项范围校验重启网关。全库旧请求继续兼容，详见 [范围接口说明](../../docs/design/知识域问答范围接口-2026-10-15.md)。

本轮免费回归在受限环境为 1150 通过、1 跳过、1 既有警告、3 项回环端口 `EPERM`；这 3 项隔离提权重跑通过，合计 1153 通过、1 跳过。新运行包的模拟网关 API→MCP→重启／失效／未知结果端到端通过；没有以这些模拟测试代替真实 DeepSeek 质量验收。


## A1知识域概览任务

源码schema v11的Worker会处理Owner API显式创建的`domain_summary_jobs`，先提交预算预留和running状态再请求网关`/v1/summarize-node`。启动时running转unknown，不重发；unknown需用户确认可能重复用量后创建新请求。备份不搬运运行任务。输入变化后的响应不发布，已发布响应读取时也复核版本。当前仅有界单节点请求，不自动安排全域多批合成；暂停策略不阻止用户显式立即执行，预算/授权仍必须满足。网关须独立运行新版源码，真实模型质量验收与模拟测试分开。


### v12多批加工计划

Worker逐步推进`domain_builds`，每一步仍由`domain_summary_jobs`持久化、预留和单次发送。叶批次可以按输入指纹复用；父合成每次重新生成。结果未知的子任务会停止整张计划，重启不会跳过未知批次继续合成。已完成批次正常保留，不在恢复包中搬运执行计划。首次仍须显式预览、授权、创建计划。开启`automatic_updates`并指定`automatic_max_sources`与`automatic_max_steps`后，Worker可以按dirty事件合并创建后续域计划，不补做单篇分析。默认关闭，累计预算不自动重置。

### 变更合并自动调度

`domain_schedule.schedule_domain_once`在本地主题维护后运行，不发送HTTP，只创建冻结计划。默认最新变更后30秒、首次dirty后最长300秒。须有同scope成功完成的手动全域构建；主题构建、单节点请求或备份恢复的产物不能替代首次确认。人工主题结构变化、超出显式规模上限、累计预算不足、失败及未知结果都会阻止自动新建。

自动计划逐步执行时不重复等待合并窗口；每步仍检查暂停、授权、输入代次和预算并原子预留。暂停拦住未发送的自动步骤，已发送请求允许保存有效响应；不会退款或假定无用量。暂停的队列不阻塞其他scope的手动请求。显式手动更新可替换暂停且没有在途请求的自动计划，保留已消费用量和可复用产物；在途请求仍须等它结束。恢复包关闭自动更新并清理运行计划，不重放付费请求。

### v13本地节点向量缓存

API/Worker须使用同版本v13数据库结构，先按API BUILD迁移与备份。Worker自动建立最新域/显式主题根的本地BGE向量，不创建任何付费AI任务。认领任务后提交事务再推理；推理结束前若根被删除/替换，不发布对应缓存。重启仅重排被中断的本地索引；模型明确失败不自动循环重试，通过Owner `analysis/index/rebuild`手动恢复。既有`--retry-semantic`仍只重试原文索引，不作为节点重建或收费AI重试入口。缓存不随备份恢复，恢复后按现有正式产物本地重建；`PKA_SEMANTIC_ENABLED=0`关闭节点向量功能。

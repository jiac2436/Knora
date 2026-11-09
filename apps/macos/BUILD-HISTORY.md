# 历史构建记录

此文档保留早期开发过程，仅供追溯；已删除的本机构建缓存不是可用构建输入。当前命令见 [BUILD.md](BUILD.md)。

# 2026-10-20 内核改造实施状态

M5已按M4冻结源码完整构建并发布到 `dist/知识收集箱.app`。随包解释器／API／Worker／执行器／系统辅助程序与全部Swift一起验收，19,957项清单及深度严格签名通过；真实DeepSeek合成短文、图表、问答、主题与域概览累计7次通过。来源、复现与质量边界见 [M5验收](../../docs/verification/kernel-refactor-20261020/M5.md)及 [构建记录](../../docs/verification/kernel-refactor-20261020/runtime-m5.json)。候选保留于 `.build/kernel-m5-candidate/知识收集箱.app`；用户只使用正式入口，后续仍通过发布器归档／替换，不手工覆盖dist。

M0—M4源码已完成共享执行器、凭证通道、平台边界抽取及正式本地采集业务迁移。`KernelCaptures.swift`、`PlatformContracts.swift`、`LocalKernelContract.swift`、`DesktopIntegration.swift`及全部 `Mac*.swift` 必须随 `Sources/*.swift` 一起编译；不要继续手选旧文件清单。新原生宿主要求同版内核版本/能力元数据，并检查 `capture_queue` 能力。当前正式来源以 [release.json](release.json) 为准，状态见 [实施记录](../../docs/verification/kernel-refactor-20261020/README.md)。

后续完整候选必须携带同版 `services/api/app`、`services/worker` 和整个 `services/ai_gateway`（包含execution/settings/prompts/provider/errors/vision），不能复用旧后台或只复制main.py。桌面启动仅通过有界私有管道传模型凭证；不要将旧 `PKA_AI_PROVIDER_KEY` 环境注入方式重新加到原生启动器。历史单项热修脚本不能用于共享内核构建，完整验收后再通过发布器替换唯一正式App。

M3新增 `app/platform_support/`、`kernel_contract.py`、`agent_launcher.py`。M4新增 `capture_*`、`feishu_capture.py` 和 `services/worker/capture.py`，运行包构建器递归收集Python源码，必须保留子目录及 `__init__.py`，不能只复制app目录第一层文件。本期未交付Windows/Linux客户端。

M4运行包还必须包含同版 `platform/macos/KnowledgePlatform`。`scripts/runtime/build_runtime.py` 会先调用 `build_platform_helper.sh`，编译、签名此辅助程序，再生成包含它及构建脚本摘要的清单。它只在内核调用CLI／系统OCR时执行一次，无监听端口；不再由界面编排飞书正文与图片。`build.sh` 会拒绝缺少它的旧运行包。M5已通过完整App嵌套签名、运行包清单和独立启动验收；后续发布仍须通过这些门槛。

正式本地客户端通过Owner API交接任务，成功保存内核回执后才清理旧暂存；未提交条目和草稿仍在客户端。明确选择外置开发API的兼容入口仍保留旧Swift发送流程，不能据此宣称该旧代码已删除。M4迁移和恢复范围见 [M4验收](../../docs/verification/kernel-refactor-20261020/M4.md)。原生源码集成测试依赖仓库虚拟环境，不能代替M5独立整包验收。

# macOS 构建与运行说明

## 当前发布规则：正式App统一名称

唯一交付为 **`dist/Knora.app`**，当前来源、摘要及验收见 [release.json](release.json)。旧带版本后缀的包已移到 `archive/`；下方所有旧路径和构建命令均是历史记录，不能直接用作当前发布命令。

`scripts/build.sh` 默认构建到 `.build/candidate/Knora.app`，缺少运行包时拒绝默认完整构建，禁止直接写dist。验收后用 `scripts/publish_app.py` 发布，自动归档旧包；详见 [RELEASE.md](RELEASE.md)。当前正式包已在微信双栈完整包上合入模型配置，并重新编译客户端；直接配置入口不再属于未交付代码。复制、失败回滚和运行状态保护已用隔离测试验证。

## 2026-10-19 当前交付：Mac 直接模型配置

入口为“设置 → 模型与 AI → 直接配置模型”，见 [配置说明](../../docs/Mac模型配置.md)。本次按最新正式包组装，只增加进程内网关、三个 Worker 传输接点和对应 Swift 界面；保留微信双栈读取修复。schema16、插件1.0.5不变，无新依赖或数据库迁移。

运行包、完整 Swift 源码摘要及测试结果见 [固定验收记录](../../docs/verification/a1/runtime-model-config-20261019.json)。候选包位于 `.build/model-config-candidate/知识收集箱.app`，正式交付仍只有 `dist/知识收集箱.app`。使用现有 `publish_app.py` 校验、归档并发布，不能再用旧二进制替换新功能。

`scripts/build_model_configuration.py` 记录此次运行包组装流程：核对正式基线和无关源码一致，再生成独立运行包与尚未通过的验收记录。验收完成前不得发布；该脚本不覆盖已存在的候选运行包。

## 2026-10-19 历史交付：微信双栈后台热修

产物 `dist/知识收集箱-v16-微信双栈修复.app`。本次以已验证的图文联合分析App／运行包为固定输入，保留原生二进制，仅更新共享网页地址检查，再完整封装签名。工作区已有独立的网关集成开发；本次热修不包含该开发，不能用当前全部源码的裸构建替代本包。schema16、插件1.0.5、图文分析能力与原数据保留，无新依赖或迁移。

```bash
.venv/bin/python -B apps/macos/scripts/build_wechat_dualstack_hotfix.py
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v16-微信双栈修复.app
```

脚本校验固定父运行包摘要、原App签名，以及读取器仅包含三处预期修改；输出目录必须不存在，不覆盖旧App。新运行包为 `apps/macos/.build/runtime/rebuilt-20261019-v16-wechat-dualstack-verified`，清单SHA-256为 `49e5e8c6e17a5652542520212197c4fda4ffb4f22b9aee32e953dcef42b3c351`。此命令针对无客户端界面变更的受控热修；后续新功能仍走完整源码构建流程。详见 [验收](../../docs/verification/a1/wechat-dualstack-acceptance-20261019.md)与[构建记录](../../docs/verification/a1/runtime-wechat-dualstack-20261019.json)。

用⌘Q退出旧App再切换，随后手动重试失败文章。既有代理开关继续生效；无需重启AI网关或更新插件。下方均为历史交付与完整源码构建说明。

## 2026-10-17 当前交付：图文联合分析

完整客户端为 `dist/知识收集箱-v16-图文联合分析.app`，运行包包含同版 API／Worker，schema16、插件1.0.5，无新依赖或迁移。1294项免费回归、严格签名、运行包一致性、随包模拟端到端和真实DeepSeek合成图表验收通过。真实质量结论限定该合成样本。结果见 [本轮验收](../../docs/verification/a1/joint-images-acceptance-20261017.md)，固定源码见 [冻结记录](../../docs/verification/a1/runtime-joint-images-20261017.json)。

```bash
PKA_APP_OUTPUT="$PWD/apps/macos/dist/知识收集箱-v16-图文联合分析.app" \
PKA_RUNTIME_PAYLOAD="$PWD/apps/macos/.build/runtime/rebuilt-20261017-v16-joint-image-analysis-verified" \
PKA_RUNTIME_MANIFEST_SHA256=48730672003f92c208f2b8dde244d22221ed041b67fa63059102ff573f4af6b1 \
  bash apps/macos/scripts/build.sh
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v16-图文联合分析.app
```

切换前等待已有任务结束，用 ⌘Q 退出旧客户端。网关需要在原终端加载当前源码和视觉模型配置，按 [网关 BUILD](../../services/ai_gateway/BUILD.md) 操作；完整 App 不需要额外启动外置 API／Worker。历史纯文产物保留，图文授权使用新命名空间，旧全量任务需重新预览。无插件更新。新请求会向现有网关发送已归档图片，普通阅读与刷新不触发模型。下方内容为历史构建记录。

## 2026-10-17 前次交付：微信图文归档

当前完整客户端为 `dist/知识收集箱-v16-微信图文归档.app`。API、Worker 与客户端必须一起更新：正文先保存，微信图片使用已有 `source_images` 独立排队并在阅读器原位置展示。schema16、插件1.0.5，无新增依赖或网关契约修改。

```bash
PKA_APP_OUTPUT="$PWD/apps/macos/dist/知识收集箱-v16-微信图文归档.app" \
PKA_RUNTIME_PAYLOAD="$PWD/apps/macos/.build/runtime/rebuilt-20261017-v16-wechat-images-verified" \
PKA_RUNTIME_MANIFEST_SHA256=7c01416ff463514e78e1e8e28884d1151030e62fec7f05279c385f5accde21c2 \
  bash apps/macos/scripts/build.sh
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v16-微信图文归档.app
```

切换前用 ⌘Q 退出旧客户端。无需启动外置 API/Worker、重启 AI 网关或更新插件。旧微信文章点击“补齐文章图片”，不会创建正文新版本或重做 AI。原件随本地备份保存；恢复时未完成的图片任务转为手动重试。新原生测试 `LocalWebImagesTest.swift` 使用独立临时库与随机测试钥匙串，不访问用户资料或调用模型。完整范围及验证见 [本轮验收](../../docs/verification/a1/wechat-images-acceptance-20261017.md)；源码与包哈希见 [冻结记录](../../docs/verification/a1/runtime-wechat-images-20261017.json)。

## 2026-10-17 前次交付：网页读取代理网络设置

当前完整客户端为 `dist/知识收集箱-v16-代理网络设置.app`，保留插件 1.0.5 和前版文章 HTTP 链接。新增“设置→网页读取→兼容代理的虚拟 IP（Fake-IP）”，默认关闭、按物理本地库自动保存。设置只在下次启动本地服务时生效；保存不会自动重启或重试，页面明确区分保存值与运行值。使用者应等任务完成后用 ⌘Q 退出重开，再重试失败文章。

```bash
PKA_APP_OUTPUT="$PWD/apps/macos/dist/知识收集箱-v16-代理网络设置.app" \
PKA_RUNTIME_PAYLOAD="$PWD/apps/macos/.build/runtime/rebuilt-20261017-v16-proxy-web-settings-verified" \
PKA_RUNTIME_MANIFEST_SHA256=25802401ef618c39adfffeb40da2c2d6d50e03a49a00d2adcc1608d1ba3d1777 \
  bash apps/macos/scripts/build.sh
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v16-代理网络设置.app
```

App 的显式环境将设置传入内置 API/Worker，终端的环境变量不能代替此入口。无数据库/备份迁移、新依赖或网关更改，无需重新安装 Codex 插件。37 项网页回归、Swift 子进程配置测试及截图微信链接的真实只读提取通过；构建与冻结结果见 [本轮验收](../../docs/verification/a1/proxy-web-settings-acceptance-20261017.md)。以下是历史构建记录。

## 2026-10-17 前次交付：Codex 链接兼容修复

当前完整客户端为 `dist/知识收集箱-v16-链接兼容修复.app`，插件版本 1.0.5。真实新 Codex 会话已输出合法原生链接，但界面未显示为可点击链接；不能用原生 URL 事件验收替代 Codex 端到端引用验收。本轮在 MCP 本机桥接中增加标准 HTTP `citation_url`，通过无资料内容的本机导航页打开原生客户端。浏览器如阻止自动打开，请点击页面按钮并确认。HTTP 引用使用当前本机端口，客户端重启后需要重新查询。

```bash
PKA_APP_OUTPUT="$PWD/apps/macos/dist/知识收集箱-v16-链接兼容修复.app" \
PKA_RUNTIME_PAYLOAD="$PWD/apps/macos/.build/runtime/rebuilt-20261017-v16-browser-article-links-verified" \
PKA_RUNTIME_MANIFEST_SHA256=f4979033b0c9a2e50f8b2436d2575c603ef9ff0d2e00049036b0563b4e5182ca \
  bash apps/macos/scripts/build.sh
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v16-链接兼容修复.app
```

先 ⌘Q 退出旧知识收集箱，再打开新版；在原 Codex 连接上“创建/更新插件”，通过 Codex 插件页正常更新到 1.0.5，并开启新会话。本轮不需要重启 AI 网关、重新分析文档或迁移数据库。1259 项免费回归、随包 API／Worker／六个 MCP 读取接口与 HTTP 导航测试通过；Codex 本体操作被电脑控制工具禁止，实际对话点击仍待用户验证。范围、限制与源码冻结记录见 [本轮验收](../../docs/verification/a1/browser-article-links-acceptance-20261017.md)。以下是历史构建，不代表当前交付。

## 2026-10-17 前次交付：文章链接

当前完整客户端为 `dist/知识收集箱-v16-文章链接.app`，内置 API／Worker／Skill 同一源码，插件版本 1.0.4。新增 `knowledge-inbox://library/<本地库身份>/source/<文章ID>`；链接只作阅读定位，既不是访问凭证，也不能更换 API 地址、执行命令或触发付费任务。Info.plist 注册 URL 协议，必须重新构建完整 App 并至少打开一次以供 macOS 注册；仅更新后台不能获得唤起能力。当前 schema16，无迁移或新依赖。

```bash
PKA_APP_OUTPUT="$PWD/apps/macos/dist/知识收集箱-v16-文章链接.app" \
PKA_RUNTIME_PAYLOAD="$PWD/apps/macos/.build/runtime/rebuilt-20261017-v16-article-links-verified" \
PKA_RUNTIME_MANIFEST_SHA256=26cda5a87ab7690994f601e765865425641049f4337113861461b6abc9c31ad0 \
  bash apps/macos/scripts/build.sh
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v16-文章链接.app
```

先 ⌘Q 退出旧 App，再打开新版。在“设置→Agent 接入”的原 Codex 连接上点击“创建/更新插件”，通过 Codex 插件页更新/重新安装后开启新会话。MCP/插件需加载新版，本轮未修改模型网关，不因文章链接要求重启网关。旧 Codex 回答不能追溯改写，更新后的新引用才有客户端链接。原生冷启动、热启动和关闭主窗口后 URL 事件已用隔离库验证；Codex 本体被电脑控制工具禁止访问，外部宿主真实点击未自动验收。[验收记录](../../docs/verification/a1/article-links-acceptance-20261017.md)。

## 2026-10-17 当前交付：可读引用

当前完整客户端为 `dist/知识收集箱-v16-可读引用.app`，包含本页下方的概览引用修复，并更新问答显示及 Codex 插件 1.0.3。schema16、原文、正式产物和备份格式不变。旧问答里的已绑定内部 UUID 在读取时显示为来源编号，不重写历史，不重发模型。构建与源码记录见 [本轮验收](../../docs/verification/a1/readable-citations-acceptance-20261017.md)。

```bash
PKA_APP_OUTPUT="$PWD/apps/macos/dist/知识收集箱-v16-可读引用.app" \
PKA_RUNTIME_PAYLOAD="$PWD/apps/macos/.build/runtime/rebuilt-20261017-v16-readable-citations-final" \
PKA_RUNTIME_MANIFEST_SHA256=cc5ffa07eb72e7dd53e5cc881ec1c5c99d1dcbeb59d658a3b148c56ea9127cab \
  bash apps/macos/scripts/build.sh
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v16-可读引用.app
```

退出旧 App 后打开新版，原网关终端 Ctrl+C，再执行 `bash services/ai_gateway/start.sh`。问答提示词升级为 `original-evidence-answer-v1.1`；共享摘要提示词不变。客户端历史刷新即可见新的引用展示，不必重新提问。Codex 需要在现有连接点击“创建/更新插件”，再通过 Codex 插件页更新/重新安装该插件并开启新会话；不直接编辑宿主缓存或 AGENTS.md。显式更新兼容客户端路径变化，原授权和其他设置保留，失败回退。下面的独立产物是历史构建记录，以本节为当前交付。

## 2026-10-17 前次交付：概览引用修复

当前完整 App 为 `dist/知识收集箱-v16-概览引用修复.app`。API／Worker 使用相同共享校验，修复合法四到十个引用被拒收的问题；摘要输入删除重复 UUID，预算仍受控。当前 schema16，无新迁移或依赖。完整验收和源码冻结记录见 [本轮记录](../../docs/verification/a1/navigation-citations-acceptance-20261017.md)。

```bash
PKA_APP_OUTPUT="$PWD/apps/macos/dist/知识收集箱-v16-概览引用修复.app" \
PKA_RUNTIME_PAYLOAD="$PWD/apps/macos/.build/runtime/rebuilt-20261017-v16-navigation-citations-verified" \
PKA_RUNTIME_MANIFEST_SHA256=33ab1aa5fd45f3173db0677466bef16e7f1e5a44d0d5816be152c901c8bcffc5 \
  bash apps/macos/scripts/build.sh
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v16-概览引用修复.app
```

先退出旧客户端，再打开上述新版；不能继续打开其他同名历史包。原网关终端 Ctrl+C 后执行 `bash services/ai_gateway/start.sh`，只读 `/health` 的 `summary_prompt_version` 应为 `domain-summary-navigation-v1.1`。API／Worker 随新 App 更新，无需另开开发服务。失败任务保留，新预览可复用有效单篇分析，再明确创建一次新概览请求；打开 App 和刷新页面不会自动重发。

当前交付以本节为准，后文日期小节和旧路径是历史构建记录，不要用旧运行包校验新功能，也不要对当前代码使用裸构建。

## 2026-10-16 摘要优先概览更新

知识域更新默认按文档概览一次汇总，超出预算才分批，并显示实际步骤进度。请求新增兼容的 v2 路径，API／Worker 与独立网关均须加载本轮源码；网关重启命令仍为 `bash services/ai_gateway/start.sh`。数据库、原文和单篇结果格式不变，本轮未新增迁移。当前工作区已有回收站 schema v16，请以 `migrations.CURRENT_VERSION` 为准，不能将后续历史 v15 小节当作当前版本。


当前完整产物为 `dist/知识收集箱-v16-摘要优先概览.app`，使用源码schema v16（既有回收站），运行包79份后台Python、19,928文件／链接。源码冻结哈希、1222项免费回归、随包端到端及签名验收见 [验收记录](../../docs/verification/a1/summary-navigation-acceptance-20261016.md)。重建命令：

```bash
PKA_APP_OUTPUT="$PWD/apps/macos/dist/知识收集箱-v16-摘要优先概览.app" \
PKA_RUNTIME_PAYLOAD="$PWD/apps/macos/.build/runtime/rebuilt-20261016-v16-summary-navigation-verified" \
PKA_RUNTIME_MANIFEST_SHA256=389497c31f228b85d38c6c1a22a88c351cb4bb9afcd4ce6c18b53f876468bd53 \
  bash apps/macos/scripts/build.sh
```

上述清单只适用于这次已验证运行包；源码再次变化须按下文受控组装流程生成新包和新摘要，不能继续沿用。

设计与边界见 [摘要优先概览设计](../../docs/design/知识域概览摘要优先优化-2026-10-16.md)。完整客户端需重新组装运行包，不能仅裸编译 Swift。真实模型质量验收仍需在切换版本后另行执行；普通回归不调用模型。


核验日期：2026-10-17。所有命令从仓库根目录执行。构建对象为本机 arm64；完整内置运行环境要求 macOS 14.5+。Swift 工具链来自 Xcode Command Line Tools，当前应用使用 ad-hoc 签名，尚非公证发行包。后文带日期的版本记录为历史说明，当前交付以开头的 2026-10-17 节为准。

## 1. 首先确定模式

| 模式 | 启动关系 | 数据 | 设置 |
| --- | --- | --- | --- |
| 完整本地客户端（日常使用） | App → `local_host.py` → 动态回环端口 API + 单 Worker | 用户本地 `knowledge.db` 及原件 | 完整八项设置 |
| 外置后台开发客户端 | App → 手动启动的 API，通常 `127.0.0.1:8000`；Worker 单独启动 | 默认 `services/api/pka-dev.db` 或显式开发配置 | 账户、网页、PDF 三项 |

AI 网关两种模式下均独立运行，不随 App 自动启动。配置见 [网关构建说明](../../services/ai_gateway/BUILD.md)。

当前 `scripts/build.sh` 默认完整候选构建必须提供运行包，并由发布器验收后替换正式包。显式外置开发输出仍不携带本地运行包，不能当成正式客户端。设置变少时先检查运行包和模式，不能删除数据库或强制取消设置显示条件。

## 2. 历史完整构建（v15 知识域全量分析）

2026-10-16 当前运行包位于 `apps/macos/.build/runtime/rebuilt-20261016-v15-domain-full-analysis`：schema v15 API/Worker、`bounded-tree-v2`、锁定依赖和本地 BGE/OCR 模型，共 **19,926 个文件/链接**，其中后台 Python 源文件 **77 个**。完整 App 独立输出到 `apps/macos/dist/知识收集箱-v15-知识域全量分析.app`。App 编译及 `codesign --verify --deep --strict` 通过，App 内运行包逐项清单、77 份后台源码一致性和 118 份源码哈希核验通过；模拟网关随包端到端也已通过。产物被 Git 忽略，其他设备须重新构建。摘要与源码快照见 [构建记录](../../docs/verification/a1/runtime-app-domain-full-20261016.json)，完整结论见 [验收记录](../../docs/verification/a1/domain-full-acceptance-20261016.md)。

```bash
PKA_APP_OUTPUT="$PWD/apps/macos/dist/知识收集箱-v15-知识域全量分析.app" \
PKA_RUNTIME_PAYLOAD="$PWD/apps/macos/.build/runtime/rebuilt-20261016-v15-domain-full-analysis" \
PKA_RUNTIME_MANIFEST_SHA256=679155bd94b0a840db8a72ba6829037d133c3d4e85c49f962a6523a5b4cb4e6c \
  bash apps/macos/scripts/build.sh
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v15-知识域全量分析.app
```

`PKA_APP_OUTPUT` 可选，须为绝对 `.app` 路径且不能是符号链接；默认仍为 `dist/知识收集箱.app`。选择独立输出可保留旧 App。正常日志须含 `PASS: ... runtime files and links verified`。修改运行包后须重新受控组装并记录摘要，不能继续沿用旧摘要。检查脚本和剖析工具须用 `python -I -B`，避免将测试生成的字节码写入受控运行包；若清单外仅有本次生成的 pyc，可移除这些明确的缓存再按原可信摘要重验，不能改写旧清单放行。

保存工作并退出旧 App，再打开新版。启动前备份本地库；v14 库由既有迁移流程升级到 v15，新增 `domain_full_analysis_jobs` 运行任务表和相关约束，不改变原文、正式摘要、主题与索引的格式。旧 v8/v13/v14 App 均不能打开 v15 库；回滚源码不会降级数据。全量任务不随备份搬运，恢复后不会继续发送付费请求；有效正式产物仍保留，本地缓存按原规则重建。迁移回归使用隔离合成库，本轮没有迁移用户实际数据。

全量分析入口、计划 API 和 Worker 必须来自同一快照。新增独立的一次授权总任务，按单篇→主题／域→索引的检查点推进；不修改既有自动更新策略，失败或未知时停止后续步骤。没有新增运行依赖，也没有改变独立 AI 网关的请求契约，本轮无需为全量调度重启网关。入口、预算、状态与限制见 [全量分析设计](../../docs/design/知识域全量分析-2026-10-16.md)。

免费回归：全量首次 1209 passed、1 skipped、1 既有 warning、1 failed；失败为新增清理测试重复使用幂等键的构造错误，修正后该文件 5 passed，合计 **1210 项通过、1 项跳过**。新增专项为 API 27、Worker 25、持久化 5，共 **57 项通过**。随包真实 HTTP／Worker 测试验证 1 篇复用、2 篇补分析→标签→主题／域概览→关键词索引、真实宿主重启保留记录，以及后续模拟 503 后查询／重启均不重播。本轮没有真实模型调用，历史真实质量结论不因全量调度扩大。

上一轮 v14 三页完整构建保留：`rebuilt-20261015-v14-domain-tabs`（SHA `ae025579bda135b0b8e28294ac44988a185a68277ed98bf9c04a718d17c97ca6`，19,923 文件/链接、74 份后台 Python）及 `dist/知识收集箱-v14-知识域三页.app`，记录见 [v14 构建记录](../../docs/verification/a1/runtime-app-domain-tabs-20261015.json)。该轮无 schema 迁移，包含三页只读查询、问答真实范围、20 秒缓存与后台刷新，免费回归合计 1153 通过、1 跳过。更早 `rebuilt-20261012-v14-reasoning`（SHA `9b61cf77d52cd73a92b4652bc3f6569b08b662db88a920a71cd2998896217616`）也是历史可信基线；不能用任一旧基线的后台验收当前 v15 功能。

历史 `rebuilt-20261009`（v8，SHA `901c4d0a803c1f84ec0d4edd6f578fcff35d4383fd50fdd478aefe06ded4278e`）、`rebuilt-20261010-v13`（SHA `a00b32a9ad67dfcaa1316591057c3aebd1f43f13fddce00c59937c3c26ce0a80`）、`rebuilt-20261010-v13-exact-identifiers`（SHA `1ef933e3ffb9c1d01d53746f24f39ba27e9dcfbffffd2d64b026f249d7681013`）和旧 App 均保留，不能用于当前 v15 后台验收。

## 3. 从固定输入重新构建运行包

输入规则与五个归档 URL、大小、摘要见 [runtime/README.md](runtime/README.md) 和 `runtime/artifacts.json`。不复制开发 `.venv` 作为内置解释器，不依赖系统 Python，不跳过摘要校验。

1. 从 `artifacts.json` 指定来源准备全部归档到独立输入目录，逐一验证大小和 SHA-256。
2. 使用固定 uv 0.7.6、独立 Python 3.10.17，按 `requirements-macos-arm64.lock` 暖缓存。锁文件包含运行依赖，开发 `requirements-ocr.txt` 不能替代此锁。
3. 调用构建器；输出目录必须不存在。下面 `/absolute/...` 是需要替换的路径。

```bash
python3 apps/macos/scripts/runtime/build_runtime.py \
  --inputs /absolute/runtime-inputs \
  --uv-cache /absolute/uv-cache \
  --uv /absolute/uv-0.7.6 \
  --output /absolute/new-runtime
```

构建器离线安装依赖，打包本地向量/OCR 模型、原生运行库及当前 API/Worker Python 源码，复制知识库 Skill 资源并生成清单。缓存不足应补齐固定制品，不更改版本范围来绕过失败。构建产物保存在持久构建目录；`/tmp` 会被清理，不能作为长期唯一副本。

清单摘要必须来自刚完成的受控构建或可信交付记录。先记录构建来源与清单摘要，再使用 `verify_runtime.py` 验证；不能为未知或损坏的运行包重写清单来使其通过。

## 4. 复用已验证依赖，更新后台源码

只改变 API/Worker Python 源码、且运行依赖和模型未变时，可复用已验证的依赖基线。2026-09-30 的归档为 `apps/macos/.build/runtime/pka-runtime-macos-arm64.tar.gz`，可信记录见 `docs/verification/l0/runtime-build-artifact.json`。这份归档含旧后台源码，不能直接拿来交付当前业务。

以下流程先验证原归档及完整解包内容，再替换明确的后台源码目录及 Skill 资源，最后为新产物生成清单。使用新的输出目录，不能在正在运行的 App 包中操作。任何步骤失败就停止。

```bash
python3 -I -B - <<'PY'
import json, shutil, sys
from pathlib import Path

root = Path.cwd()
sys.path.insert(0, str(root / 'apps/macos/scripts/runtime'))
from build_runtime import extract, tree_manifest
from verify_runtime import digest, verify

record = json.loads((root / 'docs/verification/l0/runtime-build-artifact.json').read_text())
archive = root / 'apps/macos/.build/runtime' / record['artifact']
output = root / 'apps/macos/.build/runtime/current-source-runtime'
if output.exists():
    raise RuntimeError('输出目录已存在，请选择新的输出名称')
if archive.stat().st_size != record['bytes'] or digest(archive) != record['sha256']:
    raise RuntimeError('依赖基线归档校验失败')
extract(archive, 'runtime', output)
print('基线校验:', verify(output, record['manifest_sha256']))
shutil.rmtree(output / 'backend')
for source, target in [(root / 'services/api/app', output / 'backend/app'),
                       (root / 'services/worker', output / 'backend/services/worker')]:
    for path in source.rglob('*.py'):
        if '__pycache__' in path.parts:
            continue
        dest = target / path.relative_to(source)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
shutil.copyfile(root / 'skills/personal-knowledge/SKILL.md', output / 'backend/app/agent_skill.md')
shutil.copyfile(root / 'skills/personal-knowledge/agents/openai.yaml', output / 'backend/app/agent_skill.yaml')
manifest = output / 'manifest.json'
data = json.loads(manifest.read_text())
data['files'] = [row for row in tree_manifest(output) if row['path'] != 'manifest.json']
data['base_archive_sha256'] = record['sha256']
data['scope'] = 'Verified L0 dependencies with current API/Worker source snapshot'
manifest.write_text(json.dumps(data, indent=2) + '\n')
expected = digest(manifest)
print('新产物校验:', verify(output, expected))
print('PKA_RUNTIME_PAYLOAD:', output)
print('PKA_RUNTIME_MANIFEST_SHA256:', expected)
PY
```

将输出路径及摘要传入第 2 节构建命令。依赖、模型或原生运行库有变化时执行第 3 节全量构建；此源码更新流程不能证明旧依赖满足新需求。

## 5. 交付验收

```bash
test -x apps/macos/dist/知识收集箱-v15-知识域全量分析.app/Contents/Resources/runtime/python/bin/python3
test -f apps/macos/dist/知识收集箱-v15-知识域全量分析.app/Contents/Resources/runtime/backend/app/local_host.py
codesign --verify --deep --strict apps/macos/dist/知识收集箱-v15-知识域全量分析.app
```

使用构建时记录的清单摘要对 App 内的 `Contents/Resources/runtime` 运行 `scripts/runtime/verify_runtime.py`。同时比较打包后台源码与当前源码，不能只检查 Python 存在。

用独立测试数据验证启动、退出和重启持久化：

```bash
xcrun swiftc -D CAPTURE_QUEUE_TESTS -parse-as-library -O \
  -module-cache-path apps/macos/.build/module-cache \
  apps/macos/Sources/*.swift apps/macos/scripts/LocalDomainServiceTest.swift \
  -o apps/macos/.build/local-service-build-test
apps/macos/.build/local-service-build-test \
  "$PWD/apps/macos/dist/知识收集箱-v15-知识域全量分析.app/Contents/Resources/runtime"
```

此测试使用独立临时知识库和测试钥匙串，需要本机端口与钥匙串访问权限，不连接真实模型。GUI 验收可运行 `scripts/build_ui_test.sh <运行包绝对路径>`，打开生成的独立测试 App，确认八项设置显示，再退出测试 App。

完整运行包还应通过隔离模型网关 + 真实 MCP 协议验收，以及旧库升级验收：

```bash
runtime="$PWD/apps/macos/dist/知识收集箱-v15-知识域全量分析.app/Contents/Resources/runtime"
"$runtime/python/bin/python3" -I -B apps/macos/scripts/local_domain_runtime_test.py "$runtime"
"$runtime/python/bin/python3" -I -B apps/macos/scripts/local_domain_upgrade_test.py \
  "$PWD/apps/macos/.build/runtime/rebuilt-20261009" "$runtime"
```

以上脚本使用临时合成库；第一个仅连接本机模拟网关，第二个不配置 AI。需要本机端口权限。GUI 使用独立 Bundle 与测试钥匙串，不能打开生产 App 来替代隔离测试。工程链路和本地 BGE 加载/推理已通过，真实 DeepSeek 质量与规模效果未验收。

用户数据不在 `.app` 内。默认路径为 `~/Library/Application Support/local.pka.knowledge-inbox`，日志为 `~/Library/Logs/local.pka.knowledge-inbox/local-service.log`；切换知识库后的具体路径以 `LocalLibrary` 选择为准。不得删库来处理模式切换，也不得把 schema 降级当作回滚源码的自动步骤。

## 6. 明确选择外置后台开发模式

只有需要连接外置 API 时，才执行裸 `bash apps/macos/scripts/build.sh`。随后按 [API](../../services/api/BUILD.md) 和 [Worker](../../services/worker/README.md) 说明启动服务。这会替换同一路径的完整 App；切回本地模式必须重新携带运行包构建。开发模式三项设置属于当前代码行为，不算完整客户端验收通过。

## 7. 知识库概览客户端（源码v13）

源码已增加左栏“知识库概览”：域/主题短概览、分页资料、加工节点下钻、预览确认、累计授权及暂停/自动更新控制。需打包当前API/Worker，旧v8运行包没有这些后台接口；客户端提示升级时应重组运行包，不能只重新编译Swift或裸build。

不替换现有App的源码验证：

```bash
xcrun swiftc -typecheck -D CAPTURE_QUEUE_TESTS -target arm64-apple-macosx14.0 \
  -module-cache-path apps/macos/.build/module-cache apps/macos/Sources/*.swift
xcrun swiftc -module-cache-path apps/macos/.build/module-cache \
  apps/macos/Sources/API.swift apps/macos/Sources/LocalDomainOverview.swift \
  apps/macos/scripts/LocalDomainOverviewTest.swift -o /tmp/pka-domain-overview-test
/tmp/pka-domain-overview-test
```

`LocalDomainOverviewUITest.swift`为模拟原生界面验收，必须编译所有Sources（`-D CAPTURE_QUEUE_TESTS`）并放入Bundle ID为`local.pka.domain-overview-ui-test`的独立临时App。它使用模拟数据、临时home，不能替代完整运行包、真实模型或MCP验收，不提交付费任务。

## 8. 人工主题管理客户端（2026-10-10）

最新源码在概览中增加“管理主题”：新建、更名、两级父主题及逐条资料归属。沿用 v13 主题 API，不增加迁移、运行依赖或网关字段。完整产物独立输出为 `apps/macos/dist/知识收集箱-v13-主题管理.app`，使用第2节相同运行包和清单摘要，只将 `PKA_APP_OUTPUT` 改为此绝对路径。可信构建快照见 `docs/verification/a1/runtime-app-topics-build-20261010.json`。此前两个完整 App 均保留，切换前退出旧客户端；已有 v13 库无需新迁移。

控制器自测：

```bash
xcrun swiftc -module-cache-path apps/macos/.build/module-cache \
  apps/macos/Sources/API.swift apps/macos/Sources/LocalDomainOverview.swift \
  apps/macos/Sources/LocalDomainTopics.swift apps/macos/scripts/LocalDomainTopicsTest.swift \
  -o /tmp/pka-domain-topics-test
/tmp/pka-domain-topics-test
```

第5节 `LocalDomainServiceTest.swift` 已覆盖真实主题创建、人工固定名称、两级结构、循环父级拒绝、资料加入/排除、重启持久化与零付费任务。主题管理原生界面独立测试通过。创建响应未知时刷新不能解除限制，需明确核对后继续；成员读取未完成或失败时不能修改归属。

## 9. v14 查询与关键词缓存

v14 将词索引改为整数文档缓存映射，保留既有中文单字/双字、ASCII词和前缀。接口仍返回真实 source UUID，不返回内部缓存ID。API与Worker须一起更新，不能只升级一端。模型网关协议没有变化，不要求因缓存升级而重启网关。

域/主题查询使用请求内、同SQLite锁定事务的输入快照与批量依赖读取；ORM/SQL变更或事务变化立即停止使用节点结果，不跨请求缓存。最新节点在SQL中先选定再执行依赖权限过滤，无法访问的新版本不会使旧概览复活。真实效果与资源报告位于 `docs/verification/a1/`，工程对照与真实模型质量分开记录。真实DeepSeek质量仍按用户选择暂不执行。

### v14 最终验证结果

完整API回归1085通过、1跳过。1000篇/135万字符、真实本地BGE、人工合成摘要/主题的免费工程验收全部门槛通过：主题P95约1.55秒、新建SQLite约102.20MiB。旧库迁移保留空闲页，不自动VACUUM，不能照搬新库体积到已有库。真实DeepSeek及真实业务质量未测。完整结论见 `docs/verification/a1/v14-acceptance-summary-20261011.md`。

## 2026-10-12：概览压缩与重复候选加工优化

完整产物为 `dist/知识收集箱-v14-概览优化.app`，此前v14及v8/v13 App保留。仍为schema14、bounded-tree-v2，无新数据库迁移、依赖或模型；可信基线先按原摘要验证，再复制到新目录，仅替换API/Worker源码并生成新清单。

新全域计划省略与人工主题成员完全相同、且未经人工改名/层级/成员调整的自动候选分支。候选主题及成员保留，显式对该主题分析仍可使用；部分重叠分支、人工调整和未归类资料均保留。旧任务按冻结计划继续，新预览需要重新确认，既有预算不会重置。

网关提示词独立升级为 `domain-node-evidence-v2.1`，按本次原文节选长度建议压缩并减少重复；不修改旧结果的硬校验，因此历史产物仍可读取。不随App启动，需重启原网关进程加载。没有新增收费请求，提示词改进的真实质量尚未复测。

## 2026-10-12：长引用接入修复版本

最新App为`dist/知识收集箱-v14-长证据修复.app`；运行包`apps/macos/.build/runtime/rebuilt-20261012-v14-long-evidence`，清单SHA `5847b31ef8b6533973cb53c5229e497e689625802283753ae9bf1fb3b5ad3027`。依赖/模型/schema14不变，只将domain_summary_inputs中有效长引用分成可回溯的3000字符窗口。可信构建见`docs/verification/a1/runtime-app-long-evidence-20261012.json`。

构建使用第2节命令，替换App路径、运行包和上述摘要，保留旧版本。真实客户端已确认修复后4/4预览；随后第一节点校验失败，已按授权停止，不能称整个真实知识域质量已通过。当前版本已经用于本轮实际操作，不因本次长证据修复需要重启网关。


### 2026-10-12 长文引用与最终概览修复

最新App为`dist/知识收集箱-v14-完整概览修复.app`，当前可信运行包/摘要见第2节。新请求的原文引用连续细分为最多1000字符，位置与原始证据绑定；中间节点仍保留6000字符引用上限，最终根可保留两个子节点最多12000字符证据并集，概览正文仍限500字符。schema14不变，无新增依赖和资料迁移。冻结旧请求、旧产物及备份不改写；切包前退出旧App，进行中的任务可能因新请求重建不一致安全停止，不自动重试。

独立网关提示词为domain-node-evidence-v2.2，已重启确认；引用粒度和根预算修复位于API/Worker运行包，不能仅重启独立网关代替切换新版App。用户终端推荐执行`bash services/ai_gateway/start.sh`。其他Agent不能重新采用不带运行包的默认构建进行此次功能验收。


### 最终验收版本的合成边界（2026-10-12）

第2节已更新为`知识收集箱-v14-概览验收.app`和对应可信摘要。前一版本仅放宽最终根，真实复测中间合成仍超过6000字符，因此不作为最终交付。当前版本叶批次仍6000字符/8来源/20证据；中间合成最多9000/10/40，恰为契约输入18000/20/80的一半；最终根最多18000/20/80。两子节点合并保持有界，不改变正文500字符限制。55项定向回归和独立运行包模拟API/Worker/MCP流程通过。

本次增量组装复用已严格校验且Swift源码哈希未变的上一版原生二进制，复制后仅替换运行包中的domain_builds.py与清单，并重新签名、验证全量19920文件及当前后端源码一致性。其他Agent从源码重建仍使用第2节完整构建命令；不能直接修改已签名App作为常规发布流程。


### 案例归属修复版（2026-10-12）

第2节当前产物更新为`知识收集箱-v14-归属修复.app`。网关须加载domain-node-evidence-v2.3以独立核对原文中的实体、数值、时间、原因与条件；重复子摘要不能代替事实验证。API的case-attribution-v1加工版本参与新复用键，明确更新后不复用旧代节点，原产物与备份保留，不自动发起模型请求。旧App可运行既有产物，但不能用于修复验收。82项免费专项及打包模拟主流程通过；真实质量结论见最新验收记录。


### 发送前预算约束（最终运行包，2026-10-12）

第2节最新App为`知识收集箱-v14-知识域修复.app`。有界输入助手domain_node_inputs保证叶批次与中间合成的原文输入本身不超过9000字符/10来源/40证据；终端根不超过18000/20/80。长资料的所有有效引用窗口分批进入叶节点，不截断原文；分片节点和叶批次键绑定具体证据ID，防止同一长资料的多批键冲突。中间合成按来源轮转及原文顺序取整段节选，删除引用后不继续把完整子摘要当作事实背景；快照记录输入选择覆盖统计。依赖快照保留全部子节点的资料依赖，未引用资料变更仍使旧结果失效。

新加工版本为bounded-attribution-v2，旧缓存不复用；网关仍v2.3，无需再次重启。原文、单篇产物和历史节点保留，schema14与备份格式不变，没有新增外部依赖。加工步数可能增加，必须重新预览实际预算，不沿用旧预留。运行包新增一个助手文件，共19921文件/链接；可信清单以第2节为准。

独立网关最新提示词为v2.4：保留v2.3的案例归属核对，写作目标留40%余量，硬阅读预算不变。此改动不需要再次打包App，必须重启独立网关加载。最终客户端源码全量1113项回归通过。


### 真实案例归属的思考模式验收（2026-10-12）

第2节最新App更新为`知识收集箱-v14-知识域质量修复.app`。网关分层请求v2.5启用thinking=enabled、reasoning_effort=high、max_tokens=16384，以容纳核对及最终JSON；正文500/800硬上限和300/480写作目标保持。reasoning_content不返回、不存入产物或缓存；usage保持Provider返回的用量。单篇v3的disabled/2048设置不变。

API加工版本bounded-attribution-v3-reasoning禁止新计划复用旧非思考节点；历史产物仍保留，不自动重发模型。相关113项回归及41项契约/思考文本隐私检查通过，独立运行包模拟API/Worker/MCP重新通过。真实单节点诊断已纠正此前案例指标错配；全域9步复验正在进行，不提前记为业务质量通过。独立网关必须重启加载v2.5，App打包不会替它更新进程。

独立网关现为v2.6：保留思考与阅读余量，额外将Provider输入按来源、原始块和原文位置连续排序。候选选择仍有界，证据集合、内容、ID、偏移和外部契约不变。95项网关回归及真实根节点诊断通过；网关需重启加载，App运行包无需因此再改。

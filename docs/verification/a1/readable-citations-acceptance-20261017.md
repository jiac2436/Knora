# 问答与插件可读引用验收 — 2026-10-17

## 改动与原因

原问答提示词明确要求在事实句后插入完整证据 UUID，Skill 与 MCP 指令也要求在答案中引用证据 ID。因此内部追溯标识被当作用户可读出处展示，并非数据库损坏。

- 问答提示词升级为 `original-evidence-answer-v1.1`：正文写正常答案，原文 ID 只放在独立 `citations` 数组。原有引用完整性、未知引用拒收、幂等与不自动重播规则保留。
- `rag.turn_view` 按返回来源的实际顺序，将已绑定的证据、资料及版本 UUID 显示为 `[1]` 等来源编号；兼容大小写、重复引用和无方括号的内部 ID。普通正文中其他 UUID、日期、数组不变。只改变读取视图，存储的原始回答、引用和快照不重写，也不重新调用模型。
- macOS 来源按钮增加对应编号，保留资料标题、位置、过期状态和原文跳转。
- Skill 与 MCP 初始化指令改为展示资料标题和可读位置；内部 ID 只用于工具调用，不展示完整或缩写 UUID。Skill 继续只由用户主动调用，未增加自动使用场景或修改 AGENTS.md。
- 生成的 Codex 插件升级为 **1.0.3**。原插件身份、授权范围、独立凭证、其他配置和宿主缓存保留；需要通过 Codex 正常更新流程加载新 Skill。没有直接修改宿主缓存或已发生的 Codex 对话消息。

为使本次插件更新能从新版 App 执行，兼容显式“创建/更新插件”时的客户端运行包路径变化。只允许显式更新切换路径，仍校验旧受管文件、连接及同一知识库端点，并在新服务检查通过后发布；失败时回退原插件和连接。状态或连接检查不会悄悄切换服务，也不允许借此更换知识库端点。

无新依赖、数据库迁移或存储/备份格式变化，schema 仍为 **16**。内部证据 ID 保留是原文读取、版本与权限核验所必需，正常答案不直接展示它们。

## 免费验证

- 最终整套 Python 回归 **1,242 项通过、1 项跳过、1 项既有依赖弃用警告**，无排除用例；日志 `/tmp/pka-readable-citations-final-regression-20261017.log`。
- 问答 API／网关、插件及真实 MCP stdio 协议 **79 项通过**：正文来源编号、历史不改写、原文失效标记、非引用 UUID/日期/数组保留、引用校验不放宽、初始化引用规则、1.0.2→1.0.3 升级、缓存不改写、新 App 路径兼容及更新失败回退。
- Swift 全 Sources 编译通过；Skill frontmatter 与 agent YAML 解析通过，主动调用策略 `allow_implicit_invocation: false` 保留。
- 实际 Mac `LocalService`→包内 API／Worker→回环模拟问答网关：模拟模型返回完整 UUID 标记，客户端收到 `[1]` 并仍能读取对应原文；同 key 不重发、无资料不调用、资料变更标过期、重启保留历史且不重播。测试只使用临时 home、随机 `synthetic.pka.rag-e2e` 钥匙串服务和合成资料，日志 `/tmp/pka-readable-citations-rag-runtime-20261017.log`。
- 完整 App 与运行包从可信父包受控组装。包内 19,928 个文件／链接、79 份后台 Python 和 Skill 资源与源码核验，冻结 125 份源码哈希；`codesign --verify --deep --strict` 通过。

没有查询真实知识库内容、读取真实凭证、调用真实模型或改动实际已安装的 Codex 插件及其缓存。宿主中的新版本效果需用户完成更新后，在新会话确认；已有 Codex 消息不会被改写。历史问答可直接刷新，不需要重新提问。

## 构建与操作

- App：`apps/macos/dist/知识收集箱-v16-可读引用.app`
- 运行包：`apps/macos/.build/runtime/rebuilt-20261017-v16-readable-citations-final`
- 清单 SHA-256：`cc5ffa07eb72e7dd53e5cc881ec1c5c99d1dcbeb59d658a3b148c56ea9127cab`
- App 二进制 SHA-256：`79573a606a467ef755d1cca923914e58f7e944c1f919ea3ddac7a598a1bd5c5c`
- 机器记录：[runtime-readable-citations-20261017.json](runtime-readable-citations-20261017.json)。

1. ⌘Q 退出旧客户端，打开新版；刷新问答历史即可看到可读来源。
2. 原网关终端 Ctrl+C，再执行 `bash services/ai_gateway/start.sh`，加载新问答提示词。
3. 在“设置→Agent 接入”的原 Codex 连接点击“创建/更新插件”，到 Codex 插件页更新/重新安装该插件，再开启新会话。无需重复创建授权连接，不编辑 AGENTS.md 或 Codex 缓存。

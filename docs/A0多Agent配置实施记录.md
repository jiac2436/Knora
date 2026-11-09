# A0：多 Agent 配置

日期：2026-10-03。本批补齐 Claude Code、Workbuddy 的本地 stdio 配置，复用现有独立连接授权。

## 交付和使用

设置 → Agent 接入增加配置格式选择：Codex、Claude Code、Workbuddy。切换格式时仅替换默认连接名称，用户自定义名称保留。每个有效连接可复制当前所选格式；建议每个 Agent 创建独立连接，便于分别撤销。凭证仍存于原有按知识库/连接隔离的钥匙串服务。

Codex 沿用 TOML。Claude Code 使用含 type=stdio 的 mcpServers JSON，将服务合并到 ~/.claude.json 的 mcpServers 下并保留其他内容，重启后以 /mcp 检查；依据 [Claude Code 官方 MCP 文档](https://code.claude.com/docs/en/mcp)。Workbuddy 使用含 command/args/env 的 mcpServers JSON，进入 MCP 配置入口合并到用户级 ~/.workbuddy/mcp.json；依据 [Workbuddy 官方 MCP 指南](https://staging.codebuddy.cn/docs/workbuddy/From-Beginner-to-Expert-Guide/Function-Description/MCP-Guide)。该指南的网页抓取超时，配置契约由官方页面的搜索索引正文核对；不据此宣称实际客户端验收通过。

两种 JSON 都使用随包 Python -I -B local_mcp.py 与 PKA_API_FILE/PKA_AGENT_TOKEN，延续重启端点发现、领域权限、过期及撤销规则。复制内容包含独立令牌，只用于本机私有配置，不可分享或提交仓库；不要以整段 JSON 覆盖已有文件。本批不自动写其他应用配置，不改全局 Skill 安装位置。

JSONSerialization 保证中文、引号、反斜线、控制字符及带空格路径正确还原。Claude 会展开 ${VAR}，因此生成 Claude 配置时若路径或令牌含 ${ 字面序列，会拒绝并解释如何调整路径，避免生成指向错误位置的配置。

## 验证范围

Swift 全量编译及配置测试覆盖三种格式、JSON 特殊字符还原、独立凭证作用域、无效标识/空令牌，以及 Claude 路径展开保护。

隔离客户端端到端使用合成知识库、独立测试钥匙串和生成的实际 JSON，通过官方 MCP SDK 对两种配置读取五个工具，验证授权领域搜索、原文证据读取、未授权原文拒绝。服务重启后复用同一 JSON 再次通过；撤销后工具仍能发现，但读取拒绝。没有真实模型调用。

scripts/verify_claude_mcp_config.py 以本机 Claude Code 2.1.90 运行 --bare mcp list，仅配置加载和 MCP 健康检查。独立 CLAUDE_CONFIG_DIR、临时可信项目及先行空服务基线隔离用户配置；移除继承的模型凭证，45 秒超时并清理进程组，不打印配置或原始日志。实际生成的 Claude JSON 连接成功。隔离方式依据本机 CLI 配置解析及 [官方环境变量文档](https://code.claude.com/docs/en/env-vars)。不调用 Claude 模型，因此不把健康检查当作回答质量验收。

Workbuddy 未安装，已完成配置解析与实际 stdio 协议测试，真实 Workbuddy 客户端加载/信任/模型回答仍待验收。已有 Codex 合成资料回答验证保持独立记录。

### 设置页布局修复

用户反馈右侧按钮被裁切且授权领域下方有大块空白。原因是嵌入设置卡片的 Agent 表单仍保留独立窗口的 720×600 最小尺寸，并嵌套两处滚动区域。移除表单最小尺寸和重复内边距，授权领域/连接列表按内容高度显示，使用设置页已有滚动；连接名称/状态与操作按钮分行，减少横向空间竞争。

Swift 全量构建及开发签名检查通过。通过 Computer Use 在隔离测试客户端的 840×600 设置页验证 Codex、Workbuddy 选择、完整右侧按钮、长说明换行及统一垂直滚动。该次界面检查使用空授权领域/空连接列表，连接业务与权限沿用此前端到端验收；未声称此次重新验收真实模型回答。无 API、凭证或持久化格式变更。

## 兼容和后续

没有后端、API、数据库 schema 或知识持久化格式变更，仍为 schema 8；不需要迁移。随包后端和模型与上一批一致，19,895 个文件/链接通过清单验证；开发签名检查通过。稳定发布签名及公证仍待完成。

下一步是冻结约 20 篇许可明确的质量评测资料，建立总结事实/引用、标签归一、检索命中的人工审查基线；真实调用次数和范围在运行前确认。Workbuddy 实际客户端验收需可用客户端，不自动下载或安装。

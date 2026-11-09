# A0 Agent 与知识库 Skill 接入

日期：2026-10-03。本批交付只读 MCP、知识库 Skill 和接入验收；客户端 RAG 问答另行实施。

## 工具与权限

已有 search_knowledge、get_source、get_evidence，新增 get_summary(source_id)、get_tags(source_id)。HTTP 诊断入口为 POST /v1/mcp/tools/同名工具，与官方 SDK stdio 使用相同读取服务。五个 MCP 工具声明只读，允许审计记录，不修改知识或调用分析网关。

总结只返回当前正文及上下文有效的 single/synthesis 产物，不返回局部 segment、过期结果、分析快照或候选目录。全部总结引用经过实际原文校验，切片 ID 映射成真实 evidence_id 并保留 text_start/text_end。available=false 时 Agent 应继续读原文。返回 coverage 和 source_coverage，区分分析范围与原始资料覆盖。标签只返回该资料的人工关联及当前正文版本已确认 AI 关联，保留 origins，不返回全库目录。

两条新增读取路径复用 owner、授权领域、app_only、删除过滤，删除中也不可访问；连接过期、撤销或本地 owner 不匹配由既有鉴权拒绝。成功及不存在均记审计。无数据库 schema 或知识格式变更，兼容现有 schema 7；新增响应不改变已有搜索和证据字段。

## 本地 API 转接

提供 PKA_API_URL 模式，将 stdio 请求转发到运行中的客户端 API，以 PKA_AGENT_TOKEN 的独立连接令牌认证。不创建数据库、运行迁移或初始化索引。只允许带端口的 127.0.0.1 / ::1 HTTP 地址，禁用代理和重定向，错误不回传服务响应或凭证。客户端退出后请求返回服务不可达，切换库、撤销、过期后须重新配置有效连接。

打包入口 backend/app/local_mcp.py 支持隔离 Python -I 执行，强制 API 转接模式；现有 python -m app.mcp_server 在无 PKA_API_URL 时仍保留开发用直接数据库模式，不作为客户端推荐接入方式。

Agent 只读权限不能限制用户本机进程对其自身文件的系统权限，且外部 Agent 可能将读取的资料发给其模型服务。本次接入不等于云端常驻服务或跨端同步。

## Skill 与 Codex 配置

仓库 skills/personal-knowledge/SKILL.md 可作为可分发 Skill 源文件；本批不改写用户全局配置。Skill 说明检索时机、标签筛选、总结辅助定位、原文引用、无证据处理和资料中指令的边界。

Codex stdio 配置依据 [官方 MCP 文档](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)。以下是模板，须将应用路径替换为实际安装位置，并在启动 Agent 的环境中提供 API 地址和 Agent 连接令牌，令牌不能写进可分享的配置文件：

```toml
[mcp_servers.personal_knowledge]
command = "/实际路径/知识收集箱.app/Contents/Resources/runtime/python/bin/python3"
args = ["-I", "-B", "/实际路径/知识收集箱.app/Contents/Resources/runtime/backend/app/local_mcp.py"]
env_vars = ["PKA_API_URL", "PKA_AGENT_TOKEN"]
startup_timeout_sec = 30
required = true
```

PKA_API_URL 为客户端当前本地服务地址；PKA_AGENT_TOKEN 必须来自 POST /v1/agent-connections，按所需领域创建独立连接，不能使用客户端 owner 凭证或 AI 网关凭证代替。客户端当前未提供一键生成配置/连接管理界面，以上为开发阶段接入；进程环境的传递需由启动方式保证，不假设桌面应用自动继承终端变量。其他 Agent 模板及真实验收后续补充。

## 验证与未完成项

新增 25 项 Agent 读取与 29 项转接专项测试；完整 Python 回归 550 项通过。官方 MCP SDK 子进程验证搜索→总结→原文证据及标签读取，Skill 结构验证通过。原有 get_source/get_evidence 的删除中状态过滤也已统一并回归。使用合成资料，无真实 DeepSeek 调用；这些验证不代表真实总结质量验收。

scripts/verify_codex_knowledge.py 提供显式 --run-codex 验收，可带 --runtime 指向随包运行环境。临时创建合成数据库、启动 API，以现有 Codex 登录和模型额度检查搜索/总结/标签/证据及最终引用；不持久修改 MCP 配置，退出清理临时目录和 API。

真实 Codex CLI 验收已通过：经 stdio→本地 API 调用 search_knowledge/get_summary/get_tags/get_evidence，审计确认四工具成功，最终回答包含仅在合成资料中存在的随机验收编号及真实 evidence_id。Skill 内容作为该隔离验收任务的说明输入，未安装到用户全局技能目录；不代表各客户端自动发现 Skill 已验收。首次启动参数错误已修正为 stdin 传入说明，一次运行未达到完整工具门槛，未将其计为通过；后续完整验收通过。这是工程连通与引用验证，不是批量真实知识质量评测。

剩余：真实资料质量基线、客户端连接管理/配置体验、其他 Agent 接入模板，以及客户端 RAG 问答及本地会话持久化。

后续进展：客户端 RAG 与本地会话主流程已实现，见 [RAG 记录](./A0本地RAG问答实施记录.md)。设置内连接管理、私有 Codex 配置复制及重启端点发现也已实现，见 [连接管理记录](./A0Agent连接管理实施记录.md)；以上手工环境变量模板保留作为早期开发记录，当前推荐使用客户端生成的配置。Claude Code、Workbuddy 配置已补齐，验收边界见 [多客户端配置记录](./A0多Agent配置实施记录.md)；真实资料质量基线及 Workbuddy 实际客户端验收仍待完成。

## 随包交付

最终随包 Python 的 `-I -B backend/app/local_mcp.py` 入口也通过真实 Codex 同一验收门槛：四工具审计成功，回答包含合成资料随机编号及真实证据 ID。客户端已重新构建并通过 `codesign --verify --deep --strict`。复用核验后的依赖和模型，更新后端；19,891 个运行文件/链接通过清单核验，清单 SHA-256：`e397ae376d0c2131e3557d3929703e128041c6df7a04c6a0cd81b6b588bc9055`。本地开发签名不代表发布签名或公证完成。

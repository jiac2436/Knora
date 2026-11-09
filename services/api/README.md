# Knora API 与共享内核

本目录提供资料、知识库、标签、搜索、回收站、备份、分析任务和 Agent 授权等共享业务能力。macOS App 内置本机 API 与 SQLite，由本地宿主管理生命周期和访问凭证，普通使用者不用单独启动服务。

## 开发入口

- 环境、依赖与隔离回归：[开发与验证](../../docs/开发与验证.md)。
- API/Worker 的外置调试命令：[服务启动指南](../../docs/本地服务启动指南.md)。
- 生成完整客户端：[macOS BUILD](../../apps/macos/BUILD.md)。

裸开发 API 默认使用 `services/api/pka-dev.db`；配置在 [`app/config.py`](app/config.py) 中通过环境变量读取。`GET /health` 提供服务状态；开发服务的 `/docs` 提供当前 HTTP 接口契约。桌面服务使用自己的目录、凭证和动态回环端口，不应与开发数据库混用。

## 模块边界

- `app/`：HTTP/MCP 接口、业务规则、持久化、权限、索引与运行包/扩展管理。
- `../worker/`：采集、解析、图片、分析与索引任务的执行。
- `../ai_gateway/`：内置 Worker 复用的模型执行器，以及可选外部 HTTP 网关入口。
- `app/platform_support/`：macOS 目录、私密文件、受管进程与原生辅助程序；其他桌面平台未完成适配。
- `tests/`：使用合成资料的自动化回归，不包含用户知识库，不打入基础运行包。

当前数据库为 schema16。升级和备份恢复通过本地宿主执行，不能用旧版客户端重新打开升级后的数据库。修改 API、持久化、任务或模型协议时，同时更新消费者和相应测试；打包须使用同一源码快照的 API、Worker 与执行器。

Owner 与 Agent 凭证互相独立。Agent 仅可读取授权知识库中允许共享的资料；现有配置示例见 [`agent/codex-config.toml.example`](../../agent/codex-config.toml.example)。模型调用的范围与确认由业务接口控制，不以健康检查替代实际权限或模型质量验证。

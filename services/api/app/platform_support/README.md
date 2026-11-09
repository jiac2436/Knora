# 平台能力边界（M3／M4）

业务继续保留在 `app` 和 `services.worker`。本目录只实现系统能力，不保存知识规则、提示词或授权规则。

| 能力 | 当前实现 | 兼容约定 |
| --- | --- | --- |
| 目录与运行包 | `macos.py`、`DirectoryLayout`、`RuntimeLayout` | 保留现有 Library、独立库及随包解释器路径 |
| 私密文件与锁 | `PrivateFiles`、`posix_files.py` | 当前用户、普通文件、防链接、有界读取、0600/0700、非阻塞共享/独占锁 |
| 原子发布 | 同上 | 同目录原子写、独占文件/目录发布；目标存在时拒绝，不能覆盖 |
| 受管进程 | `OwnedProcesses`、`macos.py` | Worker 就绪管道、父进程 EOF、信号停止及超时回收 |
| MCP 启动 | `app.agent_launcher` 调用本目录 | 校验绑定的运行包命令，从私密文件读 Agent 凭证，清理无关环境后 exec |
| CLI与图片OCR | `ExternalCapabilities`、`external_macos.py` | 内核经一次性原生辅助程序调用受限CLI与Apple Vision，无监听端口 |

原生客户端对应 `PlatformContracts.swift`、`CredentialStore.swift`、`DesktopIntegration.swift` 和 `LibraryStoring`；具体系统实现用 `Mac*.swift` 标识。`LocalService`、`LocalLibraryStore`、`LocalBackupTool` 保留兼容名称，分别指向 macOS 宿主、目录及备份工具适配器。窗口与 AppKit 阅读渲染仍属于 macOS 客户端，不把它们当作共享内核。

飞书 `RestrictedCommandRunning` 返回有界 stdout/stderr 与退出码；超时、取消、大小越界明确失败，执行时不继承模型密钥等无关环境。CLI 授权和文档错误由上层映射。`ImageTextRecognizing` 由 Apple Vision 实现，有效图片在OCR失败时仍可保存。M4已将正式本地采集编排、正文/XML版本对齐、图片清单、检查点及归档迁到Python共享内核；原生客户端保留CLI授权和连接交互。

辅助程序源码位于 `apps/macos/scripts/runtime/MacPlatformHelper.swift`，复用 `MacRestrictedCommands`／`MacImageTextRecognizer`，构建到运行包的 `platform/macos/KnowledgePlatform`。Python每次请求启动一个辅助程序，标准输入首行发送有界请求并保持私有管道；输出为有界类型化结果，标准错误不进日志或任务。父进程退出触发EOF并回收辅助程序和CLI，超时仅处理本次拥有的进程组。辅助程序不持有知识库或模型凭证，不是新增服务。

客户端原件仅能通过 `client_file_path`规范macOS系统 `/tmp`、`/var`别名后交给私密文件检查；不能解析用户符号链接来绕过归属检查。能力元数据增加 `capture_queue`，新客户端拒绝不具备该能力的旧后台。外置开发API兼容入口保留旧Swift加工，不属于正式本地内核路径。

## 系统支持不能由模块可导入来推断

| 系统 | 本期状态 | 后续设计 |
| --- | --- | --- |
| macOS | 实现并测试本期能力 | Keychain、Library 目录、随包 Python、原生桌面交互 |
| Linux | 保留原有 POSIX 文件/备份工具基础；桌面进程、默认目录和运行包入口明确不支持 | XDG 数据/缓存/状态/配置目录、系统凭证服务、进程回收与桌面入口需独立实现和验证 |
| Windows | 未实现，私密 I/O/锁等能力明确报错 | LocalAppData 布局、ACL、防重解析点、独占发布、Credential Manager、受管进程需独立实现和验证 |

`future_layout_design` 仅提供未实施的布局设计，始终返回 `implemented=false`。禁止通过跳过锁、所有权或权限检查模拟跨平台成功。macOS 的合成测试不代表 Windows/Linux 已通过适配。

## 启动兼容

`kernel_contract.py` 输出内核版本、契约版本、schema16、平台、CPU、运行包清单摘要与能力。Host 就绪事件及 API `/health` 共用该信息，不含密钥或本地路径。运行包摘要用于发现混装；它不替代构建清单、签名与发布验收。

新客户端在私有启动信封中声明契约/schema 版本；Host 在迁移与身份更新前拒绝不兼容版本。客户端再检查就绪事件、能力及本地清单摘要。旧Owner凭证行仍可启动基础服务；新客户端拒绝缺少版本信息的旧后台。M5已完成同版组装、独立随包验收与正式发布；后续同样禁止将新原生代码单独套在旧运行包上。

无数据库版本、备份格式或插件版本升级；既有知识数据和连接文件保持可读。已经安装的旧插件启动器仍可使用，显式更新插件时才生成新启动器，不改用户 AGENTS.md。

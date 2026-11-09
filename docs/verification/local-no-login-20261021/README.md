# Mac 客户端移除账户登录

本次于 2026-10-21 开始实施，2026-10-22 完成验收。范围仅为 Mac 用户账户登录层；本地知识库、随包内核、模型和外部服务授权契约保持。

## 实施范围

- 删除 `AccountClient.swift`、`LocalDevSession.swift` 以及旧开发登录单测；不再提供邮箱密码登录、注册、验证码、密码重置、开发登录或退出账户。
- `WorkspaceView` 启动直接调用 `openLocalLibrary()`；不读取旧账户地址、开发会话或账户钥匙串。缺少运行包显示完整 App 提示；本地服务失败保留重试、取消恢复和备份恢复入口，不回退登录页。
- `connect()` 只准备本地资料元数据，移除账户会话创建、保存和撤销分支；连接失败不清除草稿或采集队列。
- `TokenStore` 不再有默认账户命名空间，调用必须明确指定服务；本机服务 Owner、Agent／浏览器插件凭证和模型 API Key 各自保留。飞书登录属于外部授权，本次不改。
- 更新当前使用说明；三个既有 UI fixture 适配简化后的 `connect()` 签名。

与上一正式验收的逐文件 SHA-256 比对确认，仅五个现有 Swift 文件变化：`Model.swift`、`Workspace.swift`、`API.swift`、`TokenStore.swift`、`DomainManager.swift`；另删除上述两个旧登录文件。没有后台、Worker、运行包或数据库改动。

## 验证

- 完整 App 优化构建通过，独立候选位于 `apps/macos/.build/no-login-candidate/知识收集箱.app`；可信运行包清单校验 19,957 个文件及链接，严格深度签名检查通过。
- LocalStartupTest.log（历史产物已清理）：缺包无请求／凭证访问；宿主失败保留草稿及合成本机、模型、Agent 凭证；内部连接保留五项 GET 元数据读取和 Bearer；503 时保留队列文件原始字节，不发起登录或开发会话请求。
- 原生 `WorkspaceView` 挂载的两种失败界面经辅助功能和截图检查，没有登录、密码或账户入口：缺少运行包（历史产物已清理）、本地服务失败（历史产物已清理）。界面仍提供适用的恢复入口。
- LocalModelConfigurationTest.log（历史产物已清理）：模型地址、Key 保存／更新／清除、失效凭证、库隔离及配置代次回归通过，仅内存合成凭证，无真实模型调用。
- PlatformSupportTest.log（历史产物已清理）：系统路径、握手、版本及能力拒绝、受限命令和启动取消回归通过，未访问真实钥匙串。
- CaptureQueueTest.log（历史产物已清理）：草稿及暂存原件跨实例恢复、幂等键、队列损坏／磁盘不足保护通过。
- KernelCaptureTest.log（历史产物已清理）：实际候选包内置 Python、Worker 与系统辅助程序验证回执落盘失败保留原件、成功交接后清理、服务重启接续归档、草稿保留、不重复入库、合成飞书图文归档和移除队列保留知识，全部通过。
- agent-regression.log（历史产物已清理）：随包服务创建独立 Agent 连接，专属合成钥匙串项恢复、授权领域隔离、端点更新、同凭证跨重启、撤销立即 401 和 stdio 拒读通过；安装／卸载仅写临时合成宿主配置。

## 测试选择和限制

旧 `CaptureQueueTest` 携 runtime 的历史分支仍断言同步完成和客户端长期保留已交接原件，与 M4 起的后台异步队列契约不符。本次第一次运行在收到“正在等待后台归档…”回执后触发它的旧同步断言，记录保留在 CaptureQueueTest-legacy-runtime.log（历史产物已清理）。未改产品实现去迎合旧断言，也未放宽该测试；队列持久化部分使用无 runtime 单测，实际随包交接另由现行 `KernelCaptureTest` 验证。

原生界面测试为临时 AppKit 窗口，并不打开用户正式 App；无真实用户库、外部账号或真实模型调用。未做新的清洁设备、Developer ID 或公证验证。模型 Key 等受保护凭证仍可能触发 macOS 系统钥匙串授权，本次只取消用户账户登录及密码验证，不取消系统凭证保护。

## 兼容与恢复

schema16、插件 1.0.5 和运行包摘要保持不变，无数据库迁移；既有本地资料、队列、模型配置和 Agent 接入无需重新建立。旧账户钥匙串／开发偏好不再读取但不主动删除。发布器归档旧 App，不删除用户数据。

## 复现

所有 Swift 测试使用当前完整 `Sources/*.swift` 编译，示例：

```sh
xcrun swiftc -D CAPTURE_QUEUE_TESTS -parse-as-library \
  -target arm64-apple-macosx14.0 \
  -module-cache-path apps/macos/.build/no-login-test-cache \
  apps/macos/Sources/*.swift apps/macos/scripts/LocalStartupTest.swift \
  -o /tmp/pka-local-startup-test
/tmp/pka-local-startup-test
```

原生截图模式通过 `PKA_STARTUP_PREVIEW` 指定本目录，临时测试 App 需要 WindowServer 访问。随包 Agent 测试携带 `--runtime` 参数；只创建 UUID 临时库与专属合成钥匙串项，结束时清理。

## 正式发布

已更新唯一正式入口 `apps/macos/dist/知识收集箱.app`；发布后核对唯一 App、源码摘要、二进制摘要及 `release.json` 验收来源一致。旧包归档于 `apps/macos/archive/20261022-071946`，未删除用户数据。完整验收摘要见 [runtime-no-login.json](runtime-no-login.json)。

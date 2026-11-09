# macOS 知径 · Knora

SwiftUI 原生个人知识库客户端，面向 Apple Silicon、macOS 14.5+。基础 App 提供采集、阅读、精确搜索、知识库管理、回收站、备份与云端 AI；语义搜索、文字识别和复杂文档解析按需安装。

打开 App 即启动内置 Python、API、Worker 和 SQLite，不需要账户登录，也不需要用户单独启动后台或 AI 网关。模型在“设置 → 模型与 AI”配置，API Key 使用当前用户的私有配置文件保存。详见 [模型配置](../../docs/Mac模型配置.md)与[隐私说明](../../docs/隐私与本地数据.md)。

| 目的 | 文档 |
| --- | --- |
| 开发与回归 | [开发与验证](../../docs/开发与验证.md) |
| 从源码构建 App 和扩展 | [BUILD](BUILD.md) |
| 制作 DMG | [PACKAGING](PACKAGING.md) |
| 维护者替换本机交付包 | [RELEASE](RELEASE.md) |
| 扩展安装与兼容 | [扩展使用说明](../../docs/轻量客户端与扩展使用说明.md) |
| Agent 授权与接入 | [Agent 接入](../../docs/Agent接入使用说明.md) |

`Sources/` 是客户端源码，`Resources/` 是随 App 使用的图标与熊猫素材，`runtime/` 固定运行包输入，`scripts/` 包含构建、打包及回归工具。测试和开发资料不会作为用户知识库打入安装包。

源码仓库不包含 App、DMG、用户扩展或模型。`release.json` 保留维护者既有本地交付记录，不代表当前仓库附带可下载的安装包；发行版本以正式发布的对应源码、制品和校验值为准。当前候选的默认签名为 ad-hoc，尚未完成 Apple 公证及跨设备发行验收。

内部 Bundle ID、数据目录和 `knowledge-inbox://` 协议延续旧版，以保持资料及 Agent 连接兼容。App 安装路径改变后，可在原 Agent 连接中更新插件，再重新加载宿主客户端。

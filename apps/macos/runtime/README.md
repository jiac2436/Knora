# macOS arm64 固定构建输入

当前输入：CPython 3.12.16、uv 0.7.6、99 个固定 wheel，以及 BGE 中文模型、PP-OCRv6 Small 模型和 GCC 原生运行库。

- `requirements.in`：运行根依赖，不包含 pytest 和 PostgreSQL 开发驱动。
- `constraints-tested.txt`：解析约束，只安装实际依赖闭包中的包。
- `requirements-macos-arm64.lock`：固定版本及允许的 SHA-256。
- `artifacts.json`：解释器、模型及原生归档的固定来源、长度与摘要，含锁摘要。
- `capability-roots.json`：基础包、语义搜索、OCR、文档解析的依赖根。

下载、离线构建、拆分、清单生成及验证的完整命令见 [BUILD](../BUILD.md)。输出目录必须不存在，输入不通过校验时不会继续生成可用发行物；构建脚本不会复制用户知识库、开发虚拟环境或已安装扩展。

运行包升级后扩展采用 1.1.0 / CPython 3.12。旧 1.0.0 的二进制扩展不能混用；原用户数据保留，需安装新版配套扩展。见 [CHANGELOG](../../../CHANGELOG.md)。

第三方许可由 wheel 原始许可、依赖索引和 `third_party` 补充集合共同提供。基础 App 不携带可选模型；完整构建中间目录会较大，验证完成后可以清理，不能把它算作用户下载安装包大小。

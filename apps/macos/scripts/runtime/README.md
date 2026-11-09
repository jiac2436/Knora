# 运行包构建与验证工具

当前发行构建入口见 [macOS BUILD](../../BUILD.md)，固定输入及兼容说明见[运行包输入](../../runtime/README.md)。所有输出使用隔离构建目录，不读取个人知识库、凭证或已安装扩展作为发行输入。

| 工具 | 用途 |
| --- | --- |
| `download_inputs.py` | 获取并校验固定解释器、模型及原生归档 |
| `verify_dependency_sources.py` | 核对固定 wheel 来源、实际文件与镜像 |
| `build_runtime.py` | 离线安装锁定依赖、组装同版源码及许可集合 |
| `split_runtime.py` | 按依赖闭包拆分基础包和可选扩展 |
| `finalize_extensions.py` | 生成配套扩展及下载清单 |
| `prepare_dependency_downloads.py`、`prepare_ocr_downloads.py` | 校验并生成逐项下载计划 |
| `verify_runtime.py` | 按可信清单摘要核对文件、权限、链接和额外内容 |
| `probe.py` | 用合成输入验证 TLS/SQLite、PDF、OCR、文档和向量能力 |
| `native_inventory.py` | 检查原生运行库的平台、架构与外部加载路径 |
| `license_inventory.py` | 记录实际依赖与保留的许可材料 |
| `audit_dependencies.py` | 查询公开包版本的已知安全公告 |
| `test_runtime_tools.py` | 路径、链接、摘要、安装记录和输出保护回归 |

`prepare_poc.py` 与 `vendor_paddle.py` 保留为明确指定输入的兼容实验工具，不代替当前固定制品构建流程。不得复制开发虚拟环境或关闭校验以生成可用发行物。

探针使用临时 HOME 和合成资料；Python 网络审计不代替系统网络隔离。依赖清单、签名检查和本机探针也不代替最低系统及清洁设备的安装验收。

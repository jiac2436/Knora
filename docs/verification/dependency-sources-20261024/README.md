# 国内公共源依赖安装验收

来源与实现验证始于2026-10-24；最终发布核验于2026-10-25继续完成。基线为已发布轻量客户端，保留云端AI、schema16、插件1.0.5、A图标和全部已有阅读功能。

## 范围与下载源

用户要求优先采用开源依赖的国内公共下载源，不自行租用安装包服务器。来源不足的部分先报告，由用户决定。用户随后明确选择：**OCR保留离线安装，先交付语义搜索和文档解析的直接下载。**

- Python依赖：首选[清华PyPI镜像](https://mirrors.tuna.tsinghua.edu.cn/help/pypi/)，备用[阿里云镜像](https://help.aliyun.com/zh/yunxiao/user-guide/accelerate-build-processes-for-package-management-tools)。实际完整下载并校验66个可选依赖的当前固定版本；两个镜像均列出同版同哈希文件。阿里云另完整下载验证AnyDoc、ONNX Runtime、FastEmbed三个代表性包。
- 向量模型：[魔搭Qdrant/bge-small-zh-v1.5仓库](https://modelscope.cn/models/Qdrant/bge-small-zh-v1.5)。固定具体提交，7个模型／配置／分词文件完整下载后与既有生产模型逐字节一致，不替换模型，也不重建用户向量数据。
- OCR模型：Paddle官方BOS两个Small模型完整下载、大小及SHA-256校验通过。但Paddle Mac wheel的BLAS/LAPACK仍引用Homebrew GCC等路径，原生库依赖尚未完成普通用户环境的直接安装。未把“文件可下载”误记为“OCR可直接安装”。相关证据在`paddle-native-probe.json`；本次不自动安装系统工具、修改该引擎或增加自托管包。

`wheel-audit.json`记录每个wheel的名称、固定版本、Mac兼容文件名、原锁文件哈希、两个镜像链接和实际下载结果；`model-audit.json`记录模型完整下载结果；`secondary-source-audit.json`记录备用源实测。一次本机下载验收不代表公共镜像商业SLA；支持失败重试、同哈希备用源和原离线包。

## 安装与兼容

设置隐藏下载服务器输入框，用户只需点击“安装”。语义搜索约下载134.9 MB、安装239.4 MB；文档解析约下载9.3 MB、安装21.8 MB。OCR明确“仅支持离线安装”。原有三个`.pkaext`文件及其身份标识保持不变；已安装扩展无需重新下载。

安装器按App签名保护的清单下载预编译wheel及固定模型，不做pip在线解析、不运行setup.py或脚本、不编译源码、不依赖系统Python／Homebrew／Xcode。只接受白名单HTTPS源与重定向；限制大小和时间，逐文件验证哈希，重现原扩展目录及规范化元数据，再核对原文件清单，自检成功后原子发布。输入、输出与原扩展完全一致，因此无需新增扩展格式或数据库迁移。

自检在独立随包Python进程中使用合成数据；禁止Python网络连接，真实执行向量模型和CSV转换，不发送用户文章。错误／取消不启用半成品，原选择和资料保留。取消、主源失败、坏哈希、路径穿越、符号链接、输出不一致、独立CLI路径、OCR禁止自动安装等均有回归覆盖。

下载及安装期间可以继续使用客户端。安装完成后仍需等待任务结束，用⌘Q退出重开一次生效；启用、停用、卸载保持原重启规则。未新增运行中热替换原生库或自动重启Host／Worker，以免中断已有解析、搜索及模型任务。

## 验证结果

- `backend-tests.log`：**1478 passed, 1 skipped, 1 warning**，完整后台回归。既有弃用警告不在本次范围修改。
- `real-download-install.log`：最终安装代码使用候选自带Python，从真实国内源下载两项能力，完整文件清单核验及原生／模型自检成功；运行中旧Host和精确搜索不变，重启后真实语义查询找到原文，OCR明确拒绝未经验证的自动安装，0次云端调用。
- `base-cloud.log`、`library-update.log`：无扩展基础采集／精确搜索／MCP、内置云端分析、知识库更新与重启持久化通过。厂商响应为合成数据，未调用真实收费服务。
- `native-build.log`：冻结源码完整优化构建通过。`ui-build.log`及`extensions-installed.png`／`extensions-completed.png`／`extensions-ui.txt`：生产扩展视图在隔离home中真实点击两项“安装”，看到下载进度与“等待重启启用”；无下载地址输入框，OCR只显示离线提示。未对真实用户库执行安装。
- 最后仅将OCR说明调整为非技术措辞，固定依赖、模型、输出清单和安装代码未改变；最终原生UI使用的运行包含此说明。严格签名、源码／文件清单及离线包哈希另由发布记录核对。

`candidate-inputs.json`记录当前冻结源码和上一正式版；只修改`LocalExtensions.swift`、`extension_install.py`，新增`dependency_install.py`及受信下载计划／清单。并发`browser_native.py`未纳入候选，未覆盖其他对话的开发文件。

最终摘要和发布结果见`runtime-dependency-sources.json`。当前依旧只验收macOS arm64；商业签名公证、干净设备矩阵、断点续传、逐引擎进程隔离和热启用未在本次交付范围内。

## 发布完成

2026-10-25发布器确认客户端已退出后，已更新唯一正式`apps/macos/dist/知识收集箱.app`，上一轻量版归档于`apps/macos/archive/20261025-102915`。发布后核对3007项运行包清单、严格签名、原生二进制、A图标、两项在线计划与三个原离线包哈希通过。正式App逻辑大小约113.7 MB；用户知识库、凭证及扩展安装状态未被发布过程修改。

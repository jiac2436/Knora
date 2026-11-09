# DMG 安装包

目前已安装的正式本地版本和旧 `0.1.0` DMG 保持原样。开源准备版为 **0.2.0 候选**：采用 Python 3.12 和新版配套扩展，只有完成对应候选验收后才制作新的 DMG。

面向 Apple Silicon、macOS 14.5+，不通过 Mac App Store。当前默认采用 ad-hoc 签名，未经 Developer ID 签名和 Apple 公证；本机测试通过不代表跨设备安装已经验收。

## 内容与安装

映像中只显示 `Knora.app` 和 `Applications` 快捷方式。界面延续简洁浅色背景、标题、左右图标与箭头，不放安装说明文件或额外小字。将 App 拖入 Applications，复制完成后推出映像，再从“应用程序”启动。

仅封装验收过的轻量基础 App。不会复制个人安装的 OCR/扫描扩展、离线扩展包、模型、知识库、API Key、浏览器队列或本机配置。基础包保留云端 AI，扩展在设置中按需安装。旧 1.0.0 扩展不能混用新版 Python 3.12，更新后需安装 1.1.0 配套扩展并退出重开；保存的资料不删除。

未公证软件的首次打开方式见 [Apple 官方说明](https://support.apple.com/zh-cn/102445)：尝试打开后，可在“系统设置→隐私与安全性”确认可信来源的软件。不要关闭系统整体安全防护。

## 维护者构建

先按 [BUILD](BUILD.md) 从源码构建并完成回归、随包启动、扩展下载/离线推理及备份恢复验收。记录对应原生二进制摘要、运行包清单摘要、源码快照和测试结论；不能修改记录中的通过状态来绕过真实检查。

安装仅用于构建的固定工具，再指定已验收的 App 与记录：

```bash
.venv/bin/python -m pip install --require-hashes \
  --target apps/macos/.build/dmg-tools -r apps/macos/scripts/dmg-requirements.txt
.venv/bin/python -B apps/macos/scripts/build_dmg.py \
  --application apps/macos/.build/release/candidate/Knora.app \
  --release-record apps/macos/.build/release/candidate-release.json \
  --output apps/macos/.build/release/Knora-0.2.0-macOS-arm64-candidate.dmg
```

候选发行记录至少包含 `app`、`native_binary_sha256`、`manifest_sha256`、`tests.status` 和 `requires_legal: true`。打包器核对这些摘要、签名、基础包类型、资源白名单和许可目录，拒绝可选模型与推理库。同名 DMG 和校验文件存在时拒绝覆盖；打包不会替换正在运行的 App，也不会修改 `release.json`。

脚本执行映像验证、只读挂载、Finder 布局检查、完整文件及权限对比、复制安装验证，并生成 `.dmg.sha256`。之后还要在全新临时用户目录验证从 DMG 复制出的运行包；真正的清洁设备和最低系统实机验证仍需另外完成。

可用以下脚本执行复制安装后的运行包验证；摘要须取自可信打包记录。它只挂载指定映像，在临时目录复制 App，并使用合成资料和模拟模型，不会替换已安装客户端：

```bash
.venv/bin/python -B apps/macos/scripts/verify_dmg_runtime.py \
  apps/macos/.build/release/Knora-0.2.0-macOS-arm64-candidate.dmg \
  --sha256 <可信记录中的DMG摘要>
```


省略 `--release-record` 时只使用现有 `release.json` 中已验收的旧正式版本；源码变更后的新候选不能套用旧记录。需要正式替换时按 [RELEASE](RELEASE.md) 等待用户退出，再由发布器处理。

## 对外发布

源码、DMG 与配套扩展应使用同一版本的构建记录与校验值，并提供 GPL 及第三方所需的对应源码、依赖锁、构建说明和许可材料。GitHub 仓库和 Release 附件需维护者另行确认后上传；当前脚本不自动提交或上传。

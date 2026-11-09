# OCR 公共源逐项安装验收（2026-10-27）

状态：本轮实现及验收通过，已于2026-10-29随Agent接入优化发布，原下载计划和兼容规则保持。最终签名／运行包／正式路径见[联合发布记录](../agent-settings-20261029/published-verification.json)，正式版本以 apps/macos/release.json 为准。

## 本次行为

设置中的三项扩展均支持一次点击安装，并展示可展开的下载清单、名称／版本／大小及各项状态。开始安装后自动展开对应清单，固定高度独立滚动；取消后可以重试。OCR 为53个固定 Python wheel、1份Mac运行组件归档及2份Paddle官方模型归档，共56项，在线408,321,045 bytes，安装781,178,000 bytes。基础App逻辑大小117,228,596 bytes。

不要求用户寻找我们自制安装包。原生运行组件只提取4个必要库，使用App内置、输入输出均锁定SHA-256的兼容规则，重现既有已验收文件；用户端不执行pip解析、编译器、链接工具、Homebrew或Xcode。仅写扩展暂存目录，完整校验与本地引擎自检后原子激活。离线包仍为备用。

## 来源核验

- Python：清华主源／阿里备用。沿用前轮66个固定wheel的完整清华下载及原锁／PyPI哈希核验；本轮OCR53项从公共来源再次实际下载。
- 模型：Paddle官方国内BOS的PP-OCRv6 Small检测／识别模型。本轮真实完整下载并识别。
- GCC：16.2.0 arm64_sonoma bottle.1，164,159,961 bytes，SHA-256为 `2a23accb457b670c90e18d36bfbcfb212c7d0292a300881436a45630d64ed608`。清华完整下载与官方元数据一致，中科大备用文件Range返回206及相同总大小。记录见[gcc-source.json](gcc-source.json)。
- 上游官方元数据：[Homebrew GCC](https://formulae.brew.sh/api/formula/gcc.json)。国内文件：[清华](https://mirrors.tuna.tsinghua.edu.cn/homebrew-bottles/gcc-16.2.0.arm64_sonoma.bottle.1.tar.gz)、[中科大](https://mirrors.ustc.edu.cn/homebrew-bottles/gcc-16.2.0.arm64_sonoma.bottle.1.tar.gz)。

## 验收证据

- backend-tests.log（历史产物已清理）：1483 passed、1 skipped、1 warning。包括原生输入／输出摘要及范围、归档精确成员、进度／取消、健康检查后再次校验。
- real-ocr-download.log（历史产物已清理）：候选App独立Python、临时HOME及资料库，取消清理、56项实际下载、目标目录逐文件符合原离线包manifest、禁止联网的真实像素识别；运行中Host仍能采集／精确搜索；正常重启后真实Worker识别扫描PDF，实际云端调用0次。
- other-downloads.log（历史产物已清理）：语义搜索和文档解析真实下载、自检、原子安装、重启及语义检索回归，旧扩展身份保持。
- base-cloud-mcp.log（历史产物已清理）／library-update.log（历史产物已清理）：无扩展基础采集、精确搜索、重启、MCP及云端AI／知识库更新链路通过，只用合成厂商回复。
- native-build.log（历史产物已清理）：完整优化构建成功，严格签名、3010项运行包文件／链接校验通过。
- ui-progress.png（历史产物已清理）／ui-cancelled.txt（历史产物已清理）：真实生产扩展视图的隔离原生窗口，点击OCR安装自动展开56项，显示逐项状态、支持独立滚动；点击取消后显示未安装、可重试，无active身份或暂存payload残留。测试App已⌘Q退出。
- [runtime-ocr-dependencies.json](runtime-ocr-dependencies.json)：冻结源码、运行包／原生二进制／图标摘要及最终测试记录。publish-preflight.log（历史产物已清理）记录当前发布阻塞原因。

## 兼容与边界

schema16、Codex插件1.0.5、A图标、所有旧扩展身份和已安装文件格式保持，无需迁移或重下已有扩展。不读写真实用户资料或凭证，不混入并发browser_native.py开发。下载期间可继续使用，安装／启停／卸载仍在正常退出重开后生效。

公共镜像不承诺商业SLA；保留备用源和离线包。下载量因上游GCC归档大于实际提取内容，较裁剪离线包大；本轮不增加断点续传或自托管源。当前验收为本机macOS arm64配套版本及ad-hoc签名，Developer ID、公证、干净设备发行矩阵和许可证商业交付核对仍需后续完成。

## 复现命令

```bash
PYTHONPATH=services/api:. .venv/bin/python -m pytest services/api/tests -q --disable-warnings --maxfail=1

apps/macos/.build/ocr-candidate/知识收集箱.app/Contents/Resources/runtime/python/bin/python3 -I -B \
  apps/macos/scripts/ocr_download_runtime_test.py \
  apps/macos/.build/ocr-candidate/知识收集箱.app/Contents/Resources/runtime

.venv/bin/python -B apps/macos/scripts/publish_app.py \
  --source apps/macos/.build/ocr-candidate/知识收集箱.app \
  --record docs/verification/ocr-dependencies-20261027/runtime-ocr-dependencies.json --dry-run
```

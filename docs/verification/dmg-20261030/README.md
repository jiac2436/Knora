# Knora 0.1.0 DMG（2026-10-30）

以设置“关于”版本的正式 `Knora.app` 为输入，原生二进制和运行包均保持原验收摘要。产物为 `apps/macos/dist/Knora-0.1.0-macOS-arm64.dmg`，42,848,979字节；SHA-256见 [package.json](package.json) 和DMG旁的校验文件。

## 排除扫描扩展

只复制正式App，不复制整个dist或用户主目录。基础运行包profile为 `base-with-cloud-ai`，3022项内容通过校验；没有扩展安装目录、OCR模型、语义模型及Paddle／OpenCV／ONNX Runtime可选依赖。当前用户手工安装的扫描扩展位于App外的应用状态目录，未复制、未停用、未卸载；`dist/扩展包`中的离线制品也未纳入。

## 验证结果

- 映像完整性通过；可只读挂载，包含App、Applications快捷方式、安装说明。
- 映像中及复制安装后的App，与正式输入的全部文件内容、符号链接和文件权限一致。
- 三处App深度严格签名及3022项运行包清单均通过；没有重新签名或修改正式App。
- 从DMG复制到含空格的临时安装路径，推出DMG后启动其内置Python／API／Worker成功。
- 全新临时HOME中，semantic／ocr／documents均为 `not_installed`；保存与回读合成笔记、精确搜索及重启保留通过。
- 无真实用户库读写，无系统Python依赖、可选扩展下载或真实模型调用。

当前机器为Apple Silicon、macOS 26.2。未覆盖其他实体Mac或最低14.5系统实测。App继续采用ad-hoc签名；无有效Developer ID证书，DMG未经公证。保留既有App内系统版本检查，未为本轮打包修改代码。

安装与复现见 [PACKAGING.md](../../../apps/macos/PACKAGING.md)，真实执行日志见 smoke.log（历史产物已清理）。

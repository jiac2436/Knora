# 知径 · Knora 品牌更名（2026-10-30）

采用中文名“知径”、英文名“Knora”。正式应用文件为 `apps/macos/dist/Knora.app`，窗口、品牌可访问性说明和存储页显示“知径 · Knora”。飞书、Agent提示及本地文章跳转页使用Knora；新生成知识库插件的开发者名称也为Knora。

## 兼容范围

- 沿用用户选定的A方案图标及现有采集资源。
- `local.pka.knowledge-inbox` Bundle ID、`KnowledgeInbox`内部可执行名、`knowledge-inbox://`文章协议保持。
- 数据、阅读位置、配置、设备与模型凭证路径保持，不迁移或清理用户资料。
- schema16、插件1.0.5和连接ID不变。旧Agent配置如引用旧App路径，请在新版“设置→Agent接入”为原连接“创建／更新插件”，再正常重新加载宿主；本轮不会直接修改真实Codex／Claude配置。
- 从新 `Knora.app` 启动。Dock若仍固定旧路径，可移除旧快捷方式后重新固定新版。

## 验证结果

- 发布器6项隔离单测：旧名迁移、Knora后续升级、复制／写记录失败回滚、运行中及未验收阻止。
- 103项冻结运行包后台回归：文章链接、插件元数据、安装与凭证保护、接入端点。链接测试显式使用基础精确搜索，不依赖可选语义扩展。
- 真实随包API／Worker／MCP隔离测试：重复条目收敛、App路径改变识别及修复、原凭证与插件ID保留、模拟宿主重新加载及重启；未改真实宿主配置，模型调用0次。
- 完整优化Swift构建通过。核验80项原生源码摘要；仅7个既有文件更名文案并新增AppBrand，其他文件沿用上一正式冻结版本。
- 深度严格签名、原图标／采集资源、Bundle元数据和3022项运行包核验通过。运行包只改两个品牌文案模块及清单。
- 仍采用本地ad-hoc签名；本轮没有制作DMG、获取Developer ID或公证。
- 未打开真实用户资料库进行GUI验收；编译警告仅来自既有MacCredentialStore弃用API。

源码、产物摘要及发布状态见 [runtime-knora-brand.json](runtime-knora-brand.json)。可复验：

```bash
.venv/bin/python -B docs/verification/knora-brand-20261030/verify_brand.py apps/macos/dist/Knora.app
```

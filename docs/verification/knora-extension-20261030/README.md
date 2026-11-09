# Knora 浏览器插件更名与桥接修复（2026-10-30）

用户确认后，将扩展名称、弹窗、设置页、说明页及桥接提示统一为Knora。安装器默认指向 `apps/macos/dist/Knora.app`。本机Chrome／Edge旧桥接已私密备份并重新注册到该路径。

扩展仍从原 `apps/extension/dist` 加载，未增加manifest key或权限，原目录派生ID保持；Native Host仍为 `local.pka.knowledge_inbox`，已安装白名单保持。原图标、浏览器存储键、队列及库隔离规则不变，未读取或修改真实浏览器存储，也未重打包Mac App。

## 验证

- 21项扩展单测通过；manifest权限、图标尺寸及全部JavaScript语法检查通过。
- 10项原生桥接单测通过，新增旧App更名后重装桥接的回归；知识库文件、Host标识与白名单保留。
- 59项临时Chrome合成页面检查通过，覆盖预览、选区、小红书分类、图片、保存和重载提示。
- 使用正式Knora随包解释器、API和Worker，在临时HOME执行公开安装脚本：默认新路径、重复注册、真实Native Messaging消息协议、连接、幂等保存／回读、跨库阻止、离线与重启均通过，模型调用0次。
- 本机已安装启动器验证通过。检查时Knora未启动，桥接正常返回Knora离线提示；没有为验证而打开真实App或提交真实资料。
- Mac App二进制、运行包摘要、扩展图标及权限保持不变。

后台现有开发环境的旧启动协议端到端用例未执行；本轮使用上述正式随包临时库测试验证完整链路。浏览器页面检查为本地合成页面，并非登录真实网站实测。

## 使用与复验

在Chrome／Edge扩展管理页，给原插件点击一次“重新加载”，再打开Knora和插件。无需删除扩展或清空队列；重装扩展会影响原有浏览器存储，不是本轮更新步骤。

```bash
cd apps/extension
npm test
npm run check
```

项目根目录可执行隔离随包测试：

```bash
apps/macos/dist/Knora.app/Contents/Resources/runtime/python/bin/python3 -I -B \
  docs/verification/knora-extension-20261030/native_bridge_runtime_test.py
```

摘要和结果见 [verification.json](verification.json)。真实桥接备份保存在当前用户私有应用状态目录的 `browser/backups/knora-20261030-225632/`，未写入仓库。

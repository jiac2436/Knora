# Knora 维护者发布规范

当前构建及打包流程见 [BUILD](BUILD.md) 与 [PACKAGING](PACKAGING.md)。本规范用于维护者验证并替换本机交付包，不代表仓库附带已发布安装包。

## 每次发布流程（AI Agent 必须遵守）


1. 确认工作区本次交付范围，使用同一已验收源码组装运行包；记录清单SHA-256，不能把旧后台与新Swift混装。
2. 完整构建默认输出到 `.build/candidate/Knora.app`，不得直接写正式 `dist`。必须提供 `PKA_RUNTIME_PAYLOAD` 和 `PKA_RUNTIME_MANIFEST_SHA256`。外置后台开发模式须显式输出到 `.build/dev` 等独立路径。
3. 完成该改动需要的测试、签名和随包验收，生成已通过的验收JSON记录。不能仅为发布而把状态改为passed。
4. 等待用户任务结束并退出客户端；发布器检查运行状态，不会强制退出或结束Worker。
5. 使用下列命令先预检，再发布。占位路径必须替换为本次实际验收记录。

```bash
.venv/bin/python -B apps/macos/scripts/publish_app.py \
  --source apps/macos/.build/candidate/Knora.app \
  --record apps/macos/.build/release/acceptance.json --dry-run

.venv/bin/python -B apps/macos/scripts/publish_app.py \
  --source apps/macos/.build/candidate/Knora.app \
  --record apps/macos/.build/release/acceptance.json
```

发布器校验验收状态、运行包摘要、签名及记录中可用的二进制摘要；复制候选包并再次校验后，将旧包移到 `apps/macos/archive/<北京时间戳>/`，最后发布唯一正式包并更新 `release.json`。复制或记录写入失败会恢复原交付包。旧包保留可回溯，不删除用户知识库或凭证。归档不纳入Git。

发布后确认dist仅有一个App，并核对正式包签名和运行包。后续不再交付“vXX-某某修复.app”。历史热修脚本也不是新的默认发布入口。

## 路径与配置兼容

安装路径或运行包契约变化后，Agent 插件可能需要在原连接中执行“创建/更新插件”，并在宿主重新加载。发布器不会直接改写其他应用配置，也不删除用户知识库或凭证。

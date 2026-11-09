# 插件文章链接交付 — 2026-10-17

## 行为

插件新引用可显示“在知识库中打开《文章标题》”，使用服务返回的 `app_url` 原样生成 Markdown 链接。地址为：

```text
knowledge-inbox://library/<本地库身份>/source/<文章ID>
```

文章与库的 UUID 只作为 URL 定位参数，不作为引用文字。链接不含密钥、正文、访问凭证或 API 地址。点击只进入当前本地用户的阅读界面，不创建 AI 任务、不改变资料；文章链接不是 Agent 授权凭证。

## 实现与兼容

- MCP 搜索、目录、资料、标签、总结及原文读取返回可选 `app_url`；读取原文时附上文章标题，方便直接引用。仅当前所有者的本地安装能提供链接，没有本地安装时返回 null。原权限、连接过期/撤销和资料删除检查保留。
- 本地 `/v1/me` 附加实际安装的 `library_id`；客户端验证链接与当前库一致，其他库不静默切换，避免备份/恢复或副本环境中打开错误资料。非本地 API 原身份响应不增加该字段。
- macOS Info.plist 注册 `knowledge-inbox` 协议，生产 `WorkspaceView.onOpenURL` 接收系统事件。服务未就绪时暂存链接；连接后复用原文章加载器和缓存，进入对应知识域并关闭遮挡的阅读面板。
- 只接受固定协议、主机和 UUID 路径，拒绝用户名、密码、端口、查询、片段、多余路径和目录跳转。只在成功读取后替换当前文章；每次导航绑定连接和请求代次，迟到响应不覆盖新导航。
- 文章不存在/已删除、错误本地库给出明确提示，不静默打开其他文章。旧引用打开文章当前版本；本链接不声称定位某个历史版本或原文段落。
- Skill 只使用返回的链接，不根据 ID 编造 URL；优先客户端链接，没有客户端链接时才使用服务提供的原网页地址。内部 ID 仍仅供工具调用，主动调用策略不变。
- Codex 插件版本升级为 **1.0.4**，身份与授权不变；原连接可显式更新，继续支持新 App 路径和失败回退。未直接修改 AGENTS.md、Codex 缓存或历史消息。

数据库仍为 schema16，没有迁移、第三方依赖或模型网关改动。旧 API/客户端可以忽略新增字段；使用外部唤起必须安装注册新版 App。备份、原文、正式 AI 产物及存储格式不变。

参考：[OpenAI MCP 服务文档](https://developers.openai.com/plugins/build/mcp-server)建议为来源返回用户可打开的绝对 URL。该文档没有明确保证当前 Codex 所有自定义 URL 协议的点击行为，因此不能用接口规范代替实际宿主验证。

## 免费验收

- 完整 Python 回归：**1,245 项通过、1 项跳过、1 项既有依赖弃用警告**，无排除用例。日志 `/tmp/pka-article-links-regression-20261017.log`。新增本地链接、MCP 各读取路径、非本地返回 null、库身份和撤销授权边界测试通过。
- 首次普通沙箱定向测试有 3 项宿主测试因回环端口 `Operation not permitted` 失败；在受审查的隔离合成测试环境中整套重跑通过，未放宽业务校验来绕过环境限制。
- 全 Swift Sources typecheck、测试程序及完整 App 编译通过。长阅读视图只拆出一层计算属性，降低新增事件处理后的编译复杂度。
- 实际 Mac LocalService／包内 API／Worker／模拟问答网关：链接格式解析、非法参数、正确文章与知识域、错误库阻断、缺失文章提示、冷启动前排队/重连后打开均通过；同时回归来源编号、原文读取、幂等、历史和重启不重发。日志 `/tmp/pka-article-links-rag-runtime-20261017.log`。
- 独立签名的原生测试 App 使用生产 Workspace、临时库及随机测试钥匙串，通过 macOS 实际 URL 事件验证 **冷启动、已运行、关闭主窗口后再打开** 三种情况。测试完成后退出并注销临时 App，没有操作真实资料。日志 `/tmp/pka-article-links-native-ui-20261017.log`。
- 包内运行清单 19,929 个文件/链接、80 份后台 Python、Skill 资源与源码核对，冻结 128 份源码哈希；Info.plist 协议声明检查及严格签名校验通过。

本轮 **0 次真实模型调用**。电脑控制工具明确禁止操作 `com.openai.codex`，未换路径绕过；**Codex 对话内真实点击未自动验收**。用户需要更新原插件并在新会话确认，不能把原生事件验证称为 Codex 端到端点击通过。已有 Codex 回答不会被改写，新引用在新插件加载后生成。

## 构建与使用

- App：`apps/macos/dist/知识收集箱-v16-文章链接.app`
- 运行包：`apps/macos/.build/runtime/rebuilt-20261017-v16-article-links-verified`
- 清单 SHA-256：`26cda5a87ab7690994f601e765865425641049f4337113861461b6abc9c31ad0`
- App 二进制 SHA-256：`41783f6ec4dbe777d9576c77e6d97c7a65ac7196c6ba954768258f55f13e116e`
- 完整源码和验收记录：[runtime-article-links-20261017.json](runtime-article-links-20261017.json)。

先 ⌘Q 退出旧客户端，再打开新版，完成 macOS 的协议注册。在“设置→Agent 接入”原 Codex 连接点击“创建/更新插件”，到 Codex 插件页更新/重新安装后，开启新对话获取文章引用。保持相应本地知识库可用；跨库时先打开正确的库再点击。此批次不需要重启 AI 网关。

隔离原生验收的重跑方式（使用独立输出测试程序，不打开生产 App）：

```bash
xcrun swiftc -D CAPTURE_QUEUE_TESTS -parse-as-library -target arm64-apple-macosx14.0 \
  -module-cache-path apps/macos/.build/module-cache apps/macos/Sources/*.swift \
  apps/macos/scripts/ArticleLinksUITest.swift -o apps/macos/.build/article-links-ui-test
.venv/bin/python -B apps/macos/scripts/article_links_ui_test.py \
  apps/macos/.build/runtime/rebuilt-20261017-v16-article-links-verified \
  apps/macos/.build/article-links-ui-test
```

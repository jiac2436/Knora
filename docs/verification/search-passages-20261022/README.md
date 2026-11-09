# 多片段检索与正文准确定位

## 行为

- 搜索框拥有焦点时，Tab 在精确／语义搜索间来回切换，查询文字保留；模式仍按本次进程保存，重启默认精确。
- 当前文章精确搜索的第二、第三处等结果使用独立原文偏移；阅读器不再从全文重新寻找第一次相同文字。
- 知识库搜索显式请求片段结果：同篇文章可展示多个命中，每条有独立原文范围。保留文章融合排序，按每篇首条、每篇第二条交错展示，避免长文占满。结果总上限仍为既有 20 条。
- 语义结果选择实际原文窗口；总结检索选择它所引用的原文，原文中可以没有查询字面词。PDF 同时返回页码及文本偏移。仅标题匹配不伪造正文命中。

## 原因与修复

旧 WKWebView 收到正确偏移后，仍用 `indexOf` 遍历 DOM 从头查同一句，导致重复内容始终命中首处。新逻辑在 Markdown 词法解析时保存文本 token 的原始 UTF-16 范围，按来源范围创建 DOM 高亮；保留 CRLF 转换、实体、转义和格式标记的对应关系。定位不会重建整篇 DOM。恢复定位前合并移除高亮后的文本节点，避免反复定位积累碎片。

服务端原来按来源去重，本次增加可选 `SearchRequest.result_unit`（`source`／`passage`，默认 `source`）。仅客户端搜索面板请求 `passage`；旧客户端和 Agent 仍保持来源级结果。片段增加 `match_id`、`match_text`、`text_locator`，原 source_id、revision、evidence_id、locator 保持兼容。客户端先核对版本和原文片段，再把 Python Unicode scalar 偏移转换为 UTF-16，失效的新片段不回退猜测首个关键词。

不改数据库结构、schema16、插件1.0.5、依赖及用户数据，无迁移。API、Worker、网关完整源码树在可信父运行包上组装；仅 schemas.py、services.py、semantic.py 和新增 search_passages.py 变化。没有模型付费调用。

## 验证

- 87 项 API 回归：片段展开、原文范围、表情、同篇重复、文章交错与总上限、用户／库／单资料范围、删除、PDF 双定位、语义多窗口、总结引用、旧来源级兼容，以及原检索／Agent 权限回归。
- 真实 WKWebView 测试 SearchPassageReaderTest：重复短语三处分别命中；跨加粗／链接／代码、HTML 实体、表情、CRLF、列表、引用、表格、HTML；保留提示框；无查询字面词的原文选中；过期范围拒绝。
- 生产 KnowledgeView + URLProtocol 合成传输的原生交互：Tab 来回切换；同篇文章第 12 处结果回车后高亮第 12 段；输入“如何避免错误”选中语义第 18 条，高亮第 18 段原文；⌘F 切回精确后选中第三个重复词，只高亮第三段关键词。截图 exact-twelfth.png、semantic-eighteenth.png、current-third.png。
- 随包 Python/API 的独立 SQLite 测试：多个片段、准确 scalar 偏移、旧来源级兼容、库／资料范围、断开数据库重读持久数据，模型调用 0 次。

真实语义模型相关性质量依赖既有向量模型与索引；本轮使用合成向量和传输验证定位，不代表真实模型相关性评估。未作极端格式文档及帧时间性能基准。构建保留既有 Security 弃用警告。

## 发布

优化构建、签名、同版运行包及发布预检均通过；检测正式客户端已退出后，经 publish_app.py 发布至唯一正式 App。旧包归档于 `apps/macos/archive/20261022-181722`。发布后再次核对正式二进制、19958 项运行包清单、完整源码摘要及原版图标。记录见 runtime-search-passages.json 及 apps/macos/release.json。

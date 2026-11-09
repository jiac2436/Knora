# 搜索交互与精确搜索验收

## 本次行为

1. 搜索方式为“精确搜索／语义搜索”。CaptureModel 仅在内存保存选择，本次启动默认精确，重开搜索或切文不重置；重新启动 App 重置。语义搜索沿用原文／总结／标签／向量等现有融合方案。
2. 搜索面板去掉标签筛选输入、总结索引重建按钮及底部快捷键提示；其他入口与重建能力保留。隐藏的标签字段不再传入本面板请求。
3. 当前选中条目改为较亮浅紫底并带边框，整行可点击。
4. 结果高度有界；内容超出时显示原生滚动条，不受系统自动隐藏叠加滚动条偏好影响。
5. 上下键改变选择后 ScrollViewReader 跟随滚动，条目保持可见。
6. 点击搜索面板外关闭；Esc、关闭按钮、回车打开结果保留。
7. ⌘F 搜索当前资料，⌘⇧F 搜索当前知识库。快捷键在侧栏收起时仍可用；知识库请求始终传当前 domain_ids，当前资料的语义请求额外传 source_id。
8. 搜索浮层使用与 AI 概览相同的原生 smooth 0.18 秒淡入淡出；支持“减少动态效果”，浮层不挤动正文。

精确搜索在数据库中对标题与当前版本正文作完整连续字符串匹配，SQL 在 limit 前筛选，不先取语义前 20 条再在客户端过滤；不使用 AI 总结／标签／分词近似结果补全。`%` / `_` 为字面字符。定位返回 Python Unicode scalar 范围，客户端沿用既有 UTF-16 转换。附件正文尚未解析时仍可按标题查找。每次至多 20 条的既有限制保持。

## 兼容与数据

`POST /v1/search` 增加可选 mode（exact / semantic，默认 semantic）及 source_id。旧调用默认行为保持；source_id 只在已有 owner、domain、project、visibility、tag 访问范围内收窄，不扩大授权。不改数据库结构、schema16、插件1.0.5、原文或已有索引，无数据迁移。新客户端随同版运行包原子发布。

运行包在可信父包上组装 API／Worker／网关完整源码树，确认只有 backend/app/schemas.py、backend/app/services.py 内容变化，无依赖变更。构建脚本为 assemble_runtime.py，清单与源码摘要记录于 runtime-search.json。

## 已完成验证

- 81 项 API 回归通过：新增精确搜索边界，加原检索、总结检索和 Agent 权限用例。覆盖连续／分散词语、SQL 通配符转义、标题、未解析正文、跨 evidence 块、当前版本、删除、库／用户／单资料范围、limit 前筛选与非法 mode；精确路径若尝试语义调用测试立即失败。
- 使用最终运行包的独立 Python 执行 packaged_search_test.py：隔离 SQLite、真实 API、Unicode 定位、域和资料过滤、断开数据库后重读持久结果，模型调用 0 次。
- 使用生产 KnowledgeView、KnowledgeSearchPanel 与 URLProtocol 合成传输的独立 Bundle 实测：⌘F 当前内容、⌘⇧F 当前库，精确和语义切换，15 次下键／19 次下键跟随滚动（滚动条由近 0 到约 0.735／0.996），第 20 条完整可见，回车打开正确资料并关闭；外部点击与 Esc 关闭。
- 实测语义模式在切文／关闭重开后保留；退出测试进程并重新启动后，⌘F 显示精确搜索。侧栏收起后两个快捷键仍可用。
- 截图：keyboard-scroll.png、semantic-scroll.png。均为合成内容，未访问用户库或调用真实模型。

```bash
PYTHONPATH=services/api .venv/bin/python -m pytest \
  services/api/tests/test_exact_search.py services/api/tests/test_m1_api.py \
  services/api/tests/test_summary_retrieval.py services/api/tests/test_agent_knowledge.py -q

apps/macos/.build/runtime/search-20261022-final/python/bin/python3 -I -B \
  docs/verification/search-20261022/packaged_search_test.py \
  apps/macos/.build/runtime/search-20261022-final
```

动画实现及减少动态效果分支已核对；本轮未作帧时间量化或多显示器性能基准。原生构建保留既有 Security 弃用警告。

## 发布状态

最终优化构建、原生签名和同版运行包校验通过。发布预检检测正式客户端已退出，已通过发布器部署至唯一正式 App。发布后再次确认签名、二进制／运行包／源码摘要及原版 Logo；旧包归档于 `apps/macos/archive/20261022-174232`。正式发布来源见 apps/macos/release.json。

# 知识域概览三页只读接口

日期：2026-10-15（Asia/Shanghai）

实现：`services/api/app/domain_library.py`。所有接口使用现有 App Bearer 会话，校验 owner 与知识域归属，固定 `scope: "app"`；`shareable` 输入返回 422。这些接口不改变 MCP 契约。

## 1. 分类目录

`POST /v1/domains/{domain_id}/analysis/library`

请求：

```json
{"scope":"app","offset":0,"limit":100}
```

响应字段：

- `domain_id`、`scope`：本次读取范围。
- `source_count`：当前有效资料总数，排除软删除、删除中、已删除资料。
- `topic_count`：可见正式分类数，不包含系统“未分类”。
- `unclassified_count`：未被当前范围任意分类收录的资料数。
- `items[]`：`id`、`name`、`parent_id`、`source_count`、`direct_source_count`。
- `next_offset`：后续分类分页位置；末页为 `null`。

分类沿用项目已有两级约束。父级 `source_count` 统计自身与子级资料的去重并集；`direct_source_count` 只统计该级直属资料。同一文件在多个分类展示，总资料数只计一次。空的自动候选分类隐藏，手动分类及仍有可见子级的父分类保留。分页按分类名称、ID 稳定排序；`limit` 范围为 1–100。

## 2. 分支文件

`POST /v1/domains/{domain_id}/analysis/library/sources`

请求：

```json
{"scope":"app","topic_id":null,"unclassified":true,"offset":0,"limit":40}
```

- `topic_id` 与 `unclassified: true` 不能同时提供。
- 提供 `topic_id`：读取该分类的直属文件，不重复展开子分类文件。
- `unclassified: true`：读取未分类文件。
- 两者均未指定：读取整个知识域的文件。

响应：`domain_id`、`scope`、`topic_id`、`unclassified`、`total_count`、`next_offset`，以及 `items: [{id,title,revision_id}]`。文件按标题、ID 排序，默认每页 40 条，最大 100 条。客户端应以分支为缓存单位，不逐文件加载详情；只在用户打开原文时读取现有资料详情接口。

## 3. 知识域信息

`POST /v1/domains/{domain_id}/analysis/information`

请求：

```json
{"scope":"app","tag_offset":0,"tag_limit":100}
```

响应包含：

| 字段 | 口径 |
| --- | --- |
| `computed_at` | UTC ISO 时间，由客户端按系统时区展示 |
| `documents` | `total/analyzed/pending/outdated/running/failed` |
| `topics` | `total/roots/children`，与分类目录可见范围一致 |
| `tags` | `total/manual/ai/both/items/next_offset` |
| `storage` | 当前可归属数据的逻辑字节量，详见下文 |
| `rag` | 当前域、当前版本的分层向量索引情况 |
| `overview_coverage` | 当前有效知识域概览覆盖数据；没有有效概览为 `null` |

### 分析和标签

单篇分析复用 `current_summary_artifact` 与当前原文引用校验，不能把有历史产物等同于已分析。`documents` 各状态互斥：有当前有效分析优先计入 `analyzed`；否则仅考察冻结原文版本、标题、个人备注及项目上下文仍与当前资料一致的最新分析任务，待执行或运行中计入 `running`，失败或结果未知计入 `failed`。旧原文或旧上下文的任务不占用当前状态：有历史产物但无有效结果计入 `outdated`，其余计入 `pending`。复用 `is_stale` 并传入预加载上下文，不为每项任务重复读取个人备注或项目。`total` 为这五类之和。此处统计当前可使用的分析状态，不提供全部历史任务成功率。

标签复用正式标签有效性规则：人工来源有效；AI 来源必须绑定当前资料版本。`items` 项为 `{id,name,origins}`。`manual` 与 `ai` 均包含双来源标签，二者不能相加当总数；`both` 为交集。未确认标签建议不计入。

### 本地存储

`storage.measurement` 固定为 `logical_partial`：

- `recorded_asset_bytes`：归属当前域资料的已记录附件大小，以及当前资料版本已保存图片的已记录大小。
- `text_utf8_bytes`：当前正文的 UTF-8 字节数。
- `ai_payload_bytes`：当前有效单篇分析和当前有效已发布概览的结果、冻结输入 JSON 字节数。
- `vector_payload_bytes`：当前资料版本、当前有效分析/概览及当前向量模型的向量二进制数据字节数。
- `known_payload_bytes`：以上各项之和，表示已统计逻辑数据量。
- `total_bytes`、`physical_bytes`：均为 `null`，不伪造知识域独占物理磁盘占用。

该统计不包含 SQLite 页、倒排表、全部历史版本、数据库空闲页等物理开销；附件大小来自持久化记录，未逐文件重新核验磁盘存在性，也不代表独占物理空间。同一物理文件的去重、共享及文件系统压缩等因素不能从逻辑数据量推断。因此 UI 必须标示“已统计数据量”，不能作为精确磁盘总占用展示，更不能按全库大小均分。

### RAG

`rag.semantic_enabled` 为语义检索开关；`keyword_available` 表示当前域至少存在现有原文字词索引标记，不表示所有资料字词索引均完成。

`rag.layers` 为 `original/summary/topics/domain` 四层，每层包含：

```json
{"total":0,"ready":0,"pending":0,"failed":0,"disabled":0,"unavailable":0,"chunk_count":0}
```

- `ready`：当前版本有效产物的对应语义索引已完成；不使用其他域或历史版本索引充数。
- `pending`：对应索引待建立、执行中或已有数据不完整。
- `failed`：本地索引任务失败。
- `disabled`：具备对应内容，但语义索引当前关闭。
- `unavailable`：缺少可用原文、有效单篇分析或有效概览。
- `chunk_count`：当前版本、当前向量模型已存储的向量切片数量，不等同于 ready 数量。

主题和知识域概览先经 `eligible_nodes` 进行范围筛选，然后共用一次 `NodeQuerySnapshot` 的输入清单与当前版本验证，避免每个节点重新计算整个知识域快照。失效概览不会作为可检索结果计入。

## 4. 原文依据

`POST /v1/domains/{domain_id}/analysis/citations/{artifact_id}`

请求：

```json
{"scope":"app","citation_ids":["引用标识"]}
```

最多 80 个引用，每个标识最长 128 字符。只允许读取当前知识域、当前有效的已发布知识域/主题概览实际引用的依据；不提供任意加工中间节点或任意原文 ID 的读取通道。

响应：

```json
{
  "domain_id":"知识域标识",
  "scope":"app",
  "artifact_id":"概览产物标识",
  "items":[{
    "id":"引用标识",
    "source_id":"资料标识",
    "title":"资料标题",
    "revision_id":"当前原文版本",
    "evidence_id":"实际原文证据标识",
    "text":"实际引用的原文节选",
    "locator_kind":"text",
    "locator_start":0,
    "locator_end":8,
    "text_start":0,
    "text_end":8
  }]
}
```

`locator_*` 沿用原文位置协议：文本精确定位至原始证据起点加节选偏移，PDF 保留原始页码；`text_start/text_end` 是原始 `EvidenceChunk` 内的节选偏移，不能当成整篇文档的绝对字符位置。接口校验原文版本、实际节选文本与位置绑定。当前概览不可访问或引用不属于该概览返回 404；已发生内容失效则返回 409，客户端应提示刷新后重新查看，不能把旧结果当作当前事实。

## 加载与兼容性

- 所有新增接口为只读，不创建模型任务、索引任务、授权周期或自动更新策略，不调用本地嵌入推理或付费模型；仅读取已有索引与产物。
- 无新依赖、无数据库字段和 schema 迁移，不改变备份格式。
- 目录元数据批量分页，文件按展开分支分页，信息按需单次读取。分页不是跨请求强一致快照；资料在两页间变化时客户端可去重并刷新，不能据此声明事务级快照。
- 客户端应按服务地址、会话与知识域隔离缓存；同资源请求单飞；旧请求响应必须核对读取代次与范围。刷新保留已有展示，完整新结果到达后替换；切换身份或授权失效则清空缓存。
- 本轮使用隔离合成 SQLite 数据与模拟模型响应验证，未读取真实用户数据库或调用真实模型。新增 23 项测试与相邻回归共 113 项通过（26.82 秒）；含长证据窗口、非零原始证据起点、非零节选偏移、PDF 页码保持，以及旧失败/待执行/运行任务在正文或备注变更后失效的测试。有 1 项既有 Starlette 弃用警告。

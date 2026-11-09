import SwiftUI

@MainActor struct LocalDomainInformationView: View {
    @ObservedObject var store: LocalDomainOverviewStore

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 7) {
                        Text("知识域信息").font(.title2.weight(.semibold))
                        if let date = localDomainDate(store.information?.computed_at) {
                            Text("统计于 \(date) · 当前知识域").font(.callout).foregroundStyle(.secondary)
                        } else { Text("当前知识域的资料、分析与本地检索情况").font(.callout).foregroundStyle(.secondary) }
                    }
                    Spacer()
                    if store.informationLoading { ProgressView().controlSize(.small).accessibilityLabel("正在刷新知识域信息") }
                }
                if let error = store.informationError { Text(error).font(.callout).foregroundStyle(.orange) }
                if let information = store.information {
                    documentSection(information)
                    Divider()
                    storageSection(information.storage)
                    Divider()
                    classificationSection(information)
                    Divider()
                    ragSection(information.rag)
                } else if let error = store.informationError, !store.informationLoading {
                    VStack(alignment: .leading, spacing: 14) {
                        EmptyMessage(symbol: "chart.bar.doc.horizontal", title: "暂时无法读取知识域信息", detail: error)
                        Button("重新读取信息") { Task { await store.refreshSelected() } }
                    }.frame(minHeight: 320)
                } else {
                    VStack(alignment: .leading, spacing: 16) {
                        ForEach(0..<6) { _ in RoundedRectangle(cornerRadius: 5).fill(InboxStyle.elevated).frame(height: 32) }
                    }.frame(minHeight: 320, alignment: .top).accessibilityLabel("正在读取知识域信息")
                }
            }.padding(28)
        }
    }

    private func documentSection(_ information: LocalDomainInformation) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("资料与分析").font(.headline)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 170), alignment: .leading)], alignment: .leading, spacing: 18) {
                metric("文档数量", value: "\(information.documents.total)", detail: "多分类文件只计一次")
                metric("有效 AI 分析", value: "\(information.documents.analyzed) / \(information.documents.total)", detail: "当前原文与上下文仍有效")
                if let coverage = information.overview_coverage, let processed = coverage.processed_source_count {
                    metric("概览纳入", value: "\(processed) / \(coverage.source_count)", detail: "依据单篇摘要及原文节选")
                } else { metric("概览纳入", value: "暂无有效概览", detail: "与单篇分析数量分别统计") }
            }
            HStack(spacing: 16) {
                Text("未分析 \(information.documents.pending)")
                Text("需更新 \(information.documents.outdated)")
                Text("进行中 \(information.documents.running)")
                Text("失败 \(information.documents.failed)")
            }.font(.caption).foregroundStyle(.secondary)
        }
    }

    private func storageSection(_ storage: LocalDomainInformation.Storage) -> some View {
        VStack(alignment: .leading, spacing: 13) {
            Text("本地存储占用").font(.headline)
            HStack(alignment: .firstTextBaseline) {
                Text(bytes(storage.known_payload_bytes)).font(.title2.weight(.semibold)).monospacedDigit()
                Text("已统计数据量").font(.callout).foregroundStyle(.secondary)
            }
            Text("完整磁盘占用：\(storage.physical_bytes.map(bytes) ?? "暂不可按知识域准确统计")")
                .font(.callout).foregroundStyle(.secondary)
            VStack(spacing: 10) {
                detailRow("已记录附件与图片", value: bytes(storage.recorded_asset_bytes))
                detailRow("当前正文 UTF-8 数据量", value: bytes(storage.text_utf8_bytes))
                detailRow("AI 产物逻辑数据量", value: bytes(storage.ai_payload_bytes))
                detailRow("向量数据量", value: bytes(storage.vector_payload_bytes))
            }
            Text(storage.note).font(.caption).foregroundStyle(.secondary)
        }
    }

    private func classificationSection(_ information: LocalDomainInformation) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("分类与标签").font(.headline)
            HStack(alignment: .top, spacing: 50) {
                metric("分类数量", value: "\(information.topics.total)", detail: "一级 \(information.topics.roots) · 二级 \(information.topics.children)")
                metric("正式标签", value: "\(information.tags.total)", detail: "按标签去重，不含待确认建议")
            }
            Text("人工来源 \(information.tags.manual) · AI 来源 \(information.tags.ai) · 双来源 \(information.tags.both)")
                .font(.caption).foregroundStyle(.secondary)
            Text("同一标签可以同时有人工和 AI 来源，两项不能相加作为总数。“未分类”不计入分类数量。")
                .font(.caption).foregroundStyle(.secondary)
            if !information.tags.items.isEmpty {
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 120), alignment: .leading)], alignment: .leading, spacing: 10) {
                    ForEach(information.tags.items) { tag in
                        HStack(spacing: 5) {
                            Image(systemName: "tag").font(.caption)
                            Text(tag.name).font(.callout).lineLimit(2)
                        }.foregroundStyle(.secondary).padding(.horizontal, 10).padding(.vertical, 7)
                            .background(InboxStyle.elevated, in: RoundedRectangle(cornerRadius: 6))
                            .help(tag.origins.contains("manual") && tag.origins.contains("ai") ? "人工与 AI 双来源" : tag.origins.contains("ai") ? "AI 来源" : "人工来源")
                    }
                }
                if information.tags.next_offset != nil {
                    Text("当前展示前 \(information.tags.items.count) 个标签；总数按全部有效标签统计。")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func ragSection(_ rag: LocalDomainInformation.RAG) -> some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("RAG 检索准备情况").font(.headline); Spacer(); Text(rag.semantic_enabled ? "语义检索已开启" : "语义检索已关闭").font(.caption).foregroundStyle(.secondary) }
            if !rag.semantic_enabled {
                Text(rag.keyword_available == true ? "语义索引当前关闭，仍可使用已有关键词检索。" : "语义索引当前关闭；关键词检索可用性以本地配置为准。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            VStack(spacing: 0) {
                indexRow("原文索引", layer: rag.layers.original)
                indexRow("单篇摘要索引", layer: rag.layers.summary)
                indexRow("主题索引", layer: rag.layers.topics)
                indexRow("知识域索引", layer: rag.layers.domain)
            }
            Text("仅统计当前版本及有效产物。索引可用表示可参与检索，不代表回答准确；具体结论仍需核对原文。")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private func indexRow(_ title: String, layer: LocalDomainIndexLayer) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack { Text(title).font(.callout.weight(.medium)); Spacer(); Text("可用 \(layer.ready) / \(layer.total)").font(.callout).monospacedDigit() }
            HStack(spacing: 12) {
                Text("待处理 \(layer.pending)"); Text("失败 \(layer.failed)"); Text("关闭 \(layer.disabled)"); Text("无有效产物 \(layer.unavailable)")
            }.font(.caption).foregroundStyle(.secondary)
            Divider()
        }.padding(.vertical, 10)
    }

    private func metric(_ title: String, value: String, detail: String) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.callout).foregroundStyle(.secondary)
            Text(value).font(.title3.weight(.semibold)).monospacedDigit()
            Text(detail).font(.caption).foregroundStyle(.secondary)
        }.frame(maxWidth: .infinity, alignment: .leading)
    }

    private func detailRow(_ title: String, value: String) -> some View {
        HStack { Text(title).foregroundStyle(.secondary); Spacer(); Text(value).monospacedDigit() }.font(.callout)
    }

    private func bytes(_ value: Int64) -> String { ByteCountFormatter.string(fromByteCount: value, countStyle: .file) }
}

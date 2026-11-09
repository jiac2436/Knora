import SwiftUI

/// A category's reading scope is isolated from the domain tab's navigation and cache.
@MainActor struct LocalDomainTopicOverviewSheet: View {
    @ObservedObject var model: CaptureModel
    let domainID: String
    let topic: LocalDomainDirectoryTopic
    @Environment(\.dismiss) private var dismiss
    @StateObject private var store = LocalDomainOverviewStore()
    @State private var evidence: LocalDomainEvidenceSelection?
    @State private var showingQuestion = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text(topic.name).font(.title2.weight(.semibold))
                    Text("分类概览 · \(topic.source_count) 篇资料（含下级分类，已去重）").font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                Button("返回分类树") { dismiss() }
            }.padding(24)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if let navigation = store.navigation, navigation.node.id == topic.id {
                        if navigation.overview.available, let result = navigation.overview.result {
                            if let date = localDomainDate(navigation.overview.created_at) {
                                Text("更新于 \(date)").font(.caption).foregroundStyle(.secondary)
                            }
                            LocalDomainResultContent(result: result, showEvidence: { point in
                                guard let id = navigation.overview.id else { return }
                                evidence = LocalDomainEvidenceSelection(artifactID: id, point: point)
                            })
                            if let coverage = navigation.overview.coverage, let processed = coverage.processed_source_count {
                                Text("已纳入 \(processed) / \(coverage.source_count) 篇 · 依据单篇摘要及原文节选")
                                    .font(.caption).foregroundStyle(coverage.complete ? Color.secondary : .orange)
                            }
                        } else {
                            Text(navigation.overview.reason == "not_built" ? "这个分类尚未生成概览。" : "这个分类当前没有有效概览。")
                                .font(.headline)
                            Text("可以返回分类树直接阅读文件。生成或更新知识域概览时，将按确认的范围处理分类。")
                                .font(.callout).foregroundStyle(.secondary)
                        }
                        Divider()
                        Button("向这个分类提问…") { showingQuestion = true }.disabled(!model.connected)
                    } else if store.isLoading {
                        ProgressView("读取分类概览…").frame(maxWidth: .infinity, minHeight: 240)
                    } else {
                        Text(store.error ?? "暂时无法读取分类概览，请返回后重试。").foregroundStyle(.orange)
                    }
                }.padding(26)
            }
        }
        .frame(minWidth: 650, idealWidth: 820, minHeight: 530, idealHeight: 680)
        .background(InboxStyle.canvas)
        .task(id: "\(model.address)|\(model.token)|\(model.connected)|\(domainID)|\(topic.id)") {
            guard model.connected else { store.reset(); return }
            await store.configure(address: model.address, token: model.token, domainID: domainID)
            await store.selectTopic(topic.id)
        }
        .sheet(item: $evidence) { LocalDomainEvidenceView(model: model, domainID: domainID, selection: $0).buttonStyle(InboxButtonStyle(.secondary)) }
        .sheet(isPresented: $showingQuestion) {
            LocalRAGView(model: model, domainID: domainID, topicID: topic.id, scopeTitle: topic.name)
                .buttonStyle(InboxButtonStyle(.secondary))
        }
    }
}

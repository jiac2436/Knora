import SwiftUI

@MainActor struct LocalDomainTopicsView: View {
    @ObservedObject var model: CaptureModel
    let domainID: String
    @StateObject private var store = LocalDomainTopicsStore()
    @Environment(\.dismiss) private var dismiss
    @State private var editing = false
    @State private var editingID: String?
    @State private var name = ""
    @State private var parentID = ""
    @State private var sourceQuery = ""
    @State private var confirmingCreation = false
    private var busy: Bool { !model.connected || store.isLoading || store.isMutating }
    private var selected: LocalDomainTopic? { store.topics.first { $0.id == store.selectedID } }
    private var parents: [LocalDomainTopic] {
        store.topics.filter { $0.parent_id == nil && $0.id != editingID }
    }
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text("管理主题").font(.title2.weight(.semibold))
                Spacer()
                Button("刷新") { Task { await store.refresh() } }.disabled(busy)
                Button("完成") { dismiss() }.disabled(store.isMutating)
            }.padding(24)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    Text("主题用于组织本知识库的资料，可多主题归属。人工修改保存在本机，不调用模型；已授权的自动概览更新仍按原预算执行。")
                        .font(.callout).foregroundStyle(.secondary)
                    if let error = store.error { Text(error).foregroundStyle(.orange).textSelection(.enabled) }
                    if let message = store.message { Text(message).font(.caption).foregroundStyle(.secondary) }
                    if store.isLoading { ProgressView("读取主题与资料…") }
                    if store.creationUnconfirmed {
                        Text("待核对创建：\(store.unconfirmedName ?? "主题")。请刷新目录并检查原请求，确认后才能再次新建。")
                            .font(.callout).foregroundStyle(.orange)
                        Button("已核对，允许继续新建…") { confirmingCreation = true }.disabled(busy)
                    }
                    Surface {
                        VStack(alignment: .leading, spacing: 12) {
                            HStack {
                                Text("主题目录").font(.headline)
                                Spacer()
                                Button("新建主题…") { beginEdit(nil) }.disabled(busy || store.creationUnconfirmed)
                            }
                            if store.topics.isEmpty { Text("尚无主题，可以手动创建。").foregroundStyle(.secondary) }
                            ForEach(store.topics) { topic in
                                HStack(alignment: .top) {
                                    Button {
                                        Task { await store.select(topic.id) }
                                    } label: {
                                        HStack(alignment: .top) {
                                            Image(systemName: store.selectedID == topic.id ? "folder.fill" : "folder")
                                            VStack(alignment: .leading, spacing: 4) {
                                                Text(topic.name)
                                                if let parent = topic.parent_id {
                                                    Text("上级：\(store.topics.first { $0.id == parent }?.name ?? "未加载主题")").font(.caption).foregroundStyle(.secondary)
                                                }
                                            }
                                        }
                                    }.buttonStyle(.plain).disabled(busy)
                                    Spacer()
                                    Text("\(topic.source_count) 篇").font(.caption).foregroundStyle(.secondary)
                                    Button("编辑…") { beginEdit(topic) }.disabled(busy)
                                }
                            }
                        }
                    }
                    if let selected {
                        Surface {
                            VStack(alignment: .leading, spacing: 12) {
                                Text("\(selected.name) · 资料归属").font(.headline)
                                Text("勾选加入，取消勾选移出；不会删除资料，也不会改变人工标签。每次修改单独保存。")
                                    .font(.caption).foregroundStyle(.secondary)
                                TextField("在已加载资料中筛选标题", text: $sourceQuery)
                                let visible = store.sources.filter { sourceQuery.isEmpty || $0.title.localizedCaseInsensitiveContains(sourceQuery) }
                                if visible.isEmpty { Text("已加载资料中暂无匹配项。").foregroundStyle(.secondary) }
                                ForEach(visible) { source in
                                    Toggle(source.title, isOn: Binding(get: { store.members.contains(source.id) }, set: { value in
                                        Task { await store.setIncluded(sourceID: source.id, included: value) }
                                    })).disabled(busy)
                                }
                                if store.nextSourceOffset != nil {
                                    Button("加载更多资料") { Task { await store.loadMoreSources() } }.disabled(busy)
                                }
                                Text("已加载 \(store.sources.count) 篇，本主题直接收录 \(store.members.count) 篇。")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                    }
                }.padding(24)
            }
        }.frame(width: 680, height: 620).background(InboxStyle.canvas)
        .task(id: "\(model.address)|\(model.token)|\(domainID)|\(model.connected)") {
            editing = false; sourceQuery = ""
            guard model.connected else { store.reset(); return }
            await store.configure(address: model.address, token: model.token, domainID: domainID)
        }
        .sheet(isPresented: $editing) { editor.buttonStyle(InboxButtonStyle(.secondary)) }
        .alert("确认已核对创建结果", isPresented: $confirmingCreation) {
            Button("继续等待", role: .cancel) { }
            Button("已核对，继续") { store.acknowledgeUnconfirmedCreation(confirmed: true) }
        } message: { Text("原请求可能已经创建主题。确认目录和服务端结果后再继续，重复新建会产生重复主题。刷新本身不会解除限制。") }
    }
    private func beginEdit(_ topic: LocalDomainTopic?) {
        editingID = topic?.id; name = topic?.name ?? ""; parentID = topic?.parent_id ?? ""; editing = true
    }
    private var editor: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(editingID == nil ? "新建主题" : "编辑主题").font(.title2.weight(.semibold))
            TextField("主题名称", text: $name)
            InboxSelect("上级主题", selection: $parentID, options:
                [InboxSelectOption("", "无（一级主题）")] + parents.map {
                    InboxSelectOption($0.id, $0.name)
                })
            Text("支持两级主题。已有子主题的一级主题不能再设上级；后端会核验结构与归属。人工名称会固定保留。")
                .font(.caption).foregroundStyle(.secondary)
            if let error = store.error { Text(error).font(.callout).foregroundStyle(.orange) }
            HStack {
                Spacer()
                Button("取消") { editing = false }.disabled(store.isMutating)
                Button("保存") {
                    Task { if await store.save(topicID: editingID, name: name, parentID: parentID.isEmpty ? nil : parentID) { editing = false } }
                }.disabled(busy || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || name.count > 120 || (editingID == nil && store.creationUnconfirmed))
            }
        }.padding(24).frame(width: 460)
    }
}

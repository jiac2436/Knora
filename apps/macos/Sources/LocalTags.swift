import Foundation
import SwiftUI

struct LocalTag: Decodable, Identifiable {
    struct Alias: Decodable, Identifiable { let id: String; let name: String }
    let id: String
    let name: String
    let dimension: String
    let parent_id: String?
    let aliases: [Alias]
    var dimensionText: String { Self.dimensionText(dimension) }
    static func dimensionText(_ value: String) -> String {
        switch value { case "topic": "主题"; case "type": "内容类型"; case "entity": "涉及对象"; default: value }
    }
}
struct LocalSourceTag: Decodable, Identifiable {
    struct Assignment: Decodable {
        let origin: String
        let stale: Bool
        let revision_id: String?
    }
    let id: String
    let name: String
    let dimension: String
    let origins: [String]
    let stale: Bool
    let assignments: [Assignment]
    var originText: String {
        let names = origins.contains("manual") ? ["人工"] + (origins.contains("ai") ? ["AI"] : []) : origins.map { $0 == "ai" ? "AI" : $0 }
        return names.joined(separator: "、")
    }
}

@MainActor extension CaptureModel {
    var tagWriteAllowed: Bool {
        usesLocalService && connected && !busy && !browseBusy && !backupBusy && !trashBusy && !recoveringService && !aiBusy && !tagsBusy && !summaryIndexBusy && !ragBusy && !agentConnectionsBusy
    }
    func resetLocalTags() {
        tagsGeneration = UUID()
        tagsReadGeneration = UUID()
        localTags = []; sourceTags = []; legacyRuleTags = []; tagsBusy = false
        tagsMessage = "人工标签保存在本机；AI 标签需由模型分析生成。"
    }
    struct TagRequestContext {
        let generation: UUID
        let library: LocalLibraryDescriptor
        let address: String
        let token: String
        let sourceID: String?
        let sourceGeneration: UUID
    }
    func tagRequestContext(source: Bool = false) throws -> TagRequestContext {
        TagRequestContext(generation: tagsGeneration, library: try localService.currentLibrary(),
                          address: address, token: token, sourceID: source ? selectedSource?.id : nil,
                          sourceGeneration: sourceLoadGeneration)
    }
    func tagRequestCurrent(_ context: TagRequestContext) -> Bool {
        guard tagsGeneration == context.generation, connected, address == context.address, token == context.token,
              !Task.isCancelled, !backupBusy, !trashBusy,
              (try? localService.currentLibrary()) == context.library else { return false }
        return context.sourceID == nil || (selectedSource?.id == context.sourceID && sourceLoadGeneration == context.sourceGeneration)
    }
    func refreshLocalTags(source: Bool = false) async {
        guard usesLocalService, connected, !backupBusy, !trashBusy else { return }
        guard let context = try? tagRequestContext(source: source) else { return }
        let readGeneration = UUID()
        tagsReadGeneration = readGeneration
        do {
            let api = try API(address: context.address, token: context.token)
            var parsed: [LocalTag] = []
            var offset = 0
            while true {
                let catalog = try await api.call("v1/tags", query: [URLQueryItem(name: "limit", value: "100"), URLQueryItem(name: "offset", value: String(offset))])
                guard tagRequestCurrent(context), tagsReadGeneration == readGeneration else { return }
                parsed += try decodeLocalAI([LocalTag].self, body: catalog["items"] ?? [])
                guard let next = catalog["next_offset"] as? Int else { break }
                guard next > offset else { throw failure("标签目录分页响应无效。") }
                offset = next
            }
            var details: [LocalSourceTag] = [], rules: [String] = [], names: [String] = []
            if source, let id = context.sourceID {
                let response = try await api.call("v1/sources/\(id)/tags")
                details = try decodeLocalAI([LocalSourceTag].self, body: response["tag_details"] ?? [])
                rules = response["legacy_rule_tags"] as? [String] ?? []
                names = response["tags"] as? [String] ?? []
            }
            guard tagRequestCurrent(context) else { return }
            guard tagsReadGeneration == readGeneration else { return }
            localTags = parsed
            if source {
                sourceTags = details; legacyRuleTags = rules
                if let id = context.sourceID {
                    knowledgeRows = knowledgeRows.map { row in var value = row; if value.id == id { value.tags = names }; return value }
                    allKnowledgeRows = allKnowledgeRows.map { row in var value = row; if value.id == id { value.tags = names }; return value }
                }
            }
        } catch {
            guard tagRequestCurrent(context) else { return }
            guard tagsReadGeneration == readGeneration else { return }
            tagsMessage = "无法读取标签，请刷新后重试。"
        }
    }
    func mutateLocalTag(_ path: String, method: String = "POST", body: [String: Any]? = nil,
                        source: Bool = false, query: [URLQueryItem] = []) async {
        guard tagWriteAllowed, !source || selectedSource != nil else { return }
        do {
            let context = try tagRequestContext(source: source)
            tagsReadGeneration = UUID()
            tagsBusy = true
            defer { if tagsGeneration == context.generation { tagsBusy = false } }
            do {
                _ = try await API(address: context.address, token: context.token).call(path, method: method, json: body, query: query)
                guard tagRequestCurrent(context) else { return }
                tagsMessage = "标签已保存。"
                await refreshLocalTags(source: source)
            } catch {
                guard tagRequestCurrent(context) else { return }
                tagsMessage = error.localizedDescription
            }
        } catch { tagsMessage = "无法打开当前知识库。" }
    }
}

struct LocalTagCatalogView: View {
    @ObservedObject var model: CaptureModel
    @State private var name = ""
    @State private var dimension = "topic"
    @State private var parentID = ""
    @State private var editingID = ""
    @State private var editedName = ""
    @State private var editedParent = ""
    @State private var alias = ""
    @State private var mergeTarget = ""
    @State private var confirmMerge = false
    private var selected: LocalTag? { model.localTags.first { $0.id == editingID } }
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("标准标签目录").font(.headline)
            Text("标签分为主题、内容类型和涉及对象。主题支持两级；别名用于合并同义名称。目录标签本身不区分来源，资料上的标注记录区分 AI 和人工。")
                .font(.callout).foregroundStyle(.secondary)
            HStack(alignment: .bottom) {
                TextField("新标签名称", text: $name)
                InboxSelect("维度", selection: $dimension, options: [
                    InboxSelectOption("topic", "主题"),
                    InboxSelectOption("type", "内容类型"),
                    InboxSelectOption("entity", "涉及对象")
                ]).frame(width: 150)
            }
            if dimension == "topic" { parentPicker(selection: $parentID, excluding: "") }
            Button("创建标签") {
                var body: [String: Any] = ["name": name, "dimension": dimension]
                if dimension == "topic", !parentID.isEmpty { body["parent_id"] = parentID }
                Task { await model.mutateLocalTag("v1/tags", body: body) }
            }.disabled(!model.tagWriteAllowed || name.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            Divider()
            InboxSelect("编辑标签", selection: $editingID, options:
                [InboxSelectOption("", "选择标签")] + model.localTags.map {
                    InboxSelectOption($0.id, "\($0.name) · \($0.dimensionText)")
                })
            if let tag = selected {
                TextField("标签名称", text: $editedName)
                if tag.dimension == "topic" { parentPicker(selection: $editedParent, excluding: tag.id) }
                Button("保存名称与层级") {
                    let body: [String: Any] = ["name": editedName, "parent_id": editedParent.isEmpty ? NSNull() : editedParent as Any]
                    Task { await model.mutateLocalTag("v1/tags/\(tag.id)", method: "PATCH", body: body) }
                }.disabled(!model.tagWriteAllowed || editedName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                HStack {
                    TextField("添加别名", text: $alias)
                    Button("添加") {
                        let value = alias
                        Task { await model.mutateLocalTag("v1/tags/\(tag.id)/aliases", body: ["name": value]) }
                    }.disabled(!model.tagWriteAllowed || alias.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                ForEach(tag.aliases) { item in
                    HStack {
                        Text(item.name); Spacer()
                        Button("移除别名") { Task { await model.mutateLocalTag("v1/tags/\(tag.id)/aliases/\(item.id)", method: "DELETE") } }
                            .disabled(!model.tagWriteAllowed)
                    }
                }
                InboxSelect("合并到", selection: $mergeTarget, options:
                    [InboxSelectOption("", "选择同维度标签")] + model.localTags
                        .filter { $0.dimension == tag.dimension && $0.id != tag.id }
                        .map { InboxSelectOption($0.id, $0.name) })
                Button("合并标签…") { confirmMerge = true }
                    .disabled(!model.tagWriteAllowed || mergeTarget.isEmpty)
            }
            HStack { Button("刷新") { Task { await model.refreshLocalTags() } }; if model.tagsBusy { ProgressView().controlSize(.small) } }
            Text(model.tagsMessage).font(.callout).foregroundStyle(.secondary)
        }
        .task { await model.refreshLocalTags() }
        .onChange(of: editingID) { _, _ in synchronizeEditor() }
        .onChange(of: dimension) { _, _ in parentID = "" }
        .onChange(of: model.localTags.map { "\($0.id):\($0.name):\($0.parent_id ?? "")" }) { _, _ in synchronizeEditor() }
        .alert("确认合并标签？", isPresented: $confirmMerge) {
            Button("取消", role: .cancel) {}
            Button("合并", role: .destructive) {
                if let tag = selected {
                    let target = mergeTarget
                    Task { await model.mutateLocalTag("v1/tags/\(tag.id)/merge", body: ["target_id": target]) }
                }
            }
        } message: {
            Text("将 \(selected?.name ?? "") 合并到 \(model.localTags.first { $0.id == mergeTarget }?.name ?? "")。所有资料关联将转移，原名称保留为别名。此操作不能直接撤销。")
        }
    }
    private func parentPicker(selection: Binding<String>, excluding: String) -> some View {
        InboxSelect("上级主题", selection: selection, options:
            [InboxSelectOption("", "无（一级主题）")] + model.localTags
                .filter { $0.dimension == "topic" && $0.parent_id == nil && $0.id != excluding }
                .map { InboxSelectOption($0.id, $0.name) })
    }
    private func synchronizeEditor() {
        editedName = selected?.name ?? ""; editedParent = selected?.parent_id ?? ""
        alias = ""; mergeTarget = ""
    }
}

struct LocalSourceTagsView: View {
    @ObservedObject var model: CaptureModel
    @Environment(\.dismiss) private var dismiss
    @State private var selectedTagID = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("资料标签").font(.title2); Spacer(); Button("完成") { dismiss() }.disabled(model.tagsBusy) }
            Text(model.selectedSource?.title ?? "未选择资料").font(.headline)
            Text("人工添加只创建人工关联。移除一种来源不会删除同一标签的另一来源。标准标签在设置 → 标签管理中维护。")
                .font(.callout).foregroundStyle(.secondary)
            HStack(alignment: .bottom) {
                InboxSelect("标准标签", selection: $selectedTagID, options:
                    [InboxSelectOption("", "选择标签")] + model.localTags.map {
                        InboxSelectOption($0.id, "\($0.name) · \($0.dimensionText)")
                    })
                Button("人工添加") {
                    guard let source = model.selectedSource?.id else { return }
                    let id = selectedTagID
                    Task { await model.mutateLocalTag("v1/sources/\(source)/tags", body: ["tag_id": id], source: true) }
                }.disabled(!model.tagWriteAllowed || selectedTagID.isEmpty)
                Button("刷新") { Task { await model.refreshLocalTags(source: true) } }.disabled(model.tagsBusy)
            }
            Text(model.tagsMessage).font(.callout).foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    ForEach(model.sourceTags) { tag in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack {
                                Text(tag.name).font(.headline)
                                Text(LocalTag.dimensionText(tag.dimension)).foregroundStyle(.secondary)
                                Text(tag.originText).foregroundStyle(.secondary)
                            }
                            if tag.assignments.contains(where: { $0.origin == "ai" && $0.stale }) {
                                Text("AI 标注依据已过期；人工关联继续保留。").font(.caption).foregroundStyle(.secondary)
                            }
                            HStack {
                                ForEach(tag.origins, id: \.self) { origin in
                                    Button(origin == "manual" ? "移除人工标注" : "移除 AI 标注") {
                                        guard let source = model.selectedSource?.id else { return }
                                        Task { await model.mutateLocalTag("v1/sources/\(source)/tags/\(tag.id)", method: "DELETE", source: true, query: [URLQueryItem(name: "origin", value: origin)]) }
                                    }.disabled(!model.tagWriteAllowed)
                                }
                            }
                        }
                    }
                    if !model.legacyRuleTags.isEmpty {
                        Divider()
                        Text("规则标签（历史关键词匹配）").font(.headline)
                        Text(model.legacyRuleTags.joined(separator: "、")).foregroundStyle(.secondary)
                    }
                    if model.sourceTags.isEmpty { Text("暂无正式标签。可从标准目录人工添加。").foregroundStyle(.secondary) }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(22).frame(minWidth: 660, idealWidth: 780, minHeight: 420, idealHeight: 580)
        .task(id: model.selectedSource.map { "\($0.id):\($0.revision ?? 0)" }) {
            model.sourceTags = []; model.legacyRuleTags = []; selectedTagID = ""
            await model.refreshLocalTags(source: true)
        }
        .interactiveDismissDisabled(model.tagsBusy)
    }
}

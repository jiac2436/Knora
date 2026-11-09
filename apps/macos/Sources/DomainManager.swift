import SwiftUI

/// The sidebar's knowledge libraries are domains inside the current local database.
struct DomainManagerView: View {
    @ObservedObject var model: CaptureModel
    var trashOnly = false
    @State private var deleted: [DomainItem] = []
    @State private var pending: DomainItem?
    @State private var confirming = false
    @State private var working = false
    @State private var message = ""

    private var unavailable: Bool {
        !model.usesLocalService || !model.connected || working || model.busy || model.trashBusy ||
        model.backupBusy || model.browseBusy || model.statusBusy || model.recoveringService
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text(trashOnly ? "已删除的知识库" : "管理知识库").font(.title2)
            Text("删除会将知识库及库内资料一起移入回收站，不会立即清理原件。")
                .font(.callout).foregroundStyle(.secondary)
            if !model.usesLocalService {
                Text("知识库回收站仅支持本地模式。")
            } else {
                if !trashOnly {
                    ForEach(model.domains) { domain in
                        HStack {
                            Label(domain.name, systemImage: "folder")
                            if model.domainID == domain.id { Text("当前").font(.caption).foregroundStyle(.secondary) }
                            Spacer()
                            Button("删除…", role: .destructive) { pending = domain; confirming = true }
                                .disabled(unavailable)
                        }.padding(.vertical, 6)
                    }
                    if model.domains.isEmpty { Text("暂无知识库，请从知识库菜单创建。").foregroundStyle(.secondary) }
                    Divider()
                    Text("回收站").font(.headline)
                }
                ForEach(deleted) { domain in
                    HStack {
                        Label(domain.name, systemImage: "trash")
                        Spacer()
                        Button("恢复知识库") { Task { await change(domain, restoring: true) } }.disabled(unavailable)
                    }.padding(.vertical, 6)
                }
                if deleted.isEmpty { Text("没有已删除的知识库。").foregroundStyle(.secondary) }
                HStack {
                    Button("刷新") { Task { await refresh() } }.disabled(unavailable)
                    if working { ProgressView().controlSize(.small) }
                }
            }
            if !message.isEmpty { Text(message).font(.callout).textSelection(.enabled) }
        }
        .task { await refresh() }
        .confirmationDialog("删除知识库“\(pending?.name ?? "")”？", isPresented: $confirming, titleVisibility: .visible) {
            Button("移入回收站", role: .destructive) {
                guard let domain = pending else { return }
                pending = nil
                Task { await change(domain, restoring: false) }
            }
            Button("取消", role: .cancel) { pending = nil }
        } message: {
            Text("库内全部资料将一起移入回收站，可在此或设置中的回收站恢复。自动分析将停止，恢复后需重新配置。已有备份不受影响。")
        }
    }

    @MainActor private func fetch(_ api: API) async throws {
        let live = try await api.call("v1/domains")
        let trash = try await api.call("v1/domains/trash")
        func items(_ result: [String: Any]) throws -> [DomainItem] {
            guard let rows = result["items"] as? [[String: Any]] else { throw failure("知识库列表响应无效") }
            return try rows.map {
                guard let id = $0["id"] as? String, let name = $0["name"] as? String else { throw failure("知识库响应无效") }
                return DomainItem(id: id, name: name)
            }
        }
        guard model.address == api.base.absoluteString.trimmingCharacters(in: CharacterSet(charactersIn: "/")) ||
              model.address == api.base.absoluteString else { throw failure("连接已变更，请重新打开管理知识库") }
        guard model.token == api.token else { throw failure("知识库连接已变更，请重新打开管理知识库") }
        model.domains = try items(live)
        deleted = try items(trash)
    }

    @MainActor private func refresh() async {
        guard model.usesLocalService, model.connected, !working, !model.backupBusy, !model.recoveringService else { return }
        working = true
        defer { working = false }
        do { try await fetch(API(address: model.address, token: model.token)) }
        catch { message = error.localizedDescription }
    }

    @MainActor private func change(_ domain: DomainItem, restoring: Bool) async {
        guard !unavailable else { return }
        working = true; model.busy = true
        defer { working = false; model.busy = false }
        let address = model.address, token = model.token
        do {
            let api = try API(address: address, token: token)
            _ = try await api.call("v1/domains/\(domain.id)" + (restoring ? "/restore" : ""), method: restoring ? "POST" : "DELETE")
            guard model.address == address, model.token == token else { return }
            // Invalidate pending reads and cached evidence before refreshing the navigation.
            model.sourceLoadGeneration = UUID(); model.sourceListGeneration = UUID()
            model.loadingSourceID = ""; model.browseBusy = false
            model.clearKnowledgeCache()
            model.clearPDFCache()
            model.selectedSource = nil; model.selectedEvidence = nil; model.sourceRevisions = []
            model.supplementDraft = ""; model.knowledgeRows = []; model.allKnowledgeRows = []; model.nextSourceOffset = nil
            if !restoring {
                model.domains.removeAll { $0.id == domain.id }
                if model.domainID == domain.id { model.domainID = model.domains.first?.id ?? "" }
                if model.searchDomainID == domain.id { model.searchDomainID = model.domainID; model.searchProjectID = "" }
            }
            message = restoring ? "知识库已恢复；此前单独删除的资料仍留在回收站。" : "知识库及库内资料已移入回收站。"
            do { try await fetch(api) }
            catch { message += " 列表刷新失败，请点击刷新。" }
            if model.domainID.isEmpty { model.domainID = model.domains.first?.id ?? "" }
            model.busy = false
            await model.loadRecent()
        } catch { message = error.localizedDescription }
    }
}

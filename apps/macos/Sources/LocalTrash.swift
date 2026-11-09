import SwiftUI

struct LocalTrashItem: Identifiable {
    let id: String
    let title: String
    let deletedAt: String
    let contentType: String
    var canRestore: Bool
    var purgeStatus: String?

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, !id.isEmpty,
              let title = value["title"] as? String,
              let deletedAt = value["deleted_at"] as? String,
              let canRestore = value["can_restore"] as? Bool else { return nil }
        self.id = id
        self.title = title
        self.deletedAt = deletedAt
        self.contentType = value["content_type"] as? String ?? ""
        self.canRestore = canRestore
        self.purgeStatus = value["purge_status"] as? String
    }

    var statusText: String {
        switch purgeStatus {
        case "pending", "running": "正在永久删除，不能恢复"
        case "failed": "永久删除失败，可重试"
        case "domain_trashed": "请先恢复所属知识库"
        default: canRestore ? "可恢复" : "不能恢复"
        }
    }
}

extension CaptureModel {
    func resetLocalTrash() {
        if trashBusy { busy = false }
        trashGeneration = UUID()
        trashItems = []
        nextTrashOffset = nil
        trashBusy = false
        trashMessage = "回收站不会自动清理；永久删除后无法恢复。"
    }

    private var canUseLocalTrash: Bool {
        usesLocalService && connected && !trashBusy && !busy && !browseBusy &&
            !statusBusy && !backupBusy && !recoveringService && !tagsBusy && !summaryIndexBusy && !ragBusy && !agentConnectionsBusy
    }

    private func trashConnectionMatches(_ generation: UUID, address: String, token: String) -> Bool {
        trashGeneration == generation && usesLocalService && connected &&
            self.address == address && self.token == token
    }

    private func fetchTrash(api: API, offset: Int) async throws -> ([LocalTrashItem], Int?) {
        let response = try await api.call("v1/trash", query: [
            URLQueryItem(name: "limit", value: "50"), URLQueryItem(name: "offset", value: String(offset))
        ])
        guard let records = response["items"] as? [[String: Any]] else {
            throw failure("回收站响应无效，请刷新重试。")
        }
        let rows = records.compactMap(LocalTrashItem.init)
        guard rows.count == records.count else { throw failure("回收站资料响应无效，请刷新重试。") }
        let next = response["next_offset"] as? Int
        guard next == nil || next! > offset else { throw failure("回收站分页响应无效，请刷新重试。") }
        return (rows, next)
    }

    func loadLocalTrash(more: Bool = false) async {
        guard canUseLocalTrash else { return }
        if more && nextTrashOffset == nil { return }
        let generation = UUID()
        trashGeneration = generation
        let connection = (address: address, token: token)
        trashBusy = true
        busy = true
        defer { if trashGeneration == generation { trashBusy = false; busy = false } }
        do {
            let (rows, next) = try await fetchTrash(api: API(address: connection.address, token: connection.token),
                                                   offset: more ? nextTrashOffset! : 0)
            guard trashConnectionMatches(generation, address: connection.address, token: connection.token) else { return }
            if more {
                let existing = Set(trashItems.map(\.id))
                trashItems.append(contentsOf: rows.filter { !existing.contains($0.id) })
            } else { trashItems = rows }
            nextTrashOffset = next
            trashMessage = trashItems.isEmpty ? "回收站为空。" : "已显示 \(trashItems.count) 条资料；不会自动清理。"
        } catch {
            if trashConnectionMatches(generation, address: connection.address, token: connection.token) {
                trashMessage = "读取回收站失败：\(error.localizedDescription)"
            }
        }
    }

    func restoreLocalTrash(_ id: String) async { await changeLocalTrash(id, permanently: false) }
    func purgeLocalTrash(_ id: String) async { await changeLocalTrash(id, permanently: true) }

    private func changeLocalTrash(_ id: String, permanently: Bool) async {
        guard canUseLocalTrash, let item = trashItems.first(where: { $0.id == id }),
              permanently || item.canRestore else { return }
        if permanently && ["pending", "running"].contains(item.purgeStatus ?? "") { return }
        let generation = UUID()
        trashGeneration = generation
        let connection = (address: address, token: token)
        trashBusy = true
        busy = true
        defer { if trashGeneration == generation { trashBusy = false; busy = false } }
        do {
            let api = try API(address: connection.address, token: connection.token)
            _ = try await api.call(permanently ? "v1/trash/\(id)" : "v1/trash/\(id)/restore",
                                   method: permanently ? "DELETE" : "POST")
            guard trashConnectionMatches(generation, address: connection.address, token: connection.token) else { return }
            if permanently, let index = trashItems.firstIndex(where: { $0.id == id }) {
                trashItems[index].canRestore = false
                trashItems[index].purgeStatus = "pending"
            } else if !permanently {
                trashItems.removeAll { $0.id == id }
            }
            nextTrashOffset = nil
            trashMessage = permanently ? "已提交永久删除，后台处理期间不能撤销；可刷新查看结果。" : "资料已恢复。"
            do {
                let (rows, next) = try await fetchTrash(api: api, offset: 0)
                guard trashConnectionMatches(generation, address: connection.address, token: connection.token) else { return }
                trashItems = rows
                nextTrashOffset = next
            } catch {
                guard trashConnectionMatches(generation, address: connection.address, token: connection.token) else { return }
                trashMessage += " 列表刷新失败，请手动刷新。"
            }
            if !permanently {
                trashBusy = false
                busy = false
                await loadRecent()
            }
        } catch {
            if trashConnectionMatches(generation, address: connection.address, token: connection.token) {
                trashMessage = "\(permanently ? "永久删除" : "恢复")失败：\(error.localizedDescription)"
            }
        }
    }
}

struct LocalTrashView: View {
    @ObservedObject var model: CaptureModel
    @State private var pendingPurge: LocalTrashItem?
    @State private var confirmPurge = false

    private var unavailable: Bool {
        !model.connected || model.trashBusy || model.busy || model.browseBusy ||
            model.statusBusy || model.backupBusy || model.recoveringService || model.tagsBusy || model.summaryIndexBusy || model.ragBusy
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("删除的本地资料保留在此，可恢复原件、版本和补充。不会按天自动清理。")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                Button("刷新回收站") { Task { await model.loadLocalTrash() } }.disabled(unavailable)
                if model.trashBusy { ProgressView().controlSize(.small) }
            }
            DomainManagerView(model: model, trashOnly: true)
            Divider()
            ForEach(model.trashItems) { item in
                VStack(alignment: .leading, spacing: 8) {
                    Text(item.title.isEmpty ? "未命名资料" : item.title).font(.headline)
                    Text("删除时间：\(item.deletedAt)").font(.caption).foregroundStyle(.secondary)
                    Text(item.statusText).font(.callout).foregroundStyle(.secondary)
                    HStack {
                        Button("恢复") { Task { await model.restoreLocalTrash(item.id) } }
                            .disabled(unavailable || !item.canRestore)
                        Button("永久删除…", role: .destructive) {
                            pendingPurge = item
                            confirmPurge = true
                        }
                            .disabled(unavailable || ["pending", "running"].contains(item.purgeStatus ?? ""))
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading).padding(14)
                .background(InboxStyle.elevated, in: RoundedRectangle(cornerRadius: 10))
            }
            if model.nextTrashOffset != nil {
                Button("加载更多") { Task { await model.loadLocalTrash(more: true) } }.disabled(unavailable)
            }
            Text(model.trashMessage).font(.callout).textSelection(.enabled)
        }
        .task { await model.loadLocalTrash() }
        .confirmationDialog("永久删除这条资料？", isPresented: $confirmPurge, titleVisibility: .visible) {
            Button("永久删除", role: .destructive) {
                guard let item = pendingPurge else { return }
                pendingPurge = nil
                Task { await model.purgeLocalTrash(item.id) }
            }
            Button("取消", role: .cancel) { pendingPurge = nil }
        } message: {
            Text("将永久清理资料“\(pendingPurge?.title ?? "")”及关联原件、版本和补充。提交后不能撤销。已有备份中的资料会保留。")
        }
    }
}

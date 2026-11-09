import Foundation

extension CaptureModel {
    private var captureFeishuOptions: [String: Any] {
        ["executable": feishu.executablePath.trimmingCharacters(in: .whitespacesAndNewlines),
         "profile": feishu.profile.trimmingCharacters(in: .whitespacesAndNewlines)]
    }

    private func currentCapture(_ api: API, target: String, root: URL) -> Bool {
        connected && api.token == token && captureTarget == target && captureDraftStore?.root == root
    }

    /// Keep displayed rows while refreshing; never clear the list on a transient error.
    func restoreKernelCaptures() async {
        guard captureDraftsReadable, let store = captureDraftStore, let target = captureTarget else { return }
        let requestToken = token
        do {
            let api = try API(address: address, token: token)
            let migrated = try await api.call("v1/capture-queue/migrate", method: "POST", json: captureFeishuOptions)
            guard currentCapture(api, target: target, root: store.root) else { return }
            try applyKernelCaptures(migrated["items"] as? [[String: Any]] ?? [], target: target)
            await refreshKernelCaptures()
            if let errors = migrated["errors"] as? [[String: Any]], !errors.isEmpty,
               currentCapture(api, target: target, root: store.root) {
                var updated = items
                for error in errors {
                    if let id = error["id"] as? String, let index = updated.firstIndex(where: { $0.id == id }) {
                        updated[index].status = "迁移失败：" + (error["message"] as? String ?? "原件已保留")
                    }
                }
                items = updated
                _ = persistQueue()
                queueMessage = errors.compactMap { $0["message"] as? String }.joined(separator: "；")
            }
        } catch {
            if captureTarget == target && captureDraftStore?.root == store.root && token == requestToken {
                queueMessage = "采集队列交接未完成，旧队列和原件已保留：\(error.localizedDescription)"
            }
        }
    }

    func refreshKernelCaptures() async {
        guard captureDraftsReadable, let store = captureDraftStore, let target = captureTarget else { return }
        let requestToken = token
        do {
            let api = try API(address: address, token: token)
            var offset: Int? = 0
            var rows: [[String: Any]] = []
            var warnings: [String] = []
            repeat {
                try Task.checkCancellation()
                let page = try await api.call("v1/capture-queue", query: [URLQueryItem(name: "offset", value: String(offset!))], timeout: 12)
                guard currentCapture(api, target: target, root: store.root) else { return }
                rows += page["items"] as? [[String: Any]] ?? []
                warnings += page["warnings"] as? [String] ?? []
                offset = page["next_offset"] as? Int
            } while offset != nil
            try applyKernelCaptures(rows, target: target)
            queueMessage = Array(Set(warnings)).joined(separator: "；")
        } catch {
            if !Task.isCancelled && captureTarget == target && captureDraftStore?.root == store.root && token == requestToken {
                queueMessage = "后台采集状态暂不可用，已保留当前显示：\(error.localizedDescription)"
            }
        }
    }

    private func applyKernelCaptures(_ rows: [[String: Any]], target: String) throws {
        let previous = items
        var updated = items
        var changed = false
        var received: [CaptureItem] = []
        for row in rows {
            guard row["accepted"] as? Bool == true, row["hidden"] as? Bool != true,
                  let id = row["id"] as? String, UUID(uuidString: id) != nil,
                  let state = row["state"] as? String, ["queued", "running", "completed", "failed"].contains(state),
                  let receipt = row["fingerprint"] as? String, receipt.count == 64 else {
                throw failure("采集回执格式无效，原件已保留。")
            }
            let index: Int
            if let existing = updated.firstIndex(where: { $0.id.lowercased() == id.lowercased() }) {
                index = existing
                guard updated[index].destination == nil || updated[index].destination == target else {
                    throw failure("采集回执与当前账户不一致，原件已保留。")
                }
                if updated[index].kernelJobID == nil { received.append(updated[index]) }
            } else {
                var item = CaptureItem(kind: CaptureKind(serverValue: row["kind"] as? String ?? "text"),
                    title: row["title"] as? String ?? "采集资料", text: nil, file: nil)
                item.id = id
                updated.append(item)
                index = updated.count - 1
            }
            if let previousReceipt = updated[index].kernelReceipt, previousReceipt != receipt {
                throw failure("采集回执与已接收内容不一致，原件已保留。")
            }
            let label = state == "queued" ? "正在等待后台归档…" : (row["message"] as? String ?? "正在等待后台归档…")
            if updated[index].kernelJobID == id && updated[index].kernelState == state &&
                updated[index].kernelReceipt == receipt && updated[index].title == row["title"] as? String &&
                updated[index].sourceID == row["source_id"] as? String && updated[index].status == label &&
                updated[index].domainID == row["domain_id"] as? String &&
                updated[index].projectID == (row["project_id"] as? String ?? "") &&
                updated[index].text == nil && updated[index].file == nil && updated[index].feishuDocument == nil { continue }
            changed = true
            updated[index].kernelJobID = id
            updated[index].kernelState = state
            updated[index].kernelReceipt = receipt
            updated[index].destination = target
            updated[index].domainID = row["domain_id"] as? String
            updated[index].projectID = row["project_id"] as? String ?? ""
            updated[index].sourceID = row["source_id"] as? String
            updated[index].title = row["title"] as? String ?? updated[index].title
            updated[index].status = row["message"] as? String ?? "正在等待后台归档…"
            if state == "queued" { updated[index].status = "正在等待后台归档…" }
            updated[index].sent = state == "completed"
            updated[index].file = nil
            updated[index].text = nil
            updated[index].feishuDocument = nil
            updated[index].uploadedImageIDs = []
        }
        guard changed else { return }
        items = updated
        guard persistQueue() else { items = previous; throw failure(queueMessage) }
        // The kernel receipt and its local reference are both durable before cleanup.
        for item in received { captureDraftStore?.removeAssets(item) }
    }

    func sendKernelCaptures() async {
        guard connected, !busy, !trashBusy, captureDraftsReadable else { return }
        addDraft()
        guard persistQueue() else { message = queueMessage; return }
        busy = true
        defer { busy = false }
        do {
            try await ensureCaptureDomain()
            guard let store = captureDraftStore, let target = captureTarget else { throw failure("本地采集目录未就绪。") }
            let api = try API(address: address, token: token)
            let pending = items.filter { !$0.sent }
            for item in pending {
                guard currentCapture(api, target: target, root: store.root),
                      let index = items.firstIndex(where: { $0.id == item.id }) else { return }
                if let kernelID = item.kernelJobID {
                    if item.kernelState == "failed" {
                        let result = try await api.call("v1/capture-queue/\(kernelID)/retry", method: "POST")
                        guard currentCapture(api, target: target, root: store.root) else { return }
                        try applyKernelCaptures([result], target: target)
                    }
                    continue
                }
                if let previous = item.destination, previous != target {
                    items[index].status = "账户或后台已变更，请移除后重新添加"; continue
                }
                if item.kind == .link, let link = URL(string: item.text ?? ""), FeishuCLI.isFeishuLink(link), feishu.busy {
                    items[index].status = "发送失败：请等待飞书连接操作完成后重试"
                    _ = persistQueue()
                    continue
                }
                items[index].destination = target
                if items[index].domainID?.isEmpty != false { items[index].domainID = domainID }
                if items[index].projectID == nil { items[index].projectID = projectID }
                guard persistQueue() else { throw failure(queueMessage) }
                let frozen = items[index]
                var body: [String: Any] = ["version": 1, "id": frozen.id, "kind": frozen.kind.rawValue,
                    "title": frozen.title, "text": frozen.text ?? "", "domain_id": frozen.domainID!,
                    "feishu": captureFeishuOptions]
                if let project = frozen.projectID, !project.isEmpty { body["project_id"] = project }
                if let file = frozen.file {
                    guard store.owns(file) else { throw failure("采集暂存原件不属于当前知识库。") }
                    let prefix = store.files.path + "/"
                    let path = file.path
                    guard path.hasPrefix(prefix) else { throw failure("采集暂存路径无效。") }
                    var asset: [String: Any] = ["key": String(path.dropFirst(prefix.count))]
                    if let digest = frozen.fileDigest { asset["sha256"] = digest }
                    body["file"] = asset
                }
                items[index].status = "正在发送到本地后台…"
                do {
                    let result = try await api.call("v1/capture-queue", method: "POST", json: body)
                    guard currentCapture(api, target: target, root: store.root) else { return }
                    try applyKernelCaptures([result], target: target)
                } catch {
                    guard currentCapture(api, target: target, root: store.root),
                          let current = items.firstIndex(where: { $0.id == item.id }) else { return }
                    items[current].status = "发送失败：\(error.localizedDescription)"
                    _ = persistQueue()
                }
            }
            message = "已交给后台归档，可继续浏览；失败条目可点击发送重试。"
        } catch { message = error.localizedDescription }
        await refreshKernelCaptures()
    }

    func removeKernelCaptures(_ selected: [CaptureItem]) async {
        guard connected, !busy, let target = captureTarget, let store = captureDraftStore else { return }
        busy = true
        defer { busy = false }
        do {
            let api = try API(address: address, token: token)
            for item in selected {
                if let id = item.kernelJobID { _ = try await api.call("v1/capture-queue/\(id)", method: "DELETE") }
                guard currentCapture(api, target: target, root: store.root) else { return }
                let previous = items
                items.removeAll { $0.id == item.id }
                guard persistQueue() else { items = previous; throw failure(queueMessage) }
                store.removeAssets(item)
            }
        } catch { queueMessage = error.localizedDescription }
    }
}

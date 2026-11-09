import Foundation
import SwiftUI

extension SourceImageItem {
    var isWebImage: Bool { ["wechat_ua", "browser_public"].contains(retrievalMethod) }
    var isDownloading: Bool { isWebImage && ["pending", "running"].contains(status) }
}

@MainActor extension CaptureModel {
    func pollSelectedWebImages(_ id: String) async {
        let endpoint = address, credential = token, generation = sourceLoadGeneration
        let revision = selectedSource?.revision
        var failures = 0
        while selectedSource?.id == id && selectedSource?.images.contains(where: \.isDownloading) == true {
            do { try await Task.sleep(for: .seconds(2)) } catch { return }
            guard connected, address == endpoint, token == credential,
                  sourceLoadGeneration == generation, !backupBusy, !trashBusy else { return }
            do {
                let response = try await API(address: endpoint, token: credential).call("v1/sources/\(id)")
                guard !Task.isCancelled, connected, address == endpoint, token == credential,
                      sourceLoadGeneration == generation, selectedSource?.id == id,
                      selectedSource?.revision == revision else { return }
                guard let updated = KnowledgeDetail(response), updated.revision == revision else { return }
                rememberKnowledge(updated, revisions: sourceRevisions)
                selectedSource = updated
                failures = 0
            } catch {
                failures += 1
                if failures >= 3 { return }
            }
        }
    }

    func retryWebImages(sourceID: String, imageID: String? = nil) async throws {
        guard connected, !backupBusy, !trashBusy else { throw failure("请先连接知识库并等待当前操作完成。") }
        let endpoint = address, credential = token, generation = sourceLoadGeneration
        var body: [String: Any] = [:]
        if let imageID { body["image_id"] = imageID }
        let api = try API(address: endpoint, token: credential)
        _ = try await api.call("v1/sources/\(sourceID)/images/retry", method: "POST", json: body)
        let response = try await api.call("v1/sources/\(sourceID)")
        guard !Task.isCancelled, connected, address == endpoint, token == credential,
              sourceLoadGeneration == generation, selectedSource?.id == sourceID,
              let updated = KnowledgeDetail(response), selectedSource?.revision == updated.revision else { return }
        rememberKnowledge(updated, revisions: sourceRevisions)
        selectedSource = updated
    }
}

struct WebImageProgressView: View {
    @ObservedObject var model: CaptureModel
    let detail: KnowledgeDetail

    var body: some View {
        let images = detail.images.filter(\.isWebImage)
        let saved = images.filter { $0.status == "saved" }.count
        let failed = images.filter { $0.status == "unavailable" }.count
        HStack(spacing: 10) {
            Text("图片已保存 \(saved) / \(images.count)").font(.caption).foregroundStyle(.secondary)
            if images.contains(where: \.isDownloading) { ProgressView().controlSize(.small) }
            if failed > 0 {
                Button("重试 \(failed) 张失败图片") {
                    Task {
                        do { try await model.retryWebImages(sourceID: detail.id) }
                        catch { model.browseMessage = error.localizedDescription }
                    }
                }.disabled(!model.connected || model.backupBusy || model.trashBusy)
            }
        }
    }
}

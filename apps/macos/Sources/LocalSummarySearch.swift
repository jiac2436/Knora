import Foundation
import SwiftUI

@MainActor extension CaptureModel {
    var summaryIndexWriteAllowed: Bool {
        usesLocalService && connected && !summaryIndexBusy && !ragBusy && !agentConnectionsBusy && !busy && !browseBusy &&
        !backupBusy && !trashBusy && !recoveringService && !aiBusy && !tagsBusy
    }

    @discardableResult
    func rebuildSummarySearchIndex(sourceID: String? = nil) async -> String? {
        guard summaryIndexWriteAllowed, let library = try? localService.currentLibrary() else { return nil }
        let originalAddress = address
        let originalToken = token
        let listGeneration = sourceListGeneration
        let sourceGeneration = sourceLoadGeneration
        let originalSource = selectedSource?.id
        summaryIndexBusy = true
        defer {
            if address == originalAddress, token == originalToken,
               (try? localService.currentLibrary()) == library {
                summaryIndexBusy = false
            }
        }
        func isCurrent() -> Bool {
            !Task.isCancelled && connected && address == originalAddress && token == originalToken &&
            (try? localService.currentLibrary()) == library && sourceListGeneration == listGeneration &&
            sourceLoadGeneration == sourceGeneration && selectedSource?.id == originalSource && !backupBusy && !trashBusy
        }
        do {
            var body: [String: Any] = [:]
            if let sourceID { body["source_id"] = sourceID }
            _ = try await API(address: originalAddress, token: originalToken)
                .call("v1/summary-index/rebuild", method: "POST", json: body)
            guard isCurrent() else { return nil }
            browseMessage = "已提交本地总结索引重建；不会调用大模型，请稍后重新搜索。"
            return browseMessage
        } catch {
            guard isCurrent() else { return nil }
            browseMessage = error.localizedDescription
            return browseMessage
        }
    }
}

struct SummarySearchMatchView: View {
    let row: KnowledgeRow
    var body: some View {
        VStack(alignment: .leading, spacing: 3) {
            if !row.matchSources.isEmpty {
                Text(row.matchSources.map { value in
                    switch value { case "title": "标题"; case "original", "evidence", "text": "原文"; case "summary": "总结"; case "tag", "tags": "标签"; default: value }
                }.joined(separator: " · ") + "命中")
                    .font(.caption2).foregroundStyle(.secondary)
            }
            if let snippet = row.summarySnippet, !snippet.isEmpty {
                Text(snippet).lineLimit(2).font(.caption).foregroundStyle(.secondary)
                Text("总结线索，事实依据请看原文" + (row.summaryCoverage == "partial" ? " · 部分资料总结" : ""))
                    .font(.caption2).foregroundStyle(.secondary)
            }
        }
    }
}

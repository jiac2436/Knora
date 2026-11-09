import Foundation

@main struct LocalAIProductTest {
    @MainActor static func main() throws {
        let legacy: [String: Any] = ["id": "job", "source_id": "source", "kind": "organize", "status": "completed"]
        let old = try decodeLocalAI(LocalAIJob.self, body: legacy)
        precondition(old.contract_version == nil && !old.isActive)
        precondition(old.statusText == "历史分析完成")
        var current = legacy
        current["contract_version"] = 3
        let saved = try decodeLocalAI(LocalAIJob.self, body: current)
        precondition(saved.statusText != "分析完成，已更新知识检索")
        current["pipeline_status"] = "indexing"; current["index_state"] = "pending"
        let indexing = try decodeLocalAI(LocalAIJob.self, body: current)
        precondition(indexing.isActive && indexing.statusText == "正在更新知识检索")
        current["pipeline_status"] = "index_failed"; current["index_state"] = "failed"; current["index_retryable"] = true
        let failed = try decodeLocalAI(LocalAIJob.self, body: current)
        precondition(!failed.isActive && failed.index_retryable == true && failed.statusText == "分析已保存，检索更新失败")
        current["pipeline_status"] = "completed"; current["index_state"] = "ready"
        let completed = try decodeLocalAI(LocalAIJob.self, body: current)
        precondition(completed.statusText == "分析完成，已更新知识检索")
        current["pipeline_status"] = "stale"
        let stale = try decodeLocalAI(LocalAIJob.self, body: current)
        precondition(stale.statusText == "资料已变更，请重新分析")
        let artifactBody: [String: Any] = [
            "id": "artifact", "source_id": "source", "result": ["summary": "历史摘要", "summary_citations": [], "insights": [], "warnings": []],
            "evidence": [], "provider": "fixture", "model": "fixture", "stale": false
        ]
        let artifact = try decodeLocalAI(LocalAIArtifact.self, body: artifactBody)
        precondition(artifact.result.contract_version == nil && artifact.index_state == nil && artifact.result.summary == "历史摘要")
        print("AI 产品 DTO：历史兼容、索引状态与资料过期通过")
    }
}

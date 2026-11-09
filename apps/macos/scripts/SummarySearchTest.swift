import Foundation
@main struct SummarySearchContract {
    static func main() throws {
        let original: [String: Any] = ["source_id": "source", "title": "合成资料", "snippet": "真实原文", "evidence_id": "original-evidence", "locator": ["kind": "text_range", "start": 5, "end": 9]]
        guard let legacy = KnowledgeRow(original, searchResult: true) else { fatalError("旧响应不兼容") }
        precondition(legacy.matchSources.isEmpty && legacy.summarySnippet == nil && legacy.summaryArtifactID == nil && legacy.summaryCoverage == nil)
        var body = original
        body["match_sources"] = ["original", "summary", "tag"]
        body["summary_snippet"] = "摘要中的检索线索"
        body["summary_artifact_id"] = "artifact"
        body["summary_coverage"] = "partial"
        guard let row = KnowledgeRow(body, searchResult: true) else { fatalError("新响应无法解析") }
        precondition(row.snippet == "真实原文" && row.evidenceID == "original-evidence")
        precondition(row.matchSources == ["original", "summary", "tag"] && row.summarySnippet == "摘要中的检索线索" && row.summaryArtifactID == "artifact" && row.summaryCoverage == "partial")
        precondition(row.locatorStart == 5 && row.locatorEnd == 9)
        let listRow = KnowledgeRow(["id": "source", "title": "合成列表", "summary_snippet": "不应展示"], searchResult: false)!
        precondition(listRow.summarySnippet == nil && listRow.matchSources.isEmpty)
        print("PASS: 旧检索响应兼容、新总结字段、原文片段与真实证据保持独立、列表不误显示检索总结")
    }
}

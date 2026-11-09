import AppKit
import SwiftUI

// Dedicated bundle, temporary home and URLProtocol isolate every read from user data.
// Full-analysis requests are simulated entirely in memory; real model/authorization calls are rejected.
private enum OverviewPreviewFixture {
    private static let lock = NSLock()
    private static var fullJob: [String: Any]?
    private static var fullPolls = 0
    static var fullCompleted: Bool { fullJob?["status"] as? String == "completed" }
    static let titles = ["分层检索的实践与评估", "知识加工的预算与边界", "引用原文的交互设计", "召回评估与证据核验", "个人知识产品的分类方法", "尚待整理的灵感笔记"]
    static let original = "分层概览帮助定位主题，具体问题仍应回到原文证据。\n\n检索质量要同时评估细节召回和跨资料理解。\n\n这是隔离界面验收使用的合成资料，不包含用户的真实内容。"
    static func source(_ index: Int) -> [String: Any] {
        ["id": "synthetic-source-\(index)", "title": titles[index - 1], "revision_id": "synthetic-revision-\(index)"]
    }
    static func point(_ text: String) -> [String: Any] { ["text": text, "citations": ["synthetic-citation"]] }
    static func coverage(_ count: Int = 6) -> [String: Any] {
        ["source_count": count, "processed_source_count": fullCompleted ? count : min(4, count), "cited_source_count": min(3, count),
         "original_text_complete": false, "excluded": count == 6 && !fullCompleted ? ["synthetic-source-5": "no_valid_summary", "synthetic-source-6": "no_valid_summary"] : [:]]
    }
    static func layer(_ total: Int, _ ready: Int, unavailable: Int = 0) -> [String: Any] {
        ["total": total, "ready": ready, "pending": total - ready - unavailable, "failed": 0,
         "disabled": 0, "unavailable": unavailable, "chunk_count": ready * 2]
    }
    static func fullDocument(_ index: Int, status: String) -> [String: Any] {
        ["source_id": "synthetic-source-\(index)", "title": titles[index - 1], "status": status]
    }
    static func makeFullJob(id: String, poll: Int = 0, cancelled: Bool = false) -> [String: Any] {
        let completed = poll >= 9 && !cancelled
        let documentsReady = poll >= 5
        let phase = completed ? "completed" : poll >= 7 ? "indexing" : documentsReady ? "overview" : "documents"
        let status = cancelled ? "cancelled" : completed ? "completed" : poll == 0 ? "pending" : "running"
        var documents = (1...4).map { fullDocument($0, status: "reused") }
        documents.append(fullDocument(5, status: cancelled ? "cancelled" : poll >= 3 ? "completed" : poll > 0 ? "running" : "waiting"))
        documents.append(fullDocument(6, status: cancelled ? "cancelled" : documentsReady ? "completed" : poll >= 3 ? "running" : "waiting"))
        return ["id": id, "request_id": id, "status": status, "phase": phase,
            "total_sources": 6, "reused_sources": 4, "analyzed_sources": documentsReady ? 2 : poll >= 3 ? 1 : 0,
            "completed_sources": documentsReady ? 6 : poll >= 3 ? 5 : 4,
            "current_source_title": phase == "documents" && poll > 0 && !cancelled ? titles[poll >= 3 ? 5 : 4] : NSNull(),
            "build_id": documentsReady ? "synthetic-full-build" : NSNull(), "last_error": cancelled ? "用户停止后续步骤（内存模拟）" : NSNull(),
            "created_at": "2026-10-16T02:00:00Z", "completed_at": completed || cancelled ? "2026-10-16T02:01:00Z" : NSNull(),
            "budget": ["calls": 258, "input_token_reservation": 25700000,
                "used_calls": completed ? 5 : documentsReady ? 3 : poll > 0 ? 1 : 0,
                "used_input_tokens": completed ? 250000 : documentsReady ? 150000 : poll > 0 ? 50000 : 0,
                "actual_input_tokens": completed ? 12600 : documentsReady ? 6500 : 0], "documents": documents]
    }
    static func response(_ path: String, _ body: [String: Any]? = nil, method: String = "GET") -> [String: Any]? {
        lock.lock(); defer { lock.unlock() }
        if path.contains("/analysis/full/") {
            if path.hasSuffix("/latest") { return ["job": fullJob ?? NSNull() as Any] }
            if path.hasSuffix("/preview") {
                return ["preview_hash": "synthetic-full-preview", "total_sources": 6, "reused_sources": 4,
                    "analyze_sources": 2, "blocked_sources": [], "calls": 258, "input_token_reservation": 25700000,
                    "budget_basis": "bounded_upper_limit", "configured": true, "requires_unknown_confirmation": false,
                    "documents": (1...6).map { fullDocument($0, status: $0 <= 4 ? "reused" : "analyze") }]
            }
            if path.hasSuffix("/jobs") && method == "POST" {
                guard body?["confirmed"] as? Bool == true, body?["preview_hash"] as? String == "synthetic-full-preview",
                      let id = body?["request_id"] as? String else { return nil }
                if fullJob?["id"] as? String != id { fullPolls = 0; fullJob = makeFullJob(id: id) }
                return fullJob
            }
            if path.hasSuffix("/cancel"), let job = fullJob, let id = job["id"] as? String {
                fullJob = makeFullJob(id: id, poll: fullPolls, cancelled: true)
                return fullJob
            }
            if path.contains("/jobs/"), method == "GET", let job = fullJob, let id = job["id"] as? String {
                if ["pending", "running"].contains(job["status"] as? String ?? "") {
                    fullPolls += 1; fullJob = makeFullJob(id: id, poll: fullPolls)
                }
                return fullJob
            }
            return nil
        }
        switch path {
        case "/v1/me": return ["id": "preview-owner", "display_name": "界面验收"]
        case "/v1/domains": return ["items": [["id": "preview-domain", "name": "AI 产品实践"]]]
        case "/v1/projects": return ["items": []]
        case "/v1/ocr/preferences": return ["model": "small", "engine": "pdfium"]
        case "/v1/web/preferences": return ["engine": "legacy", "cleaning_enabled": false]
        default: break
        }
        if path.hasSuffix("/analysis/library") {
            return ["domain_id": "preview-domain", "scope": "app", "source_count": 6, "topic_count": 3,
                "unclassified_count": 1, "next_offset": NSNull(), "items": [
                    ["id": "topic-a", "name": "检索与证据", "parent_id": NSNull(), "source_count": 4, "direct_source_count": 3],
                    ["id": "topic-a-child", "name": "评估方法", "parent_id": "topic-a", "source_count": 2, "direct_source_count": 2],
                    ["id": "topic-b", "name": "知识加工与产品体验", "parent_id": NSNull(), "source_count": 2, "direct_source_count": 2]]]
        }
        if path.hasSuffix("/library/sources") {
            let topic = body?["topic_id"] as? String
            let unclassified = body?["unclassified"] as? Bool ?? false
            let ids = unclassified ? [6] : topic == "topic-a" ? [1, 2, 3] : topic == "topic-a-child" ? [1, 4] : topic == "topic-b" ? [3, 5] : Array(1...6)
            let offset = body?["offset"] as? Int ?? 0
            let items = Array(ids.dropFirst(offset).prefix(2)).map(source)
            return ["domain_id": "preview-domain", "scope": "app", "topic_id": topic as Any? ?? NSNull(),
                "unclassified": unclassified, "items": items, "total_count": ids.count,
                "next_offset": offset + items.count < ids.count ? offset + items.count : NSNull()]
        }
        if path.hasSuffix("/information") {
            return ["domain_id": "preview-domain", "scope": "app", "computed_at": "2026-10-15T06:30:00Z",
                "documents": ["total": 6, "analyzed": fullCompleted ? 6 : 4, "pending": fullCompleted ? 0 : 1, "outdated": fullCompleted ? 0 : 1, "running": 0, "failed": 0],
                "topics": ["total": 3, "roots": 2, "children": 1],
                "tags": ["total": 4, "manual": 2, "ai": 3, "both": 1, "items": [
                    ["id": "tag-a", "name": "知识检索", "origins": ["manual", "ai"]],
                    ["id": "tag-b", "name": "证据核验", "origins": ["ai"]],
                    ["id": "tag-c", "name": "产品设计", "origins": ["manual"]],
                    ["id": "tag-d", "name": "知识加工", "origins": ["ai"]]], "next_offset": NSNull()],
                "storage": ["total_bytes": NSNull(), "physical_bytes": NSNull(), "recorded_asset_bytes": 204800,
                    "text_utf8_bytes": 16000, "ai_payload_bytes": 6000, "vector_payload_bytes": 12000,
                    "known_payload_bytes": 238800, "measurement": "logical_partial",
                    "note": "记录的附件大小及逻辑数据量；不含 SQLite 页、索引、历史版本等物理开销。"],
                "rag": ["semantic_enabled": true, "keyword_available": true, "layers": ["original": layer(6, 6),
                    "summary": layer(6, fullCompleted ? 6 : 4, unavailable: fullCompleted ? 0 : 2), "topics": layer(3, 3), "domain": layer(1, 1)]],
                "overview_coverage": coverage()]
        }
        if path.contains("/citations/") {
            return ["domain_id": "preview-domain", "scope": "app", "artifact_id": "preview-artifact", "items": [
                ["id": "synthetic-citation", "source_id": "synthetic-source-1", "title": titles[0],
                 "revision_id": "synthetic-revision-1", "evidence_id": "synthetic-evidence", "text": original,
                 "locator_kind": "text", "locator_start": 0, "locator_end": original.count,
                 "text_start": 0, "text_end": original.count]]]
        }
        if path.hasPrefix("/v1/sources/synthetic-source-") {
            let index = Int(path.components(separatedBy: "-").last ?? "1") ?? 1
            return ["id": "synthetic-source-\(index)", "title": titles[max(0, min(5, index - 1))],
                    "domain_id": "preview-domain", "capture_type": "link", "revision": 1, "extracted_text": original,
                    "processing_status": "usable", "tags": ["知识检索"], "context": ["version": 1]]
        }
        if path.hasSuffix("navigation") {
            let topic = body?["topic_id"] as? String
            let name = topic == nil ? "AI 产品实践" : topic == "topic-a" ? "检索与证据" : topic == "topic-a-child" ? "评估方法" : "知识加工与产品体验"
            let currentCoverage = coverage(topic == nil ? 6 : topic == "topic-a" ? 4 : 2)
            return ["domain_id": "preview-domain", "scope": "app",
                "node": ["id": topic ?? "preview-domain", "kind": topic == nil ? "domain" : "topic", "name": name, "parent_id": NSNull()],
                "overview": ["available": true, "id": "preview-artifact", "created_at": "2026-10-15T06:15:00Z", "coverage": currentCoverage,
                    "result": ["overview": point("这组资料围绕 AI 产品的知识加工、检索质量与用户体验，重点关注如何让原始资料成为可追溯、可复用的知识。"),
                        "insights": [point("分层概览帮助定位主题，具体问题仍应回到原文证据。"), point("自动加工需要明确的预算与范围，内容变更后保持结果有效。"), point("检索质量要同时评估细节召回和跨资料理解。"), point("分类与文件归属独立于后台的加工批次。")],
                        "limitations": [point(fullCompleted ? "概览用于定位资料，具体判断仍请核对原文证据。" : "当前仍有两篇资料未纳入概览，不代表全部原文。")]]],
                "index_status": "ready", "topics": ["items": [], "next_offset": NSNull()],
                "sources": ["items": Array(1...6).map(source), "next_offset": NSNull()]]
        }
        if path.hasSuffix("schedule-state") {
            return ["phase": "partial", "automatic_status": "automatic_updates_disabled", "remaining_calls": 8,
                "remaining_input_tokens": 90000, "latest_build": NSNull(), "policy": ["enabled": true, "paused": false,
                "automatic_updates": false, "automatic_max_sources": 100, "automatic_max_steps": 20,
                "max_calls": 20, "max_input_tokens": 200000, "debounce_seconds": 30, "max_wait_seconds": 300]]
        }
        if path.hasSuffix("preview") {
            return ["preview_hash": String(repeating: "a", count: 64), "coverage": coverage(),
                    "calls": 3, "input_token_reservation": 48000, "total_steps": 5, "reused_batches": 2]
        }
        return nil
    }
}

private final class OverviewPreviewProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "127.0.0.1" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        var requestData = request.httpBody
        if requestData == nil, let stream = request.httpBodyStream {
            stream.open(); defer { stream.close() }
            var data = Data(), buffer = [UInt8](repeating: 0, count: 4096)
            while stream.hasBytesAvailable && data.count < 1_048_576 {
                let count = stream.read(&buffer, maxLength: buffer.count)
                if count <= 0 { break }
                data.append(contentsOf: buffer.prefix(count))
            }
            requestData = data
        }
        let requestBody = requestData.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        let fixture = OverviewPreviewFixture.response(request.url!.path, requestBody, method: request.httpMethod ?? "GET")
        let denied = fixture == nil && request.httpMethod != "GET"
        let body = fixture ?? (denied ? ["message": "界面预览不提交任务或预算授权"] : [:])
        let data = try! JSONSerialization.data(withJSONObject: body)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: denied ? 403 : 200,
            httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: data)
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main struct LocalDomainOverviewUITest: App {
    @StateObject private var model: CaptureModel
    @StateObject private var store: LocalDomainOverviewStore
    init() {
        precondition(["local.pka.domain-overview-ui-test", "local.pka.domain-full-ui-test"].contains(Bundle.main.bundleIdentifier ?? ""), "界面验收必须使用独立测试 Bundle")
        _ = URLProtocol.registerClass(OverviewPreviewProtocol.self)
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("pka-domain-ui-\(UUID().uuidString)")
        let model = CaptureModel(localService: LocalService(home: home, credentialService: "synthetic.domain-ui"), runtime: nil)
        model.address = "http://127.0.0.1:18181"; model.token = "synthetic-preview"
        _model = StateObject(wrappedValue: model)
        _store = StateObject(wrappedValue: LocalDomainOverviewStore(transport: Self.response))
    }
    @MainActor private static func response(_ api: API, _ path: String, _ method: String, _ body: [String: Any]?) async throws -> [String: Any] {
        // Delays make rapid-tab switching and in-flight read handling visible during manual QA.
        try await Task.sleep(nanoseconds: path.hasSuffix("/information") ? 280_000_000 : 100_000_000)
        guard let fixture = OverviewPreviewFixture.response(path, body, method: method) else { throw failure("界面预览不提交任务或预算授权") }
        return fixture
    }
    var body: some Scene {
        Window("知识域全量分析 · 隔离界面验收", id: "preview") {
            LocalDomainOverviewView(model: model, domainID: "preview-domain", store: store)
                .preferredColorScheme(.dark)
                .tint(InboxStyle.accent)
                .buttonStyle(InboxButtonStyle(.secondary))
                .task { await model.connect() }
        }.windowResizability(.contentSize)
    }
}

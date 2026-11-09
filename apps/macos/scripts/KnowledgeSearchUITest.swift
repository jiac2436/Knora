import AppKit
import SwiftUI

private final class SearchFixtureProtocol: URLProtocol, @unchecked Sendable {
    static let body = (1...30).map { "第 \($0) 段：避坑清单包含需要核对的原文内容。" }.joined(separator: "\n\n")
    static func detail(_ id: String) -> [String: Any] {
        ["id": id, "title": "搜索验收资料 \(id)", "domain_id": "fixture-library", "revision": 1,
         "content_type": "file", "extracted_text": body, "context": ["version": 1]]
    }
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.path
        var value: [String: Any] = ["items": []]
        if path == "/v1/me" { value = ["id": "fixture-owner", "display_name": "隔离用户"] }
        else if path == "/v1/domains" { value = ["items": [["id": "fixture-library", "name": "搜索验收知识库"]]] }
        else if path == "/v1/search" {
            var data = request.httpBody ?? Data()
            if data.isEmpty, let stream = request.httpBodyStream {
                stream.open(); defer { stream.close() }
                var buffer = [UInt8](repeating: 0, count: 4096)
                while stream.hasBytesAvailable { let n = stream.read(&buffer, maxLength: buffer.count); if n <= 0 { break }; data.append(contentsOf: buffer.prefix(n)) }
            }
            let input = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] ?? [:]
            precondition(input["domain_ids"] as? [String] == ["fixture-library"])
            let semantic = input["mode"] as? String == "semantic"
            precondition(input["result_unit"] as? String == "passage")
            let paragraphs = Self.body.components(separatedBy: "\n\n")
            value = ["summary_index": "ready", "items": (1...20).map { index -> [String: Any] in
                let paragraph = paragraphs[index - 1]
                let rawRange = Self.body.range(of: paragraph)!
                let start = Self.body[..<rawRange.lowerBound].unicodeScalars.count
                let length = paragraph.unicodeScalars.count
                return ["source_id": "fixture-1", "match_id": "fixture-1:\(index)",
                 "title": "同篇文章 · 第 \(index) 处", "content_type": "file", "revision": 1,
                 "snippet": paragraph, "match_text": paragraph,
                 "match_sources": semantic ? ["original", "summary"] : ["original"],
                 "summary_snippet": semantic ? "语义线索，选择对应的原文段落。" : "",
                 "locator": ["kind": "text", "start": start, "end": start + length]]
            }]
        } else if path == "/v1/sources" { value = ["items": [Self.detail("fixture-1")]] }
        else if path.hasPrefix("/v1/sources/"), !path.hasSuffix("/revisions") {
            value = Self.detail(request.url!.lastPathComponent)
        }
        let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: value))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main struct KnowledgeSearchUITest: App {
    @StateObject private var model: CaptureModel
    init() {
        precondition(Bundle.main.bundleIdentifier == "local.pka.search-ui-test")
        URLProtocol.registerClass(SearchFixtureProtocol.self)
        let home = FileManager.default.temporaryDirectory.appendingPathComponent("pka-search-ui-\(UUID().uuidString)")
        let model = CaptureModel(localService: LocalService(home: home, credentialService: "synthetic.search-ui"), runtime: nil)
        model.address = "http://127.0.0.1:18181"; model.token = "synthetic-fixture"
        precondition(model.knowledgeSearchMode == .exact)
        model.knowledgeSearchMode = .semantic
        precondition(model.knowledgeSearchMode == .semantic)
        model.knowledgeSearchMode = .exact
        _model = StateObject(wrappedValue: model)
    }
    var body: some Scene {
        Window("搜索交互 · 隔离验收", id: "search-test") {
            KnowledgeView(model: model, showSettings: .constant(false))
                .frame(minWidth: 1000, minHeight: 700)
                .preferredColorScheme(.dark).tint(InboxStyle.accent)
                .buttonStyle(InboxButtonStyle(.secondary))
                .task { await model.connect(); await model.loadRecent(); await model.openSource("fixture-1") }
        }
    }
}

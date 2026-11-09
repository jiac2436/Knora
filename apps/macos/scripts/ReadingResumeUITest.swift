import AppKit
import SwiftUI
import CryptoKit

private enum ResumeFixture {
    static let first = "11111111-1111-4111-8111-111111111111"
    static let second = "22222222-2222-4222-8222-222222222222"
    static let domain = "33333333-3333-4333-8333-333333333333"
    static let text = (0..<180).map { "## 第\($0)节 · 阅读位置验收\n\n这是第\($0)段合成正文，用于验证重启后继续阅读。" + String(repeating: "保留当前位置，可以继续阅读后续资料。", count: 10) }.joined(separator: "\n\n")
    static func response(_ path: String) -> [String: Any] {
        let rows: [[String: Any]] = [second, first].map { ["id": $0, "domain_id": domain, "title": $0 == first ? "上次阅读的文章" : "最新的一篇文章", "capture_type": "url", "revision": 1, "processing_status": "usable"] }
        if path == "/v1/me" { return ["id": "synthetic-reading-ui", "display_name": "界面验收"] }
        if path == "/v1/domains" { return ["items": [["id": domain, "name": "阅读恢复验收"]]] }
        if path == "/v1/sources" { return ["items": rows] }
        if path.hasSuffix("/revisions") { return ["items": []] }
        if path == "/v1/sources/\(first)" { return ["id": first, "domain_id": domain, "title": "上次阅读的文章", "capture_type": "url", "revision": 1, "processing_status": "usable", "extracted_text": text] }
        if path == "/v1/sources/\(second)" { return ["id": second, "domain_id": domain, "title": "最新的一篇文章", "capture_type": "url", "revision": 1, "extracted_text": "这是另一篇合成文章。"] }
        return ["items": []]
    }
}

private final class ResumeProtocol: URLProtocol {
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "127.0.0.1" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let denied = (request.httpMethod ?? "GET") != "GET"
        let body = denied ? ["message": "隔离界面禁止提交任务"] : ResumeFixture.response(request.url!.path)
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: denied ? 403 : 200, httpVersion: nil, headerFields: ["Content-Type":"application/json"])!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: body))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main struct ReadingResumeUITest: App {
    @StateObject private var model: CaptureModel
    @State private var settings = false
    init() {
        precondition(Bundle.main.bundleIdentifier == "local.pka.reading-resume-ui-test")
        _ = URLProtocol.registerClass(ResumeProtocol.self)
        let file = Bundle.main.resourceURL!.appendingPathComponent("state-path.txt")
        let location = try! String(contentsOf: file, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        precondition(location.hasPrefix("/private/tmp/pka-reading-resume-ui-"))
        let directory = URL(fileURLWithPath: location)
        let service = LocalService(home: directory.appendingPathComponent("fake-home"), credentialService: "synthetic.resume-ui")
        let model = CaptureModel(localService: service, runtime: nil)
        let store = ReadingSessionStore(directory: directory)
        if store.current == nil {
            store.select(sourceID: ResumeFixture.first, domainID: ResumeFixture.domain)
            store.record(text: ReaderTextPosition(sourceOffset: (ResumeFixture.text as NSString).range(of: "这是第90段").location,
                imageID: nil, elementFraction: 0.25, scrollFraction: 0.5,
                contentSignature: SHA256.hash(data: Data(ResumeFixture.text.utf8)).map { String(format: "%02x", $0) }.joined()), sourceID: ResumeFixture.first)
            store.flush()
        }
        model.readingSessionStore = store
        model.address = "http://127.0.0.1:18384"; model.token = "synthetic-ui-only"
        _model = StateObject(wrappedValue: model)
    }
    var body: some Scene {
        Window("阅读恢复 · 隔离验收", id: "reading-resume") {
            Group {
                if model.connected { KnowledgeView(model: model, showSettings: $settings) }
                else { ProgressView("正在打开合成知识库").frame(width: 1100, height: 720) }
            }
            .frame(minWidth: 1000, minHeight: 680).preferredColorScheme(.dark)
            .tint(InboxStyle.accent).buttonStyle(InboxButtonStyle(.secondary))
            .task { await model.connect() }
            .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in model.cleanup() }
        }
    }
}

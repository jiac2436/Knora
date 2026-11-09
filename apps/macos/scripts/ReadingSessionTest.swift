import AppKit
import Foundation
import PDFKit
import SwiftUI
import CryptoKit

private let firstID = "11111111-1111-4111-8111-111111111111"
private let secondID = "22222222-2222-4222-8222-222222222222"
private let domainID = "33333333-3333-4333-8333-333333333333"

final class ReadingFixtureProtocol: URLProtocol, @unchecked Sendable {
    static let lock = NSLock()
    nonisolated(unsafe) static var missing = false
    nonisolated(unsafe) static var delay = false
    nonisolated(unsafe) static var requested: [String] = []
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "127.0.0.1" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.path
        Self.lock.lock(); Self.requested.append(path); let missing = Self.missing, delay = Self.delay; Self.lock.unlock()
        let status = path == "/v1/sources/\(firstID)" && missing ? 404 : 200
        let body: [String: Any]
        switch path {
        case "/v1/me": body = ["id": "synthetic-owner"]
        case "/v1/domains": body = ["items": [["id": domainID, "name": "合成知识库"]]]
        case "/v1/projects", "/v1/sources": body = ["items": []]
        case let p where p.hasSuffix("/revisions"): body = ["items": []]
        case let p where p.hasPrefix("/v1/sources/"):
            body = ["id": String(p.split(separator: "/").last!), "domain_id": domainID, "title": "合成阅读资料", "capture_type": "url", "revision": 1, "extracted_text": "只用于隔离测试的正文。"]
        default: body = [:]
        }
        let send: @Sendable () -> Void = { [self] in
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type":"application/json"])!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: body))
            client?.urlProtocolDidFinishLoading(self)
        }
        if delay && path == "/v1/sources/\(firstID)" { DispatchQueue.global().asyncAfter(deadline: .now() + 0.25, execute: send) }
        else { send() }
    }
    override func stopLoading() {}
}

@main struct ReadingSessionTest {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("pka-reading-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        try testStorage(directory)
        try await testSelection(directory)
        try await testWebReader(directory)
        try await testPDFReader()
        print("PASS: 阅读会话落盘、退出刷新、库隔离、异常记录保护、启动恢复、404回退、切文竞争、正文锚点／宽度／延迟图片／搜索优先、PDF页内位置")
    }

    @MainActor static func testStorage(_ root: URL) throws {
        let store = ReadingSessionStore(directory: root)
        precondition(store.current == nil)
        store.select(sourceID: firstID, domainID: domainID)
        let text = ReaderTextPosition(sourceOffset: 820, imageID: nil, elementFraction: 0.4, scrollFraction: 0.5, contentSignature: String(repeating: "a", count: 64))
        store.record(text: text, sourceID: firstID)
        store.flush()
        let reopened = ReadingSessionStore(directory: root)
        precondition(reopened.current?.text == text && reopened.current?.sourceID == firstID)
        let permissions = try FileManager.default.attributesOfItem(atPath: store.file.path)[.posixPermissions] as? Int
        precondition(permissions == 0o600)
        let saved = try String(contentsOf: store.file, encoding: .utf8)
        precondition(!saved.contains("正文") && !saved.contains("token") && !saved.contains("http"))
        store.select(sourceID: secondID, domainID: domainID)
        store.record(text: text, sourceID: firstID)
        precondition(store.current?.text == nil, "旧文章迟到的滚动消息不能污染新文章")
        let other = ReadingSessionStore(directory: root.appendingPathComponent("another-library"))
        precondition(other.current == nil, "不同本地库不能共用阅读记录")
        store.forget(sourceID: secondID)
        precondition(!FileManager.default.fileExists(atPath: store.file.path))
        try Data("broken".utf8).write(to: store.file)
        let corrupt = ReadingSessionStore(directory: root)
        corrupt.select(sourceID: firstID, domainID: domainID)
        let corruptedText = try String(contentsOf: store.file, encoding: .utf8)
        precondition(corrupt.error != nil && corruptedText == "broken")
        try FileManager.default.removeItem(at: store.file)
        let target = root.appendingPathComponent("untouched.txt")
        try Data("untouched".utf8).write(to: target)
        try FileManager.default.createSymbolicLink(at: store.file, withDestinationURL: target)
        let symlink = ReadingSessionStore(directory: root)
        symlink.select(sourceID: firstID, domainID: domainID)
        let protectedText = try String(contentsOf: target, encoding: .utf8)
        precondition(symlink.error != nil && protectedText == "untouched")
        try FileManager.default.removeItem(at: store.file)
    }

    @MainActor static func testSelection(_ root: URL) async throws {
        _ = URLProtocol.registerClass(ReadingFixtureProtocol.self)
        let store = ReadingSessionStore(directory: root.appendingPathComponent("selection"))
        store.select(sourceID: firstID, domainID: domainID)
        let service = LocalService(home: root.appendingPathComponent("fake-home"), credentialService: "synthetic.reading-session")
        let model = CaptureModel(localService: service, runtime: nil)
        model.readingSessionStore = store
        model.address = "http://127.0.0.1:18383"; model.token = "synthetic-test-only"
        await model.connect()
        await model.restoreLastReadingSession()
        precondition(model.selectedSource?.id == firstID, "不依赖最近50条列表即可恢复原文章")
        model.selectedSource = nil; model.clearKnowledgeCache()
        ReadingFixtureProtocol.missing = true
        await model.restoreLastReadingSession()
        precondition(model.selectedSource == nil && store.current == nil, "已删除文章清理失效书签")
        ReadingFixtureProtocol.missing = false
        store.select(sourceID: firstID, domainID: domainID)
        model.pendingArticleLink = try ArticleLink(URL(string: "knowledge-inbox://library/\(domainID)/source/\(secondID)")!)
        await model.restoreLastReadingSession()
        precondition(model.selectedSource == nil, "显式外部链接优先")
        model.pendingArticleLink = nil
        ReadingFixtureProtocol.delay = true
        let restoring = Task { await model.restoreLastReadingSession() }
        try await Task.sleep(for: .milliseconds(50))
        await model.openSource(secondID)
        await restoring.value
        precondition(model.selectedSource?.id == secondID && store.current?.sourceID == secondID, "恢复的迟到响应不能覆盖用户选择")
        ReadingFixtureProtocol.delay = false
        model.cleanup()
        URLProtocol.unregisterClass(ReadingFixtureProtocol.self)
    }

    @MainActor static func ready(_ reader: NativeWebTextReader) async throws {
        for _ in 0..<100 {
            try await Task.sleep(for: .milliseconds(60))
            if (try? await reader.evaluateForTesting("typeof window.pkaReportReadingPosition === 'function'")) as? Bool == true { return }
        }
        throw failure("阅读位置桥接未就绪")
    }

    @MainActor static func testWebReader(_ root: URL) async throws {
        let text = (0..<180).map { "## 段落\($0)\n\n这是第\($0)段合成阅读正文，包含**加粗**与🙂表情。" + String(repeating: "用于验证阅读位置恢复。", count: 12) }.joined(separator: "\n\n")
        let store = ReadingSessionStore(directory: root.appendingPathComponent("web"))
        store.select(sourceID: firstID, domainID: domainID)
        var reader: NativeWebTextReader? = NativeWebTextReader(frame: NSRect(x: 0, y: 0, width: 760, height: 500))
        let window = NSWindow(contentRect: reader!.frame, styleMask: [.titled, .resizable], backing: .buffered, defer: false)
        window.contentView = reader; window.orderFront(nil)
        reader!.configure(revision: 1, text: text, size: 16)
        reader!.bindReadingPosition(nil) { store.record(text: $0, sourceID: firstID) }
        try await ready(reader!)
        _ = try await reader!.evaluateForTesting("window.scrollTo(0,document.querySelectorAll('h2')[100].offsetTop+35); true")
        try await Task.sleep(for: .milliseconds(120))
        _ = try await reader!.evaluateForTesting("window.pkaReportReadingPosition(); true")
        try await Task.sleep(for: .milliseconds(60))
        store.flush()
        let saved = ReadingSessionStore(directory: root.appendingPathComponent("web")).current!.text!
        precondition(saved.scrollFraction > 0.3 && saved.sourceOffset != nil, "实际滚动必须保存原文锚点")
        let before = try await reader!.evaluateForTesting("document.querySelector('[data-pka-start=\"\(saved.sourceOffset!)\"]').closest('p,h2').getBoundingClientRect().top") as! Double
        window.contentView = nil; reader = nil
        let reopened = NativeWebTextReader(frame: NSRect(x: 0, y: 0, width: 760, height: 500))
        window.contentView = reopened
        reopened.configure(revision: 1, text: text, size: 16)
        reopened.bindReadingPosition(saved) { store.record(text: $0, sourceID: firstID) }
        try await ready(reopened); try await Task.sleep(for: .milliseconds(100))
        let restored = try await reopened.evaluateForTesting("document.querySelector('[data-pka-start=\"\(saved.sourceOffset!)\"]').closest('p,h2').getBoundingClientRect().top") as! Double
        precondition(abs(before - restored) < 3, "新阅读器应恢复相同段落位置：\(before) / \(restored)")
        reopened.setFrameSize(NSSize(width: 580, height: 500)); window.setContentSize(NSSize(width: 580, height: 500))
        try await Task.sleep(for: .milliseconds(150))
        let fraction = try await reopened.evaluateForTesting("(function(){var r=document.querySelector('[data-pka-start=\"\(saved.sourceOffset!)\"]').closest('p,h2').getBoundingClientRect();return -r.top/r.height;})()") as! Double
        precondition(abs(fraction - saved.elementFraction) < 0.08, "窄窗口保持同一段落内进度")
        // Late layout above the anchor must not pull the saved paragraph away.
        _ = try await reopened.evaluateForTesting("var late=document.createElement('div');late.style.height='350px';document.getElementById('content-area').prepend(late);true")
        try await Task.sleep(for: .milliseconds(140))
        let afterImage = try await reopened.evaluateForTesting("(function(){var r=document.querySelector('[data-pka-start=\"\(saved.sourceOffset!)\"]').closest('p,h2').getBoundingClientRect();return -r.top/r.height;})()") as! Double
        precondition(abs(afterImage - saved.elementFraction) < 0.08, "延迟图片改变高度后保持锚点")
        let target = (text as NSString).range(of: "## 段落3\n")
        reopened.locate(TextReaderRequest(sourceID: firstID, revision: 1, range: NSRange(location: target.location+3, length: 3)))
        var visible = false
        for _ in 0..<60 {
            try await Task.sleep(for: .milliseconds(30))
            let located = try await reopened.evaluateForTesting("document.querySelector('mark').getBoundingClientRect().top") as! Double
            if located >= 0 && located < 500 { visible = true; break }
        }
        precondition(visible, "搜索命中位置优先于旧阅读书签")
        try await Task.sleep(for: .milliseconds(180))
        let settled = try await reopened.evaluateForTesting("document.querySelector('mark').getBoundingClientRect().top") as! Double
        precondition(settled >= 0 && settled < 500, "搜索定位后不能被恢复观察器拉回旧书签")
        window.contentView = nil
    }

    @MainActor static func testPDFReader() async throws {
        let document = PDFDocument()
        for _ in 0..<4 {
            let image = NSImage(size: NSSize(width: 600, height: 1200), flipped: false) { rect in
                NSColor.white.setFill(); rect.fill(); return true
            }
            document.insert(PDFPage(image: image)!, at: document.pageCount)
        }
        var currentPage = 1
        var recorded: ReaderPDFPosition?
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 500), styleMask: [.titled], backing: .buffered, defer: false)
        let view = PDFView()
        let position = ReaderPDFPosition(page: 3, x: 0, y: 800)
        let content = PDFReader(view: view, document: document, request: PDFPageRequest(page: 3, position: position), currentPage: Binding(get: { currentPage }, set: { currentPage = $0 }), onPosition: { recorded = $0 })
        window.contentView = NSHostingView(rootView: content); window.orderFront(nil)
        try await Task.sleep(for: .milliseconds(350))
        precondition(recorded?.page == 3, "PDF恢复页码及页内坐标，实际\(String(describing: recorded))")
        precondition(abs((recorded?.y ?? 0) - 800) < 3, "PDF应恢复页内点，不仅跳到页首")
        view.go(to: PDFDestination(page: document.page(at: 3)!, at: CGPoint(x: 0, y: 600)))
        try await Task.sleep(for: .milliseconds(140))
        precondition(recorded?.page == 4, "PDF滚动应更新阅读记录")
        window.contentView = nil
    }
}

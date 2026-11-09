import AppKit
import SwiftUI

final class CaptureFixtureProtocol: URLProtocol {
    static var sourceState = "running"
    static var requests = 0
    static var fail = false
    static var emptyDomains = false
    static var domainCreates = 0
    static var failCreate = false
    static var lastSourceQuery: String?
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "127.0.0.1" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.path
        var data: [String: Any] = [:]
        switch path {
        case "/v1/me": data = ["id": "synthetic-owner", "display_name": "测试"]
        case "/v1/domains":
            if request.httpMethod == "POST" {
                Self.domainCreates += 1
                if Self.failCreate {
                    client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet)); return
                }
                Self.emptyDomains = false
                data = ["id": "default-library", "name": "默认知识库"]
            } else {
                data = ["items": Self.emptyDomains ? [] : [["id": "library", "name": "MyKnow"]]]
            }
        case "/v1/sources":
            Self.lastSourceQuery = request.url?.query
            data = ["items": []]
        case "/v1/projects": data = ["items": []]
        case "/v1/ocr/preferences", "/v1/web/preferences": break
        case "/v1/sources/pdf":
            Self.requests += 1
            if Self.fail {
                client?.urlProtocol(self, didFailWithError: URLError(.notConnectedToInternet)); return
            }
            data = ["ocr": ["status": Self.sourceState], "processing_status": Self.sourceState == "completed" ? "usable" : "processing"]
        case "/v1/sources/link":
            Self.requests += 1
            data = ["processing_status": Self.sourceState == "completed" ? "usable" : "processing"]
        default: fatalError("Unexpected request: \(path)")
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: data))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main struct CaptureWorkbenchTest {
    @MainActor static func main() async throws {
        let cases: [(String, Bool, CaptureStage)] = [
            ("待发送", false, .waiting), ("上传中 30%", false, .receiving),
            ("PDF 已上传 · 等待后台识别", true, .reading), ("PDF 识别中…", true, .reading),
            ("文档解析失败 · 请在阅读器查看原因", true, .attention),
            ("PDF 已处理 · 未识别到文字", true, .saved), ("原文件已保存 · 未解析", true, .saved),
            ("网页正文已读取 · 可搜索", true, .ready), ("笔记已保存", true, .ready),
            ("发送失败：断网", false, .attention), ("PDF 识别已取消", true, .attention),
            ("原件已保存 · 状态暂不可用，请刷新重试", true, .attention)
        ]
        for (status, sent, expected) in cases { precondition(CaptureStage.resolve(status: status, sent: sent) == expected, status) }
        for (status, sent, stage) in cases {
            var item = CaptureItem(kind: .note, title: "分类边界", text: "正文", file: nil)
            item.status = status; item.sent = sent
            let expected: CaptureQueueFilter = stage == .waiting ? .pending : stage.active ? .processing : stage == .attention ? .failed : .completed
            precondition(item.queueFilter == expected, status)
        }
        var finished = CaptureItem(kind: .note, title: "笔记", text: "正文", file: nil)
        finished.status = "笔记已保存"; finished.sent = true
        let pending = CaptureItem(kind: .note, title: "待发送", text: "正文", file: nil)
        var failed = finished; failed.id = UUID().uuidString; failed.status = "发送失败：断网"
        precondition(CaptureWorkbenchActivity(items: [finished], submitting: false).canReturnHome)
        precondition(!CaptureWorkbenchActivity(items: [], submitting: false).canReturnHome)
        precondition(!CaptureWorkbenchActivity(items: [finished], submitting: true).canReturnHome)
        precondition(!CaptureWorkbenchActivity(items: [finished, pending], submitting: false).canReturnHome)
        precondition(!CaptureWorkbenchActivity(items: [finished, failed], submitting: false).canReturnHome)
        precondition(CaptureWorkbenchActivity(items: [finished], submitting: false) !=
                     CaptureWorkbenchActivity(items: [finished, pending], submitting: false))
        URLProtocol.registerClass(CaptureFixtureProtocol.self)
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-panda-test-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let store = CaptureQueueStore(root: root.appendingPathComponent("queue"))
        let model = CaptureModel(queueStore: store,
            localService: LocalService(home: root, credentialService: "synthetic.panda"), runtime: root)
        model.address = "http://127.0.0.1:19876"
        model.token = "synthetic"
        await model.connect()
        precondition(model.connected && model.usesLocalService)
        precondition(model.searchDomainID == model.domainID && model.domainID == "library")
        await model.loadRecent()
        precondition(CaptureFixtureProtocol.lastSourceQuery?.contains("domain_id=library") == true)
        await model.loadAllSources()
        precondition(CaptureFixtureProtocol.lastSourceQuery?.contains("domain_id=library") == true)
        try await model.ensureCaptureDomain()
        precondition(CaptureFixtureProtocol.domainCreates == 0)
        model.domains = []; model.domainID = ""
        try await model.ensureCaptureDomain()
        precondition(model.domainID == "library" && CaptureFixtureProtocol.domainCreates == 0,
                     "先刷新后台列表，有知识库时不能重复创建")
        model.domains = []; model.domainID = ""
        CaptureFixtureProtocol.emptyDomains = true
        CaptureFixtureProtocol.failCreate = true
        do {
            try await model.ensureCaptureDomain()
            preconditionFailure("创建失败不能继续上传")
        } catch { precondition(model.domainID.isEmpty && model.domains.isEmpty) }
        CaptureFixtureProtocol.failCreate = false
        try await model.ensureCaptureDomain()
        precondition(model.domainID == "default-library" && model.searchDomainID == model.domainID)
        precondition(model.domains.first?.name == "默认知识库")
        let creates = CaptureFixtureProtocol.domainCreates
        try await model.ensureCaptureDomain()
        precondition(CaptureFixtureProtocol.domainCreates == creates)
        model.domains = [DomainItem(id: "library", name: "MyKnow")]
        model.domainID = "library"; model.searchDomainID = "library"
        var pdf = CaptureItem(kind: .file, title: "产品设计.pdf", text: nil, file: nil)
        pdf.sourceID = "pdf"; pdf.destination = "local|synthetic-owner"; pdf.sent = true
        pdf.status = "PDF 已上传 · 等待后台识别"
        var link = CaptureItem(kind: .link, title: "北京历史", text: "https://example.com", file: nil)
        link.sourceID = "link"; link.destination = pdf.destination; link.sent = true
        link.status = "链接已保存 · 等待读取网页"
        model.items = [pdf, link]
        await model.refreshFileStatuses(quiet: true)
        precondition(CaptureFixtureProtocol.requests == 2)
        precondition(model.items.allSatisfy { $0.stage == .reading })
        CaptureFixtureProtocol.sourceState = "completed"
        await model.refreshFileStatuses(quiet: true)
        precondition(model.items.allSatisfy { $0.stage == .ready })
        let requests = CaptureFixtureProtocol.requests
        await model.refreshFileStatuses(quiet: true)
        precondition(CaptureFixtureProtocol.requests == requests, "已完成任务不应轮询")
        let restored = try store.load()!
        precondition(restored.items.allSatisfy { $0.stage == .ready })
        CaptureFixtureProtocol.fail = true
        await model.refreshFileStatuses(quiet: true, includeFinished: true)
        precondition(model.items[0].stage == .attention && model.items[0].sent)
        model.items[0].destination = "local|different-owner"
        let before = CaptureFixtureProtocol.requests
        await model.refreshFileStatuses(quiet: true)
        precondition(CaptureFixtureProtocol.requests == before, "其他知识库的条目不得请求")
        model.items = [pending, finished, failed, pdf]
        let beforeClear = CaptureFixtureProtocol.requests
        model.clearQueue(.processing)
        precondition(model.items.count == 4)
        model.clearQueue(.completed)
        precondition(model.items.count == 3 && !model.items.contains { $0.id == finished.id })
        let afterClear = try store.load()!
        precondition(afterClear.items.count == 3)
        model.clearQueue(.pending)
        precondition(model.items.count == 2)
        model.clearQueue(.failed)
        precondition(model.items.count == 1 && model.items[0].id == pdf.id)
        precondition(CaptureFixtureProtocol.requests == beforeClear, "清空队列不得请求删除后台文章")
        print("PASS: 12阶段与分类映射、成功归位条件/失败保持/新任务取消、本地状态刷新、停止轮询、持久化及跨库隔离")

        if let output = ProcessInfo.processInfo.environment["PKA_CAPTURE_PREVIEW"] {
            _ = NSApplication.shared
            CaptureFixtureProtocol.fail = false
            CaptureFixtureProtocol.sourceState = "running"
            model.items = [pdf, link]
            for index in 1...12 {
                model.items.append(CaptureItem(kind: .note, title: "笔记 \(index)", text: "合成资料，用于检查固定队列滚动。", file: nil))
            }
            model.message = ""
            let view = CaptureView(model: model, showSettings: .constant(false)).preferredColorScheme(.dark)
            let host = NSHostingView(rootView: view)
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 950), styleMask: [.titled], backing: .buffered, defer: false)
            window.contentView = host
            window.orderFront(nil)
            let outputURL = URL(fileURLWithPath: output)
            func snapshot(_ name: String) throws {
                host.layoutSubtreeIfNeeded()
                let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
                host.cacheDisplay(in: host.bounds, to: bitmap)
                try bitmap.representation(using: .png, properties: [:])!.write(
                    to: outputURL.deletingLastPathComponent().appendingPathComponent(name + ".png"))
            }
            func editor(in view: NSView) -> PasteTextView? {
                if let result = view as? PasteTextView { return result }
                return view.subviews.lazy.compactMap { editor(in: $0) }.first
            }
            try await Task.sleep(for: .seconds(1))
            window.makeFirstResponder(nil)
            try await Task.sleep(for: .milliseconds(150))
            try snapshot("capture")
            let input = editor(in: host)!
            precondition(window.makeFirstResponder(input))
            try await Task.sleep(for: .milliseconds(150))
            try snapshot("focused")
            window.makeFirstResponder(nil)
            window.setContentSize(NSSize(width: 898, height: 680))
            try await Task.sleep(for: .milliseconds(300))
            try snapshot("minimum-size")
            window.setContentSize(NSSize(width: 1280, height: 950))
            model.items = [pdf, link]
            CaptureFixtureProtocol.sourceState = "completed"
            await model.refreshFileStatuses(quiet: true, includeFinished: true)
            try await Task.sleep(for: .seconds(1))
            try snapshot("completed-before-return")
            try await Task.sleep(for: .seconds(5))
            try snapshot("completed-returned")
            model.items = [pdf]
            CaptureFixtureProtocol.sourceState = "failed"
            await model.refreshFileStatuses(quiet: true, includeFinished: true)
            try await Task.sleep(for: .seconds(6))
            try snapshot("failed-stays")
            // A new active task must cancel the pending five-second return.
            CaptureFixtureProtocol.sourceState = "completed"
            await model.refreshFileStatuses(quiet: true, includeFinished: true)
            try await Task.sleep(for: .seconds(3))
            CaptureFixtureProtocol.sourceState = "running"
            await model.refreshFileStatuses(quiet: true, includeFinished: true)
            try await Task.sleep(for: .seconds(3))
            try snapshot("new-task-cancels-return")
            model.items = []
            try await Task.sleep(for: .milliseconds(300))
            try snapshot("empty")
            print("PASS: production SwiftUI focus, fixed queue, five-second return, failed hold and timer cancellation rendered")
            if ProcessInfo.processInfo.environment["PKA_CAPTURE_INTERACTIVE"] != nil {
                // Keep the isolated window available for mouse/keyboard verification.
                for _ in 0..<3 { try await Task.sleep(for: .seconds(60)) }
            }
            window.orderOut(nil)
        }
    }
}

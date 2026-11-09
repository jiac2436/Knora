import AppKit
import SwiftUI

private final class OverviewAPIStub: URLProtocol {
    static var artifact: [String: Any] = [:]
    static var failRead = false
    static var hasArtifact = true
    static var deliveryDelay: TimeInterval = 0
    static var paths: [String] = []
    private static let lock = NSLock()
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "ai-overview.test" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        let path = request.url!.path
        Self.paths.append((request.httpMethod ?? "GET") + " " + path)
        let fail = Self.failRead && path == "/v1/ai/artifacts"
        let body: [String: Any] = path == "/v1/me" ? ["id": "synthetic", "display_name": "合成验收"] :
            path == "/v1/ai/artifacts" ? ["items": Self.hasArtifact ? [Self.artifact] : []] : ["items": []]
        let delay = Self.deliveryDelay
        Self.lock.unlock()
        let deliver = {
            self.client?.urlProtocol(self, didReceive: HTTPURLResponse(url: self.request.url!, statusCode: fail ? 500 : 200,
                httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
            self.client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: body))
            self.client?.urlProtocolDidFinishLoading(self)
        }
        if delay > 0 { DispatchQueue.global().asyncAfter(deadline: .now() + delay, execute: deliver) }
        else { deliver() }
    }

    override func stopLoading() {}
}

@MainActor private final class OverviewFixture: ObservableObject {
    @Published var presentation = AIOverviewPresentation()
    @Published var readerFrame: CGRect = .zero
}

@MainActor private struct FloatingOverviewFixture: View {
    @ObservedObject var model: CaptureModel
    @ObservedObject var fixture: OverviewFixture
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var buttonFrame: CGRect = .zero
    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Text(model.selectedSource!.title).font(.system(size: 18, weight: .semibold))
                Spacer()
                LocalAIAnalyzeButton(model: model, compact: true,
                    showOverview: { fixture.presentation.toggle() }, showDetails: {})
                    .buttonStyle(InboxIconButtonStyle())
                    .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("knowledgeReader")) }
                        action: { buttonFrame = $0 }
            }.padding(.horizontal, 24).frame(height: 56)
            Divider()
            TextReader(store: model.textReaderStore, sourceID: "floating-fixture", revision: 1,
                text: model.selectedSource!.text, size: 17, request: nil)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
                .onGeometryChange(for: CGRect.self) { $0.frame(in: .named("knowledgeReader")) }
                    action: { fixture.readerFrame = $0 }
        }.background(InboxStyle.canvas)
        .overlay(alignment: .top) {
            ZStack(alignment: .top) {
            if fixture.presentation.isVisible {
                LocalAIResultCard(model: model, isExpanded: $fixture.presentation.isExpanded,
                    analysisButtonMidX: buttonFrame.isEmpty ? nil : buttonFrame.midX)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.horizontal, 24).padding(.top, 59)
                    .transition(.opacity)
            }
            }
            .animation(reduceMotion ? nil : .smooth(duration: 0.18), value: fixture.presentation.isVisible)
        }
        .coordinateSpace(name: "knowledgeReader")
    }
}

@main struct AIOverviewFloatingTest {
    @MainActor private static func accessibilityElement(_ identifier: String, matchingLabel: Bool = false, in root: Any) -> AnyObject? {
        var visited = Set<ObjectIdentifier>()
        func find(_ value: Any) -> AnyObject? {
            let object = value as AnyObject
            guard visited.insert(ObjectIdentifier(object)).inserted else { return nil }
            if let element = value as? NSObject {
                // SwiftUI's virtual nodes implement the selectors without full protocol conformance.
                if matchingLabel {
                    if element.responds(to: #selector(NSAccessibilityProtocol.accessibilityLabel)),
                       object.accessibilityLabel?() == identifier { return object }
                } else if element.responds(to: #selector(NSAccessibilityProtocol.accessibilityIdentifier)),
                          object.accessibilityIdentifier?() == identifier { return object }
                if element.responds(to: #selector(NSAccessibilityProtocol.accessibilityChildren)) {
                for child in object.accessibilityChildren?() ?? [] {
                    if let result = find(child) { return result }
                }
                }
            }
            if let view = value as? NSView {
                for child in view.subviews {
                    if let result = find(child) { return result }
                }
            }
            return nil
        }
        return find(root)
    }

    @MainActor static func main() {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.regular)
        // Enable only this synthetic process's accessibility bridge; no system preferences change.
        NSApp.accessibilitySetValue(true, forAttribute: NSAccessibility.Attribute(rawValue: "AXEnhancedUserInterface"))
        Task { @MainActor in
            do { try await run(); NSApp.terminate(nil) }
            catch { print("FAIL: \(error)"); exit(1) }
        }
        NSApp.run()
    }
    @MainActor static func run() async throws {
        let body: [String: Any] = ["id": "result", "source_id": "floating-test",
            "result": ["contract_version": 3, "summary": "以会员工具为例，从重复开发中提炼产品，通过社区答疑找到首批客户。",
                "summary_citations": [], "insights": [
                    ["text": "从反复出现的问题中识别可复用的产品机会。", "citations": []],
                    ["text": "先验证真实需求，再逐步完善服务。", "citations": []],
                    ["text": "用有帮助的内容建立长期的客户关系。", "citations": []]], "warnings": []],
            "evidence": [], "provider": "synthetic", "model": "synthetic", "stale": false, "pipeline_status": "completed"]
        let artifact = try decodeLocalAI(LocalAIArtifact.self, body: body)
        let saved = AIOverviewState(sourceID: "floating-test", jobs: [], artifacts: [artifact])
        precondition(saved.action() == .overview && saved.action(forceReanalysis: true) == .confirm)
        let empty = AIOverviewState(sourceID: "floating-test", jobs: [], artifacts: [])
        precondition(empty.action() == .confirm)
        let job = try decodeLocalAI(LocalAIJob.self, body: ["id": "job", "source_id": "floating-test", "kind": "analyze", "status": "running"])
        let active = AIOverviewState(sourceID: "floating-test", jobs: [job], artifacts: [artifact])
        precondition(active.action() == .progress && !active.canExpand)
        precondition(saved.canExpand && !empty.canExpand)
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-ai-floating-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let model = CaptureModel(queueStore: CaptureQueueStore(root: root.appendingPathComponent("queue")),
            localService: LocalService(home: root, credentialService: "synthetic.ai-floating"), runtime: root)
        model.address = "https://ai-overview.test"
        URLProtocol.registerClass(OverviewAPIStub.self)
        defer { URLProtocol.unregisterClass(OverviewAPIStub.self) }
        await model.connect()
        precondition(model.connected)
        let text = "# 年化百万的产品，从一个小需求开始\n\n阅读正文的起点在展开与收起概览时保持不变。\n\n" + (1...12).map {
            "## 第 \($0) 节：观察真实需求\n\n先记录问题、核对依据，再决定下一步。知识概览浮在这一层正文之上，文章的滚动位置和布局不应改变。"
        }.joined(separator: "\n\n")
        model.selectedSource = KnowledgeDetail(["id": "floating-test", "title": "从重复开发中，发现产品机会", "content_type": "link", "revision": 1, "extracted_text": text])!
        OverviewAPIStub.artifact = body
        model.aiArtifacts = []; model.aiJobs = []
        let restored = await model.refreshLocalAI(sourceID: "floating-test")
        precondition(restored && model.aiSourceReadPhase == .ready)
        precondition(AIOverviewState(sourceID: "floating-test", jobs: model.aiJobs, artifacts: model.aiArtifacts).action() == .overview)
        precondition(!OverviewAPIStub.paths.contains(where: { $0.contains("/plan") || $0 == "POST /v1/ai/jobs" }))
        model.aiReadStatus = nil
        precondition(model.aiSourceReadPhase == .loading)
        OverviewAPIStub.failRead = true
        let failedRead = await model.refreshLocalAI(sourceID: "floating-test")
        precondition(!failedRead && model.aiSourceReadPhase == .failed)
        OverviewAPIStub.failRead = false
        OverviewAPIStub.hasArtifact = false
        let emptyRead = await model.refreshLocalAI(sourceID: "floating-test")
        precondition(emptyRead && model.aiSourceReadPhase == .ready && model.aiArtifacts.isEmpty)
        OverviewAPIStub.hasArtifact = true
        model.aiReadStatus = nil
        OverviewAPIStub.deliveryDelay = 0.10
        let outdatedRead = Task { await model.refreshLocalAI(sourceID: "floating-test") }
        try await Task.sleep(for: .milliseconds(20))
        model.sourceLoadGeneration = UUID()
        let outdated = await outdatedRead.value
        precondition(!outdated && model.aiSourceReadPhase == .loading)
        OverviewAPIStub.deliveryDelay = 0
        let freshRead = await model.refreshLocalAI(sourceID: "floating-test")
        precondition(freshRead && model.aiSourceReadPhase == .ready)
        var presentation = AIOverviewPresentation()
        precondition(!presentation.isVisible && !presentation.isExpanded)
        presentation.toggle()
        precondition(presentation.isVisible && !presentation.isExpanded)
        presentation.isExpanded = true
        presentation.toggle()
        precondition(!presentation.isVisible && !presentation.isExpanded)
        presentation.toggle()
        precondition(presentation.isVisible && !presentation.isExpanded)
        for _ in 0..<20 {
            presentation.toggle()
            precondition(!presentation.isVisible && !presentation.isExpanded)
            presentation.toggle()
            precondition(presentation.isVisible && !presentation.isExpanded)
        }
        print("PASS: loading/ready-empty/ready-result/failed distinguished; same-source generation race discarded")
        print("PASS: overview starts hidden; toolbar opens title only and always resets details, including repeated toggles; active jobs cannot expand details")
        print("PASS: saved result routes to overview after startup reload; only explicit reanalysis routes to confirmation; active routes to progress; failed read stops; no plan/job POST")
        model.aiArtifacts = [artifact]; model.aiJobs = []
        model.sourceTags = try decodeLocalAI([LocalSourceTag].self, body: ["Memberstack", "SEO与流量分析", "SaaS会员工具", "出海产品选品"].enumerated().map {
            ["id": "tag-\($0.offset)", "name": $0.element, "dimension": "topic", "origins": ["ai"], "stale": false, "assignments": []] as [String: Any]
        })
        guard let output = ProcessInfo.processInfo.environment["PKA_AI_FLOATING_PREVIEW"] else { return }
        let fixture = OverviewFixture()
        let host = NSHostingView(rootView: FloatingOverviewFixture(model: model, fixture: fixture).preferredColorScheme(.dark))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 780), styleMask: [.titled], backing: .buffered, defer: false)
        window.title = "AI 浮层验收（合成资料）"; window.contentView = host; window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        func press(_ identifier: String, view: NSView? = nil) throws {
            guard let element = accessibilityElement(identifier, in: view ?? host), element.accessibilityPerformPress?() == true else {
                throw NSError(domain: "AIOverviewFloatingTest", code: 1,
                    userInfo: [NSLocalizedDescriptionKey: "无法执行原生辅助功能按钮：\(identifier)"])
            }
        }
        @discardableResult func capture(_ name: String, view: NSView? = nil) throws -> NSBitmapImageRep {
            let view = view ?? host
            view.layoutSubtreeIfNeeded()
            let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds)!
            view.cacheDisplay(in: view.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: output).appendingPathComponent(name + ".png"))
            return bitmap
        }
        func upperReaderPixel(_ bitmap: NSBitmapImageRep) -> [CGFloat] {
            // This fixture leaves the area below the toolbar and above the document title empty.
            // AX removes fading nodes immediately, so also inspect the rendered surface for old-card remnants.
            let color = bitmap.colorAt(x: bitmap.pixelsWide / 2, y: Int(Double(bitmap.pixelsHigh) * 0.105))!.usingColorSpace(.deviceRGB)!
            return [color.redComponent, color.greenComponent, color.blueComponent]
        }
        try await Task.sleep(for: .milliseconds(1200)); try capture("initial-hidden")
        let before = fixture.readerFrame
        precondition(!fixture.presentation.isVisible && !fixture.presentation.isExpanded)
        precondition(accessibilityElement("ai-overview-details-toggle", in: host) == nil)
        try press("ai-overview-toggle")
        try await Task.sleep(for: .milliseconds(400)); try capture("collapsed")
        precondition(fixture.presentation.isVisible && !fixture.presentation.isExpanded)
        for (phase, name) in [(AIReadPhase.loading, "loading"), (AIReadPhase.failed, "failed")] {
            model.aiReadStatus = AIReadStatus(sourceID: "floating-test", sourceGeneration: model.sourceLoadGeneration, phase: phase)
            try await Task.sleep(for: .milliseconds(100))
            let toolbar = accessibilityElement("ai-overview-toggle", in: host)!
            precondition(accessibilityElement("AI 分析处理中", matchingLabel: true, in: toolbar) == nil)
            let requests = OverviewAPIStub.paths.count
            try press("ai-overview-toggle")
            precondition(!fixture.presentation.isVisible && !fixture.presentation.isExpanded)
            precondition(OverviewAPIStub.paths.count == requests && model.aiSourceReadPhase == phase)
            try await Task.sleep(for: .milliseconds(400)); try capture(name + "-hidden")
            precondition(before == fixture.readerFrame && OverviewAPIStub.paths.count == requests)
            try press("ai-overview-toggle")
            precondition(fixture.presentation.isVisible && !fixture.presentation.isExpanded)
            precondition(OverviewAPIStub.paths.count == requests && model.aiSourceReadPhase == phase)
            try await Task.sleep(for: .milliseconds(400)); try capture(name + "-visible")
            precondition(before == fixture.readerFrame && OverviewAPIStub.paths.count == requests)
        }
        model.aiReadStatus = AIReadStatus(sourceID: "floating-test", sourceGeneration: model.sourceLoadGeneration, phase: .ready)
        try await Task.sleep(for: .milliseconds(100))
        try press("ai-overview-toggle")
        try await Task.sleep(for: .milliseconds(400)); try capture("hidden")
        precondition(before == fixture.readerFrame && !fixture.presentation.isVisible && !fixture.presentation.isExpanded)
        try press("ai-overview-toggle")
        try await Task.sleep(for: .milliseconds(400)); try capture("toolbar-reopened")
        precondition(before == fixture.readerFrame && fixture.presentation.isVisible && !fixture.presentation.isExpanded)
        try press("ai-overview-details-toggle")
        try await Task.sleep(for: .milliseconds(70)); try capture("expanding-070ms")
        try await Task.sleep(for: .milliseconds(60)); try capture("expanding-130ms")
        try await Task.sleep(for: .milliseconds(270)); try capture("expanded")
        precondition(before == fixture.readerFrame && before.height > 200 && fixture.presentation.isExpanded)
        try press("ai-overview-details-toggle")
        try await Task.sleep(for: .milliseconds(70)); try capture("collapsing-070ms")
        try await Task.sleep(for: .milliseconds(60)); try capture("collapsing-130ms")
        try await Task.sleep(for: .milliseconds(270)); try capture("arrow-collapsed")
        precondition(before == fixture.readerFrame && fixture.presentation.isVisible && !fixture.presentation.isExpanded)
        try press("ai-overview-details-toggle")
        try await Task.sleep(for: .milliseconds(400))
        precondition(fixture.presentation.isVisible && fixture.presentation.isExpanded)
        try press("ai-overview-toggle")
        try await Task.sleep(for: .milliseconds(400)); try capture("details-hidden")
        precondition(before == fixture.readerFrame && !fixture.presentation.isVisible && !fixture.presentation.isExpanded)
        try press("ai-overview-toggle")
        try await Task.sleep(for: .milliseconds(400)); try capture("details-reopened-as-title")
        precondition(before == fixture.readerFrame && fixture.presentation.isVisible && !fixture.presentation.isExpanded)
        try press("ai-overview-details-toggle")
        try await Task.sleep(for: .milliseconds(400))
        window.setContentSize(NSSize(width: 600, height: 680))
        try await Task.sleep(for: .milliseconds(500)); try capture("narrow-expanded")
        let narrow = fixture.readerFrame
        try press("ai-overview-details-toggle")
        try await Task.sleep(for: .milliseconds(500)); try capture("narrow-collapsed")
        precondition(narrow == fixture.readerFrame && !fixture.presentation.isExpanded)
        try press("ai-overview-details-toggle")
        try await Task.sleep(for: .milliseconds(400))
        precondition(fixture.presentation.isExpanded)
        model.aiJobs = [job]
        try await Task.sleep(for: .milliseconds(500)); try capture("reanalyzing")
        precondition(narrow == fixture.readerFrame && !fixture.presentation.isExpanded)
        precondition(!AIOverviewState(sourceID: model.selectedSource?.id, jobs: model.aiJobs, artifacts: model.aiArtifacts).canExpand)
        precondition(accessibilityElement("ai-overview-details-toggle", in: host) == nil)
        try press("ai-overview-toggle")
        try await Task.sleep(for: .milliseconds(400)); try capture("processing-hidden")
        precondition(narrow == fixture.readerFrame && !fixture.presentation.isVisible && !fixture.presentation.isExpanded)
        precondition(model.aiJobs.count == 1 && model.aiJobs.first?.id == job.id && model.aiJobs.first?.isActive == true)
        try press("ai-overview-toggle")
        try await Task.sleep(for: .milliseconds(400)); try capture("processing-visible")
        precondition(narrow == fixture.readerFrame && fixture.presentation.isVisible && !fixture.presentation.isExpanded)
        precondition(model.aiJobs.count == 1 && model.aiJobs.first?.id == job.id && model.aiJobs.first?.isActive == true)
        precondition(accessibilityElement("ai-overview-details-toggle", in: host) == nil)
        model.aiJobs = []; model.aiArtifacts = []; model.aiReadStatus = nil
        model.sourceLoadGeneration = UUID()
        OverviewAPIStub.deliveryDelay = 0.15
        let production = NSHostingView(rootView: KnowledgeView(model: model, showSettings: .constant(false)).preferredColorScheme(.dark))
        window.setContentSize(NSSize(width: 1280, height: 820)); window.contentView = production
        try await Task.sleep(for: .milliseconds(50)); try capture("startup-loading", view: production)
        precondition(model.aiSourceReadPhase == .loading)
        precondition(accessibilityElement("AI 知识概览", matchingLabel: true, in: production) == nil)
        precondition(accessibilityElement("AI 分析处理中", matchingLabel: true, in: production) == nil)
        for _ in 0..<30 {
            if model.aiSourceReadPhase == .ready { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        precondition(model.aiSourceReadPhase == .ready && !model.aiArtifacts.isEmpty)
        try await Task.sleep(for: .milliseconds(300))
        let emptyReaderPixel = upperReaderPixel(try capture("startup-restored", view: production))
        precondition(accessibilityElement("AI 知识概览", matchingLabel: true, in: production) == nil)
        try press("ai-overview-toggle", view: production)
        try await Task.sleep(for: .milliseconds(400)); try capture("startup-explicit-open", view: production)
        precondition(accessibilityElement("ai-overview-details-toggle", in: production) != nil)
        precondition(accessibilityElement("ai-overview-reanalyze", in: production) == nil)
        try press("ai-overview-details-toggle", view: production)
        try await Task.sleep(for: .milliseconds(400))
        precondition(accessibilityElement("ai-overview-reanalyze", in: production) != nil)
        for (index, sourceID) in ["floating-second", "floating-test"].enumerated() {
            var switchedArtifact = body
            switchedArtifact["source_id"] = sourceID
            OverviewAPIStub.artifact = switchedArtifact
            model.sourceLoadGeneration = UUID()
            model.selectedSource = KnowledgeDetail(["id": sourceID, "title": "切换资料 \(index + 1)", "content_type": "link", "revision": 1, "extracted_text": text])!
            try await Task.sleep(for: .milliseconds(50))
            let switchedPixel = upperReaderPixel(try capture("switch-\(index + 1)-loading", view: production))
            precondition(zip(emptyReaderPixel, switchedPixel).allSatisfy { abs($0.0 - $0.1) < 1.0 / 255 },
                "切换资料时不应有旧概览的淡出残影，也不应闪出新资料的加载标题")
            precondition(model.aiSourceReadPhase == .loading)
            precondition(accessibilityElement("AI 知识概览", matchingLabel: true, in: production) == nil)
            precondition(accessibilityElement("AI 分析处理中", matchingLabel: true, in: production) == nil)
            for _ in 0..<30 {
                if model.aiSourceReadPhase == .ready { break }
                try await Task.sleep(for: .milliseconds(100))
            }
            precondition(model.aiSourceReadPhase == .ready && model.aiArtifacts.first?.source_id == sourceID)
            try await Task.sleep(for: .milliseconds(300)); try capture("switch-\(index + 1)-restored", view: production)
            precondition(accessibilityElement("AI 知识概览", matchingLabel: true, in: production) == nil)
            try press("ai-overview-toggle", view: production)
            try await Task.sleep(for: .milliseconds(400)); try capture("switch-\(index + 1)-explicit-open", view: production)
            precondition(accessibilityElement("ai-overview-details-toggle", in: production) != nil)
            precondition(accessibilityElement("ai-overview-reanalyze", in: production) == nil)
        }
        precondition(!OverviewAPIStub.paths.contains(where: { $0.contains("/plan") || $0 == "POST /v1/ai/jobs" }))
        print("PASS: actual KnowledgeView starts hidden while silently restoring saved analysis; delayed article switches reset hidden and never expose a loading spinner; restored results stay hidden until explicit toolbar press opens title only")
        print("PASS: toolbar hides title or expanded details and reopens title only; reader frame unchanged across hidden, collapsed and expanded overlay at 1100 and 600 points")
        print("PASS: native accessibility presses trigger production toolbar and independent detail-arrow callbacks; processing exposes no detail toggle")
        print("PASS: loading and failed-read toolbar presses immediately toggle the window without extra GET/POST requests or changing read phase")
        print("PASS: active job survives overview visibility toggles with details collapsed; new analysis auto-collapses; native tag chips and reanalysis footer rendered")
        window.orderOut(nil)
    }
}

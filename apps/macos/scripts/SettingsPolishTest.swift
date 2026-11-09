import AppKit
import SwiftUI

private final class SettingsPolishAPI: URLProtocol {
    static var configured = false
    static var failConfiguration = false
    static var configurationDelay: TimeInterval = 0
    static var paths: [String] = []
    static let lock = NSLock()
    override class func canInit(with request: URLRequest) -> Bool { request.url?.host == "settings-polish.test" }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        Self.lock.lock()
        let path = request.url!.path
        Self.paths.append((request.httpMethod ?? "GET") + " " + path)
        let fail = path == "/v1/ai/config" && Self.failConfiguration
        let delay = path == "/v1/ai/config" ? Self.configurationDelay : 0
        let body: [String: Any]
        switch path {
        case "/v1/me": body = ["id": "synthetic-settings-owner", "display_name": "合成验收"]
        case "/v1/ai/config": body = ["configured": Self.configured]
        case "/v1/ai/plan": body = ["plan_hash": "synthetic-plan", "calls": 1, "sections": 1,
            "coverage": "full", "covered_chars": 20, "total_chars": 20, "source_coverage": "full_text"]
        case "/v1/agent-connections":
            body = ["items": ["Codex", "Claude Code", "Workbuddy"].enumerated().map { index, name in
                ["id": "00000000-0000-4000-8000-00000000000\(index)", "client_name": name,
                 "status": "active", "created_at": "2026-10-30T00:00:00Z", "expires_at": "2026-11-29T00:00:00Z",
                 "domain_ids": ["domain-1"]] as [String: Any]
            }]
        default: body = ["items": []]
        }
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
    static func reset() {
        lock.lock(); defer { lock.unlock() }
        configured = false; failConfiguration = false; configurationDelay = 0; paths = []
    }
    static var requests: [String] { lock.lock(); defer { lock.unlock() }; return paths }
}

@MainActor private final class SettingsPolishFixture: ObservableObject {
    @Published var selection: Set<String> = ["domain-1"]
    @Published var pickerHeight: CGFloat = 0
    @Published var overviewToggles = 0
    let domains = (1...36).map { DomainItem(id: "domain-\($0)", name: String(format: "知识库 %02d · 合成资料", $0)) }
}

@MainActor private struct SettingsPolishPicker: View {
    @ObservedObject var fixture: SettingsPolishFixture
    var body: some View {
        Surface {
            AgentLibraryPicker(domains: fixture.domains, selection: $fixture.selection)
        }
        .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { fixture.pickerHeight = $0 }
        .padding(24)
        .frame(maxHeight: .infinity, alignment: .top)
        .background(InboxStyle.canvas).foregroundStyle(InboxStyle.text)
        .buttonStyle(InboxButtonStyle(.secondary))
    }
}

@MainActor private struct SettingsPolishAI: View {
    @ObservedObject var model: CaptureModel
    @ObservedObject var fixture: SettingsPolishFixture
    var body: some View {
        VStack {
            HStack {
                Text("AI 分析 · 合成资料").font(.headline)
                Spacer()
                LocalAIAnalyzeButton(model: model, compact: true,
                    showOverview: { fixture.overviewToggles += 1 }, showDetails: {})
                    .buttonStyle(InboxIconButtonStyle())
            }
            Spacer()
        }.padding(24).frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(InboxStyle.canvas).foregroundStyle(InboxStyle.text)
    }
}

@main struct SettingsPolishTest {
    @MainActor static func main() {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.regular)
        NSApp.accessibilitySetValue(true, forAttribute: NSAccessibility.Attribute(rawValue: "AXEnhancedUserInterface"))
        Task { @MainActor in
            do { try await run(); NSApp.terminate(nil) }
            catch { print("FAIL: \(error)"); exit(1) }
        }
        NSApp.run()
    }

    @MainActor private static func run() async throws {
        setbuf(stdout, nil)
        let manager = FileManager.default
        let root = manager.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("pka-settings-polish-\(UUID())")
        try manager.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        defer { try? manager.removeItem(at: root) }
        let output = ProcessInfo.processInfo.environment["PKA_SETTINGS_POLISH_OUTPUT"].map { URL(fileURLWithPath: $0) }
        let model = CaptureModel(queueStore: CaptureQueueStore(root: root.appendingPathComponent("queue")),
            localService: LocalService(home: root, credentialService: "synthetic.settings-polish.\(UUID())"), runtime: root)
        model.address = "https://settings-polish.test"
        URLProtocol.registerClass(SettingsPolishAPI.self)
        defer { URLProtocol.unregisterClass(SettingsPolishAPI.self) }
        await model.connect()
        try check(model.connected, "合成连接未就绪")
        let library = try model.localService.currentLibrary()
        defer {
            for suffix in ["", ".mode", ".profile.v1"] {
                UserDefaults.standard.removeObject(forKey: LocalModelConfiguration.key(library) + suffix)
            }
        }
        let fixture = SettingsPolishFixture()
        model.domains = fixture.domains
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 700, height: 700),
            styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "设置与分析交互验收（合成数据）"
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        defer { window.orderOut(nil) }

        let feishu = FeishuConnection(); feishu.executablePath = ""; feishu.profile = ""
        window.contentView = NSHostingView(rootView: FeishuSettingsView(connection: feishu)
            .padding(24).preferredColorScheme(.dark).background(InboxStyle.canvas)
            .foregroundStyle(InboxStyle.text).buttonStyle(.plain))
        try await pause()
        try check(find("飞书 CLI 路径", in: window, label: true) != nil && find("飞书 CLI 配置名称", in: window, label: true) != nil,
                  "飞书配置输入框未显示")
        for title in ["检查连接", "连接 / 补充读取授权", "断开连接"] {
            try check(find(title, in: window, label: true) != nil, "飞书操作按钮缺失：\(title)")
        }
        try capture(window.contentView!, named: "feishu", output: output)
        print("PASS: 飞书输入框及全部连接按钮原生渲染，父级plain样式下仍保留显式统一样式")

        window.contentView = NSHostingView(rootView: SettingsPolishPicker(fixture: fixture).preferredColorScheme(.dark))
        try await pause()
        let expandedHeight = fixture.pickerHeight
        try press("agent-library-toggle", in: window)
        try await pause()
        try check(fixture.pickerHeight < expandedHeight - 100 && fixture.selection == ["domain-1"], "收起未缩减高度或改变授权")
        try check(find("查找授权知识库", in: window, label: true) == nil, "收起后仍展示筛选框")
        try press("选择全部", in: window, label: true)
        try await pause()
        try check(fixture.selection.count == 36, "收起状态下全选失败")
        try capture(window.contentView!, named: "libraries-collapsed", output: output)
        try press("agent-library-toggle", in: window)
        try await pause()
        try check(find("查找授权知识库", in: window, label: true) != nil && fixture.selection.count == 36,
                  "展开丢失筛选入口或授权")
        try capture(window.contentView!, named: "libraries-expanded", output: output)
        print("PASS: 36库列表真实按钮展开/收起、高度自适应、收起后全选及选择保留")

        let installer = LocalAgentIntegration(home: root)
        window.contentView = NSHostingView(rootView: ScrollView {
            LocalAgentConnectionsView(model: model, installer: installer).padding(24)
        }.preferredColorScheme(.dark).background(InboxStyle.canvas).foregroundStyle(InboxStyle.text)
            .buttonStyle(InboxButtonStyle(.secondary)).textFieldStyle(InboxFieldStyle()))
        try await pause()
        try press("agent-library-toggle", in: window)
        try await pause()
        try check(find("高级设置与手动接入", in: window, label: true) == nil, "高级设置仍在显示")
        try check(find("更多连接操作", in: window, label: true) != nil, "当前连接缺少必要的更多操作")
        try press("Workbuddy", in: window, label: true)
        try await pause()
        try check(find("复制配置", in: window, label: true) != nil && find("断开连接", in: window, label: true) != nil,
                  "移除高级设置导致Workbuddy手动接入或撤销入口丢失")
        try capture(window.contentView!, named: "workbuddy", output: output)
        print("PASS: 高级区已移除，Codex更多操作及Workbuddy复制/断开入口保留，无凭证或宿主配置写入")

        func select(_ id: String) {
            model.selectedSource = KnowledgeDetail(["id": id, "title": "合成验收文章", "content_type": "note",
                "revision": 1, "extracted_text": "用于验证未配置模型时的提示，不包含个人资料。"])
            model.sourceLoadGeneration = UUID(); model.aiArtifacts = []; model.aiJobs = []; model.aiReadStatus = nil
        }
        select("ai-unconfigured")
        SettingsPolishAPI.reset()
        window.contentView = NSHostingView(rootView: SettingsPolishAI(model: model, fixture: fixture).preferredColorScheme(.dark))
        try await pause()
        try press("ai-overview-toggle", in: window)
        try await pause()
        try check(find("请先配置 AI", in: window, label: true) != nil, "首次状态未加载时点击未配置AI没有提示")
        try check(!SettingsPolishAPI.requests.contains { $0.contains("/plan") || $0 == "POST /v1/ai/jobs" },
                  "未配置AI仍请求分析预览或创建任务")
        try capture(window.attachedSheet?.contentView ?? window.contentView!, named: "ai-configuration-required", output: output)
        try press("前往配置", in: window, label: true)
        try await pause()
        try check(find("API Key", in: window, label: true) != nil && find("模型与 AI", in: window, label: true) != nil,
                  "配置入口未打开模型设置页")
        try press("完成", in: window, label: true)
        try await pause()
        print("PASS: 未分析文章首次点击即弹出配置提示，直接进入模型设置，没有预览/任务请求")

        SettingsPolishAPI.reset(); SettingsPolishAPI.configured = true
        try press("ai-overview-toggle", in: window)
        try await pause()
        try check(find("开始 AI 分析？", in: window, label: true) != nil, "已配置时未保留分析确认")
        try check(SettingsPolishAPI.requests.contains("POST /v1/ai/plan") && !SettingsPolishAPI.requests.contains("POST /v1/ai/jobs"),
                  "分析确认前创建了任务，或未正常获取范围")
        try press("取消", in: window, label: true)
        try await pause()
        print("PASS: 已配置模型仍需逐次确认，取消后不创建分析任务")

        SettingsPolishAPI.reset(); SettingsPolishAPI.failConfiguration = true
        try press("ai-overview-toggle", in: window)
        try await pause()
        try check(find("无法检查 AI 配置", in: window, label: true) != nil, "配置请求失败没有反馈")
        try press("好", in: window, label: true)
        try await pause()
        SettingsPolishAPI.reset(); SettingsPolishAPI.configurationDelay = 0.4
        try press("ai-overview-toggle", in: window)
        try await Task.sleep(for: .milliseconds(100))
        select("ai-another-source")
        try await pause()
        try check(find("请先配置 AI", in: window, label: true) == nil && fixture.overviewToggles == 0,
                  "切文后旧配置响应触发新文章弹窗或概览")
        print("PASS: 配置读取失败可见提示，切文迟到响应不会操作新文章")

        SettingsPolishAPI.reset()
        let id = model.selectedSource!.id
        let artifact = try decodeLocalAI(LocalAIArtifact.self, body: ["id": "synthetic-result", "source_id": id,
            "result": ["summary": "已保存的合成分析", "summary_citations": [], "insights": [], "warnings": []],
            "evidence": [], "provider": "synthetic", "model": "synthetic", "stale": false])
        model.aiArtifacts = [artifact]
        model.aiReadStatus = AIReadStatus(sourceID: id, sourceGeneration: model.sourceLoadGeneration, phase: .ready)
        try press("ai-overview-toggle", in: window)
        try await pause()
        try check(fixture.overviewToggles == 1 && SettingsPolishAPI.requests.isEmpty, "无模型时已保存结果不能直接查看")
        model.aiArtifacts = []
        model.aiJobs = [try decodeLocalAI(LocalAIJob.self, body: ["id": "synthetic-job", "source_id": id,
            "kind": "analyze", "status": "running"])]
        try press("ai-overview-toggle", in: window)
        try await pause()
        try check(fixture.overviewToggles == 2 && SettingsPolishAPI.requests.isEmpty, "已有任务不能直接打开进度")
        print("PASS: 已保存结果/执行中任务始终直接切换概览，不被模型配置检查阻塞")
    }

    @MainActor private static func pause() async throws { try await Task.sleep(for: .milliseconds(800)) }
    private static func check(_ value: @autoclosure () -> Bool, _ message: String) throws {
        guard value() else { throw failure(message) }
    }
    @MainActor private static func find(_ name: String, in root: Any, label: Bool = false, actionable: Bool = false) -> AnyObject? {
        var visited = Set<ObjectIdentifier>()
        func search(_ value: Any) -> AnyObject? {
            let object = value as AnyObject
            guard visited.insert(ObjectIdentifier(object)).inserted else { return nil }
            if let node = value as? NSObject {
                let selector = label ? #selector(NSAccessibilityProtocol.accessibilityLabel) : #selector(NSAccessibilityProtocol.accessibilityIdentifier)
                let role = (object.accessibilityRole?())?.rawValue ?? ""
                let canPress = !actionable || !label || ["AXButton", "AXMenuButton", "AXPopUpButton", "AXCheckBox", "AXRadioButton"].contains(role)
                if canPress, node.responds(to: selector), (label ? object.accessibilityLabel?() : object.accessibilityIdentifier?()) == name { return object }
                if canPress, label, node.responds(to: #selector(NSAccessibilityProtocol.accessibilityValue)),
                   node.perform(#selector(NSAccessibilityProtocol.accessibilityValue))?.takeUnretainedValue() as? String == name { return object }
                if node.responds(to: #selector(NSAccessibilityProtocol.accessibilityChildren)) {
                    for child in object.accessibilityChildren?() ?? [] { if let result = search(child) { return result } }
                }
            }
            if let window = value as? NSWindow {
                if let sheet = window.attachedSheet, let result = search(sheet) { return result }
                if let view = window.contentView, let result = search(view) { return result }
            }
            if let view = value as? NSView {
                for child in view.subviews { if let result = search(child) { return result } }
            }
            return nil
        }
        return search(root)
    }
    @MainActor private static func press(_ name: String, in window: NSWindow, label: Bool = false) throws {
        func nativeButton(_ view: NSView) -> NSButton? {
            if let button = view as? NSButton, button.title == name, button.isEnabled,
               !button.isHiddenOrHasHiddenAncestor { return button }
            for child in view.subviews { if let result = nativeButton(child) { return result } }
            return nil
        }
        if label, let sheet = window.attachedSheet, let content = sheet.contentView,
           let button = nativeButton(content) { button.performClick(nil); return }
        guard let element = find(name, in: window, label: label, actionable: true) else {
            throw failure("无法点击原生按钮：\(name)")
        }
        if element.accessibilityPerformPress?() == true { return }
        if let button = element as? NSButton, button.isEnabled { button.performClick(nil); return }
        if label, let target = window.attachedSheet, ["前往配置", "取消", "好"].contains(name) {
            let cancel = name == "取消"
            let text = cancel ? "\u{1b}" : "\r"
            if let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
                windowNumber: target.windowNumber, context: nil, characters: text,
                charactersIgnoringModifiers: text, isARepeat: false, keyCode: cancel ? 53 : 36) {
                target.makeKey()
                NSApp.postEvent(event, atStart: false)
                return
            }
        }
        throw failure("原生按钮未执行：\(name)")
    }
    @MainActor private static func capture(_ view: NSView, named name: String, output: URL?) throws {
        guard let output else { return }
        view.layoutSubtreeIfNeeded()
        guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { throw failure("截图失败") }
        view.cacheDisplay(in: view.bounds, to: bitmap)
        try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(name + ".png"))
    }
}

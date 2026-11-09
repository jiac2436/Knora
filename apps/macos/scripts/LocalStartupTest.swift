import AppKit
import SwiftUI

/// Uses only an isolated queue, memory credentials, fake owned processes and URLProtocol responses.
private final class StartupCredentials: CredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var values: [String: String]
    private var accessed: [String] = []

    init(values: [String: String]) { self.values = values }
    func read(service: String) -> CredentialRead {
        lock.lock(); defer { lock.unlock() }
        accessed.append(service)
        return values[service].map(CredentialRead.available) ?? .missing
    }
    func write(_ value: String, service: String) throws {
        lock.lock(); defer { lock.unlock() }
        accessed.append(service); values[service] = value
    }
    func remove(service: String) throws {
        lock.lock(); defer { lock.unlock() }
        accessed.append(service); values.removeValue(forKey: service)
    }
    var snapshot: [String: String] {
        lock.lock(); defer { lock.unlock() }
        return values
    }
    var services: [String] {
        lock.lock(); defer { lock.unlock() }
        return accessed
    }
}

private final class StartupAPIStub: URLProtocol {
    struct Call {
        let method: String
        let path: String
        let authorization: String?
    }
    private static let lock = NSLock()
    private static var recorded: [Call] = []
    private static var failMetadata = false

    static func reset(failing: Bool = false) {
        lock.lock(); defer { lock.unlock() }
        recorded = []; failMetadata = failing
    }
    static var calls: [Call] {
        lock.lock(); defer { lock.unlock() }
        return recorded
    }
    override class func canInit(with request: URLRequest) -> Bool {
        request.url?.host?.hasSuffix("startup.test") == true
    }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        let path = request.url!.path
        Self.lock.lock()
        Self.recorded.append(Call(method: request.httpMethod ?? "GET", path: path,
                                  authorization: request.value(forHTTPHeaderField: "Authorization")))
        let fail = Self.failMetadata && path == "/v1/me"
        Self.lock.unlock()
        let body: [String: Any]
        switch path {
        case "/v1/me": body = ["id": "synthetic-owner", "display_name": "本地用户"]
        case "/v1/domains": body = ["items": [["id": "synthetic-domain", "name": "合成知识库"]]]
        case "/v1/ocr/preferences": body = ["model": "small", "engine": "pdfium"]
        case "/v1/web/preferences": body = ["engine": "legacy", "cleaning_enabled": false]
        default: body = ["items": []]
        }
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: fail ? 503 : 200,
            httpVersion: nil, headerFields: ["Content-Type": "application/json"])!, cacheStoragePolicy: .notAllowed)
        let payload: [String: Any] = fail ? ["message": "合成资料服务暂不可用"] : body
        client?.urlProtocol(self, didLoad: try! JSONSerialization.data(withJSONObject: payload))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main struct LocalStartupTest {
    @MainActor static func main() {
        if ProcessInfo.processInfo.environment["PKA_STARTUP_PREVIEW"] != nil {
            _ = NSApplication.shared
            NSApp.setActivationPolicy(.regular)
            NSApp.accessibilitySetValue(true, forAttribute: NSAccessibility.Attribute(rawValue: "AXEnhancedUserInterface"))
            Task { @MainActor in
                do { try await run(); NSApp.terminate(nil) }
                catch { print("FAIL: \(error)"); exit(1) }
            }
            NSApp.run()
        } else {
            Task { @MainActor in
                do { try await run(); exit(0) }
                catch { print("FAIL: \(error)"); exit(1) }
            }
            RunLoop.main.run()
        }
    }

    @MainActor private static func run() async throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent("pka-startup-test-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: root) }
        // Registration is process-only. Existing user preferences and Keychain items are never changed.
        UserDefaults.standard.register(defaults: [
            "sessionMode": "development", "developmentEmail": "synthetic@example.invalid",
            "developmentToken": "synthetic-legacy-token", "apiAddress": "https://legacy.startup.test"
        ])
        URLProtocol.registerClass(StartupAPIStub.self)
        defer { URLProtocol.unregisterClass(StartupAPIStub.self) }

        let serviceKey = "synthetic.startup." + UUID().uuidString
        let secrets = [serviceKey: "synthetic-private-owner", serviceKey + ".model": "synthetic-model-key",
                       serviceKey + ".agent." + UUID().uuidString.lowercased(): "synthetic-agent-token"]
        let credentials = StartupCredentials(values: secrets)
        let service = LocalService(home: root.appendingPathComponent("home"),
                                   credentialService: serviceKey, credentials: credentials)
        defer { service.stopAndWait() }
        let queue = CaptureQueueStore(root: root.appendingPathComponent("queue"))
        let missing = CaptureModel(queueStore: queue, localService: service, runtime: nil)
        missing.draft = "免登录启动失败也必须保留的草稿"
        let draft = missing.draft
        await missing.openLocalLibrary()
        precondition(!missing.connected && !missing.authMessage.isEmpty)
        let missingSnapshot = try queue.load()
        precondition(missing.draft == draft && missingSnapshot?.draft == draft)
        precondition(StartupAPIStub.calls.isEmpty && credentials.services.isEmpty,
                     "运行包缺失时不能尝试旧账户登录或读取凭证")
        print("PASS: 缺少运行包仅报告本地诊断，无旧账户请求、无凭证访问，草稿保留")

        let runtime = root.appendingPathComponent("runtime")
        let python = MacPaths().runtime(runtime).python
        try manager.createDirectory(at: python.deletingLastPathComponent(), withIntermediateDirectories: true)
        try executable("#!/bin/sh\nIFS= read -r startup\nprintf '%s\\n' '{\"event\":\"failed\",\"message\":\"合成本地启动失败，资料已保留。\"}'\nexit 1\n", to: python)
        let failed = CaptureModel(queueStore: queue, localService: service, runtime: runtime)
        await failed.openLocalLibrary()
        precondition(!failed.connected && failed.authMessage.contains("合成本地启动失败"))
        let failedSnapshot = try queue.load()
        precondition(failed.draft == draft && failedSnapshot?.draft == draft)
        precondition(credentials.snapshot == secrets, "服务启动失败不能清理本机、模型或 Agent 凭证")
        precondition(credentials.services.allSatisfy { $0.hasPrefix(serviceKey) })
        precondition(StartupAPIStub.calls.isEmpty)
        print("PASS: 内置服务失败保留草稿及独立本机/模型/Agent 凭证，不回退账户登录")

        if let output = ProcessInfo.processInfo.environment["PKA_STARTUP_PREVIEW"] {
            try manager.createDirectory(atPath: output, withIntermediateDirectories: true)
            try await preview(missing, named: "missing-runtime", output: output)
            try await preview(failed, named: "failed-local-service", output: output)
        }

        let metadata = CaptureModel(queueStore: queue, localService: service, runtime: runtime)
        metadata.address = "https://metadata.startup.test"
        metadata.token = "synthetic-private-owner"
        StartupAPIStub.reset()
        await metadata.connect()
        precondition(metadata.connected && metadata.token == "synthetic-private-owner")
        let expected = ["/v1/me", "/v1/domains", "/v1/projects", "/v1/ocr/preferences", "/v1/web/preferences"]
        precondition(Array(StartupAPIStub.calls.prefix(expected.count)).map(\.path) == expected)
        precondition(StartupAPIStub.calls.prefix(expected.count).allSatisfy { $0.method == "GET" })
        precondition(StartupAPIStub.calls.allSatisfy { $0.authorization == "Bearer synthetic-private-owner" })
        precondition(!StartupAPIStub.calls.contains { $0.path.contains("/auth/") || $0.path.contains("/dev/") })
        precondition(credentials.snapshot == secrets)
        print("PASS: 内部 metadata 连接保留 GET/Bearer 合约，无账户、密码或开发会话请求")

        metadata.draft = "连接中断时仍需保留的草稿"
        metadata.addDraft()
        metadata.draft = draft
        let before = try Data(contentsOf: queue.manifest)
        let pendingIDs = metadata.items.map(\.id)
        StartupAPIStub.reset(failing: true)
        await metadata.connect()
        precondition(!metadata.connected && metadata.authMessage.contains("合成资料服务暂不可用"))
        precondition(metadata.draft == draft && metadata.items.map(\.id) == pendingIDs)
        let after = try Data(contentsOf: queue.manifest)
        precondition(after == before)
        precondition(StartupAPIStub.calls.count == 1 && StartupAPIStub.calls[0].path == "/v1/me")
        precondition(credentials.snapshot == secrets)
        print("PASS: metadata 连接失败保留待处理内容、草稿和全部独立凭证，不发起登录回退")
    }

    private static func executable(_ text: String, to url: URL) throws {
        try Data(text.utf8).write(to: url)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
    }

    @MainActor private static func preview(_ model: CaptureModel, named name: String, output: String) async throws {
        let host = NSHostingView(rootView: WorkspaceView(model: model))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1100, height: 720),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.contentView = host
        window.title = "本地启动 · 合成验收"
        window.makeKeyAndOrderFront(nil)
        defer { window.orderOut(nil) }
        try await Task.sleep(for: .milliseconds(700))
        host.layoutSubtreeIfNeeded()
        let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
        host.cacheDisplay(in: host.bounds, to: bitmap)
        try bitmap.representation(using: .png, properties: [:])!.write(to:
            URL(fileURLWithPath: output).appendingPathComponent(name + ".png"))
        let labels = accessibilityLabels(in: host)
        let title = model.usesLocalService ? "本地知识库暂时无法打开" : "客户端运行包不完整"
        try labels.joined(separator: "\n").write(to: URL(fileURLWithPath: output).appendingPathComponent(name + "-accessibility.txt"), atomically: true, encoding: .utf8)
        precondition(labels.contains(where: { $0.contains(title) }), "未显示本地诊断界面")
        for phrase in ["密码", "退出登录", "创建账户", "忘记密码", "无需密码开发登录"] {
            precondition(!labels.contains(where: { $0.contains(phrase) }), "仍有账户入口：\(phrase)")
        }
        print("PASS: Workspace 原生\(name)界面仅显示本地诊断，无登录/密码入口")
    }

    @MainActor private static func accessibilityLabels(in root: Any) -> [String] {
        var visited = Set<ObjectIdentifier>()
        func collect(_ value: Any) -> [String] {
            let object = value as AnyObject
            guard visited.insert(ObjectIdentifier(object)).inserted else { return [] }
            var labels: [String] = []
            if let item = value as? NSObject {
                if item.responds(to: #selector(NSAccessibilityProtocol.accessibilityLabel)),
                   let label = object.accessibilityLabel?() { labels.append(label) }
                if item.responds(to: #selector(NSAccessibilityProtocol.accessibilityValue)),
                   let text = item.perform(NSSelectorFromString("accessibilityValue"))?.takeUnretainedValue() as? String {
                    labels.append(text)
                }
                if item.responds(to: #selector(NSAccessibilityProtocol.accessibilityChildren)) {
                    for child in object.accessibilityChildren?() ?? [] { labels += collect(child) }
                }
            }
            if let view = value as? NSView { for child in view.subviews { labels += collect(child) } }
            return labels
        }
        return collect(root)
    }
}

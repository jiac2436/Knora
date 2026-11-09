import Foundation
import Darwin

private final class HostQuietCredentials: CredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var ownerResult: CredentialRead
    private var calls: [String] = []
    private var forbidden = 0
    init(owner: CredentialRead) { ownerResult = owner }
    func read(service: String) -> CredentialRead {
        lock.lock(); defer { lock.unlock() }; forbidden += 1; return .denied
    }
    func readWithoutInteraction(service: String) -> CredentialRead {
        lock.lock(); defer { lock.unlock() }; calls.append(service)
        return service.hasSuffix(".model") || service.hasSuffix(".ai-gateway") ? .unavailable : ownerResult
    }
    func write(_ value: String, service: String) throws {
        lock.lock(); defer { lock.unlock() }; forbidden += 1
        throw failure("合成启动测试不允许写入钥匙串")
    }
    func remove(service: String) throws {
        lock.lock(); defer { lock.unlock() }; forbidden += 1
        throw failure("合成启动测试不允许删除钥匙串")
    }
    func denyOwner() { lock.lock(); defer { lock.unlock() }; ownerResult = .denied }
    var interactiveCount: Int { lock.lock(); defer { lock.unlock() }; return forbidden }
    func quietCount(_ service: String) -> Int {
        lock.lock(); defer { lock.unlock() }; return calls.filter { $0 == service }.count
    }
}

/// Real bundled host/API/worker, temporary libraries and synthetic credential providers only.
@main struct LocalDeviceCredentialHostTest {
    static func main() async throws {
        setbuf(stdout, nil)
        guard CommandLine.arguments.count == 2 else { throw failure("需要已验证的运行包路径") }
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1]).resolvingSymlinksInPath()
        let manager = FileManager.default
        let root = manager.temporaryDirectory.resolvingSymlinksInPath()
            .appendingPathComponent("pka-device-host-\(UUID().uuidString)")
        let home = root.appendingPathComponent("home")
        try manager.createDirectory(at: home, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let namespace = "synthetic.device-host." + UUID().uuidString
        let oldToken = String(repeating: "a", count: 64)
        let credentials = HostQuietCredentials(owner: .available(oldToken))
        let firstService = LocalService(home: home, credentialService: namespace, credentials: credentials)
        let library = try firstService.currentLibrary()
        var services = [firstService]
        var libraries = [library]
        defer {
            for service in services { service.stopAndWait() }
            for item in libraries {
                for suffix in ["", ".mode", ".profile.v1"] {
                    UserDefaults.standard.removeObject(forKey: LocalModelConfiguration.key(item) + suffix)
                }
            }
            try? manager.removeItem(at: root)
        }
        // A configured model whose Keychain item needs interaction must not block opening the library.
        UserDefaults.standard.set(try JSONEncoder().encode(LocalModelOptions(model: "synthetic-unavailable-model")),
                                  forKey: LocalModelConfiguration.key(library))
        UserDefaults.standard.set("direct", forKey: LocalModelConfiguration.key(library) + ".mode")
        let first = try await Task.detached { try firstService.start(runtime: runtime) }.value
        try check(first.token == oldToken, "无需交互可读的旧设备凭证未原值迁移")
        let repeated = try await Task.detached { try firstService.start(runtime: runtime) }.value
        try check(first.token == repeated.token && first.address == repeated.address, "重复start意外替换运行中的连接")
        let api = try API(address: first.address, token: first.token)
        let originalOwner = try await api.call("v1/me")["id"] as? String
        let identity = try inspect(runtime: runtime, library: library)
        let domain = try await api.call("v1/domains", method: "POST", json: ["name": "设备凭证合成知识库"])
        guard let domainID = domain["id"] as? String else { throw failure("合成知识库未创建") }
        let marker = "syntheticdevicestableowner"
        let capture = try await api.call("v1/captures", method: "POST", json: [
            "capture_type": "text", "title": "设备凭证回归笔记", "coverage": "full_text",
            "content": ["text": marker + " 更换设备存储方式不应改变资料。", "image_urls": []] as [String: Any],
            "context": ["domain_id": domainID], "client": ["kind": "desktop", "version": "synthetic-device-test"]],
            key: UUID().uuidString)
        guard let sourceID = capture["id"] as? String else { throw failure("合成资料未保存") }
        let connections = try LocalAgentConnectionsAPI(address: first.address, token: first.token)
        let agent = try await connections.create(name: "合成设备凭证回归 Agent", domainIDs: [domainID], days: 1)
        try await checkAgent(address: first.address, token: agent.access_token, marker: marker, sourceID: sourceID)
        try check(credentials.interactiveCount == 0 && credentials.quietCount(namespace) == 1 &&
                  credentials.quietCount(LocalModelConfiguration.key(library)) > 0,
                  "首次打开库触发了交互凭证读取，或未验证静默模型密钥路径")
        print("PASS: 真实内核静默迁移设备凭证，模型密钥需授权时基础库仍能打开，零交互钥匙串调用")

        firstService.stopAndWait()
        let next = LocalService(home: home, credentialService: namespace, credentials: credentials)
        services.append(next)
        let second = try await Task.detached { try next.start(runtime: runtime) }.value
        let secondAPI = try API(address: second.address, token: second.token)
        let secondOwner = try await secondAPI.call("v1/me")["id"] as? String
        try check(second.token == oldToken && secondOwner == originalOwner &&
                  (try inspect(runtime: runtime, library: library)) == identity,
                  "客户端重启改变持久凭证、所有者或库/设备ID")
        _ = try await secondAPI.call("v1/sources/\(sourceID)")
        try await checkAgent(address: second.address, token: agent.access_token, marker: marker, sourceID: sourceID)
        try check(credentials.quietCount(namespace) == 1 && credentials.interactiveCount == 0,
                  "已有本地文件后重启仍读取旧设备钥匙串")
        print("PASS: 新宿主实例复用本地文件，owner/库/设备ID与资料不变，原 Agent 凭证跨重启有效")

        next.stopAndWait()
        // Simulate the pre-migration state where the old item exists but macOS will not reveal it quietly.
        try MacDeviceCredentialStore(home: home, legacy: credentials).remove(for: library)
        credentials.denyOwner()
        let denied = LocalService(home: home, credentialService: namespace, credentials: credentials)
        services.append(denied)
        let third = try await Task.detached { try denied.start(runtime: runtime) }.value
        try check(third.token != oldToken, "旧凭证拒绝访问时未生成新的本机凭证")
        try await expectUnauthorized(address: third.address, token: oldToken)
        let thirdAPI = try API(address: third.address, token: third.token)
        let thirdOwner = try await thirdAPI.call("v1/me")["id"] as? String
        try check(thirdOwner == originalOwner && (try inspect(runtime: runtime, library: library)) == identity,
                  "旧钥匙串拒绝访问改变了身份或资料库")
        _ = try await thirdAPI.call("v1/sources/\(sourceID)")
        try await checkAgent(address: third.address, token: agent.access_token, marker: marker, sourceID: sourceID)
        denied.stopAndWait()
        let stable = try await Task.detached { try denied.start(runtime: runtime) }.value
        try check(stable.token == third.token && credentials.quietCount(namespace) == 2 && credentials.interactiveCount == 0,
                  "新生成凭证未稳定保存，或恢复时重新访问旧设备钥匙串")
        print("PASS: 旧钥匙串拒绝时自动生成并保存，旧 owner token 返回401，资料和独立 Agent 授权保留")

        denied.stopAndWait()
        let package = root.appendingPathComponent("synthetic.pka-backup")
        _ = try await Task.detached {
            try LocalBackupTool.export(runtime: runtime, data: library.data, cache: library.cache, logs: library.logs, output: package)
        }.value
        let generation = denied.operationToken
        let restored = try await Task.detached {
            try denied.prepareRestoredLibrary(runtime: runtime, package: package, expectedGeneration: generation)
        }.value
        libraries.append(restored)
        try denied.activateLibrary(restored)
        let restoredConnection = try await Task.detached { try denied.start(runtime: runtime) }.value
        let restoredAPI = try API(address: restoredConnection.address, token: restoredConnection.token)
        let restoredOwner = try await restoredAPI.call("v1/me")["id"] as? String
        let restoredIdentity = try inspect(runtime: runtime, library: restored)
        try check(restoredOwner == originalOwner && restoredIdentity["owner_id"] == identity["owner_id"] &&
                  restoredIdentity["library_id"] == identity["library_id"] && restoredIdentity["device_id"] != identity["device_id"],
                  "恢复库没有保留库/owner身份或没有更新设备ID")
        _ = try await restoredAPI.call("v1/sources/\(sourceID)")
        try await expectUnauthorized(address: restoredConnection.address, token: agent.access_token)
        try check(credentials.interactiveCount == 0, "恢复库准备/probe/启动访问交互式钥匙串")
        denied.stopAndWait()
        let fresh = LocalService(home: home, credentialService: namespace, credentials: credentials)
        services.append(fresh)
        let reopened = try await Task.detached { try fresh.start(runtime: runtime) }.value
        try check(reopened.token == restoredConnection.token && credentials.quietCount(restored.credentialService) == 1,
                  "恢复库凭证未落盘或重启重新访问旧钥匙串")
        let reopenedAPI = try API(address: reopened.address, token: reopened.token)
        _ = try await reopenedAPI.call("v1/sources/\(sourceID)")
        try check(credentials.interactiveCount == 0, "完整回归包含交互钥匙串访问")
        print("PASS: 真实备份恢复probe、活动库切换和新实例重开全程免交互，恢复资料可读且未复制原设备Agent授权")
    }

    private static func check(_ value: Bool, _ message: String) throws {
        if !value { throw failure(message) }
    }
    private static func expectUnauthorized(address: String, token: String) async throws {
        do { _ = try await API(address: address, token: token).call("v1/me") }
        catch {
            try check((error as NSError).userInfo["HTTPStatus"] as? Int == 401, "旧凭证失败但不是预期401")
            return
        }
        throw failure("不应继续有效的凭证仍能访问")
    }
    private static func checkAgent(address: String, token: String, marker: String, sourceID: String) async throws {
        let api = try API(address: address, token: token)
        for _ in 0..<120 {
            let body = try await api.call("v1/mcp/tools/search_knowledge", method: "POST", json: ["query": marker, "limit": 20])
            if (body["items"] as? [[String: Any]] ?? []).contains(where: { $0["source_id"] as? String == sourceID }) { return }
            try await Task.sleep(for: .milliseconds(100))
        }
        throw failure("原 Agent 凭证不能检索合成资料")
    }
    private static func inspect(runtime: URL, library: LocalLibraryDescriptor) throws -> [String: String] {
        let process = Process(), output = Pipe()
        process.executableURL = runtime.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", "-B", "-c", "import json,sqlite3,sys; db=sqlite3.connect('file:'+sys.argv[1]+'/knowledge.db?mode=ro',uri=True); row=db.execute('SELECT owner_id,library_id,device_id FROM local_installations WHERE id=1').fetchone(); print(json.dumps(dict(zip(['owner_id','library_id','device_id'],row)))); db.close()", library.data.path]
        process.standardOutput = output
        try process.run()
        let data = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0, let result = try JSONSerialization.jsonObject(with: data) as? [String: String] else {
            throw failure("读取合成库身份失败")
        }
        return result
    }
}

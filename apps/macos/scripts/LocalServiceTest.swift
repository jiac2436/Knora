import Foundation

@main struct LocalServiceTest {
    static func main() async throws {
        guard CommandLine.arguments.count == 2 else { fatalError("需要运行包路径") }
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
        let home = FileManager.default.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("pka-service-test-\(UUID().uuidString)")
        let key = "local.pka.service-test.\(UUID().uuidString)"
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let service = LocalService(home: home, credentialService: key)
        defer {
            service.stop()
            try? FileManager.default.removeItem(at: home)
        }
        let first = try await Task.detached { try service.start(runtime: runtime) }.value
        let api = try API(address: first.address, token: first.token)
        let user = try await api.call("v1/me")
        guard let owner = user["id"] as? String else { fatalError("本地身份缺失") }
        let library = try service.currentLibrary()
        let privateCredential = try MacDeviceCredentialStore(home: home, legacy: KeychainCredentialStore()).loadOrCreate(for: library)
        assert(privateCredential == first.token)
        let domain = try await api.call("v1/domains", method: "POST", json: ["name": "本地集成测试"])
        _ = try await api.call("v1/captures", method: "POST", json: [
            "capture_type": "browser_selection", "title": "本地持久化测试", "coverage": "partial_text",
            "content": ["text": "重启后资料应仍可检索", "image_urls": []] as [String: Any],
            "context": ["domain_id": domain["id"]!] as [String: Any],
            "client": ["kind": "desktop", "version": "test"]], key: UUID().uuidString)
        service.stop()
        var stopped = false
        for _ in 0..<60 {
            do { _ = try await api.call("health") }
            catch { stopped = true; break }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        assert(stopped, "退出后 API 应停止")
        // Allow the host to finish its worker cleanup and release the instance lock.
        try await Task.sleep(nanoseconds: 1_000_000_000)
        let second = try await Task.detached { try service.start(runtime: runtime) }.value
        assert(second.token == first.token)
        let restarted = try API(address: second.address, token: second.token)
        let restored = try await restarted.call("v1/me")
        assert(restored["id"] as? String == owner)
        let sources = try await restarted.call("v1/sources")
        assert((sources["items"] as? [[String: Any]])?.count == 1)
        print("PASS: 包内 Python、动态端口、私有文件设备凭证、本地采集、退出清理及重启持久化")
        service.stop()
        try await Task.sleep(nanoseconds: 1_000_000_000)
    }
}

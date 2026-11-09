import Foundation

private final class SyntheticCredentials: CredentialStore {
    private var values: [String: String] = [:]
    func read(service: String) -> CredentialRead { values[service].map(CredentialRead.available) ?? .missing }
    func write(_ value: String, service: String) throws { values[service] = value }
    func remove(service: String) throws { values.removeValue(forKey: service) }
}

private struct CommandStub: RestrictedCommandRunning {
    var exitCode: Int32 = 0
    var error: RestrictedCommandFailure?
    func run(executable: URL, arguments: [String], timeout: TimeInterval, workingDirectory: URL?,
             maximumDirectoryBytes: Int?) async throws -> RestrictedCommandResult {
        precondition(arguments.prefix(2) == ["--profile", "synthetic"])
        precondition(maximumDirectoryBytes == FeishuImages.maximumImageBytes)
        if let error { throw error }
        return RestrictedCommandResult(output: Data("{\"ok\":true}".utf8),
            errors: Data("{\"error\":{\"type\":\"auth\"}}".utf8), exitCode: exitCode)
    }
}

private struct OCRStub: ImageTextRecognizing {
    func recognize(file: URL) throws -> String { "合成图片文字" }
}

@main struct PlatformSupportTest {
    static func main() async throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent("pka-platform-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: root) }
        let runtime = root.appendingPathComponent("runtime")
        let layout = MacPaths().runtime(runtime)
        try manager.createDirectory(at: layout.python.deletingLastPathComponent(), withIntermediateDirectories: true)
        precondition(layout.backend == runtime.appendingPathComponent("backend"))
        let developmentID = try LocalKernelContract.runtimeID(runtime)
        precondition(developmentID == "development")
        let manifest = runtime.appendingPathComponent("manifest.json")
        try Data("{\"files\":[]}".utf8).write(to: manifest)
        let identity = try LocalKernelContract.runtimeID(runtime)
        precondition(identity.count == 64)
        let valid = LocalKernelInfo(kernel_version: "0.1.0", contract_version: 1, schema_version: 16,
            platform: "macos", architecture: "arm64", runtime_id: identity,
            capabilities: ["private_files": true, "file_locks": true, "owned_processes": true, "desktop_client": true, "capture_queue": true])
        try LocalKernelContract.validate(valid, expectedRuntimeID: identity)
        var object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(valid)) as! [String: Any]
        for (key, value) in [("contract_version", 2 as Any), ("schema_version", 999 as Any),
                             ("platform", "windows" as Any), ("runtime_id", "other" as Any),
                             ("capabilities", ["private_files": true] as Any)] {
            var altered = object; altered[key] = value
            let info = try JSONDecoder().decode(LocalKernelInfo.self, from: JSONSerialization.data(withJSONObject: altered))
            try rejected { try LocalKernelContract.validate(info, expectedRuntimeID: identity) }
        }
        object.removeValue(forKey: "contract_version")
        try rejected { _ = try JSONDecoder().decode(LocalKernelInfo.self, from: JSONSerialization.data(withJSONObject: object)) }
        print("PASS: 路径兼容、运行包身份、版本/schema/能力拒绝")

        let cli = FeishuCLI(executable: layout.python, profile: " synthetic ", commands: CommandStub())
        let output = try await cli.json(["auth", "status"])
        precondition(output["ok"] as? Bool == true)
        for error in [RestrictedCommandFailure.start, .timeout, .outputLimit] {
            let failed = FeishuCLI(executable: layout.python, profile: "synthetic", commands: CommandStub(error: error))
            do { _ = try await failed.run(["auth"]); fatalError("错误未传回") } catch {}
        }
        let denied = FeishuCLI(executable: layout.python, profile: "synthetic", commands: CommandStub(exitCode: 1))
        do { _ = try await denied.run(["auth"]); fatalError("授权错误未传回") }
        catch { precondition(error.localizedDescription.contains("登录已失效")) }
        let recognized = try FeishuImages.recognize(file: root, using: OCRStub())
        precondition(recognized == "合成图片文字")
        print("PASS: CLI/OCR 替身注入、超时/大小/授权错误传递")

        let commands = MacRestrictedCommands()
        let result = try await commands.run(executable: URL(fileURLWithPath: "/usr/bin/printf"),
            arguments: ["synthetic-output"], timeout: 5, workingDirectory: nil, maximumDirectoryBytes: nil)
        precondition(result.exitCode == 0 && String(data: result.output, encoding: .utf8) == "synthetic-output")
        do {
            _ = try await commands.run(executable: URL(fileURLWithPath: "/bin/sleep"), arguments: ["10"],
                timeout: 0.05, workingDirectory: nil, maximumDirectoryBytes: nil)
            fatalError("超时未拒绝")
        } catch RestrictedCommandFailure.timeout {}
        let cancelled = Task { try await commands.run(executable: URL(fileURLWithPath: "/bin/sleep"),
            arguments: ["10"], timeout: 15, workingDirectory: nil, maximumDirectoryBytes: nil) }
        try await Task.sleep(nanoseconds: 100_000_000)
        cancelled.cancel()
        do { _ = try await cancelled.value; fatalError("取消未拒绝") } catch is CancellationError {}
        do {
            _ = try await commands.run(executable: URL(fileURLWithPath: "/usr/bin/head"),
                arguments: ["-c", "12000001", "/dev/zero"], timeout: 5, workingDirectory: nil, maximumDirectoryBytes: nil)
            fatalError("输出上限未拒绝")
        } catch RestrictedCommandFailure.outputLimit {}
        print("PASS: 本地受限进程正常执行、超时、取消、输出上限")

        // A fake host exercises the native readiness path without network or Keychain.
        try executable("#!/bin/sh\nprintf '%s\\n' '{\"event\":\"ready\",\"address\":\"http://127.0.0.1:1\"}'\nexec /bin/sleep 30\n", to: layout.python)
        let service = LocalService(home: root.appendingPathComponent("home"),
            credentialService: "synthetic." + UUID().uuidString, credentials: SyntheticCredentials())
        defer { service.stopAndWait() }
        try rejected { _ = try service.start(runtime: runtime) }
        precondition(!service.isRunning)
        try executable("#!/bin/sh\n/usr/bin/touch \"$0.started\"\nexec /bin/sleep 30\n", to: layout.python)
        let start = Task.detached { try service.start(runtime: runtime) }
        var started = false
        for _ in 0..<100 {
            if manager.fileExists(atPath: layout.python.path + ".started") { started = true; break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        precondition(started)
        let began = Date()
        service.stop()
        do { _ = try await start.value; fatalError("取消后返回连接") } catch {}
        precondition(Date().timeIntervalSince(began) < 3)
        print("PASS: 握手缺失安全退出、启动取消，未访问真实钥匙串")
    }

    private static func executable(_ value: String, to url: URL) throws {
        try Data(value.utf8).write(to: url)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: url.path)
    }

    private static func rejected(_ action: () throws -> Void) throws {
        do { try action() } catch { return }
        throw NSError(domain: "PlatformSupportTest", code: 1, userInfo: [NSLocalizedDescriptionKey: "应当拒绝的操作成功了"])
    }
}

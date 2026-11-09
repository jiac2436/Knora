import Foundation

/// Blocking operations: invoke from a background task after stopping the local service.
enum MacBackupTool {
    static func export(runtime: URL, data: URL, cache: URL, logs: URL, output: URL) throws -> [String: Any] {
        try run(runtime: runtime, action: "export", package: output, data: data, cache: cache, logs: logs)
    }

    static func exportReadable(runtime: URL, data: URL, cache: URL, logs: URL, output: URL) throws -> [String: Any] {
        try run(runtime: runtime, action: "readable", package: output, data: data, cache: cache, logs: logs)
    }

    static func verify(runtime: URL, package: URL) throws -> [String: Any] {
        try run(runtime: runtime, action: "verify", package: package)
    }

    /// The CLI accepts only a new destination; it never overwrites an existing library.
    static func restore(runtime: URL, package: URL, destination: URL, cache: URL, logs: URL,
                        credential: String) throws -> [String: Any] {
        guard (32...256).contains(credential.utf8.count),
              credential.utf8.allSatisfy({ (33...126).contains($0) }) else {
            throw failure("本地恢复凭证无效，请重新生成后重试。")
        }
        return try run(runtime: runtime, action: "restore", package: package,
                       data: destination, cache: cache, logs: logs, credential: credential)
    }

    private static func run(runtime: URL, action: String, package: URL, data: URL? = nil,
                            cache: URL? = nil, logs: URL? = nil, credential: String? = nil) throws -> [String: Any] {
        for url in [runtime, package, data, cache, logs].compactMap({ $0 }) {
            guard url.isFileURL, url.path.hasPrefix("/") else {
                throw failure("备份与恢复需要使用本地绝对路径。")
            }
        }
        let python = MacPaths().runtime(runtime).python
        let script = MacPaths().runtime(runtime).backend.appendingPathComponent("app/local_backup.py")
        guard FileManager.default.isExecutableFile(atPath: python.path),
              FileManager.default.fileExists(atPath: script.path) else {
            throw failure("客户端备份运行环境不完整，请重新安装后重试。")
        }
        var arguments = ["-I", "-B", script.path, "--action", action, "--package", package.path]
        if let data { arguments += ["--data-dir", data.path] }
        if let cache { arguments += ["--cache-dir", cache.path] }
        if let logs { arguments += ["--log-dir", logs.path] }
        let process = Process()
        let input = Pipe()
        let output = Pipe()
        let errors = Pipe()
        let stdout = LocalBackupOutput(limit: 1_048_576)
        let stderr = LocalBackupOutput(limit: 65_536)
        process.executableURL = python
        process.arguments = arguments
        process.currentDirectoryURL = runtime
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": NSTemporaryDirectory()]
        process.standardInput = input
        process.standardOutput = output
        process.standardError = errors
        do { try process.run() }
        catch { throw failure("无法启动本地备份工具，请检查运行环境后重试。") }
        try? input.fileHandleForReading.close()
        try? output.fileHandleForWriting.close()
        try? errors.fileHandleForWriting.close()

        let readers = DispatchGroup()
        for (pipe, buffer) in [(output, stdout), (errors, stderr)] {
            readers.enter()
            DispatchQueue.global(qos: .utility).async {
                defer { try? pipe.fileHandleForReading.close(); readers.leave() }
                while true {
                    let chunk = pipe.fileHandleForReading.readData(ofLength: 8_192)
                    if chunk.isEmpty { return }
                    buffer.append(chunk)
                }
            }
        }
        let timeoutState = LocalBackupOutput(limit: 1)
        // Large libraries may take time to hash, compress and validate.
        let timeout = DispatchWorkItem {
            guard process.isRunning else { return }
            timeoutState.append(Data([1]))
            MacProcessTermination.stop(process)
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + 1_800, execute: timeout)
        defer { timeout.cancel() }
        do {
            if let credential { try input.fileHandleForWriting.write(contentsOf: Data((credential + "\n").utf8)) }
            try input.fileHandleForWriting.close()
        } catch {
            try? input.fileHandleForWriting.close()
            if process.isRunning { process.terminate() }
        }
        process.waitUntilExit()
        readers.wait()
        if !timeoutState.snapshot.data.isEmpty {
            throw failure("备份操作超过 30 分钟，请检查存储空间与磁盘状态后重试。")
        }
        let response = stdout.snapshot
        guard !response.exceeded, !stderr.snapshot.exceeded else {
            throw failure("本地备份工具返回内容过大，请检查运行环境后重试。")
        }
        guard let result = (try? JSONSerialization.jsonObject(with: response.data)) as? [String: Any] else {
            throw failure("本地备份工具返回异常，请检查运行环境后重试。")
        }
        if process.terminationReason != .exit || process.terminationStatus != 0 {
            throw failure(result["error"] as? String ?? "本地备份操作失败，请检查存储空间与文件权限后重试。")
        }
        if let message = result["error"] as? String { throw failure(message) }
        return result
    }
}

/// Drain both pipes concurrently while keeping diagnostic output bounded in memory.
private final class LocalBackupOutput: @unchecked Sendable {
    private let lock = NSLock()
    private let limit: Int
    private var data = Data()
    private var exceeded = false

    init(limit: Int) { self.limit = limit }

    func append(_ chunk: Data) {
        lock.lock()
        defer { lock.unlock() }
        let remaining = limit - data.count
        if chunk.count > remaining { exceeded = true }
        data.append(chunk.prefix(remaining))
    }

    var snapshot: (data: Data, exceeded: Bool) {
        lock.lock()
        defer { lock.unlock() }
        return (data, exceeded)
    }
}

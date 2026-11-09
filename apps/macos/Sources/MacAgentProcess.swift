import Foundation

extension LocalAgentInstaller {
    static func runRequest(runtime: URL, request: [String: Any]) throws -> LocalAgentInstallResult {
        let process = Process(), input = Pipe(), output = Pipe()
        let buffer = LocalAgentInstallOutput(limit: 65_536)
        let timeoutState = LocalAgentInstallOutput(limit: 1)
        process.executableURL = MacPaths().runtime(runtime).python
        process.arguments = ["-I", "-B", MacPaths().runtime(runtime).backend.appendingPathComponent("app/agent_install.py").path]
        process.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TMPDIR": NSTemporaryDirectory()]
        process.standardInput = input; process.standardOutput = output; process.standardError = FileHandle.nullDevice
        let data = try JSONSerialization.data(withJSONObject: request)
        guard data.count <= 65_536 else { throw failure("Agent 配置过大，请检查路径或使用手动配置。") }
        do { try process.run() } catch { throw failure("无法启动接入工具，请使用新版打包客户端。") }
        try? input.fileHandleForReading.close(); try? output.fileHandleForWriting.close()
        let readers = DispatchGroup(); readers.enter()
        DispatchQueue.global(qos: .utility).async {
            defer { try? output.fileHandleForReading.close(); readers.leave() }
            while true {
                let chunk = output.fileHandleForReading.readData(ofLength: 8_192)
                if chunk.isEmpty { return }
                buffer.append(chunk)
            }
        }
        let timeout = DispatchWorkItem {
            guard process.isRunning else { return }
            timeoutState.append(Data([1])); MacProcessTermination.stop(process)
        }
        DispatchQueue.global().asyncAfter(deadline: .now() + 55, execute: timeout)
        defer { timeout.cancel() }
        do { try input.fileHandleForWriting.write(contentsOf: data); try input.fileHandleForWriting.close() }
        catch { try? input.fileHandleForWriting.close(); if process.isRunning { process.terminate() } }
        process.waitUntilExit(); readers.wait()
        guard timeoutState.snapshot.data.isEmpty else { throw failure("接入检查超时，请先刷新配置状态再重试。") }
        let response = buffer.snapshot
        guard !response.exceeded, let result = try? JSONDecoder().decode(LocalAgentInstallResult.self, from: response.data) else {
            throw failure("接入工具未返回有效结果，请先检查配置状态。")
        }
        guard process.terminationReason == .exit, process.terminationStatus == 0 else { throw failure(result.message) }
        return result
    }
}

private final class LocalAgentInstallOutput: @unchecked Sendable {
    private let lock = NSLock()
    private let limit: Int
    private var data = Data()
    private var exceeded = false
    init(limit: Int) { self.limit = limit }
    func append(_ chunk: Data) {
        lock.lock(); defer { lock.unlock() }
        let remaining = limit - data.count
        if chunk.count > remaining { exceeded = true }
        data.append(chunk.prefix(remaining))
    }
    var snapshot: (data: Data, exceeded: Bool) {
        lock.lock(); defer { lock.unlock() }
        return (data, exceeded)
    }
}

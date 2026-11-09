import Foundation

struct MacRestrictedCommands: RestrictedCommandRunning {
    func run(executable: URL, arguments: [String], timeout: TimeInterval, workingDirectory: URL?,
             maximumDirectoryBytes: Int?) async throws -> RestrictedCommandResult {
        let control = MacCommandControl()
        return try await withTaskCancellationHandler {
            try await Task.detached(priority: .userInitiated) {
                try control.run(executable: executable, arguments: arguments, timeout: timeout,
                    workingDirectory: workingDirectory, maximumDirectoryBytes: maximumDirectoryBytes)
            }.value
        } onCancel: { control.cancel() }
    }
}

// No shell interpolation, no token access, and no stdout/stderr logs. Private files
// avoid pipe deadlocks for large documents and are removed after every invocation.
private final class MacCommandControl: @unchecked Sendable {
    private let lock = NSLock()
    private var cancelled = false

    func cancel() { lock.lock(); cancelled = true; lock.unlock() }
    private var isCancelled: Bool { lock.lock(); defer { lock.unlock() }; return cancelled }

    func run(executable: URL, arguments: [String], timeout: TimeInterval, workingDirectory: URL?, maximumDirectoryBytes: Int?) throws -> RestrictedCommandResult {
        if isCancelled { throw CancellationError() }
        let manager = FileManager.default
        let directory = manager.temporaryDirectory.appendingPathComponent("pka-cli-\(UUID().uuidString)")
        try manager.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        defer { try? manager.removeItem(at: directory) }
        let output = directory.appendingPathComponent("output")
        let errors = directory.appendingPathComponent("errors")
        for url in [output, errors] {
            guard manager.createFile(atPath: url.path, contents: nil, attributes: [.posixPermissions: 0o600]) else {
                throw failure("无法创建外部工具的私密临时文件。")
            }
        }
        let out = try FileHandle(forWritingTo: output)
        let err = try FileHandle(forWritingTo: errors)
        defer { try? out.close(); try? err.close() }
        let process = Process()
        process.executableURL = executable
        process.arguments = arguments
        var environment = ProcessInfo.processInfo.environment.filter {
            ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL"].contains($0.key)
        }
        environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
        environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
        // Prevent incidental workspace bindings from changing the selected account.
        process.currentDirectoryURL = workingDirectory ?? manager.homeDirectoryForCurrentUser
        process.environment = environment
        process.standardOutput = out
        process.standardError = err
        process.standardInput = FileHandle.nullDevice
        do { try process.run() } catch { throw RestrictedCommandFailure.start }
        let deadline = Date().addingTimeInterval(timeout)
        var exceeded = false
        var timedOut = false
        while process.isRunning {
            let size = [output, errors].reduce(0) { count, url in
                count + ((try? manager.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0)
            }
            exceeded = size > 12_000_000
            if let workingDirectory {
                let files = (try? manager.contentsOfDirectory(at: workingDirectory, includingPropertiesForKeys: [.fileSizeKey])) ?? []
                let downloaded = files.reduce(0) { $0 + ((try? $1.resourceValues(forKeys: [.fileSizeKey]).fileSize) ?? 0) }
                exceeded = exceeded || downloaded > (maximumDirectoryBytes ?? Int.max)
            }
            timedOut = Date() > deadline
            if isCancelled || exceeded || timedOut {
                MacProcessTermination.kill(process)
                break
            }
            Thread.sleep(forTimeInterval: 0.05)
        }
        process.waitUntilExit()
        if isCancelled { throw CancellationError() }
        if timedOut { throw RestrictedCommandFailure.timeout }
        if exceeded { throw RestrictedCommandFailure.outputLimit }
        let result = try boundedRead(output)
        guard result.count <= 12_000_000 else { throw RestrictedCommandFailure.outputLimit }
        let errorData = try boundedRead(errors)
        guard result.count + errorData.count <= 12_000_000 else { throw RestrictedCommandFailure.outputLimit }
        return RestrictedCommandResult(output: result, errors: errorData, exitCode: process.terminationStatus)
    }

    private func boundedRead(_ url: URL) throws -> Data {
        let stream = try FileHandle(forReadingFrom: url)
        defer { try? stream.close() }
        let data = try stream.read(upToCount: 12_000_001) ?? Data()
        guard data.count <= 12_000_000 else { throw RestrictedCommandFailure.outputLimit }
        return data
    }
}


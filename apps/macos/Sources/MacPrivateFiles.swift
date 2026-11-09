import Foundation
import Security
import Darwin

enum MacPrivateFiles {
    static func randomCredential() throws -> String {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else {
            throw failure("无法生成本地凭证，请重试。")
        }
        return bytes.map { String(format: "%02x", $0) }.joined()
    }

    static func physicalPath(_ url: URL) throws -> String {
        guard let value = realpath(url.path, nil) else {
            throw failure("未找到知识库 Skill 模板，请使用新版客户端。")
        }
        defer { free(value) }
        return String(cString: value)
    }
}

enum MacProcessTermination {
    static func kill(_ process: Process) {
        if process.isRunning { Darwin.kill(process.processIdentifier, SIGKILL) }
    }

    static func stop(_ process: Process, grace: TimeInterval = 5) {
        if process.isRunning { process.terminate() }
        DispatchQueue.global().asyncAfter(deadline: .now() + grace) { kill(process) }
    }
}

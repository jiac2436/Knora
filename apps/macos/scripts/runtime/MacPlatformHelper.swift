import Foundation
import Darwin

func failure(_ message: String) -> NSError {
    NSError(domain: "KnowledgePlatform", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
}

/// Private IPC only. No listener, provider configuration, authorization flow or knowledge rules.
@main enum MacPlatformHelper {
    static func main() async {
        var response: [String: Any] = ["version": 1, "ok": false, "error": "invalidRequest"]
        do {
            var data = Data()
            while data.count <= 65536 {
                let byte = FileHandle.standardInput.readData(ofLength: 1)
                guard !byte.isEmpty else { throw failure("missing request") }
                data.append(byte)
                if byte.first == 10 { break }
            }
            guard data.count <= 65536, let message = try JSONSerialization.jsonObject(with: data) as? [String: Any],
                  message["version"] as? Int == 1 else { throw failure("invalid request") }
            let task = Task { () throws -> [String: Any] in
                if message["action"] as? String == "cli" {
                    guard let path = message["executable"] as? String, path.hasPrefix("/"),
                          let arguments = message["arguments"] as? [String], arguments.count <= 100,
                          let timeout = message["timeout"] as? Double, (0.01...300).contains(timeout) else {
                        throw failure("invalid command")
                    }
                    let directory = (message["directory"] as? String).map { URL(fileURLWithPath: $0) }
                    let result = try await MacRestrictedCommands().run(executable: URL(fileURLWithPath: path),
                        arguments: arguments, timeout: timeout, workingDirectory: directory,
                        maximumDirectoryBytes: message["maximum_directory_bytes"] as? Int)
                    return ["version": 1, "ok": true, "output": result.output.base64EncodedString(),
                            "errors": result.errors.base64EncodedString(), "exit_code": Int(result.exitCode)]
                }
                if message["action"] as? String == "ocr", let path = message["file"] as? String, path.hasPrefix("/") {
                    try Task.checkCancellation()
                    let text = try MacImageTextRecognizer().recognize(file: URL(fileURLWithPath: path))
                    try Task.checkCancellation()
                    return ["version": 1, "ok": true, "text": text]
                }
                throw failure("invalid action")
            }
            DispatchQueue.global().async {
                while !FileHandle.standardInput.readData(ofLength: 1).isEmpty {}
                task.cancel()
                // The Python adapter creates a dedicated session. An OCR request
                // stuck in a system API must not leave a helper after parent death.
                if ProcessInfo.processInfo.environment["PKA_PLATFORM_HELPER_OWNED"] == "1", getpgrp() == getpid() {
                    DispatchQueue.global().asyncAfter(deadline: .now() + 2) { killpg(getpgrp(), SIGKILL) }
                }
            }
            do { response = try await task.value }
            catch RestrictedCommandFailure.start { response["error"] = "start" }
            catch RestrictedCommandFailure.timeout { response["error"] = "timeout" }
            catch RestrictedCommandFailure.outputLimit { response["error"] = "outputLimit" }
            catch is CancellationError { response["error"] = "cancelled" }
            catch { response["error"] = message["action"] as? String == "ocr" ? "invalidImage" : "failed" }
        } catch { /* Fixed diagnostics never include arguments, file contents or CLI output. */ }
        if var output = try? JSONSerialization.data(withJSONObject: response) {
            output.append(10)
            try? FileHandle.standardOutput.write(contentsOf: output)
        }
        exit(response["ok"] as? Bool == true ? 0 : 1)
    }
}

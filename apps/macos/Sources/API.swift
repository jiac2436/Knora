import Foundation

struct API {
    let base: URL
    let token: String

    init(address: String, token: String) throws {
        guard let url = URL(string: address.trimmingCharacters(in: .whitespacesAndNewlines)),
              let host = url.host, url.user == nil, url.password == nil,
              url.query == nil, url.fragment == nil,
              url.scheme == "https" || (url.scheme == "http" && ["localhost", "127.0.0.1", "::1"].contains(host)) else {
            throw failure("请输入 HTTPS 服务地址；本机地址可使用 HTTP")
        }
        base = url
        self.token = token.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    func call(_ path: String, method: String = "GET", json: [String: Any]? = nil,
              key: String? = nil, file: URL? = nil, query: [URLQueryItem] = [],
              progress: (@Sendable (Double) -> Void)? = nil, timeout: TimeInterval = 120) async throws -> [String: Any] {
        var components = URLComponents(url: base.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { components.queryItems = query }
        var request = URLRequest(url: components.url!)
        request.httpMethod = method
        request.timeoutInterval = timeout
        if !token.isEmpty { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let key { request.setValue(key, forHTTPHeaderField: "Idempotency-Key") }
        if let json {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: json)
        }
        let result: (Data, URLResponse)
        if let file {
            request.setValue("application/octet-stream", forHTTPHeaderField: "Content-Type")
            if let progress {
                let session = URLSession(configuration: .default,
                                         delegate: FileUploadProgressDelegate(progress), delegateQueue: nil)
                defer { session.finishTasksAndInvalidate() }
                result = try await session.upload(for: request, fromFile: file)
            } else {
                result = try await URLSession.shared.upload(for: request, fromFile: file)
            }
        } else {
            result = try await URLSession.shared.data(for: request)
        }
        let body = (try? JSONSerialization.jsonObject(with: result.0)) as? [String: Any] ?? [:]
        guard let response = result.1 as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            let status = (result.1 as? HTTPURLResponse)?.statusCode ?? 0
            throw NSError(domain: "KnowledgeInbox", code: 1, userInfo: [
                NSLocalizedDescriptionKey: body["message"] as? String ?? "服务请求失败，请检查知识库连接",
                "HTTPStatus": status])
        }
        return body
    }

    func downloadFile(_ path: String, into directory: URL, named name: String) async throws -> URL {
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.timeoutInterval = 120
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (temporary, response) = try await URLSession.shared.download(for: request)
        guard let response = response as? HTTPURLResponse, (200..<300).contains(response.statusCode) else {
            throw failure("无法打开文件原件")
        }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let destination = directory.appendingPathComponent(name)
        if FileManager.default.fileExists(atPath: destination.path) {
            try FileManager.default.removeItem(at: destination)
        }
        try FileManager.default.moveItem(at: temporary, to: destination)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
        return destination
    }
}

private final class FileUploadProgressDelegate: NSObject, URLSessionTaskDelegate {
    private let report: @Sendable (Double) -> Void

    init(_ report: @escaping @Sendable (Double) -> Void) { self.report = report }

    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        guard totalBytesExpectedToSend > 0 else { return }
        report(min(1, Double(totalBytesSent) / Double(totalBytesExpectedToSend)))
    }
}

func failure(_ message: String) -> NSError {
    NSError(domain: "KnowledgeInbox", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
}

func standaloneLink(_ text: String) -> URL? {
    let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
    guard !value.contains(where: { $0.isWhitespace }), let url = URL(string: value),
          ["http", "https"].contains(url.scheme?.lowercased() ?? ""), url.host != nil else { return nil }
    return url
}

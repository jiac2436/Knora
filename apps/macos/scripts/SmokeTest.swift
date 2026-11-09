import Foundation

// Run only against an isolated development database; creates synthetic test data.
@main struct SmokeTest {
    static func main() async throws {
        let address = ProcessInfo.processInfo.environment["PKA_TEST_API"] ?? "http://127.0.0.1:18765"
        assert(standaloneLink("https://example.com/a") != nil)
        assert(standaloneLink("参考 https://example.com") == nil)
        assert(standaloneLink("file:///tmp/a") == nil)
        do {
            _ = try API(address: "http://example.com", token: "")
            fatalError("Remote HTTP should be rejected")
        } catch {}
        let anonymous = try API(address: address, token: "")
        let login = try await anonymous.call("v1/dev/sessions", method: "POST", json: ["email": "mac-smoke@example.com", "name": "Mac Smoke"])
        let api = try API(address: address, token: login["access_token"] as! String)
        let domain = try await api.call("v1/domains", method: "POST", json: ["name": "Mac \(UUID().uuidString)"])
        let domainID = domain["id"] as! String
        for text in ["Mac 客户端 HTTP 验证", "https://example.com/article"] {
            let link = standaloneLink(text)
            var payload: [String: Any] = ["capture_type": link == nil ? "text" : "url",
                "title": link?.absoluteString ?? "笔记",
                "content": ["text": link == nil ? text : ""], "coverage": link == nil ? "full_text" : "link_only",
                "context": ["domain_id": domainID], "client": ["kind": "desktop", "version": "0.1.0"]]
            if let link { payload["source_url"] = link.absoluteString }
            let result = try await api.call("v1/captures", method: "POST", json: payload, key: UUID().uuidString)
            assert(result["id"] is String)
            assert(result["content_type"] as? String == (link == nil ? "note" : "link"))
        }
        let file = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: file) }
        try Data("Mac 文件上传验证".utf8).write(to: file)
        let key = UUID().uuidString
        let query = [URLQueryItem(name: "domain_id", value: domainID), URLQueryItem(name: "filename", value: "验证.txt")]
        let first = try await api.call("v1/file-captures", method: "POST", key: key, file: file, query: query)
        let second = try await api.call("v1/file-captures", method: "POST", key: key, file: file, query: query)
        assert(first["id"] as? String == second["id"] as? String)
        assert(first["coverage"] as? String == "file_only")
        assert(first["content_type"] as? String == "file")
        print("PASS: Swift HTTP 笔记、链接、文件上传与幂等重试")
    }
}

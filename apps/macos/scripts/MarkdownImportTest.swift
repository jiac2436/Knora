import Foundation

@main struct MarkdownImportTest {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else { fatalError("请传入已验证的运行包路径") }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-markdown-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let serviceName = "local.pka.markdown-test.\(UUID().uuidString)"
        let service = LocalService(home: root, credentialService: serviceName)
        defer { service.stop(); TokenStore.delete(service: serviceName) }
        let queue = CaptureQueueStore(root: root.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox/capture-queue"))
        let model = CaptureModel(queueStore: queue, localService: service,
                                 runtime: URL(fileURLWithPath: CommandLine.arguments[1]))
        await model.startLocalService()
        guard model.connected else { throw failure(model.authMessage) }
        model.domainName = "Markdown 测试"
        await model.createDomain()
        guard !model.domainID.isEmpty else { throw failure(model.message) }

        let body = "# 项目计划\n\n编写代码并测试。"
        let original = root.appendingPathComponent("计划.md")
        try Data(body.utf8).write(to: original)
        model.addFiles([original])
        guard model.items.count == 1 && model.items[0].kind == .file else { throw failure(model.message) }
        await model.send()
        guard model.items[0].sent else { throw failure(model.items[0].status) }
        guard let sourceID = model.items[0].sourceID else { throw failure("未返回资料 ID") }

        let api = try API(address: model.address, token: model.token)
        var detail: [String: Any] = [:]
        for _ in 0..<100 {
            detail = try await api.call("v1/sources/\(sourceID)")
            let parsing = detail["document_parse"] as? [String: Any]
            if parsing?["status"] as? String == "completed" { break }
            if parsing?["status"] as? String == "failed" { throw failure("Markdown 解析失败：\(parsing?["last_error"] ?? "未知")") }
            try await Task.sleep(nanoseconds: 200_000_000)
        }
        guard detail["extracted_text"] as? String == body else { throw failure("正文未按原文保存") }
        let search = try await api.call("v1/search", method: "POST", json: ["query": "编写代码", "limit": 10])
        let results = search["items"] as? [[String: Any]] ?? []
        guard results.contains(where: { $0["source_id"] as? String == sourceID }) else { throw failure("Markdown 未进入搜索索引") }
        print("PASS: Mac 文件采集、本地随包 Worker 解析、正文保存与搜索")
    }
}

import Foundation

@main struct LocalRAGContractTest {
    @MainActor static func main() async throws {
        let libraryID = UUID().uuidString.lowercased(), articleID = UUID().uuidString.lowercased()
        let articleLink = "knowledge-inbox://library/\(libraryID)/source/\(articleID)"
        let parsed = try ArticleLink(URL(string: articleLink)!)
        precondition(parsed.libraryID == libraryID && parsed.sourceID == articleID)
        for invalid in [articleLink + "?token=secret", articleLink + "#fragment", articleLink + "/extra",
                        articleLink.replacingOccurrences(of: "knowledge-inbox:", with: "https:"),
                        articleLink.replacingOccurrences(of: "//library/", with: "//user@library/"),
                        articleLink.replacingOccurrences(of: "//library/", with: "//library:8000/"),
                        articleLink.replacingOccurrences(of: articleID, with: "../article"),
                        articleLink.replacingOccurrences(of: articleID, with: "%2e%2e") ] {
            do { _ = try ArticleLink(URL(string: invalid)!); preconditionFailure("非法文章链接被接受") }
            catch { }
        }
        let citation: [String: Any] = ["evidence_id": "original-evidence", "source_id": "source", "revision_id": "revision",
            "title": "合成资料", "locator_kind": "text_range", "locator_start": 10, "locator_end": 25, "stale": false]
        var value: [String: Any] = ["id": "turn", "session_id": "session", "question": "有什么依据？", "status": "succeeded",
            "answer": "基于原文的回答 [1]", "last_error": NSNull(), "citations": [citation], "insufficient_evidence": false, "warnings": []]
        let result = try decodeLocalAI(LocalRAGTurn.self, body: value)
        precondition(result.answer == "基于原文的回答 [1]" && !result.isActive && result.statusText == "回答完成")
        precondition(result.citations.first?.evidence_id == "original-evidence" && result.citations.first?.locator_start == 10)
        value["answer"] = NSNull(); value["status"] = "pending"; value["citations"] = []
        let pending = try decodeLocalAI(LocalRAGTurn.self, body: value)
        precondition(pending.isActive && pending.answer == nil && pending.citations.isEmpty)
        value["status"] = "unknown"; value["last_error"] = "网关结果未确认"
        let unknown = try decodeLocalAI(LocalRAGTurn.self, body: value)
        precondition(!unknown.isActive && unknown.statusText.contains("额度"))
        value["status"] = "succeeded"; value["insufficient_evidence"] = true
        let empty = try decodeLocalAI(LocalRAGTurn.self, body: value)
        precondition(empty.statusText == "资料不足")
        var oldCitation = citation; oldCitation["stale"] = true
        value["citations"] = [oldCitation]
        let stale = try decodeLocalAI(LocalRAGTurn.self, body: value)
        precondition(stale.citations.first?.stale == true)
        let session = try decodeLocalAI(LocalRAGSession.self, body: ["id": "session", "title": "本地问答", "created_at": "2026-10-03T00:00:00Z"])
        precondition(session.title == "本地问答")
        let request = LocalRAGRequest(sessionID: session.id, question: "问题", tagQuery: "本地标签", key: "same-key")
        precondition(request.body["confirm_send"] as? Bool == true && request.body["tag_query"] as? String == "本地标签")
        precondition(request.matches(sessionID: session.id, question: "问题", tagQuery: "本地标签"))
        precondition(!request.matches(sessionID: "other-library-session", question: "问题", tagQuery: "本地标签"))
        let noFilter = LocalRAGRequest(sessionID: session.id, question: "问题", tagQuery: "", key: "key")
        precondition(noFilter.body["tag_query"] == nil)
        precondition(noFilter.body["domain_ids"] == nil && noFilter.body["topic_id"] == nil)
        let scoped = LocalRAGRequest(sessionID: session.id, question: "问题", tagQuery: "", key: "scope-key",
            domainID: "domain", topicID: "topic")
        precondition(scoped.body["domain_ids"] as? [String] == ["domain"] && scoped.body["topic_id"] as? String == "topic")
        precondition(scoped.matches(sessionID: session.id, question: "问题", tagQuery: "", domainID: "domain", topicID: "topic"))
        precondition(!scoped.matches(sessionID: session.id, question: "问题", tagQuery: "", domainID: "other", topicID: "topic"))
        precondition(!scoped.matches(sessionID: session.id, question: "问题", tagQuery: "", domainID: "domain", topicID: nil))
        value["scope"] = ["domain_ids": ["domain"], "topic_id": "topic"]
        let scopedTurn = try decodeLocalAI(LocalRAGTurn.self, body: value)
        precondition(scopedTurn.scope == LocalRAGScope(domain_ids: ["domain"], topic_id: "topic"))
        print("PASS: 本地问答 DTO、真实原文引用、过期引用、待处理/未知/资料不足状态、显式发送、域/主题真实请求范围及幂等边界")
        if CommandLine.arguments.count > 1 {
            try await endToEnd(runtime: URL(fileURLWithPath: CommandLine.arguments[1]))
        }
    }
}

extension LocalRAGContractTest {
    @MainActor static func endToEnd(runtime: URL) async throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent("pka-local-rag-e2e-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        let key = "synthetic.pka.rag-e2e.\(UUID().uuidString)"
        let service = LocalService(home: root.appendingPathComponent("home"), credentialService: key)
        let library = try service.currentLibrary()
        let gateway = Process()
        defer {
            service.stop()
            if gateway.isRunning { gateway.terminate(); gateway.waitUntilExit() }
            TokenStore.delete(service: key)
            TokenStore.delete(service: LocalAIConfiguration.tokenService(library))
            UserDefaults.standard.removeObject(forKey: LocalAIConfiguration.addressKey(library))
            try? manager.removeItem(at: root)
        }
        let ready = root.appendingPathComponent("gateway-ready.json")
        gateway.executableURL = runtime.appendingPathComponent("python/bin/python3")
        gateway.arguments = ["-I", "-B", URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("local_rag_mock_gateway.py").path, ready.path]
        gateway.standardOutput = FileHandle.nullDevice; gateway.standardError = FileHandle.nullDevice
        try gateway.run()
        for _ in 0..<100 {
            if manager.fileExists(atPath: ready.path) { break }
            guard gateway.isRunning else { throw failure("合成 RAG 网关启动失败") }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        let configuration = try JSONSerialization.jsonObject(with: Data(contentsOf: ready)) as? [String: Any]
        guard let port = configuration?["port"] as? Int else { throw failure("合成网关没有返回端口") }
        try LocalAIConfiguration.save(address: "http://127.0.0.1:\(port)", replacementToken: "synthetic-rag-gateway-token", clear: false, library: library)
        let model = CaptureModel(queueStore: CaptureQueueStore(root: library.data.appendingPathComponent("capture-queue")), localService: service, runtime: runtime)
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        var api = try API(address: model.address, token: model.token)
        var client = try LocalRAGAPI(address: model.address, token: model.token)
        let configured = try await client.configuration()
        try check(configured, "AI 网关配置未传到本地问答 API")
        let domain = try await api.call("v1/domains", method: "POST", json: ["name": "合成 RAG 领域"])
        let capture = try await api.call("v1/captures", method: "POST", json: [
            "capture_type": "browser_selection", "title": "RAG合成检索", "coverage": "partial_text",
            "content": ["text": "RAG合成检索：知识问答原文留在本机，云端仅处理显式授权的问题和检索原文。", "image_urls": []] as [String: Any],
            "context": ["domain_id": domain["id"]!] as [String: Any], "client": ["kind": "desktop", "version": "synthetic-rag-test"]], key: UUID().uuidString)
        guard let sourceID = capture["id"] as? String else { throw failure("合成资料未创建") }
        var found = false
        for _ in 0..<120 {
            let search = try await api.call("v1/search", method: "POST", json: ["query": "RAG合成检索", "limit": 10])
            if (search["items"] as? [[String: Any]] ?? []).contains(where: { $0["source_id"] as? String == sourceID }) { found = true; break }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        try check(found, "合成资料未完成本地索引")
        let session = try await client.createSession(title: "合成本地问答")
        let request = LocalRAGRequest(sessionID: session.id, question: "RAG合成检索", tagQuery: "", key: UUID().uuidString)
        let created = try await client.submit(request)
        let result = try await waitForTurn(client: client, sessionID: session.id, turnID: created.id)
        try check(result.status == "succeeded" && result.answer?.contains("合成回答") == true && !result.insufficient_evidence, "模拟回答未保存到本地")
        guard let citation = result.citations.first else { throw failure("回答缺少原文引用") }
        try check(result.answer?.contains("[1]") == true && result.answer?.contains(citation.evidence_id) == false,
            "历史/兼容模型输出中的内部证据ID仍显示在答案中")
        try check(citation.source_id == sourceID && !citation.stale, "引用资料或有效性错误")
        let evidence = try await api.call("v1/evidence/\(citation.evidence_id)")
        try check((evidence["text"] as? String)?.contains("RAG合成检索") == true, "引用无法读取真实原始证据")
        let identity = try await api.call("v1/me")
        guard let localLibraryID = identity["library_id"] as? String else { throw failure("本地库身份缺失") }
        let openURL = URL(string: "knowledge-inbox://library/\(localLibraryID)/source/\(sourceID)")!
        model.receiveArticleLink(openURL)
        await model.openPendingArticleLink()
        try check(model.selectedSource?.id == sourceID && model.searchDomainID == domain["id"] as? String,
                  "文章链接未定位正确文章和知识域")
        let navigation = model.articleNavigationID
        model.receiveArticleLink(URL(string: "knowledge-inbox://library/\(UUID().uuidString)/source/\(sourceID)")!)
        await model.openPendingArticleLink()
        try check(model.articleLinkError?.contains("另一知识库") == true && model.articleNavigationID == navigation,
                  "错误知识库链接未被阻断")
        model.receiveArticleLink(URL(string: "knowledge-inbox://library/\(localLibraryID)/source/\(UUID().uuidString)")!)
        await model.openPendingArticleLink()
        try check(model.articleLinkError?.contains("不存在") == true && model.articleNavigationID == navigation,
                  "缺失文章链接未提示")
        model.articleLinkError = nil
        let duplicate = try await client.submit(request)
        try check(duplicate.id == result.id, "重复请求创建了新付费任务")
        try check(try gatewayCount(ready) == 1, "幂等请求重复调用网关")
        let noHitRequest = LocalRAGRequest(sessionID: session.id, question: "无资料的问题", tagQuery: "不存在的合成标签-\(UUID().uuidString)", key: UUID().uuidString)
        let noHit = try await client.submit(noHitRequest)
        try check(noHit.status == "succeeded" && noHit.insufficient_evidence && noHit.citations.isEmpty, "没有资料时未直接返回不足")
        try check(try gatewayCount(ready) == 1, "无资料仍调用了云端模型")
        _ = try await api.call("v1/sources/\(sourceID)/revisions", method: "POST", json: ["text": "合成资料修订：已有新版本。"])
        let edited = try await client.turns(sessionID: session.id)
        try check(edited.first(where: { $0.id == result.id })?.citations.first?.stale == true, "原文改动后未标注历史引用过期")
        model.cancelLocalServiceStart()
        _ = service.stopAndWait()
        model.receiveArticleLink(openURL)
        await model.openPendingArticleLink()
        try check(model.pendingArticleLink != nil, "冷启动前文章链接未保留")
        await model.startLocalService()
        await model.openPendingArticleLink()
        try check(model.connected, "重启本地服务失败：\(model.authMessage)")
        try check(model.selectedSource?.id == sourceID && model.pendingArticleLink == nil,
                  "服务恢复后未打开等待中的文章链接")
        api = try API(address: model.address, token: model.token)
        client = try LocalRAGAPI(address: model.address, token: model.token)
        let savedSessions = try await client.sessions()
        let savedTurns = try await client.turns(sessionID: session.id)
        try check(savedSessions.contains(where: { $0.id == session.id }) && savedTurns.contains(where: { $0.id == result.id && $0.answer == result.answer }), "重启丢失本地会话或回答")
        try check(try gatewayCount(ready) == 1, "重启重放了已完成模型请求")
        _ = try await api.call("v1/evidence/\(citation.evidence_id)")
        print("PASS: 真实 Mac LocalService→API/Worker→模拟问答网关→本地会话/回答→原文引用；同 key 不重发、无资料零调用、原文变更标过期、重启保留历史且不重放；无真实模型费用")
    }
    static func gatewayCount(_ ready: URL) throws -> Int {
        let value = try String(contentsOf: ready.deletingPathExtension().appendingPathExtension("count"), encoding: .utf8)
        guard let count = Int(value) else { throw failure("无法读取模拟网关调用计数") }
        return count
    }
    static func waitForTurn(client: LocalRAGAPI, sessionID: String, turnID: String) async throws -> LocalRAGTurn {
        for _ in 0..<180 {
            let turns = try await client.turns(sessionID: sessionID)
            if let turn = turns.first(where: { $0.id == turnID }), !turn.isActive {
                guard turn.status == "succeeded" else { throw failure(turn.last_error ?? turn.statusText) }
                return turn
            }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        throw failure("本地问答任务未在期限内完成")
    }
    static func check(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw failure(message) }
    }
}

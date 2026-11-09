import Foundation

@main struct LocalAITest {
    @MainActor static func main() async throws {
        for value in ["https://gateway.example/", "http://127.0.0.1:8899", "http://localhost:8899"] {
            try check(!LocalAIConfiguration.validatedAddress(value).isEmpty, "合法网关被拒绝")
        }
        try check(LocalAIConfiguration.validatedAddress("  ") == "", "空地址错误")
        for value in ["http://gateway.example", "https://user:secret@gateway.example", "https://gateway.example?token=secret", "https://gateway.example#secret", "file:///tmp/a", "http://127.0.0.1:0"] {
            try rejected { _ = try LocalAIConfiguration.validatedAddress(value) }
        }
        print("PASS: HTTPS、本机开发地址与非法地址校验")

        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-ai-test-\(UUID().uuidString)")
        let store = LocalLibraryStore(home: root, credentialService: "synthetic.ai.\(UUID().uuidString)")
        let first = try store.current(), second = try store.allocate()
        defer {
            for library in [first, second] {
                TokenStore.delete(service: LocalAIConfiguration.tokenService(library))
                UserDefaults.standard.removeObject(forKey: LocalAIConfiguration.addressKey(library))
                UserDefaults.standard.removeObject(forKey: LocalModelConfiguration.key(library) + ".profile.v1")
            }
            try? FileManager.default.removeItem(at: root)
        }
        try check(LocalAIConfiguration.tokenService(first) != LocalAIConfiguration.tokenService(second), "钥匙串空间未隔离")
        try LocalAIConfiguration.save(address: "https://gateway.example", replacementToken: "synthetic-gateway-token", clear: false, library: first)
        try check(LocalModelStartup.prepare(first).gateway_token == "synthetic-gateway-token", "访问凭证未进入私有启动消息")
        try check(LocalAIConfiguration.environment(first).isEmpty, "凭证不应进入环境变量")
        try check(LocalAIConfiguration.environment(second).isEmpty, "凭证泄露到另一个知识库")
        try LocalAIConfiguration.save(address: "https://gateway.example/v1", replacementToken: "", clear: false, library: first)
        try check(LocalModelStartup.prepare(first).gateway_token == "synthetic-gateway-token", "留空更新丢失凭证")
        try rejected { try LocalAIConfiguration.save(address: "https://gateway.example", replacementToken: "bad\ntoken", clear: false, library: first) }
        try rejected { try LocalAIConfiguration.save(address: "https://gateway.example", replacementToken: "", clear: false, library: second) }
        try check(UserDefaults.standard.string(forKey: LocalAIConfiguration.addressKey(first))?.contains("synthetic-gateway-token") == false, "地址配置包含凭证")
        try LocalAIConfiguration.save(address: "", replacementToken: "", clear: true, library: first)
        try check(LocalAIConfiguration.environment(first).isEmpty && TokenStore.load(service: LocalAIConfiguration.tokenService(first)) == nil, "清除配置失效")
        print("PASS: 独立合成钥匙串、库隔离、凭证替换/保留/清除与无效凭证拒绝")

        var body: [String: Any] = [
            "id": "artifact", "source_id": "source", "provider": "deepseek", "model": "synthetic-model",
            "stale": false, "applied_at": NSNull(),
            "evidence": [["id": "ev", "text": "依据", "locator_kind": "line", "locator_start": 1, "locator_end": 1]],
            "result": ["summary": "概览", "summary_citations": ["ev"], "insights": [["text": "要点", "citations": ["ev"]]],
                       "organizing_suggestion": ["domain_id": NSNull(), "project_ids": ["project"], "reason": "理由", "citations": ["ev"]], "warnings": []]
        ]
        let artifact = try decodeLocalAI(LocalAIArtifact.self, body: body)
        try check(artifact.result.organizing_suggestion?.project_ids == ["project"] && artifact.evidence.first?.text == "依据", "整理建议或证据契约错误")
        body["result"] = ["summary": "概览", "summary_citations": ["ev"], "insights": [], "organizing_suggestion": NSNull(), "warnings": []]
        body["stale"] = true
        let analysis = try decodeLocalAI(LocalAIArtifact.self, body: body)
        try check(analysis.result.organizing_suggestion == nil && analysis.stale, "分析空建议或过期标记错误")
        let job = try decodeLocalAI(LocalAIJob.self, body: ["id": "job", "source_id": "source", "kind": "analyze", "status": "unknown", "last_error": NSNull(), "artifact_id": NSNull()])
        try check(job.statusText.contains("重复计费"), "未知结果未提示重复调用费用")
        print("PASS: 分析/整理/过期/未知结果 DTO 契约")
        if CommandLine.arguments.count > 1 {
            try await endToEnd(runtime: URL(fileURLWithPath: CommandLine.arguments[1]))
        }
    }
    @MainActor static func endToEnd(runtime: URL) async throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent("pka-local-ai-e2e-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        let home = root.appendingPathComponent("home")
        let key = "synthetic.pka.ai-e2e.\(UUID().uuidString)"
        let service = LocalService(home: home, credentialService: key)
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
        gateway.arguments = ["-I", "-B", URL(fileURLWithPath: #filePath).deletingLastPathComponent().appendingPathComponent("local_ai_mock_gateway.py").path, ready.path]
        gateway.standardOutput = FileHandle.nullDevice
        gateway.standardError = FileHandle.nullDevice
        try gateway.run()
        for _ in 0..<100 {
            if manager.fileExists(atPath: ready.path) { break }
            guard gateway.isRunning else { throw failure("合成网关启动失败") }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        let readyBody = try JSONSerialization.jsonObject(with: Data(contentsOf: ready)) as? [String: Any]
        guard let port = readyBody?["port"] as? Int else { throw failure("合成网关没有返回端口") }
        try LocalAIConfiguration.save(address: "http://127.0.0.1:\(port)", replacementToken: "synthetic-e2e-gateway-token", clear: false, library: library)
        let model = CaptureModel(queueStore: CaptureQueueStore(root: library.data.appendingPathComponent("capture-queue")), localService: service, runtime: runtime)
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        let api = try API(address: model.address, token: model.token)
        let config = try await api.call("v1/ai/config")
        try check(config["configured"] as? Bool == true, "Mac配置未传入真实本地API")
        let originalDomain = try await api.call("v1/domains", method: "POST", json: ["name": "合成原始领域"])
        _ = try await api.call("v1/domains", method: "POST", json: ["name": "合成目标领域"])
        _ = try await api.call("v1/projects", method: "POST", json: ["name": "合成目标项目"])
        let capture = try await api.call("v1/captures", method: "POST", json: [
            "capture_type": "browser_selection", "title": "合成 AI 主流程测试", "coverage": "partial_text",
            "content": ["text": "合成 AI 主流程测试：知识产物应留在本机，标签自动生效并更新检索。", "image_urls": []] as [String: Any],
            "context": ["domain_id": originalDomain["id"]!] as [String: Any],
            "client": ["kind": "desktop", "version": "synthetic-ai-test"]], key: UUID().uuidString)
        guard let sourceID = capture["id"] as? String else { throw failure("合成资料未创建") }
        await model.openSource(sourceID, force: true)
        await model.createLocalAI(kind: "analyze")
        guard !model.aiJobs.isEmpty else { throw failure(model.aiMessage) }
        for _ in 0..<180 {
            await model.refreshLocalAI(sourceID: sourceID)
            if !model.aiArtifacts.isEmpty { break }
            if let failed = model.aiJobs.first(where: { ["failed", "unknown", "cancelled"].contains($0.status) }) {
                throw failure(failed.last_error ?? failed.statusText)
            }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        guard let artifact = model.aiArtifacts.first else { throw failure("合成AI任务未完成") }
        try check(artifact.provider == "synthetic" && artifact.model == "mock-no-fees" && !artifact.stale, "网关结果未正确写入本地产物")
        try check(artifact.result.organizing_suggestion == nil, "新分析不应建议改变领域项目")
        try check(!artifact.evidence.isEmpty && artifact.result.summary_citations.contains(artifact.evidence[0].id), "本地证据引用丢失")
        let before = try await api.call("v1/sources/\(sourceID)")
        try check(before["domain_id"] as? String == originalDomain["id"] as? String, "模型结果修改了领域")
        try check(artifact.result.contract_version == 3 && artifact.result.overview != nil, "v3紧凑总结未保存")
        guard let candidates = artifact.tag_suggestions, candidates.count == 2 else { throw failure("标签历史记录未保存") }
        try check(candidates.allSatisfy { $0.status == "accepted" }, "新标签未自动发布")
        let confirmed = try await api.call("v1/sources/\(sourceID)/tags")
        guard let labels = confirmed["tag_details"] as? [[String: Any]], labels.count == 2,
              let label = labels.first else { throw failure("AI标签未自动生效") }
        try check(label["origins"] as? [String] == ["ai"], "自动标签丢失AI来源")
        _ = try await api.call("v1/sources/\(sourceID)/tags", method: "POST", json: ["tag_id": label["id"]!])
        _ = try await api.call("v1/sources/\(sourceID)/tags/\(label["id"]!)", method: "DELETE", query: [URLQueryItem(name: "origin", value: "ai")])
        let combined = try await api.call("v1/sources/\(sourceID)/tags")
        let preserved = (combined["tag_details"] as? [[String: Any]])?.first { $0["id"] as? String == label["id"] as? String }
        try check(preserved?["origins"] as? [String] == ["manual"], "删除AI来源影响人工关联")
        try await waitForKnowledge(model: model, sourceID: sourceID)
        let shortCalls = try Data(contentsOf: ready.deletingLastPathComponent().appendingPathComponent("gateway-calls.json"))
        let rebuild = await model.rebuildSummarySearchIndex(sourceID: sourceID)
        try check(rebuild?.contains("不会调用大模型") == true, "本地索引无法恢复")
        try check(try Data(contentsOf: ready.deletingLastPathComponent().appendingPathComponent("gateway-calls.json")) == shortCalls, "索引恢复重复调用模型")
        let after = try await api.call("v1/sources/\(sourceID)")
        try check(after["domain_id"] as? String == originalDomain["id"] as? String, "自动分析改变归属")
        try check(after["extracted_text"] as? String == before["extracted_text"] as? String, "自动分析修改正文")
        print("PASS: v3一次分析→自动AI标签→保留人工来源→索引恢复不调用模型，原文归属不变")
        let longText = String(repeating: "合成 AI 主流程测试：长文分段应保存已完成总结并保留原文引用。\n", count: 1900)
        let longCapture = try await api.call("v1/captures", method: "POST", json: [
            "capture_type": "browser_selection", "title": "合成长文", "coverage": "partial_text",
            "content": ["text": longText, "image_urls": []] as [String: Any],
            "context": ["domain_id": originalDomain["id"]!] as [String: Any],
            "client": ["kind": "desktop", "version": "synthetic-long-test"]], key: UUID().uuidString)
        guard let longID = longCapture["id"] as? String else { throw failure("合成长文未创建") }
        await model.openSource(longID, force: true)
        guard let plan = await model.previewLocalAI(kind: "analyze") else { throw failure("长文计划预览失败：\(model.aiMessage)") }
        try check(plan.calls == plan.sections + 1 && plan.sections > 1, "长文调用计划错误")
        try check(plan.source_coverage == "partial_text" && plan.covered_chars == plan.total_chars, "解析证据覆盖与原资料采集范围丢失")
        await model.createLocalAI(kind: "analyze", planHash: plan.plan_hash, expectedSourceID: longID)
        for _ in 0..<240 {
            await model.refreshLocalAI(sourceID: longID)
            if model.aiJobs.first?.status == "completed" { break }
            if let failed = model.aiJobs.first(where: { ["failed", "unknown", "cancelled"].contains($0.status) }) {
                throw failure(failed.last_error ?? failed.statusText)
            }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        guard let longJob = model.aiJobs.first, longJob.status == "completed",
              let final = model.aiArtifacts.first(where: { $0.phase == "synthesis" }) else { throw failure("长文汇总未完成") }
        try check(longJob.completed_steps == plan.calls && longJob.total_steps == plan.calls, "分段进度未完成")
        try check(model.aiArtifacts.filter { $0.phase == "segment" }.count == plan.sections, "已完成分段产物未保留")
        try check(final.result.overview != nil && final.tag_suggestions?.count == 2 && final.coverage == "full", "最终汇总与标签候选丢失")
        try check(final.evidence.contains { $0.original_evidence_id != nil && ($0.text_end ?? 0) > ($0.text_start ?? 0) }, "切片原始证据映射与文字偏移丢失")
        try check(model.aiArtifacts.filter { $0.phase == "segment" }.allSatisfy { ($0.tag_suggestions ?? []).isEmpty && $0.result.organizing_suggestion == nil }, "分段产物不应生成分类或标签")
        for _ in 0..<120 {
            let state = try await api.call("v1/summary-index")
            if ["ready", "failed"].contains(state["status"] as? String ?? "") { break }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        let summarySearch = try await api.call("v1/search", method: "POST", json: ["query": "非真实模型", "limit": 20])
        guard let summaryRows = summarySearch["items"] as? [[String: Any]],
              let summaryHit = summaryRows.first(where: { $0["source_id"] as? String == longID }) else { throw failure("有效总结关键词无法检索") }
        try check((summaryHit["match_sources"] as? [String])?.contains("summary") == true && summaryHit["summary_artifact_id"] as? String == final.id, "总结命中来源丢失")
        guard let evidenceID = summaryHit["evidence_id"] as? String else { throw failure("总结检索未返回原始证据") }
        _ = try await api.call("v1/evidence/\(evidenceID)")
        let tagSearch = try await api.call("v1/search", method: "POST", json: ["query": "非真实模型", "tag_query": "本地知识管理", "limit": 20])
        try check((tagSearch["items"] as? [[String: Any]] ?? []).contains { $0["source_id"] as? String == longID }, "自动标签无法筛选总结检索")
        try await waitForKnowledge(model: model, sourceID: longID)
        let connection = try await api.call("v1/agent-connections", method: "POST", json: ["client_name": "合成知识检索", "domain_ids": [originalDomain["id"]!], "expires_in_days": 1])
        guard let agentToken = connection["access_token"] as? String else { throw failure("合成Agent连接未创建") }
        let agent = try API(address: model.address, token: agentToken)
        let hits = try await agent.call("v1/mcp/tools/search_knowledge", method: "POST", json: ["query": "长文分段", "tag_query": "本地知识管理", "limit": 20])
        guard let rows = hits["items"] as? [[String: Any]], let hit = rows.first(where: { $0["source_id"] as? String == longID }), let originalEvidenceID = hit["evidence_id"] as? String else { throw failure("MCP未找到自动分析资料") }
        _ = try await agent.call("v1/mcp/tools/get_evidence", method: "POST", json: ["evidence_id": originalEvidenceID])
        let longAfter = try await api.call("v1/sources/\(longID)")
        try check(longAfter["domain_id"] as? String == originalDomain["id"] as? String, "长文改变了领域")
        try check(longAfter["extracted_text"] as? String == longText.trimmingCharacters(in: .whitespacesAndNewlines), "长文分析修改了原文")
        print("PASS: 长文计划→持久分段→紧凑最终结果→自动标签→知识检索就绪→MCP读取原文；无真实模型调用")

    }
    @MainActor static func waitForKnowledge(model: CaptureModel, sourceID: String) async throws {
        for _ in 0..<180 {
            await model.refreshLocalAI(sourceID: sourceID)
            if model.aiJobs.first?.pipeline_status == "completed" { return }
            if model.aiJobs.first?.pipeline_status == "index_failed" { throw failure("分析成功但索引失败") }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        throw failure("知识索引未在预算内就绪")
    }
    static func check(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw failure(message) }
    }
    static func rejected(_ operation: () throws -> Void) throws {
        do { try operation() } catch { return }
        throw failure("非法输入被接受")
    }
}

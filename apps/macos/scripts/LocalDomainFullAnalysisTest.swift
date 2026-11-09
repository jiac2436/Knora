import Foundation

@MainActor private final class FullAnalysisFixture {
    struct Held { let continuation: CheckedContinuation<[String: Any], Error>; let payload: [String: Any] }
    var requests: [(String, String, [String: Any]?)] = []
    var jobs: [String: [String: Any]] = [:]
    var holds: Set<String> = []
    var waiting: [String: Held] = [:]
    var failures: [String: Int] = [:]
    var loseCreate = false
    var acceptLostCreate = true
    var sources = 3
    var requiresUnknown = false
    var configured = true
    var blocked = false
    func key(_ kind: String, _ token: String = "test-token", _ domain: String = "domain") -> String { "\(kind)|\(token)|\(domain)" }
    func count(_ kind: String) -> Int { requests.filter { $0.0.hasPrefix(kind + "|") }.count }
    func job(_ id: String, status: String = "running") -> [String: Any] {
        ["id": id, "request_id": id, "status": status, "phase": status == "completed" ? "completed" : "documents",
         "total_sources": sources, "reused_sources": 1, "analyzed_sources": status == "completed" ? 2 : 0,
         "completed_sources": status == "completed" ? sources : 1, "current_source_title": NSNull(),
         "build_id": NSNull(), "last_error": NSNull(), "created_at": "2026-10-16T01:00:00Z", "completed_at": NSNull(),
         "budget": ["calls": 258, "input_token_reservation": 25610000, "used_calls": 0, "used_input_tokens": 0],
         "documents": [["source_id": "source", "title": "合成资料", "status": "reused"]]]
    }
    func call(_ api: API, _ path: String, _ method: String, _ body: [String: Any]?) async throws -> [String: Any] {
        let domain = String(path.split(separator: "/")[2])
        let kind = path.hasSuffix("/preview") ? "preview" : path.hasSuffix("/latest") ? "latest" : path.hasSuffix("/cancel") ? "cancel" : method == "POST" ? "create" : "read"
        let key = key(kind, api.token, domain), scope = self.key("scope", api.token, domain)
        requests.append((key, method, body))
        if let status = failures[key] { throw NSError(domain: "synthetic", code: status, userInfo: ["HTTPStatus": status, NSLocalizedDescriptionKey: "合成失败"] ) }
        let payload: [String: Any]
        switch kind {
        case "latest": payload = ["job": jobs[scope] ?? NSNull() as Any]
        case "preview": payload = ["reanalyze": body?["reanalyze"] as? Bool ?? false, "stale_sources": 1, "skipped_sources": (body?["reanalyze"] as? Bool == true) ? 0 : 1, "preview_hash": "preview-\(sources)", "total_sources": sources, "reused_sources": 1,
            "analyze_sources": sources - 1, "blocked_sources": blocked ? [["source_id": NSNull(), "title": "合成域", "reason": "超过分层上限"]] : [], "requires_unknown_confirmation": requiresUnknown, "configured": configured, "budget_basis": "bounded_upper_limit", "calls": 258, "input_token_reservation": 25610000,
            "documents": [["source_id": "source", "title": "合成资料", "status": "analyze"]]]
        case "create":
            let created = job(body?["request_id"] as! String)
            if !loseCreate || acceptLostCreate { jobs[scope] = created }
            if loseCreate {
                loseCreate = false
                throw NSError(domain: NSURLErrorDomain, code: NSURLErrorNetworkConnectionLost)
            }
            payload = created
        case "cancel":
            let cancelled = job(jobs[scope]?["id"] as! String, status: "cancelled")
            jobs[scope] = cancelled; payload = cancelled
        default: payload = jobs[scope] ?? job("synthetic-job")
        }
        if holds.remove(key) != nil {
            return try await withCheckedThrowingContinuation { waiting[key] = Held(continuation: $0, payload: payload) }
        }
        return payload
    }
    func waitFor(_ key: String) async throws {
        for _ in 0..<2000 {
            if waiting[key] != nil { return }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        throw failure("没有出现预期的延迟请求：\(key)")
    }
    func release(_ key: String) { if let held = waiting.removeValue(forKey: key) { held.continuation.resume(returning: held.payload) } }
}

@main struct LocalDomainFullAnalysisTest {
    @MainActor static func check(_ condition: Bool, _ message: String) throws { if !condition { throw failure(message) } }
    @MainActor static func main() async throws {
        let suite = "local-domain-full-test-\(UUID().uuidString)", fixture = FullAnalysisFixture()
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var progressPayload = fixture.job("progress-job")
        progressPayload["phase"] = "overview"
        progressPayload["overview_progress"] = ["completed_steps": 3, "total_steps": 5, "current_step_title": "分组汇总"]
        let decoded = try JSONDecoder().decode(LocalDomainFullJob.self, from: JSONSerialization.data(withJSONObject: progressPayload))
        try check(decoded.overview_progress?.completed_steps == 3 && decoded.overview_progress?.total_steps == 5,
                  "概览进度解码真实完成步数，不使用固定阶段百分比")
        let legacy = try JSONDecoder().decode(LocalDomainFullJob.self, from: JSONSerialization.data(withJSONObject: fixture.job("legacy-job")))
        try check(legacy.overview_progress == nil, "旧任务缺少步骤进度时仍能读取")
        let store = LocalDomainFullAnalysisStore(transport: fixture.call, defaults: defaults)
        await store.configure(address: "http://127.0.0.1:8000", token: "test-token", domainID: "domain")
        try check(store.job == nil && fixture.count("create") == 0 && fixture.count("preview") == 0, "打开只读最新任务，不创建或自动确认模型调用")
        fixture.holds.insert(fixture.key("preview"))
        let first = Task { await store.loadPreview() }
        try await fixture.waitFor(fixture.key("preview"))
        let duplicate = Task { await store.loadPreview() }; await Task.yield()
        fixture.release(fixture.key("preview")); await first.value; await duplicate.value
        try check(fixture.count("preview") == 1 && store.preview?.total_sources == 3, "预览同资源单飞")
        fixture.sources = 4; fixture.holds.insert(fixture.key("preview"))
        let previewRefresh = Task { await store.loadPreview() }
        try await fixture.waitFor(fixture.key("preview"))
        try check(store.preview?.total_sources == 3 && store.isPreviewing, "重新预览期间保留已有内容，避免闪烁")
        await store.create(confirmed: true)
        try check(fixture.count("create") == 0, "重新预览未完成时不能用旧快照启动")
        fixture.release(fixture.key("preview")); await previewRefresh.value
        await store.create(confirmed: false)
        try check(fixture.count("create") == 0, "必须显式确认才创建")
        fixture.blocked = true; await store.loadPreview(); await store.create(confirmed: true)
        try check(store.preview?.blocked_sources.first?.source_id == nil && fixture.count("create") == 0, "域级阻断无source_id也能解码并阻止启动")
        fixture.blocked = false; fixture.configured = false; await store.loadPreview(); await store.create(confirmed: true)
        try check(fixture.count("create") == 0, "缺少网关且需要模型时阻止启动")
        fixture.configured = true; fixture.requiresUnknown = true; await store.loadPreview(); await store.create(confirmed: true)
        try check(fixture.count("create") == 0, "资料曾结果未知即使无全量任务也必须额外确认")
        fixture.requiresUnknown = false; await store.loadPreview()
        fixture.failures[fixture.key("preview")] = 503
        await store.loadPreview(); await store.create(confirmed: true)
        try check(store.preview?.total_sources == 4 && store.previewError != nil && fixture.count("create") == 0,
                  "预览失败保留旧内容，但不能确认过时快照")
        fixture.failures.removeAll(); await store.loadPreview()

        let latestKey = fixture.key("latest")
        fixture.holds.insert(latestKey)
        let oldLatest = Task { await store.refreshLatest(force: true) }
        try await fixture.waitFor(latestKey)
        await store.create(confirmed: true)
        let firstID = store.job?.id
        try check(firstID != nil && store.pendingRequestID == nil, "创建返回后确认原请求ID")
        fixture.release(latestKey); await oldLatest.value
        try check(store.job?.id == firstID, "创建前的迟到latest不能清除新任务")
        let previewsBefore = fixture.count("preview")
        await store.loadPreview(); await store.create(confirmed: true)
        try check(fixture.count("create") == 1 && fixture.count("preview") == previewsBefore, "进行中的任务禁止再建或再预览")
        let readKey = fixture.key("read")
        fixture.holds.insert(readKey)
        let oldRead = Task { await store.refreshJob() }
        try await fixture.waitFor(readKey)
        await store.cancel()
        fixture.release(readKey); await oldRead.value
        try check(store.job?.status == "cancelled", "停止前的迟到进度不能覆盖停止状态")

        fixture.jobs[fixture.key("scope")] = fixture.job("unknown-job", status: "unknown")
        await store.refreshLatest(force: true); await store.loadPreview()
        let beforeUnknown = fixture.count("create")
        await store.create(confirmed: true)
        try check(fixture.count("create") == beforeUnknown && store.mutationError != nil, "unknown不自动重试，必须额外明确确认")
        await store.create(confirmed: true, confirmedUnknown: true)
        try check(fixture.count("create") == beforeUnknown + 1, "核对unknown后可按新预览明确创建")
        let confirmedBody = fixture.requests.last { $0.0.hasPrefix("create|") }?.2
        try check(confirmedBody?["confirmed_unknown"] as? Bool == true && confirmedBody?["confirmed"] as? Bool == true, "确认标记真实透传")

        await store.configure(address: "http://127.0.0.1:8000", token: "test-token", domainID: "lost-response")
        await store.loadPreview(); fixture.loseCreate = true
        await store.create(confirmed: true)
        let lostID = store.pendingRequestID
        try check(lostID != nil && store.mutationError != nil, "响应丢失保留原请求，不自动重发")
        let beforeReopen = fixture.count("create")
        let reopened = LocalDomainFullAnalysisStore(transport: fixture.call, defaults: defaults)
        await reopened.configure(address: "http://127.0.0.1:8000", token: "test-token", domainID: "lost-response")
        try check(reopened.job?.id == lostID && reopened.pendingRequestID == nil && fixture.count("create") == beforeReopen,
                  "重开读取latest按原request_id恢复，不重复调用")

        await store.configure(address: "http://127.0.0.1:8000", token: "secret-test-token", domainID: "not-accepted")
        await store.loadPreview(); fixture.loseCreate = true; fixture.acceptLostCreate = false
        await store.create(confirmed: true)
        let heldID = store.pendingRequestID
        let rawDefaults = defaults.dictionaryRepresentation().filter { $0.key.hasPrefix("local-domain-full-pending.") }
        try check(!rawDefaults.isEmpty && !rawDefaults.keys.contains { $0.contains("secret-test-token") || $0.contains("not-accepted") }, "持久scope仅存SHA256，不含凭证或资料标识")
        for case let data as Data in rawDefaults.values {
            let text = String(data: data, encoding: .utf8) ?? ""
            try check(!text.contains("secret-test-token") && !text.contains("合成资料"), "恢复记录不写token、标题或正文")
        }
        let recovery = LocalDomainFullAnalysisStore(transport: fixture.call, defaults: defaults)
        await recovery.configure(address: "http://127.0.0.1:8000", token: "secret-test-token", domainID: "not-accepted")
        try check(recovery.pendingRequestID == heldID, "后台暂未出现任务时保留原请求供人工恢复")
        await recovery.restorePendingRequest()
        let retried = fixture.requests.last { $0.0.hasPrefix("create|") }?.2
        try check(retried?["request_id"] as? String == heldID && retried?["preview_hash"] as? String == "preview-4" && recovery.pendingRequestID == nil, "手动恢复严格复用原请求ID及hash")

        let oldScope = fixture.key("latest", "secret-test-token", "not-accepted")
        fixture.holds.insert(oldScope)
        let oldIdentity = Task { await recovery.refreshLatest(force: true) }
        try await fixture.waitFor(oldScope)
        await recovery.configure(address: "http://127.0.0.1:8000", token: "new-token", domainID: "new-domain")
        try check(recovery.job == nil && recovery.preview == nil && recovery.pendingRequestID == nil, "身份切换立即清理旧缓存")
        fixture.release(oldScope); await oldIdentity.value
        try check(recovery.job == nil, "已取消旧身份请求晚到不发布")
        fixture.jobs[fixture.key("scope", "new-token", "new-domain")] = fixture.job("old-endpoint-job", status: "completed")
        await recovery.refreshLatest(force: true)
        let endpointRead = fixture.key("latest", "new-token", "new-domain")
        fixture.holds.insert(endpointRead)
        let oldEndpoint = Task { await recovery.refreshLatest(force: true) }
        try await fixture.waitFor(endpointRead)
        let sameEndpointRead = Task { await recovery.refreshLatest(force: true) }
        await Task.yield()
        fixture.jobs[fixture.key("scope", "new-token", "new-domain")] = nil
        await recovery.configure(address: "http://127.0.0.1:8099", token: "new-token", domainID: "new-domain")
        fixture.release(endpointRead); await oldEndpoint.value; await sameEndpointRead.value
        try check(recovery.job == nil, "端点切换同样隔离，旧端点晚到响应不能发布")
        await recovery.loadPreview()
        fixture.failures[fixture.key("latest", "new-token", "new-domain")] = 401
        await recovery.refreshLatest(force: true)
        try check(recovery.requiresReconnect && recovery.preview == nil && recovery.job == nil, "凭证失效清除所有可见资料并进入重新连接态")
        let countAfterExpiry = fixture.requests.count
        await recovery.refreshLatest(force: true); await recovery.loadPreview(); await recovery.create(confirmed: true)
        try check(fixture.requests.count == countAfterExpiry, "失效后不继续轮询、预览或创建")
        fixture.failures.removeAll()
        await recovery.configure(address: "http://127.0.0.1:8000", token: "new-token", domainID: "new-domain")
        try check(!recovery.requiresReconnect, "显式重连恢复")
        let modeFixture = FullAnalysisFixture()
        let modes = LocalDomainFullAnalysisStore(transport: modeFixture.call, defaults: defaults)
        await modes.configure(address: "http://127.0.0.1:8000", token: "mode-token", domainID: "modes")
        await modes.loadPreview()
        try check(!modes.reanalyze && modes.preview?.skipped_sources == 1, "重新分析默认关闭，显示跳过过期数量")
        let modeKey = modeFixture.key("preview", "mode-token", "modes")
        modeFixture.holds.insert(modeKey)
        let delayedDefault = Task { await modes.loadPreview() }
        try await modeFixture.waitFor(modeKey)
        await modes.setReanalyze(true)
        try check(modes.preview?.reanalyze == true && modes.preview?.skipped_sources == 0, "勾选自动刷新新范围")
        modeFixture.release(modeKey); await delayedDefault.value
        try check(modes.preview?.reanalyze == true, "旧模式迟到响应不能覆盖新模式")
        modeFixture.loseCreate = true; modeFixture.acceptLostCreate = false
        await modes.create(confirmed: true)
        let original = modeFixture.requests.last { $0.0.hasPrefix("create|") }?.2
        try check(original?["reanalyze"] as? Bool == true, "确认传递强制重分析")
        let savedMode = LocalDomainFullAnalysisStore(transport: modeFixture.call, defaults: defaults)
        await savedMode.configure(address: "http://127.0.0.1:8000", token: "mode-token", domainID: "modes")
        await savedMode.restorePendingRequest()
        let restored = modeFixture.requests.last { $0.0.hasPrefix("create|") }?.2
        try check(restored?["reanalyze"] as? Bool == true && restored?["request_id"] as? String == original?["request_id"] as? String,
                  "重启恢复保留原模式和同一请求，避免重复授权")
        print("PASS: 全量分析预览与确认、单飞、刷新保留、晚到响应防护、原ID持久恢复、unknown阻断、停止、身份隔离、授权失效与只读恢复")
    }
}

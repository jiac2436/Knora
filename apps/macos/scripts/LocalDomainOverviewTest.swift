import Foundation

@MainActor private final class DomainFixture {
    var requests: [(String, String, [String: Any]?)] = []
    var loseAuthorization = false
    var loseBuild = false
    var failNavigation = false
    var oldBackend = false
    var delayOld = false
    var phase = "not_built"
    var latest: [String: Any]?
    var policyEnabled = false
    var remainingCalls = 10
    var remainingTokens = 100000
    let coverage: [String: Any] = ["source_count": 3, "processed_source_count": 2,
                                 "original_text_complete": false, "excluded": ["third": "missing_summary"]]
    func navigation(domain: String, body: [String: Any]) -> [String: Any] {
        let topic = body["topic_id"] as? String
        let offset = body["source_offset"] as? Int ?? 0
        let topicOffset = body["topic_offset"] as? Int ?? 0
        return ["domain_id": domain, "scope": "app", "node": ["id": topic ?? domain,
            "kind": topic == nil ? "domain" : "topic", "name": "知识域", "parent_id": NSNull()],
            "overview": ["available": false, "reason": "not_built"], "index_status": "unavailable",
            "topics": ["items": [["id": "topic\(topicOffset)", "name": "主题", "parent_id": NSNull(), "source_count": 2]], "next_offset": topicOffset == 0 ? 40 as Any : NSNull()],
            "sources": ["items": [["id": "source\(offset)", "title": "原资料", "revision_id": NSNull()]],
                        "next_offset": offset == 0 ? 40 as Any : NSNull()]]
    }
    func build(id: String) -> [String: Any] {
        ["id": id, "status": "pending", "topic_id": NSNull(), "coverage": coverage,
         "budget": ["calls": 2, "input_token_reservation": 24000, "total_steps": 2, "reused_batches": 0],
         "steps": [["key": "root", "status": "waiting", "artifact_id": NSNull()]], "last_error": NSNull()]
    }
    func call(_ api: API, _ path: String, _ method: String, _ body: [String: Any]?) async throws -> [String: Any] {
        requests.append((path, method, body))
        if delayOld && api.token == "old" && path.hasSuffix("navigation") {
            try await Task.sleep(nanoseconds: 60_000_000)
        }
        let domain = path.split(separator: "/")[2]
        if path.hasSuffix("navigation") {
            if oldBackend { throw NSError(domain: "KnowledgeInbox", code: 1,
                userInfo: ["HTTPStatus": 404, NSLocalizedDescriptionKey: "模拟旧运行包"]) }
            if failNavigation { throw failure("模拟读取失败") }
            return navigation(domain: String(domain), body: body ?? [:])
        }
        if path.hasSuffix("schedule-state") {
            return ["phase": phase, "automatic_status": "disabled", "remaining_calls": remainingCalls,
                    "remaining_input_tokens": remainingTokens, "latest_build": latest as Any? ?? NSNull(),
                    "policy": ["enabled": policyEnabled, "paused": false, "automatic_updates": false,
                     "automatic_max_sources": 0, "automatic_max_steps": 0, "max_calls": 0,
                     "max_input_tokens": 0, "debounce_seconds": 30, "max_wait_seconds": 300]]
        }
        if path.hasSuffix("preview") {
            return ["preview_hash": String(repeating: "a", count: 64), "coverage": coverage,
                    "calls": 2, "input_token_reservation": 24000, "total_steps": 2, "reused_batches": 0]
        }
        if path.hasSuffix("authorization") {
            if loseAuthorization { loseAuthorization = false; throw URLError(.timedOut) }
            return ["authorized": true]
        }
        if path.hasSuffix("/build/jobs") {
            if loseBuild { loseBuild = false; throw URLError(.networkConnectionLost) }
            return build(id: body!["request_id"] as! String)
        }
        if path.contains("/build/jobs/") { return build(id: String(path.split(separator: "/").last!)) }
        if path.contains("processing-nodes") {
            return ["available": false, "reason": "stale_or_unsupported_inputs"]
        }
        return ["paused": true, "queued": 1, "status": "pending"]
    }
}

@main struct LocalDomainOverviewTest {
    @MainActor static func check(_ value: Bool, _ message: String) throws {
        if !value { throw failure(message) }
    }
    @MainActor static func main() async throws {
        let fixture = DomainFixture()
        let store = LocalDomainOverviewStore(transport: fixture.call)
        await store.configure(address: "http://127.0.0.1:8000", token: "synthetic", domainID: "domain")
        try check(store.navigation?.domain_id == "domain" && store.schedule?.phase == "not_built", "导航与策略解码")
        try check(fixture.requests.allSatisfy { $0.2?["scope"] as? String == "app" }, "scope应固定为app")
        await store.loadMoreSources()
        try check(store.navigation?.sources.items.map(\.id) == ["source0", "source40"], "资料分页")
        await store.loadMoreTopics()
        try check(store.navigation?.sources.items.map(\.id) == ["source0", "source40"] &&
                  store.navigation?.topics.items.count == 2, "主题分页不替换已加载资料页")
        try check(!fixture.requests.contains { $0.0.hasSuffix("authorization") || $0.0.hasSuffix("/build/jobs") }, "导航读取不授权不收费")
        await store.selectTopic("topic")
        try check(store.selectedTopicID == "topic" && store.navigation?.node.kind == "topic", "主题下钻")
        await store.previewBuild()
        await store.authorizeAndBuild(confirmed: false)
        try check(!fixture.requests.contains { $0.0.hasSuffix("authorization") }, "未明确确认不得授权")
        fixture.loseAuthorization = true; fixture.loseBuild = true
        await store.authorizeAndBuild(confirmed: true)
        let authorizationID = store.pendingRequestID
        try check(authorizationID != nil && store.build == nil, "未知授权响应保留请求")
        await store.createBuild()
        try check(store.pendingRequestID == authorizationID, "未知授权不能另建任务")
        await store.retryPendingMutation()
        let buildID = store.pendingRequestID
        try check(buildID != nil && buildID != authorizationID, "授权成功后未知任务响应保留任务编号")
        await store.retryPendingMutation()
        try check(store.build?.id == buildID && store.pendingRequestID == nil, "重试任务只用原编号")
        let authorizations = fixture.requests.filter { $0.0.hasSuffix("authorization") }
        let jobs = fixture.requests.filter { $0.0.hasSuffix("/build/jobs") }
        try check(authorizations.count == 2 && jobs.count == 2, "幂等两阶段重试次数")
        try check(authorizations[0].2?["authorization_id"] as? String == authorizations[1].2?["authorization_id"] as? String,
                  "授权重试不重置周期")
        try check(jobs[0].2?["request_id"] as? String == jobs[1].2?["request_id"] as? String, "任务重试不重复计费")
        let policy = authorizations[0].2?["policy"] as! [String: Any]
        try check(policy["max_calls"] as? Int == 2 && policy["max_input_tokens"] as? Int == 24000 &&
                  policy["automatic_updates"] as? Bool == false, "默认预算精准按预览且自动关闭")
        await store.saveAuthorization(confirmed: true, automaticUpdates: true, maxCalls: 10,
                                      maxInputTokens: 100000, maxSources: 0, maxSteps: 0)
        try check(fixture.requests.filter { $0.0.hasSuffix("authorization") }.count == 2, "自动更新必须有明确范围")
        await store.setPaused(true)
        try check(fixture.requests.last(where: { $0.0.hasSuffix("pause") })?.1 == "PATCH", "暂停不重置授权周期")
        fixture.phase = "unknown"
        await store.refresh(); await store.previewBuild()
        await store.createBuild()
        try check(fixture.requests.filter { $0.0.hasSuffix("/build/jobs") }.count == 2, "unknown必须显式确认")
        await store.createBuild(confirmedUnknown: true)
        try check(fixture.requests.last(where: { $0.0.hasSuffix("/build/jobs") })?.2?["confirmed_unknown"] as? Bool == true,
                  "未知结果确认传递")
        fixture.latest = fixture.build(id: "restored-task")
        await store.refresh()
        try check(store.build?.id == "restored-task", "重新打开恢复最新任务")
        fixture.failNavigation = true; fixture.phase = "ready"
        await store.refresh()
        try check(store.schedule?.phase == "ready", "导航失败不阻塞独立状态读取")
        fixture.failNavigation = false; fixture.delayOld = true
        let old = Task { await store.configure(address: "http://127.0.0.1:8000", token: "old", domainID: "old-domain") }
        try await Task.sleep(nanoseconds: 5_000_000)
        await store.configure(address: "http://127.0.0.1:8000", token: "new", domainID: "new-domain")
        await old.value
        try check(store.navigation?.domain_id == "new-domain", "旧身份响应不能覆盖新知识库")
        await store.previewBuild(); fixture.loseBuild = true
        await store.createBuild(confirmedUnknown: true)
        try check(store.pendingRequestID != nil, "丢失响应待重试")
        await store.configure(address: "http://127.0.0.1:8000", token: "third", domainID: "third-domain")
        try check(store.pendingRequestID == nil && store.preview == nil, "切换身份丢弃旧域重试")
        await store.readProcessing("artifact")
        try check(store.processing?.available == false, "陈旧加工节点安全降级")
        await store.rebuildIndex()
        try check(fixture.requests.contains { $0.0.hasSuffix("index/rebuild") }, "本地索引重建")
        fixture.oldBackend = true
        await store.refresh()
        try check(store.error?.contains("更新 API 与 Worker") == true, "旧运行包明确升级提示")
        store.reset()
        try check(store.selectedTopicID == nil && store.navigation == nil && store.pendingRequestID == nil, "断线清数据与身份")
        await store.configure(address: "http://unsafe.example", token: "bad", domainID: "bad")
        try check(store.navigation == nil && store.schedule == nil, "非法连接清除旧身份")
        let quota = DomainFixture()
        let quotaStore = LocalDomainOverviewStore(transport: quota.call)
        await quotaStore.configure(address: "http://127.0.0.1:8000", token: "quota", domainID: "quota")
        await quotaStore.previewBuild()
        try check(quotaStore.needsBuildAuthorization, "未授权需在确认页授权")
        quota.policyEnabled = true
        await quotaStore.refresh()
        try check(!quotaStore.needsBuildAuthorization, "预算足够不重置授权")
        quota.remainingCalls = 1
        await quotaStore.refresh()
        try check(quotaStore.needsBuildAuthorization, "调用数耗尽需要新授权")
        quota.remainingCalls = 10; quota.remainingTokens = 1000
        await quotaStore.refresh()
        try check(quotaStore.needsBuildAuthorization, "输入预留不足需要新授权")
        try check(LocalDomainCoverage(source_count: 4, processed_source_count: 4, cited_source_count: nil,
            original_text_complete: false, excluded: [:]).complete, "全部纳入与全文阅读区分")
        try check(!LocalDomainCoverage(source_count: 4, processed_source_count: 3, cited_source_count: nil,
            original_text_complete: false, excluded: [:]).complete, "部分结果不作为全域概览")
        print("PASS: 概览 DTO、分页下钻、显式授权、预算范围、两阶段幂等重试、未知确认、最新任务恢复及身份隔离、预算耗尽与局部覆盖")
    }
}

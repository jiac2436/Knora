import Foundation

/// Deliberately delayed replies can finish after cancellation to exercise the publication guards.
@MainActor private final class LibraryLoadingFixture {
    struct Held {
        let continuation: CheckedContinuation<[String: Any], Error>
        let payload: [String: Any]
    }
    var requests: [String] = []
    var holds: Set<String> = []
    var waiting: [String: Held] = [:]
    var failures: [String: Int] = [:]
    var total = 2
    var files = 80
    var buildStatus: String?
    var revision = 1

    func key(_ kind: String, token: String = "synthetic", domain: String = "domain") -> String {
        "\(kind)|\(token)|\(domain)"
    }
    func waitFor(_ key: String) async throws {
        for _ in 0..<2000 {
            if waiting[key] != nil { return }
            try await Task.sleep(nanoseconds: 1_000_000)
        }
        throw failure("模拟延迟请求未出现：\(key)")
    }
    func release(_ key: String) {
        let held = waiting.removeValue(forKey: key)
        held?.continuation.resume(returning: held!.payload)
    }
    func count(_ key: String) -> Int { requests.filter { $0 == key }.count }
    func navigation(_ domain: String, topic: String?) -> [String: Any] {
        ["domain_id": domain, "scope": "app", "node": ["id": topic ?? domain,
            "kind": topic == nil ? "domain" : "topic", "name": "合成知识域", "parent_id": NSNull()],
         "overview": ["available": false, "reason": "not_built"], "index_status": "unavailable",
         "topics": ["items": [], "next_offset": NSNull()],
         "sources": ["items": [["id": "source-\(revision)", "title": "合成资料", "revision_id": NSNull()]], "next_offset": NSNull()]]
    }
    func information(_ domain: String) -> [String: Any] {
        let index: [String: Any] = ["total": total, "ready": total, "pending": 0, "failed": 0,
                                    "disabled": 0, "unavailable": 0, "chunk_count": total]
        return ["domain_id": domain, "scope": "app", "computed_at": "2026-10-15T00:00:00Z",
            "documents": ["total": total, "analyzed": total, "pending": 0, "outdated": 0, "running": 0, "failed": 0],
            "topics": ["total": 2, "roots": 1, "children": 1],
            "tags": ["total": 0, "manual": 0, "ai": 0, "both": 0, "items": [], "next_offset": NSNull()],
            "storage": ["total_bytes": NSNull(), "physical_bytes": NSNull(), "recorded_asset_bytes": 0,
                        "text_utf8_bytes": 120, "ai_payload_bytes": 0, "vector_payload_bytes": 0,
                        "known_payload_bytes": 120, "measurement": "logical_partial", "note": "合成逻辑量"],
            "rag": ["semantic_enabled": true, "keyword_available": true,
                    "layers": ["original": index, "summary": index, "topics": index, "domain": index]],
            "overview_coverage": NSNull()]
    }
    func call(_ api: API, _ path: String, _ method: String, _ body: [String: Any]?) async throws -> [String: Any] {
        let domain = String(path.split(separator: "/")[2])
        let kind = path.hasSuffix("library/sources") ? "files" : String(path.split(separator: "/").last!)
        let identifier = key(kind, token: api.token, domain: domain)
        requests.append(identifier)
        if let status = failures[identifier] {
            throw NSError(domain: "synthetic", code: status,
                userInfo: ["HTTPStatus": status, NSLocalizedDescriptionKey: "合成读取失败"])
        }
        let payload: [String: Any]
        switch kind {
        case "navigation": payload = navigation(domain, topic: body?["topic_id"] as? String)
        case "schedule-state":
            let coverage: [String: Any] = ["source_count": total, "processed_source_count": total]
            let latest: Any = buildStatus.map { status in
                ["id": "synthetic-build", "status": status, "topic_id": NSNull(), "coverage": coverage,
                 "budget": ["calls": 1, "input_token_reservation": 1000, "total_steps": 1], "steps": [], "last_error": NSNull()] as [String: Any]
            } ?? NSNull()
            payload = ["phase": buildStatus ?? "not_built", "automatic_status": "disabled", "remaining_calls": 5,
                "remaining_input_tokens": 10000, "latest_build": latest,
                "policy": ["enabled": true, "paused": false, "automatic_updates": false,
                    "automatic_max_sources": 0, "automatic_max_steps": 0, "max_calls": 5,
                    "max_input_tokens": 10000, "debounce_seconds": 30, "max_wait_seconds": 300]]
        case "library":
            payload = ["domain_id": domain, "scope": "app", "source_count": total, "topic_count": 2,
                "unclassified_count": 1, "items": [
                    ["id": "root-category", "name": "一级分类", "parent_id": NSNull(), "source_count": total, "direct_source_count": 1],
                    ["id": "child-category", "name": "二级分类", "parent_id": "root-category", "source_count": 1, "direct_source_count": 1]],
                "next_offset": NSNull()]
        case "information": payload = information(domain)
        case "files":
            let offset = body?["offset"] as? Int ?? 0, limit = body?["limit"] as? Int ?? 40
            let end = min(files, offset + limit)
            let rows: [[String: Any]] = offset < end ? (offset..<end).map {
                ["id": "file-\($0)", "title": "资料 \($0)", "revision_id": "revision-\(revision)"]
            } : []
            payload = ["domain_id": domain, "scope": "app", "topic_id": body?["topic_id"] ?? NSNull(),
                "unclassified": body?["unclassified"] ?? false, "items": rows,
                "total_count": files, "next_offset": end < files ? end as Any : NSNull()]
        default: payload = [:]
        }
        if holds.remove(identifier) != nil {
            return try await withCheckedThrowingContinuation { waiting[identifier] = Held(continuation: $0, payload: payload) }
        }
        return payload
    }
}

@main struct LocalDomainLoadingTest {
    @MainActor static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }
    @MainActor static func main() async throws {
        let fixture = LibraryLoadingFixture()
        let store = LocalDomainOverviewStore(transport: fixture.call)
        await store.configure(address: "http://127.0.0.1:8000", token: "synthetic", domainID: "domain")
        let rootID = store.navigation?.node.id
        let libraryKey = fixture.key("library"), infoKey = fixture.key("information")
        fixture.holds = [libraryKey, infoKey]
        let catalog = Task { await store.selectSection(.topics) }
        try await fixture.waitFor(libraryKey)
        let information = Task { await store.selectSection(.information) }
        try await fixture.waitFor(infoKey)
        let sameCatalog = Task { await store.selectSection(.topics) }
        await Task.yield()
        try check(store.selectedSection == .topics, "页面选择应立即生效，不等待读取")
        try check(store.directoryLoading && store.informationLoading, "两个页面独立加载")
        fixture.release(infoKey); await information.value
        try check(store.selectedSection == .topics && store.information?.documents.total == 2, "隐藏页面结果只填缓存，不切换当前页面")
        fixture.release(libraryKey); await catalog.value; await sameCatalog.value
        try check(fixture.count(libraryKey) == 1 && fixture.count(infoKey) == 1, "同资源并发读取合并为一次请求")
        for _ in 0..<15 {
            await store.selectSection(.information); await store.selectSection(.topics); await store.selectSection(.overview)
        }
        try check(fixture.count(libraryKey) == 1 && fixture.count(infoKey) == 1, "缓存有效时快速切页不重复请求")
        try check(store.navigation?.node.id == rootID, "切页不清空已有概览")

        fixture.total = 3; fixture.holds.insert(infoKey)
        let refresh = Task { await store.loadInformation(force: true) }
        try await fixture.waitFor(infoKey)
        try check(store.information?.documents.total == 2 && store.informationLoading, "后台刷新期间保留原有信息")
        await store.selectSection(.overview)
        fixture.release(infoKey); await refresh.value
        try check(store.selectedSection == .overview && store.information?.documents.total == 3, "晚到响应不抢占当前页")
        fixture.failures[infoKey] = 503
        await store.loadInformation(force: true)
        try check(store.information?.documents.total == 3 && store.informationError != nil && !store.informationLoading,
                  "刷新失败保留内容并终止加载状态")
        fixture.failures[infoKey] = nil

        let branch = "root-category", filesKey = fixture.key("files")
        await store.loadTreeSources(topicID: branch)
        await store.loadTreeSources(topicID: branch, more: true)
        try check(store.treeSources[branch]?.items.count == 80, "文件分页追加不替换已有页")
        fixture.files = 75; fixture.revision = 2; fixture.holds.insert(filesKey)
        let refreshFiles = Task { await store.loadTreeSources(topicID: branch, force: true) }
        try await fixture.waitFor(filesKey)
        try check(store.treeSources[branch]?.items.count == 80, "刷新文件时保留已加载所有页")
        fixture.release(filesKey); await refreshFiles.value
        try check(store.treeSources[branch]?.items.count == 75 && store.treeSources[branch]?.total_count == 75,
                  "后台刷新原子替换已读范围，删除文件不残留")
        try check(store.treeSources[branch]?.items.first?.revision_id == "revision-2", "分页版本更新")
        try check(LocalDomainOverviewStore.treeKey(topicID: nil, unclassified: true) != LocalDomainOverviewStore.treeKey(topicID: nil),
                  "未分类与全部文件缓存隔离")

        let zeroAge = LocalDomainOverviewStore(transport: fixture.call, cacheLifetime: 0)
        await zeroAge.configure(address: "http://127.0.0.1:8000", token: "synthetic", domainID: "domain")
        await zeroAge.selectSection(.topics); await zeroAge.loadTreeSources(topicID: branch)
        let beforeFiles = fixture.count(filesKey)
        await zeroAge.selectSection(.information); fixture.files = 2
        await zeroAge.selectSection(.topics)
        try check(fixture.count(filesKey) > beforeFiles && zeroAge.treeSources[branch]?.items.count == 2,
                  "切回目录会刷新过期的已展开文件缓存")

        fixture.holds.insert(infoKey)
        let oldRead = Task { await store.loadInformation(force: true) }
        try await fixture.waitFor(infoKey)
        await store.configure(address: "http://127.0.0.1:8000", token: "new-token", domainID: "new-domain")
        try check(store.information == nil && store.directory == nil && store.treeSources.isEmpty, "切换身份立即清空其他身份的缓存")
        fixture.total = 9; await store.loadInformation()
        fixture.release(infoKey); await oldRead.value
        try check(store.information?.domain_id == "new-domain" && store.information?.documents.total == 9,
                  "取消后仍返回的旧响应不能覆盖新身份")
        fixture.failures[fixture.key("preview", token: "new-token", domain: "new-domain")] = 401
        await store.previewBuild()
        try check(store.navigation == nil && store.information == nil && store.directory == nil && store.treeSources.isEmpty,
                  "预览收到失效授权时清除所有敏感缓存")
        let afterExpiry = fixture.requests.count
        await store.selectSection(.topics); await store.selectSection(.information)
        try check(store.requiresReconnect && store.selectedSection == .overview,
                  "授权失效后呈现重新连接状态，不进入空数据分类或信息骨架")
        try check(!store.directoryLoading && !store.informationLoading && fixture.requests.count == afterExpiry,
                  "授权失效后切页不会伪装为仍在读取或重复发请求")
        await store.configure(address: "http://127.0.0.1:8000", token: "synthetic", domainID: "domain")
        await store.selectSection(.information)
        try check(!store.requiresReconnect && store.information != nil, "重新连接成功后恢复页面读取")

        let polling = LocalDomainOverviewStore(transport: fixture.call)
        fixture.buildStatus = "pending"; fixture.total = 4
        await polling.configure(address: "http://127.0.0.1:8000", token: "synthetic", domainID: "domain")
        await polling.selectSection(.information)
        let beforeNavigation = fixture.count(fixture.key("navigation")), beforeInformation = fixture.count(infoKey)
        await polling.refreshProgress()
        try check(fixture.count(fixture.key("navigation")) == beforeNavigation && fixture.count(infoKey) == beforeInformation,
                  "未变化的任务轮询只读元数据")
        fixture.buildStatus = "completed"; fixture.total = 5; fixture.revision = 3
        await polling.refreshProgress()
        try check(polling.information?.documents.total == 5 && polling.navigation?.sources.items.first?.id == "source-3",
                  "任务完成自动更新概览与当前信息页")
        try check(!fixture.requests.contains { $0.hasPrefix("jobs|") || $0.hasPrefix("authorization|") }, "切页与轮询不创建收费任务")
        print("PASS: 三页快速切换、独立缓存、同资源单飞、刷新保留内容、文件分页原子替换、过期分支刷新、身份隔离、授权失效清理及任务完成自动回显")
    }
}

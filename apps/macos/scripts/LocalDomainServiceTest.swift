import Foundation

/// Real bundled API/worker + native LocalService, isolated data and private device credentials.
/// Never configures a gateway, grants model budget or creates analysis jobs.
@main struct LocalDomainServiceTest {
    @MainActor static func check(_ value: Bool, _ message: String) throws {
        if !value { throw failure(message) }
    }
    @MainActor static func inspectDatabase(runtime: URL, data: URL) throws -> [String: Any] {
        let process = Process()
        process.executableURL = runtime.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", "-B", "-c", """
import json,sqlite3,sys
from pathlib import Path
sys.path.insert(0,str(Path(sys.argv[2])/'backend'))
from app.migrations import CURRENT_VERSION
connection=sqlite3.connect('file:'+str(Path(sys.argv[1])/'knowledge.db')+'?mode=ro',uri=True)
counts={table:connection.execute('SELECT COUNT(*) FROM '+table).fetchone()[0] for table in
 ('sources','ai_jobs','domain_summary_jobs','domain_builds','domain_processing_preferences')}
version=connection.execute('SELECT MAX(version) FROM schema_migrations').fetchone()[0]
connection.close()
print(json.dumps(dict(version=version,expected_version=CURRENT_VERSION,counts=counts)))
""", data.path, runtime.path]
        let output = Pipe(), errors = Pipe()
        process.standardOutput = output; process.standardError = errors
        try process.run(); process.waitUntilExit()
        let result = output.fileHandleForReading.readDataToEndOfFile()
        guard process.terminationStatus == 0 else {
            let reason = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            throw failure("隔离库检查失败：" + reason)
        }
        guard let body = try JSONSerialization.jsonObject(with: result) as? [String: Any] else {
            throw failure("隔离库检查结果格式错误")
        }
        return body
    }
    @MainActor static func checkDatabase(_ body: [String: Any]) throws {
        try check(body["version"] as? Int == body["expected_version"] as? Int && (body["expected_version"] as? Int ?? 0) >= 13,
                  "运行包与数据库版本不一致或缺少知识域能力")
        guard let counts = body["counts"] as? [String: Int] else { throw failure("隔离库计数缺失") }
        try check(counts["sources"] == 41, "资料数量错误")
        try check(["ai_jobs", "domain_summary_jobs", "domain_builds", "domain_processing_preferences"].allSatisfy {
            counts[$0] == 0
        }, "只读导航、预览或本地索引意外创建收费任务或授权")
    }
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else { throw failure("需要已验证的运行包路径") }
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
        let home = FileManager.default.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("pka-domain-service-\(UUID().uuidString)")
        let key = "synthetic.pka.domain-service.\(UUID().uuidString)"
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let service = LocalService(home: home, credentialService: key)
        let library = try service.currentLibrary()
        defer {
            service.stopAndWait()
            TokenStore.delete(service: LocalAIConfiguration.tokenService(library))
            UserDefaults.standard.removeObject(forKey: LocalAIConfiguration.addressKey(library))
            try? FileManager.default.removeItem(at: home)
        }
        let first = try await Task.detached { try service.start(runtime: runtime) }.value
        let api = try API(address: first.address, token: first.token)
        let owner = try await api.call("v1/me")["id"] as? String
        let privateCredential = try MacDeviceCredentialStore(home: home, legacy: KeychainCredentialStore()).loadOrCreate(for: library)
        try check(owner != nil && privateCredential == first.token, "私有设备凭证与本地身份错误")
        let domain = try await api.call("v1/domains", method: "POST", json: ["name": "概览运行包验收"])
        guard let domainID = domain["id"] as? String else { throw failure("知识域创建失败") }
        let store = LocalDomainOverviewStore()
        await store.configure(address: first.address, token: first.token, domainID: domainID)
        try check(store.error == nil && store.navigation?.sources.items.isEmpty == true &&
                  store.navigation?.overview.available == false, "空知识域真实导航失败")
        try check(store.schedule?.policy.enabled == false && store.schedule?.policy.automatic_updates == false &&
                  store.schedule?.latest_build == nil, "首次状态不应默认授权或自动执行")
        for index in 0..<41 {
            _ = try await api.call("v1/captures", method: "POST", json: [
                "capture_type": "text", "title": "验收资料 \(index)", "coverage": "full_text",
                "content": ["text": "这是一条独立的本地概览验收笔记。编号 \(index)。", "image_urls": []] as [String: Any],
                "context": ["domain_id": domainID], "client": ["kind": "desktop", "version": "isolated-test"]],
                key: UUID().uuidString)
        }
        await store.refresh()
        try check(store.error == nil && store.navigation?.sources.items.count == 40 &&
                  store.navigation?.sources.next_offset == 40, "真实导航分页第一页错误")
        await store.loadMoreSources()
        try check(store.error == nil && store.navigation?.sources.items.count == 41 &&
                  Set(store.navigation?.sources.items.map(\.id) ?? []).count == 41 &&
                  store.navigation?.sources.next_offset == nil, "真实分页追加/去重错误")
        let topics = LocalDomainTopicsStore()
        await topics.configure(address: first.address, token: first.token, domainID: domainID)
        let createdTopic = await topics.save(topicID: nil, name: "人工测试主题", parentID: nil)
        try check(createdTopic && topics.error == nil, "真实主题创建失败")
        guard let topicID = topics.selectedID, let memberID = store.navigation?.sources.items.first?.id else { throw failure("主题或测试资料缺失") }
        await topics.select(topicID)
        await topics.setIncluded(sourceID: memberID, included: true)
        try check(topics.error == nil && topics.members.contains(memberID), "真实归属加入失败")
        await topics.loadMoreSources()
        try check(topics.sources.count == 41, "主题管理全域资料分页")
        let renamed = await topics.save(topicID: topicID, name: "固定人工名称", parentID: nil)
        try check(renamed && topics.topics.first { $0.id == topicID }?.name == "固定人工名称", "人工命名固定失败")
        let childCreated = await topics.save(topicID: nil, name: "人工子主题", parentID: topicID)
        try check(childCreated, "真实二级主题创建失败")
        guard let childID = topics.selectedID else { throw failure("子主题缺失") }
        let invalidParent = await topics.save(topicID: topicID, name: "固定人工名称", parentID: childID)
        try check(!invalidParent && topics.error != nil, "循环父级应由后端拒绝")
        await topics.select(childID)
        await topics.setIncluded(sourceID: memberID, included: true)
        await topics.setIncluded(sourceID: memberID, included: false)
        try check(topics.error == nil && !topics.members.contains(memberID), "真实归属排除失败")
        let original = try await api.call("v1/sources/\(memberID)")
        try check(original["extracted_text"] as? String != nil, "移出主题不应删除正文")
        await store.previewBuild()
        try check(store.preview == nil && store.error?.contains("没有有效单篇摘要") == true &&
                  store.error?.contains("不会自动补做收费分析") == true, "未分析资料预览应给出明确错误")
        let rebuilt = try await api.call("v1/domains/\(domainID)/analysis/index/rebuild", method: "POST", json: ["scope": "app"])
        try check(rebuilt["queued"] as? Int == 0 && ["disabled", "not_built"].contains(rebuilt["status"] as? String ?? ""),
                  "没有概览时重建应安全降级")
        await store.rebuildIndex()
        try check(store.error == nil && store.navigation?.index_status == "unavailable", "Store索引重建错误")
        try checkDatabase(inspectDatabase(runtime: runtime, data: library.data))
        await store.loadMoreSources()
        let oldSourceIDs = Set(store.navigation?.sources.items.map(\.id) ?? [])
        _ = service.stopAndWait()
        store.reset()
        let second = try await Task.detached { try service.start(runtime: runtime) }.value
        let restartedAPI = try API(address: second.address, token: second.token)
        let restartedOwner = try await restartedAPI.call("v1/me")["id"] as? String
        try check(second.token == first.token && restartedOwner == owner, "重启本地身份或钥匙串凭证发生变化")
        await store.configure(address: second.address, token: second.token, domainID: domainID)
        await store.loadMoreSources()
        try check(store.error == nil && Set(store.navigation?.sources.items.map(\.id) ?? []) == oldSourceIDs,
                  "重启后资料或导航未保留")
        try check(store.schedule?.policy.enabled == false && store.schedule?.latest_build == nil, "重启意外授权或收费加工")
        await topics.configure(address: second.address, token: second.token, domainID: domainID)
        await topics.select(topicID)
        try check(topics.error == nil && topics.members.contains(memberID) && topics.topics.contains { $0.id == topicID && $0.name == "固定人工名称" }, "重启应保留人工名称与成员")
        await topics.select(childID)
        try check(topics.error == nil && !topics.members.contains(memberID), "重启保留人工排除")
        try checkDatabase(inspectDatabase(runtime: runtime, data: library.data))
        print("PASS: 当前独立运行包、真实LocalService/API/Worker、隔离钥匙串、空库状态、41篇分页、明确预览阻断、本地索引安全降级、人工主题/两级结构/归属排除、无收费任务及重启持久化")
    }
}

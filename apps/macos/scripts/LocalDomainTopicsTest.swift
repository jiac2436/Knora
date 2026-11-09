import Foundation

@MainActor private final class TopicFixture {
    var requests: [(String, String, [String: Any]?)] = []
    var loseCreate = false
    var failMembers = false
    var delayOld = false
    var invalidPage = false
    var ids: Set<String> = ["source0", "source40"]
    var names = ["root": "一级主题", "child": "二级主题"]
    func call(_ api: API, _ path: String, _ method: String, _ body: [String: Any]?) async throws -> [String: Any] {
        requests.append((path, method, body))
        if delayOld && api.token == "old" { try await Task.sleep(nanoseconds: 60_000_000) }
        if path.contains("topics?scope") {
            let second = path.contains("offset=100")
            let id = second ? "child" : "root"
            return ["items": [["id": id, "name": names[id]!, "parent_id": second ? "root" as Any : NSNull(), "source_count": ids.count]],
                    "next_offset": second ? NSNull() : (invalidPage ? 0 : 100) as Any]
        }
        if path.hasSuffix("navigation") {
            let offset = body?["source_offset"] as? Int ?? 0
            return ["domain_id": "domain", "scope": "app", "node": ["id": "domain", "kind": "domain", "name": "领域", "parent_id": NSNull()],
                    "overview": ["available": false], "index_status": "unavailable", "topics": ["items": [], "next_offset": NSNull()],
                    "sources": ["items": [["id": "source\(offset)", "title": "合成资料", "revision_id": NSNull()]], "next_offset": offset == 0 ? 40 as Any : NSNull()]]
        }
        if path.contains("/sources?") {
            if failMembers { throw URLError(.notConnectedToInternet) }
            let second = path.contains("offset=100"), id = second ? "source40" : "source0"
            return ["items": ids.contains(id) ? [["id": id, "title": "资料", "revision_id": NSNull()]] : [], "next_offset": second ? NSNull() : 100 as Any]
        }
        if path.contains("/members/") {
            let id = String(path.split(separator: "/").last!)
            if body?["included"] as? Bool == true { ids.insert(id) } else { ids.remove(id) }
            return ["source_id": id]
        }
        if method == "POST" {
            if loseCreate { loseCreate = false; throw URLError(.timedOut) }
            return ["id": "root"]
        }
        if method == "PATCH" { names["root"] = body?["name"] as? String; return ["id": "root"] }
        throw failure("未知测试请求")
    }
}
@main struct LocalDomainTopicsTest {
    @MainActor static func check(_ value: Bool, _ message: String) throws { if !value { throw failure(message) } }
    @MainActor static func main() async throws {
        let fixture = TopicFixture()
        let subject = LocalDomainTopicsStore(transport: fixture.call)
        await subject.configure(address: "http://127.0.0.1:8000", token: "test", domainID: "domain")
        try check(subject.topics.map(\.id) == ["root", "child"], "完整主题分页")
        await subject.select("root")
        try check(subject.members == ["source0", "source40"], "成员分页完成才能修改")
        await subject.loadMoreSources()
        try check(subject.sources.map(\.id) == ["source0", "source40"], "全域资料分页")
        await subject.setIncluded(sourceID: "source40", included: false)
        try check(!subject.members.contains("source40"), "移出资料归属")
        try check(fixture.requests.contains { $0.0.hasSuffix("/members/source40") && $0.1 == "PUT" && $0.2?["included"] as? Bool == false }, "显式排除契约")
        let writes = fixture.requests.filter { $0.1 != "GET" && !$0.0.hasSuffix("navigation") }.count
        let blank = await subject.save(topicID: nil, name: "  ", parentID: nil)
        try check(!blank && fixture.requests.filter { $0.1 != "GET" && !$0.0.hasSuffix("navigation") }.count == writes, "空名称不发送")
        let edited = await subject.save(topicID: "root", name: " 人工更名 ", parentID: nil)
        try check(edited && subject.topics.first?.name == "人工更名", "人工更名刷新")
        let edit = fixture.requests.last { $0.1 == "PATCH" }!
        try check(edit.2?["parent_id"] is NSNull && edit.2?["name"] as? String == "人工更名", "清除父级与名称清理")
        fixture.loseCreate = true
        let created = await subject.save(topicID: nil, name: "新主题", parentID: "root")
        try check(!created && subject.creationUnconfirmed, "创建丢响应不盲目重复")
        let count = fixture.requests.count
        _ = await subject.save(topicID: nil, name: "新主题", parentID: nil)
        try check(fixture.requests.count == count, "未核对不能重复创建")
        await subject.refresh()
        try check(subject.creationUnconfirmed && subject.unconfirmedName == "新主题", "刷新不能自动解除创建未知")
        subject.acknowledgeUnconfirmedCreation(confirmed: false)
        try check(subject.creationUnconfirmed, "未确认不能解除限制")
        subject.acknowledgeUnconfirmedCreation(confirmed: true)
        try check(!subject.creationUnconfirmed, "明确核对后允许继续")
        fixture.failMembers = true
        await subject.select("child")
        try check(subject.selectedID == nil && subject.members.isEmpty, "成员读取失败不显示虚假复选状态")
        let failed = fixture.requests.count
        await subject.setIncluded(sourceID: "source0", included: false)
        try check(fixture.requests.count == failed, "未知归属不写入")
        fixture.failMembers = false; fixture.delayOld = true
        let old = Task { await subject.configure(address: "http://127.0.0.1:8000", token: "old", domainID: "old") }
        try await Task.sleep(nanoseconds: 5_000_000)
        await subject.configure(address: "http://127.0.0.1:8000", token: "new", domainID: "new")
        await old.value
        try check(subject.topics.count == 2 && !subject.isLoading, "旧读取不覆盖新身份")
        fixture.delayOld = false
        await subject.configure(address: "http://127.0.0.1:8000", token: "old", domainID: "old")
        fixture.delayOld = true
        let mutation = Task { await subject.save(topicID: "root", name: "旧身份迟到写入", parentID: nil) }
        try await Task.sleep(nanoseconds: 5_000_000)
        await subject.configure(address: "http://127.0.0.1:8000", token: "new", domainID: "new")
        let newName = subject.topics.first?.name
        let saved = await mutation.value
        try check(!saved && subject.topics.first?.name == newName && subject.message == nil && !subject.isMutating, "旧身份写响应不能污染新库")
        fixture.invalidPage = true
        await subject.refresh()
        try check(subject.error?.contains("分页") == true, "无进展分页拒绝无限循环")
        try check(!fixture.requests.contains { $0.0.contains("authorization") || $0.0.contains("build/jobs") || $0.1 == "DELETE" }, "人工操作不收费不删原文")
        subject.reset(); try check(subject.topics.isEmpty && subject.members.isEmpty && subject.selectedID == nil, "断线清理")
        print("PASS: 主题目录/成员/资料分页、人工更名/父级、归属排除、创建未知响应、成员读取失败、身份隔离与零收费")
    }
}

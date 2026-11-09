import Foundation

@main struct LocalTagsTest {
    @MainActor static func main() async throws {
        let tag = try decodeLocalAI(LocalTag.self, body: ["id": "t", "name": "检索增强生成", "dimension": "topic", "parent_id": "parent", "aliases": [["id": "alias", "name": "RAG"]]])
        try check(tag.aliases.first?.name == "RAG" && tag.parent_id == "parent" && tag.dimensionText == "主题", "标准标签/别名/层级契约错误")
        let detail = try decodeLocalAI(LocalSourceTag.self, body: ["id": "t", "name": "RAG", "dimension": "topic", "origins": ["ai", "manual"], "stale": false,
            "assignments": [["origin": "ai", "stale": true, "revision_id": "old"] as [String: Any], ["origin": "manual", "stale": false, "revision_id": NSNull()]]])
        try check(detail.originText == "人工、AI" && !detail.stale && detail.assignments.first?.stale == true, "双来源去重、人工优先或独立过期状态错误")
        let model = CaptureModel(runtime: nil)
        try check(!model.tagWriteAllowed, "非本地或未连接时允许标签写入")
        model.sourceTags = [detail]; model.localTags = [tag]; model.tagsBusy = true
        let generation = model.tagsGeneration
        model.resetLocalTags()
        try check(model.tagsGeneration != generation && model.sourceTags.isEmpty && model.localTags.isEmpty && !model.tagsBusy, "库切换未取消晚到标签状态")
        print("PASS: 标签/别名/主题层级 DTO、双来源与独立过期、未连接禁写、状态代次重置")
        if CommandLine.arguments.count > 1 { try await endToEnd(runtime: URL(fileURLWithPath: CommandLine.arguments[1])) }
    }
    @MainActor static func endToEnd(runtime: URL) async throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent("pka-local-tags-e2e-\(UUID().uuidString)")
        let key = "synthetic.pka.tags-e2e.\(UUID().uuidString)"
        let service = LocalService(home: root, credentialService: key)
        let library = try service.currentLibrary()
        defer {
            service.stop(); TokenStore.delete(service: key)
            try? manager.removeItem(at: root)
        }
        let model = CaptureModel(queueStore: CaptureQueueStore(root: library.data.appendingPathComponent("capture-queue")), localService: service, runtime: runtime)
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        let api = try API(address: model.address, token: model.token)
        let domain = try await api.call("v1/domains", method: "POST", json: ["name": "合成标签领域"])
        let capture = try await api.call("v1/captures", method: "POST", json: [
            "capture_type": "browser_selection", "title": "合成标签测试", "coverage": "partial_text",
            "content": ["text": "合成知识库标签测试原文，保留人工标注与来源。", "image_urls": []] as [String: Any],
            "context": ["domain_id": domain["id"]!] as [String: Any], "client": ["kind": "desktop", "version": "synthetic-tags-test"]], key: UUID().uuidString)
        guard let id = capture["id"] as? String else { throw failure("合成资料创建失败") }
        await model.openSource(id, force: true)
        await model.mutateLocalTag("v1/tags", body: ["name": "人工测试", "dimension": "topic"])
        guard let tag = model.localTags.first(where: { $0.name == "人工测试" }) else { throw failure(model.tagsMessage) }
        await model.mutateLocalTag("v1/tags", body: ["name": "子主题", "dimension": "topic", "parent_id": tag.id])
        guard let child = model.localTags.first(where: { $0.name == "子主题" }) else { throw failure(model.tagsMessage) }
        try check(child.parent_id == tag.id, "两级主题未加载")
        await model.mutateLocalTag("v1/tags/\(child.id)", method: "PATCH", body: ["name": "重命名子主题", "parent_id": NSNull()])
        try check(model.localTags.first(where: { $0.id == child.id })?.parent_id == nil, "主题父节点清除失败")
        await model.mutateLocalTag("v1/tags/\(tag.id)/aliases", body: ["name": "合成别名"])
        try check(model.localTags.first(where: { $0.id == tag.id })?.aliases.first?.name == "合成别名", "真实API别名未加载")
        await model.mutateLocalTag("v1/sources/\(id)/tags", body: ["tag_id": tag.id], source: true)
        try check(model.sourceTags.count == 1 && model.sourceTags.first?.origins == ["manual"], "人工关联未成功或来源错误")
        await model.mutateLocalTag("v1/sources/\(id)/tags", body: ["tag_id": tag.id], source: true)
        try check(model.sourceTags.count == 1, "重复人工添加未去重")
        model.backupBusy = true
        await model.mutateLocalTag("v1/sources/\(id)/tags/\(tag.id)", method: "DELETE", source: true)
        try check(model.sourceTags.count == 1, "备份期间仍允许写入")
        model.backupBusy = false
        let snapshot = try model.tagRequestContext(source: true)
        model.sourceLoadGeneration = UUID()
        try check(!model.tagRequestCurrent(snapshot), "资料重载的晚到响应未拦截")
        let snapshot2 = try model.tagRequestContext(source: true)
        model.token += "changed"
        try check(!model.tagRequestCurrent(snapshot2), "凭证变化后晚到响应未拦截")
        model.token = snapshot2.token
        await model.mutateLocalTag("v1/sources/\(id)/tags/\(tag.id)", method: "DELETE", source: true, query: [URLQueryItem(name: "origin", value: "manual")])
        try check(model.sourceTags.isEmpty, "人工关联按来源移除失败")
        await model.mutateLocalTag("v1/tags", body: ["name": "目标标签", "dimension": "topic"])
        guard let target = model.localTags.first(where: { $0.name == "目标标签" }) else { throw failure(model.tagsMessage) }
        await model.mutateLocalTag("v1/sources/\(id)/tags", body: ["tag_id": tag.id], source: true)
        await model.mutateLocalTag("v1/tags/\(tag.id)/merge", body: ["target_id": target.id])
        await model.refreshLocalTags(source: true)
        try check(model.sourceTags.first?.id == target.id && model.sourceTags.first?.origins == ["manual"], "合并未保留资料人工来源")
        try check(model.localTags.first(where: { $0.id == target.id })?.aliases.contains(where: { $0.name == "人工测试" }) == true, "合并未保留旧名称别名")
        let source = try await api.call("v1/sources/\(id)")
        try check((source["extracted_text"] as? String)?.contains("合成知识库标签测试原文") == true, "标签操作修改原文")
        for index in 0..<101 {
            _ = try await api.call("v1/tags", method: "POST", json: ["name": "分页对象-\(index)", "dimension": "entity"])
        }
        model.resetLocalTags()
        await model.refreshLocalTags(source: true)
        try check(model.sourceTags.first?.id == target.id, "状态重置后标签无法重载")
        try check(model.localTags.filter { $0.dimension == "entity" }.count == 101, "标签目录分页丢失第二页")
        print("PASS: 真实临时本地API标签创建/别名/人工关联/重复去重/按来源删除/合并/原文保留/重载；备份禁写与晚到保护")
    }
    static func check(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw failure(message) }
    }
}

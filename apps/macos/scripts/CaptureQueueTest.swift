import Foundation

@main struct CaptureQueueTest {
    @MainActor static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-queue-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let store = CaptureQueueStore(root: root.appendingPathComponent("queue"))
        let original = root.appendingPathComponent("original.pdf")
        try Data("sample file".utf8).write(to: original)
        let model = CaptureModel(queueStore: store)
        model.draft = "还没有加入列表的草稿"
        let recoveredDraft = CaptureModel(queueStore: store)
        assert(recoveredDraft.draft == model.draft)
        model.addDraft()
        model.addFiles([original])
        assert(model.items.count == 2)
        let ids = model.items.map(\.id)
        let staged = model.items[1].file!
        assert(store.owns(staged))
        try FileManager.default.removeItem(at: original)
        model.cleanup()
        assert(FileManager.default.fileExists(atPath: staged.path))
        let resumed = CaptureModel(queueStore: store)
        assert(resumed.items.map(\.id) == ids)
        assert(resumed.items[0].text == "还没有加入列表的草稿")
        let digest = try store.digest(staged)
        assert(resumed.items[1].fileDigest == digest)
        resumed.remove(resumed.items[1])
        assert(!FileManager.default.fileExists(atPath: staged.path))
        let remaining = try store.load()
        assert(remaining?.items.count == 1)

        let markdown = root.appendingPathComponent("计划.md")
        try Data("# 计划\n\n可搜索正文".utf8).write(to: markdown)
        let markdownStore = CaptureQueueStore(root: root.appendingPathComponent("markdown-queue"))
        let markdownModel = CaptureModel(queueStore: markdownStore)
        markdownModel.addFiles([markdown])
        assert(markdownModel.items.count == 1)
        assert(markdownModel.items[0].title == "计划.md")
        assert(markdownModel.items[0].kind == .file)
        assert(markdownModel.items[0].file != nil)

        // Stable media IDs and cached payload survive a restart after partial upload.
        let image = root.appendingPathComponent("image.bin")
        try Data("image".utf8).write(to: image)
        let media = FeishuImage(ordinal: 0, token: "resource-id", blockID: "block", caption: "caption", file: image)
        let document = FeishuDocument(markdown: "body", title: "title", documentID: "document", revisionID: 3,
                                      cliVersion: "test", partial: false, images: [media])
        var item = CaptureItem(kind: .link, title: "title", text: "https://example.feishu.cn/docx/document", file: nil)
        item.feishuDocument = try store.archive(document, itemID: item.id)
        item.destination = "local|owner"
        item.sourceID = "known-source"
        item.uploadedImageIDs = [media.id]
        try store.save(CaptureQueueSnapshot(draft: "", items: [item], domainID: "domain", projectID: "project"))
        let restored = try store.load()!.items[0]
        assert(restored.id == item.id && restored.sourceID == "known-source")
        assert(restored.feishuDocument?.images[0].id == media.id)
        assert(restored.uploadedImageIDs == [media.id])
        assert(store.owns(restored.feishuDocument!.images[0].file!))

        // Invalid paths cannot be committed, and corrupted/unknown state is not overwritten by the model.
        let good = try Data(contentsOf: store.manifest)
        let invalid = CaptureItem(kind: .file, title: "invalid", text: nil, file: root.appendingPathComponent("outside"))
        do {
            try store.save(CaptureQueueSnapshot(draft: "", items: [invalid], domainID: "", projectID: ""))
            fatalError("不应接受外部原件路径")
        } catch {}
        let afterInvalid = try Data(contentsOf: store.manifest)
        assert(afterInvalid == good)
        let future = Data("{\"version\":99,\"draft\":\"keep\",\"items\":[],\"domainID\":\"\",\"projectID\":\"\"}".utf8)
        try future.write(to: store.manifest)
        let blocked = CaptureModel(queueStore: store)
        blocked.draft = "不应覆盖"
        assert(!blocked.persistQueue())
        let afterFuture = try Data(contentsOf: store.manifest)
        assert(afterFuture == future)
        try Data("broken".utf8).write(to: store.manifest)
        let broken = CaptureModel(queueStore: store)
        broken.cleanup()
        let afterBroken = try Data(contentsOf: store.manifest)
        assert(afterBroken == Data("broken".utf8))
        try good.write(to: store.manifest)
        let fullDisk = CaptureQueueStore(root: store.root, writeSnapshot: { _, _ in
            throw CocoaError(.fileWriteOutOfSpace)
        })
        let unsaved = CaptureModel(queueStore: fullDisk)
        unsaved.draft = "空间不足时不能覆盖原队列"
        assert(!unsaved.persistQueue())
        assert(unsaved.queueMessage.contains("磁盘空间不足"))
        unsaved.remove(unsaved.items[0])
        assert(unsaved.items.count == 1)
        let preserved = try Data(contentsOf: store.manifest)
        assert(preserved == good)
        print("PASS: 模拟磁盘不足时保留原队列、提示原因且不删除条目")
        if CommandLine.arguments.count > 1 {
            let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
            let home = root.appendingPathComponent("integration")
            let serviceName = "local.pka.queue-test.\(UUID().uuidString)"
            let service = LocalService(home: home, credentialService: serviceName)
            defer { service.stop(); TokenStore.delete(service: serviceName) }
            let localQueue = CaptureQueueStore(root: home.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox/capture-queue"))
            let first = CaptureModel(queueStore: localQueue, localService: service, runtime: runtime)
            await first.startLocalService()
            assert(first.connected, first.authMessage)
            first.domainName = "恢复测试"
            await first.createDomain()
            first.draft = "服务已经保存，但客户端没有记录收到响应"
            first.addDraft()
            let csv = root.appendingPathComponent("sample.csv")
            try Data("name,value\nexample,1\n".utf8).write(to: csv)
            first.addFiles([csv])
            let persistedFile = first.items[1].file!
            await first.send()
            assert(first.items[1].sent, first.items[1].status)
            assert(FileManager.default.fileExists(atPath: persistedFile.path))
            assert(first.items[0].sent, first.items[0].status)
            let originalID = first.items[0].id
            // Simulate interruption after server commit and before the response is persisted.
            first.items[0].sent = false
            first.items[0].sourceID = nil
            first.items[1].sent = false
            first.items[1].sourceID = nil
            assert(first.persistQueue())
            first.cleanup()
            service.stop()
            try await Task.sleep(nanoseconds: 1_500_000_000)
            let second = CaptureModel(queueStore: localQueue, localService: service, runtime: runtime)
            assert(second.items[0].id == originalID)
            await second.startLocalService()
            assert(second.connected, second.authMessage)
            await second.send()
            assert(second.items[0].sent, second.items[0].status)
            assert(second.items[1].sent, second.items[1].status)
            let api = try API(address: second.address, token: second.token)
            let sources = try await api.call("v1/sources")
            assert((sources["items"] as? [[String: Any]])?.count == 2, "重试不应重复采集")
            let observer = NotificationCenter.default.addObserver(forName: .localServiceStopped, object: nil, queue: .main) { notification in
                Task { @MainActor in
                    second.localServiceStopped(service: notification.object as? LocalService)
                }
            }
            service.interruptOwnedHostForTesting()
            var recoveryStarted = false
            for _ in 0..<300 {
                if second.recoveringService { recoveryStarted = true }
                if recoveryStarted && second.connected && !second.recoveringService { break }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            NotificationCenter.default.removeObserver(observer)
            assert(recoveryStarted && second.connected && !second.recoveringService, second.authMessage)
            let recoveredAPI = try API(address: second.address, token: second.token)
            let afterCrash = try await recoveredAPI.call("v1/sources")
            assert((afterCrash["items"] as? [[String: Any]])?.count == 2)
            let healthyAddress = second.address
            await second.checkLocalServiceAfterWake()
            assert(second.connected && second.address == healthyAddress, "健康服务不应在唤醒检查时被重启")
            let savedQueue = try Data(contentsOf: localQueue.manifest)
            try FileManager.default.setAttributes([.posixPermissions: 0o500], ofItemAtPath: localQueue.root.path)
            defer { try? FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: localQueue.root.path) }
            second.draft = "目录不可写时阻止发送"
            assert(!second.persistQueue())
            await second.send()
            let noWrites = try await recoveredAPI.call("v1/sources")
            assert((noWrites["items"] as? [[String: Any]])?.count == 2)
            let afterWriteFailure = try Data(contentsOf: localQueue.manifest)
            assert(afterWriteFailure == savedQueue)
            let itemCount = second.items.count
            second.remove(second.items[1])
            assert(second.items.count == itemCount && FileManager.default.fileExists(atPath: persistedFile.path))
            try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: localQueue.root.path)
            assert(second.persistQueue())
            await second.send()
            assert(second.items.last?.sent == true)
            let savedAfterRepair = try await recoveredAPI.call("v1/sources")
            assert((savedAfterRepair["items"] as? [[String: Any]])?.count == 3)
            print("PASS: 目录不可写时阻止请求并保留队列/原件，恢复写入后可继续保存")

            let expire = Process()
            expire.executableURL = runtime.appendingPathComponent("python/bin/python3")
            expire.arguments = ["-I", "-B", "-c", "import sqlite3,sys; db=sqlite3.connect(sys.argv[1]); db.execute(\"UPDATE sessions SET expires_at='2000-01-01 00:00:00'\"); db.commit(); db.close()",
                                home.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox/knowledge.db").path]
            try expire.run()
            expire.waitUntilExit()
            assert(expire.terminationStatus == 0)
            await second.checkLocalServiceAfterWake()
            for _ in 0..<300 {
                if second.connected && !second.recoveringService { break }
                try await Task.sleep(nanoseconds: 100_000_000)
            }
            assert(second.connected && !second.recoveringService, second.authMessage)
            let renewedAPI = try API(address: second.address, token: second.token)
            _ = try await renewedAPI.call("v1/me")
            let afterWake = try await renewedAPI.call("v1/sources")
            assert((afterWake["items"] as? [[String: Any]])?.count == 3)
            print("PASS: 模拟唤醒检查及过期凭证恢复，资料保持不变")
            service.stop()
            try await Task.sleep(nanoseconds: 1_000_000_000)
            print("PASS: 宿主强制退出后的自动恢复及数据保留")
            print("PASS: 真实服务重启及提交响应丢失后的幂等恢复")
        }
        print("PASS: 草稿/文件重启恢复、稳定幂等键、飞书中断状态、原件清理、路径安全与损坏保护")
    }
}

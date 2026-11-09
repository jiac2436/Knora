import Foundation

/// Exercises the client restore flow against bundled API/worker processes and isolated Keychain items.
@main struct LocalRestoreTest {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else { throw failure("需要运行包路径") }
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-restore-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let home = root.appendingPathComponent("synthetic-home")
        let namespace = "local.pka.restore-test.\(UUID().uuidString)"
        let service = LocalService(home: home, credentialService: namespace)
        let originalData = service.backupDirectories.data
        let queue = CaptureQueueStore(root: originalData.appendingPathComponent("capture-queue"))
        let model = CaptureModel(queueStore: nil, localService: service, runtime: runtime)
        var restartedService: LocalService?
        defer {
            service.stopAndWait()
            restartedService?.stopAndWait()
            for name in service.credentialServicesForTesting { TokenStore.delete(service: name) }
            for name in restartedService?.credentialServicesForTesting ?? [] { TokenStore.delete(service: name) }
            try? FileManager.default.removeItem(at: root)
        }
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        model.domainName = "隔离恢复合成测试"
        await model.createDomain()
        model.draft = "恢复包内的合成笔记"
        model.addDraft()
        await model.send()
        guard let noteID = model.items.first?.sourceID else { throw failure(model.message) }
        let originalAPI = try API(address: model.address, token: model.token)
        let revisions = try await originalAPI.call("v1/sources/\(noteID)/revisions")
        let evidenceIDs = (revisions["items"] as? [[String: Any]] ?? []).flatMap {
            ($0["evidence"] as? [[String: Any]] ?? []).compactMap { $0["evidence_id"] as? String }
        }
        try check(!evidenceIDs.isEmpty, "原始笔记没有生成证据")
        let originalFile = root.appendingPathComponent("synthetic.csv")
        let bytes = Data("name,value\nrestore,42\n".utf8)
        try bytes.write(to: originalFile)
        model.addFiles([originalFile])
        await model.send()
        guard let fileID = model.items.last?.sourceID else { throw failure(model.message) }
        let package = root.appendingPathComponent("synthetic.pka-backup")
        await model.exportLocalBackup(to: package)
        try check(model.connected && model.backupMessage.hasPrefix("已导出备份"), model.backupMessage)

        let pointer = home.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox-state/active-library.json")
        let savedPointerBeforeCancel = try? Data(contentsOf: pointer)
        let staleOperation = service.operationToken
        service.stop()
        let currentOperation = service.operationToken
        try expectFailure({ _ = try service.start(runtime: runtime, expectedGeneration: staleOperation) },
                          "取消前派发的启动操作没有被拒绝")
        try check(!service.isRunning, "旧启动操作创建了晚到服务")
        try check(service.stopAndWait(expectedGeneration: staleOperation) == nil,
                  "旧停止操作没有被拒绝")
        try check(service.operationToken == currentOperation, "旧停止操作修改了新一代操作标识")
        try expectFailure({
            _ = try service.prepareRestoredLibrary(runtime: runtime, package: package, expectedGeneration: staleOperation)
        }, "取消前派发的恢复操作没有被拒绝")
        try check(!service.isRunning && service.operationToken == currentOperation,
                  "旧恢复操作启动服务或改变了新一代操作标识")
        try check((try? Data(contentsOf: pointer)) == savedPointerBeforeCancel,
                  "旧恢复操作改变了活动库指针")
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        print("PASS: 已取消操作的旧 token 拒绝晚到启动、停止与恢复，活动指针保持不变")

        model.draft = "备份之后新增的合成笔记"
        model.addDraft()
        await model.send()
        guard let addedID = model.items.last?.sourceID else { throw failure(model.message) }
        model.draft = "原库尚未发送的采集内容"
        model.addDraft()
        model.addFiles([originalFile])
        model.draft = "原库未入库草稿"
        let originalItems = model.items.map(\.id)
        let originalQueue = try queue.load()
        try check(originalQueue?.draft == "原库未入库草稿", "原库草稿没有落盘")
        let pendingFile = model.items.last?.file

        await model.restoreLocalBackup(from: package)
        let restoredData = service.backupDirectories.data
        try check(model.connected && !model.backupBusy && !model.busy, model.backupMessage)
        try check(restoredData.standardizedFileURL != originalData.standardizedFileURL, "恢复覆盖了当前库")
        try check(model.canReturnToPreviousLocalLibrary, "恢复后没有返回原库入口")
        try check(model.draft.isEmpty && model.items.isEmpty, "原库采集队列泄漏到恢复库")
        try check(try queue.load()?.draft == "原库未入库草稿", "恢复改变了原库草稿")
        try check(pendingFile.map { FileManager.default.fileExists(atPath: $0.path) } == true,
                  "恢复清理了原库待发送原件")
        let restoredAPI = try API(address: model.address, token: model.token)
        try await checkRestoredContent(api: restoredAPI, noteID: noteID, fileID: fileID,
                                       evidenceIDs: evidenceIDs, bytes: bytes, downloads: root.appendingPathComponent("downloads"))
        try await checkMissing(api: restoredAPI, id: addedID)
        print("PASS: 真实客户端恢复至独立库，旧证据与原件可读，新增资料和草稿留在原库")

        await model.returnToPreviousLocalLibrary()
        try check(model.connected && !model.backupBusy && !model.busy, model.backupMessage)
        try check(service.backupDirectories.data.standardizedFileURL == originalData.standardizedFileURL,
                  "返回操作没有重新打开原库")
        try check(model.draft == "原库未入库草稿" && model.items.map(\.id) == originalItems,
                  "返回原库未恢复原有草稿与采集队列")
        let returnedAPI = try API(address: model.address, token: model.token)
        _ = try await returnedAPI.call("v1/sources/\(addedID)")
        _ = try await returnedAPI.call("v1/sources/\(noteID)")
        print("PASS: 返回原库后新增资料、持久草稿和待发送原件均保留")

        let damaged = root.appendingPathComponent("damaged.pka-backup")
        try Data("synthetic damaged backup".utf8).write(to: damaged)
        let beforeInvalidItems = model.items.map(\.id)
        await model.restoreLocalBackup(from: damaged)
        try check(model.connected && !model.backupBusy && !model.busy, model.backupMessage)
        try check(service.backupDirectories.data.standardizedFileURL == originalData.standardizedFileURL,
                  "损坏备份改变了活动库")
        try check(model.draft == "原库未入库草稿" && model.items.map(\.id) == beforeInvalidItems,
                  "恢复失败改变了原有草稿")
        let failedAPI = try API(address: model.address, token: model.token)
        _ = try await failedAPI.call("v1/sources/\(addedID)")
        print("PASS: 损坏备份恢复失败后活动库、草稿及原服务保持可用")

        let originalPointer = try Data(contentsOf: pointer)
        try FileManager.default.removeItem(at: pointer)
        try FileManager.default.createDirectory(at: pointer, withIntermediateDirectories: false)
        await model.restoreLocalBackup(from: package)
        try check(model.connected && !model.backupBusy && !model.busy, model.backupMessage)
        try check(model.draft == "原库未入库草稿" && model.items.map(\.id) == originalItems,
                  "活动库指针写失败改变了原有草稿")
        let pointerFailureAPI = try API(address: model.address, token: model.token)
        _ = try await pointerFailureAPI.call("v1/sources/\(addedID)")
        try FileManager.default.removeItem(at: pointer)
        try originalPointer.write(to: pointer, options: .atomic)
        try check(service.backupDirectories.data.standardizedFileURL == originalData.standardizedFileURL,
                  "活动库指针写失败发布了其他活动库")
        print("PASS: 活动库指针写失败后直接重连原库，原资料与草稿保持可用")

        await model.restoreLocalBackup(from: package)
        let persistedData = service.backupDirectories.data
        try check(model.connected && persistedData.standardizedFileURL != originalData.standardizedFileURL,
                  model.backupMessage)
        model.cleanup()
        service.stopAndWait()
        let freshService = LocalService(home: home, credentialService: namespace)
        restartedService = freshService
        try check(freshService.backupDirectories.data.standardizedFileURL == persistedData.standardizedFileURL,
                  "新服务实例没有加载已持久化活动库指针")
        let freshModel = CaptureModel(queueStore: nil, localService: freshService, runtime: runtime)
        await freshModel.startLocalService()
        try check(freshModel.connected, freshModel.authMessage)
        try check(freshModel.draft.isEmpty && freshModel.items.isEmpty, "重新启动读取了原库采集队列")
        let freshAPI = try API(address: freshModel.address, token: freshModel.token)
        _ = try await freshAPI.call("v1/sources/\(noteID)")
        try await checkMissing(api: freshAPI, id: addedID)
        freshModel.cleanup()
        print("PASS: 新客户端实例自动打开恢复库，活动库指针及独立队列经重启验证")
    }

    private static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }

    private static func expectFailure(_ operation: () throws -> Void, _ message: String) throws {
        do { try operation() }
        catch { return }
        throw failure(message)
    }

    private static func checkMissing(api: API, id: String) async throws {
        do {
            _ = try await api.call("v1/sources/\(id)")
        } catch {
            try check((error as NSError).userInfo["HTTPStatus"] as? Int == 404, "资料请求失败但不是预期的 404")
            return
        }
        throw failure("备份之后新增的资料出现在恢复库中")
    }

    private static func checkRestoredContent(api: API, noteID: String, fileID: String,
                                           evidenceIDs: [String], bytes: Data, downloads: URL) async throws {
        let note = try await api.call("v1/sources/\(noteID)")
        try check(KnowledgeDetail(note)?.text.contains("恢复包内的合成笔记") == true, "恢复后的笔记正文丢失")
        for id in evidenceIDs {
            let evidence = try await api.call("v1/evidence/\(id)")
            try check((evidence["text"] as? String)?.contains("恢复包内的合成笔记") == true,
                      "恢复后旧证据不可读")
        }
        let file = try await api.downloadFile("v1/sources/\(fileID)/file", into: downloads, named: "restored.csv")
        try check(try Data(contentsOf: file) == bytes, "恢复原件字节不同")
    }
}

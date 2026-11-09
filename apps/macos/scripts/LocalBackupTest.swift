import Foundation

/// Real bundled API/worker and backup CLI, using only synthetic temporary data.
@main struct LocalBackupTest {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else { throw failure("需要运行包路径") }
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
        let root = FileManager.default.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("pka-backup-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let originalServiceName = "local.pka.backup-test.\(UUID().uuidString)"
        let restoredServiceName = "local.pka.backup-test.\(UUID().uuidString)"
        let service = LocalService(home: root.appendingPathComponent("original"), credentialService: originalServiceName)
        let restored = LocalService(home: root.appendingPathComponent("restored"), credentialService: restoredServiceName)
        defer {
            service.stopAndWait()
            restored.stopAndWait()
            try? FileManager.default.removeItem(at: root)
        }
        try FileManager.default.createDirectory(at: root.appendingPathComponent("original"), withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let queue = CaptureQueueStore(root: service.backupDirectories.data.appendingPathComponent("capture-queue"))
        let model = CaptureModel(queueStore: queue, localService: service, runtime: runtime)
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        model.domainName = "备份恢复合成测试"
        await model.createDomain()
        model.draft = "需要保留的合成笔记"
        model.addDraft()
        await model.send()
        let noteID = try await waitForSource(model, index: 0)
        let originalAPI = try API(address: model.address, token: model.token)
        let versions = try await originalAPI.call("v1/sources/\(noteID)/revisions")
        let evidence = (versions["items"] as? [[String: Any]] ?? []).flatMap {
            ($0["evidence"] as? [[String: Any]] ?? []).compactMap { $0["evidence_id"] as? String }
        }
        try check(!evidence.isEmpty, "合成笔记没有证据")
        let original = root.appendingPathComponent("synthetic.csv")
        let bytes = Data("name,value\nbackup,42\n".utf8)
        try bytes.write(to: original)
        model.addFiles([original])
        await model.send()
        let fileID = try await waitForSource(model, index: model.items.count - 1)
        model.draft = "尚未入库的合成草稿"
        let originalIdentity = try inspect(runtime, data: service.backupDirectories.data)
        let package = root.appendingPathComponent("synthetic.pka-backup")
        await model.exportLocalBackup(to: package)
        try check(model.connected && !model.backupBusy && !model.busy, model.backupMessage)
        try check(model.backupMessage.hasPrefix("已导出备份"), model.backupMessage)
        try check(model.draft == "尚未入库的合成草稿", "导出改变了本地草稿")
        _ = try LocalBackupTool.verify(runtime: runtime, package: package)
        print("PASS: Swift 模型停服导出、自动重连、保留本地草稿，真实 CLI 校验通过")

        let savedPackage = try Data(contentsOf: package)
        await model.exportLocalBackup(to: package)
        try check(model.connected && model.backupMessage.hasPrefix("导出失败"), model.backupMessage)
        try check(try Data(contentsOf: package) == savedPackage, "失败导出覆盖已有恢复包")
        let reopenedAPI = try API(address: model.address, token: model.token)
        _ = try await reopenedAPI.call("v1/sources/\(noteID)")
        print("PASS: 已有输出目标拒绝覆盖，失败后原服务重连且资料仍可读取")

        let restoredHome = root.appendingPathComponent("restored")
        try FileManager.default.createDirectory(at: restoredHome, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        let restoredLibrary = try restored.currentLibrary()
        let credential = try MacDeviceCredentialStore(home: restoredHome, legacy: KeychainCredentialStore()).loadOrCreate(for: restoredLibrary)
        let directories = restored.backupDirectories
        let identity = try LocalBackupTool.restore(runtime: runtime, package: package,
            destination: directories.data, cache: directories.cache, logs: directories.logs, credential: credential)
        try check(identity["owner_id"] as? String == originalIdentity["owner_id"] as? String, "恢复改变了所有者")
        try check(identity["library_id"] as? String == originalIdentity["library_id"] as? String, "恢复改变了知识库 ID")
        try check(identity["device_id"] as? String != originalIdentity["device_id"] as? String, "恢复复用了设备 ID")
        let connection = try await Task.detached { try restored.start(runtime: runtime) }.value
        let restoredAPI = try API(address: connection.address, token: connection.token)
        let source = try await restoredAPI.call("v1/sources/\(noteID)")
        try check(KnowledgeDetail(source)?.text.contains("需要保留的合成笔记") == true, "恢复笔记正文丢失")
        for id in evidence {
            let item = try await restoredAPI.call("v1/evidence/\(id)")
            try check((item["text"] as? String)?.contains("需要保留的合成笔记") == true, "恢复后旧证据不可读")
        }
        let download = try await restoredAPI.downloadFile("v1/sources/\(fileID)/file", into: root.appendingPathComponent("downloads"), named: "restored.csv")
        try check(try Data(contentsOf: download) == bytes, "恢复后原件字节不同")
        try check(!FileManager.default.fileExists(atPath: directories.data.appendingPathComponent("capture-queue").path), "恢复复制了未入库采集队列")
        print("PASS: 新目录恢复后真实服务启动，所有者/库 ID 保留、设备更新、旧证据与原件可读")

        let damaged = root.appendingPathComponent("damaged.pka-backup")
        try Data("synthetic corrupt backup".utf8).write(to: damaged)
        let rejected = root.appendingPathComponent("rejected/data")
        do {
            _ = try LocalBackupTool.restore(runtime: runtime, package: damaged, destination: rejected,
                cache: root.appendingPathComponent("rejected/cache"), logs: root.appendingPathComponent("rejected/logs"), credential: credential)
            throw failure("损坏恢复包意外成功")
        } catch {
            try check(!FileManager.default.fileExists(atPath: rejected.path), "损坏恢复包发布了目标目录")
        }
        print("PASS: 损坏包不发布新库；临时服务和私有凭证测试目录将清理")
        model.cleanup()
    }

    private static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }

    /// Native sends now submit durable background jobs; wait for their archived receipt.
    @MainActor private static func waitForSource(_ model: CaptureModel, index: Int) async throws -> String {
        for _ in 0..<300 {
            await model.refreshKernelCaptures()
            if model.items.indices.contains(index) {
                let item = model.items[index]
                if item.sent, let sourceID = item.sourceID { return sourceID }
                if item.kernelState == "failed" { throw failure(item.status) }
            }
            try await Task.sleep(for: .milliseconds(100))
        }
        throw failure("合成采集任务未在限定时间内完成：" + model.queueMessage)
    }

    private static func inspect(_ runtime: URL, data: URL) throws -> [String: Any] {
        let process = Process()
        let output = Pipe()
        process.executableURL = runtime.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", "-B", "-c", "import json,sqlite3,sys; db=sqlite3.connect('file:'+sys.argv[1]+'/knowledge.db?mode=ro',uri=True); row=db.execute('SELECT owner_id,library_id,device_id FROM local_installations WHERE id=1').fetchone(); print(json.dumps(dict(zip(['owner_id','library_id','device_id'],row)))); db.close()", data.path]
        process.standardOutput = output
        try process.run()
        let bytes = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0,
              let result = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
            throw failure("合成库身份检查失败")
        }
        return result
    }
}

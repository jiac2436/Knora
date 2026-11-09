import Foundation

/// Real bundled API/worker, isolated synthetic library and random test Keychain.
@main struct LocalTrashTest {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else { throw failure("需要运行包路径") }
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-trash-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let namespace = "local.pka.trash-test.\(UUID().uuidString)"
        let service = LocalService(home: root.appendingPathComponent("home"), credentialService: namespace)
        defer {
            service.stopAndWait()
            TokenStore.delete(service: namespace)
            try? FileManager.default.removeItem(at: root)
        }
        let model = CaptureModel(localService: service, runtime: runtime)
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        model.domainName = "回收站合成测试"
        await model.createDomain()
        model.draft = "合成回收站正文"
        model.addDraft()
        await model.send()
        guard let noteID = model.items.first?.sourceID else { throw failure(model.message) }
        await model.openSource(noteID, force: true)
        model.supplementDraft = "合成补充保留历史证据"
        await model.addSupplement()
        let originalAPI = try API(address: model.address, token: model.token)
        let versions = try await originalAPI.call("v1/sources/\(noteID)/revisions")
        let oldEvidence = (versions["items"] as? [[String: Any]] ?? []).flatMap {
            ($0["evidence"] as? [[String: Any]] ?? []).compactMap { $0["evidence_id"] as? String }
        }
        try check(oldEvidence.count >= 2, "合成笔记未生成历史证据")
        try check(await model.deleteSource(noteID), model.browseMessage)
        await model.loadLocalTrash()
        try check(model.trashItems.contains { $0.id == noteID && $0.canRestore }, model.trashMessage)
        try await expectMissing(originalAPI, path: "v1/sources/\(noteID)")
        for id in oldEvidence { try await expectMissing(originalAPI, path: "v1/evidence/\(id)") }
        let hidden = try await originalAPI.call("v1/search", method: "POST", json: ["query": "合成回收站正文"])
        try check((hidden["items"] as? [[String: Any]] ?? []).isEmpty, "删除后仍能检索资料")
        model.cancelLocalServiceStart()
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        try await Task.sleep(nanoseconds: 2_500_000_000)
        await model.loadLocalTrash()
        try check(model.trashItems.contains { $0.id == noteID && $0.canRestore }, "重启 Worker 后回收站资料被清理")
        await model.restoreLocalTrash(noteID)
        try check(!model.trashItems.contains { $0.id == noteID }, model.trashMessage)
        await model.openSource(noteID, force: true)
        try check(model.selectedSource?.text.contains("合成补充保留历史证据") == true, "恢复丢失补充")
        let api = try API(address: model.address, token: model.token)
        for id in oldEvidence {
            let evidence = try await api.call("v1/evidence/\(id)")
            try check((evidence["text"] as? String)?.contains("合成回收站正文") == true, "旧证据未恢复")
        }
        print("PASS: Swift 模型删除后正文/搜索/证据隐藏，Worker 重启不清理，恢复保留补充与旧证据")

        let original = root.appendingPathComponent("synthetic.csv")
        let bytes = Data("name,value\ntrash,42\n".utf8)
        try bytes.write(to: original)
        model.addFiles([original])
        await model.send()
        guard let fileID = model.items.last?.sourceID else { throw failure(model.message) }
        for _ in 0..<60 {
            await model.openSource(fileID, force: true)
            if model.selectedSource?.documentParseStatus == "completed" { break }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        try check(model.selectedSource?.documentParseStatus == "completed", "CSV 尚未解析")
        try check(await model.deleteSource(fileID), model.browseMessage)
        await model.loadLocalTrash()
        try check(model.trashItems.contains { $0.id == fileID && $0.canRestore }, model.trashMessage)
        await model.restoreLocalTrash(fileID)
        let downloaded = try await api.downloadFile("v1/sources/\(fileID)/file", into: root.appendingPathComponent("downloads"), named: "restored.csv")
        try check(try Data(contentsOf: downloaded) == bytes, "恢复原件字节不同")
        print("PASS: 已解析 CSV 经回收站恢复，原件字节保持一致")

        try check(await model.deleteSource(fileID), model.browseMessage)
        await model.loadLocalTrash()
        await model.purgeLocalTrash(fileID)
        try check(model.trashMessage.contains("永久删除"), model.trashMessage)
        var completed = false
        for _ in 0..<60 {
            let listing = try await api.call("v1/trash")
            if !(listing["items"] as? [[String: Any]] ?? []).contains(where: { $0["id"] as? String == fileID }) {
                completed = true
                break
            }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        try check(completed, "后台永久清理未完成")
        try await expectMissing(api, path: "v1/sources/\(fileID)")
        try await expectMissing(api, path: "v1/trash/\(fileID)/restore", method: "POST")
        try inspectPurged(runtime, data: service.backupDirectories.data, id: fileID)
        _ = try await api.call("v1/sources/\(noteID)")
        print("PASS: Swift 模型提交永久清理，真实 Worker 清除原件/版本/证据，无法恢复，其他资料保留")
        model.cleanup()
    }

    private static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }

    private static func expectMissing(_ api: API, path: String, method: String = "GET") async throws {
        var missing = false
        do { _ = try await api.call(path, method: method) }
        catch { missing = (error as NSError).userInfo["HTTPStatus"] as? Int == 404 }
        try check(missing, "已删除内容未返回 404：\(path)")
    }

    private static func inspectPurged(_ runtime: URL, data: URL, id: String) throws {
        let process = Process()
        process.executableURL = runtime.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", "-B", "-c", #"""
import pathlib,sqlite3,sys
root=pathlib.Path(sys.argv[1]); identifier=sys.argv[2]
assert any(p.name.startswith('pka-trash-test-') for p in root.parents)
db=sqlite3.connect(f'file:{root / "knowledge.db"}?mode=ro',uri=True)
assert db.execute('SELECT processing_status,current_revision_id FROM sources WHERE id=?',(identifier,)).fetchone()==('deleted',None)
for table in ('file_assets','source_revisions','personal_context','semantic_index_jobs','evidence_embeddings'):
    assert db.execute(f'SELECT COUNT(*) FROM {table} WHERE source_id=?',(identifier,)).fetchone()[0]==0
assert db.execute('SELECT status FROM deletion_jobs WHERE source_id=? ORDER BY created_at DESC LIMIT 1',(identifier,)).fetchone()[0]=='completed'
assert not any(p.is_file() for p in (root/'assets').iterdir())
assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
assert db.execute('PRAGMA foreign_key_check').fetchall()==[]
db.close()
"""#, data.path, id]
        try process.run()
        process.waitUntilExit()
        try check(process.terminationStatus == 0, "临时库永久清理检查失败")
    }
}

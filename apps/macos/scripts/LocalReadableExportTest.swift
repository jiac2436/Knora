import Foundation

/// Uses only synthetic temporary libraries and a separately named test Keychain item.
@main struct LocalReadableExportTest {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else { throw failure("需要运行包路径") }
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-readable-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let credentialService = "local.pka.readable-test.\(UUID().uuidString)"
        let service = LocalService(home: root.appendingPathComponent("home"), credentialService: credentialService)
        defer {
            service.stopAndWait()
            TokenStore.delete(service: credentialService)
            try? FileManager.default.removeItem(at: root)
        }
        let model = CaptureModel(localService: service, runtime: runtime)
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        model.domainName = "可读导出合成知识域"
        await model.createDomain()
        model.draft = "合成正文：跨客户端仍可阅读"
        model.addDraft()
        await model.send()
        guard let noteID = model.items.first?.sourceID else { throw failure(model.message) }
        await model.openSource(noteID, force: true)
        model.supplementDraft = "合成补充：保留版本证据"
        await model.addSupplement()
        _ = try await API(address: model.address, token: model.token).call(
            "v1/sources/\(noteID)/context", method: "PATCH", json: ["note": "合成个人备注：需要保留"])
        let original = root.appendingPathComponent("synthetic.csv")
        let bytes = Data("name,value\nreadable,42\n".utf8)
        try bytes.write(to: original)
        model.addFiles([original])
        await model.send()
        guard let fileID = model.items.last?.sourceID else { throw failure(model.message) }
        model.draft = "只在当前库保留的合成草稿"
        let output = root.appendingPathComponent("readable.zip")
        await model.exportReadableKnowledge(to: output)
        try check(model.connected && !model.busy && !model.backupBusy, model.backupMessage)
        try check(model.backupMessage.contains("已导出"), model.backupMessage)
        try check(model.draft == "只在当前库保留的合成草稿", "导出改变了当前草稿")
        try inspect(runtime: runtime, package: output, noteID: noteID, fileID: fileID)
        print("PASS: Swift 模型真实停服导出并重连，ZIP正文、历史版本、证据、补充与原件通过校验，草稿保留")

        let saved = try Data(contentsOf: output)
        await model.exportReadableKnowledge(to: output)
        try check(model.connected && model.backupMessage.contains("失败"), model.backupMessage)
        try check(try Data(contentsOf: output) == saved, "失败导出覆盖了已有包")
        let api = try API(address: model.address, token: model.token)
        _ = try await api.call("v1/sources/\(noteID)")
        print("PASS: 拒绝覆盖已有ZIP，失败后重连且原资料可读")
        var refused = false
        do { _ = try LocalBackupTool.verify(runtime: runtime, package: output) }
        catch { refused = true }
        try check(refused, "可读导出包被错误识别为恢复包")
        print("PASS: 可读包不能用于恢复；临时服务、数据及测试钥匙串将清理")
        model.cleanup()
    }

    private static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }

    private static func inspect(runtime: URL, package: URL, noteID: String, fileID: String) throws {
        let process = Process()
        process.executableURL = runtime.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", "-B", "-c", #"""
import hashlib,json,sys,zipfile
with zipfile.ZipFile(sys.argv[1]) as archive:
    manifest=json.loads(archive.read('manifest.json'))
    assert manifest['format']=='pka-readable-export' and manifest['version']==1
    assert set(archive.namelist())=={'manifest.json'} | {r['path'] for r in manifest['files']}
    assert not any(n.endswith('.db') or 'capture-queue' in n for n in archive.namelist())
    for item in manifest['files']:
        content=archive.read(item['path'])
        assert len(content)==item['size_bytes'] and hashlib.sha256(content).hexdigest()==item['sha256']
    metadata=json.loads(archive.read('metadata.json'))
    note=next(s for s in metadata['sources'] if s['id']==sys.argv[2])
    assert '合成正文：跨客户端仍可阅读' in archive.read(note['path']).decode()
    assert len(note['revisions'])==2
    assert all(r['evidence'] for r in note['revisions'])
    assert all('合成正文：跨客户端仍可阅读' in archive.read(r['path']).decode() for r in note['revisions'])
    assert '合成补充：保留版本证据' in note['revisions'][-1]['extracted_text']
    assert '合成补充：保留版本证据' in archive.read(note['path']).decode()
    assert '合成个人备注：需要保留' in json.dumps(note['personal_context'],ensure_ascii=False)
    original=next(s for s in metadata['sources'] if s['id']==sys.argv[3])['file_asset']
    assert archive.read(original['path'])==b'name,value\nreadable,42\n'
    assert '只在当前库保留的合成草稿' not in archive.read('metadata.json').decode()
"""#, package.path, noteID, fileID]
        try process.run()
        process.waitUntilExit()
        try check(process.terminationStatus == 0, "合成可读ZIP内容校验失败")
    }
}

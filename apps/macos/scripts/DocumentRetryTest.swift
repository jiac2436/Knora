import Foundation

/// Runs only against an isolated temporary library and a separately named Keychain item.
@main struct DocumentRetryTest {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else { throw failure("需要运行包路径") }
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-document-retry-test-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let home = root.appendingPathComponent("home")
        let credentialService = "local.pka.document-retry-test.\(UUID().uuidString)"
        let service = LocalService(home: home, credentialService: credentialService)
        defer {
            service.stop()
            TokenStore.delete(service: credentialService)
            try? FileManager.default.removeItem(at: root)
        }
        let data = home.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox")
        let queue = CaptureQueueStore(root: data.appendingPathComponent("capture-queue"))
        let model = CaptureModel(queueStore: queue, localService: service, runtime: runtime)
        await model.startLocalService()
        try check(model.connected, model.authMessage)
        model.domainName = "文档重试合成测试"
        await model.createDomain()
        let broken = Data("synthetic invalid docx".utf8)
        let original = root.appendingPathComponent("synthetic.docx")
        try broken.write(to: original)
        model.addFiles([original])
        await model.send()
        try check(model.items.count == 1 && model.items[0].sent, model.message)
        guard let id = model.items[0].sourceID else { throw failure("没有返回资料 ID") }
        let api = try API(address: model.address, token: model.token)
        try await waitForParse(model, id: id, status: "failed")
        try check(!(model.selectedSource?.documentParseError ?? "").isEmpty, "解析失败没有原因")
        model.supplementDraft = "合成用户补充：必须保留"
        await model.addSupplement()
        try check(model.selectedSource?.text.contains("合成用户补充：必须保留") == true, model.browseMessage)
        let revision = model.selectedSource?.revision
        let versions = try await api.call("v1/sources/\(id)/revisions")
        let oldEvidence = evidenceIDs(versions)
        try check(!oldEvidence.isEmpty, "用户补充没有建立证据")

        // Hold only this fixture's DB write lock to keep the retry response in flight.
        model.draft = "用于切换阅读的合成笔记 B"
        model.addDraft()
        await model.send()
        guard let otherID = model.items.last?.sourceID, otherID != id else {
            throw failure("合成笔记 B 没有保存")
        }
        await model.openSource(id, force: true)
        let blocker = try holdWriteLock(runtime, data: data)
        defer {
            try? blocker.input.fileHandleForWriting.close()
            if blocker.process.isRunning { blocker.process.terminate() }
        }
        var retryFinished = false
        let retry = Task { @MainActor in
            await model.retryDocumentParse()
            retryFinished = true
        }
        for _ in 0..<40 {
            if model.browseBusy { break }
            try await Task.sleep(nanoseconds: 25_000_000)
        }
        try check(model.browseBusy && !retryFinished, "没有观察到进行中的重试请求")
        try await Task.sleep(nanoseconds: 250_000_000)
        try check(!retryFinished, "数据库写锁未阻止重试返回")
        await model.openSource(otherID, force: true)
        try check(model.selectedSource?.id == otherID, "重试期间未能切换资料")
        let switchedMessage = model.browseMessage
        // Model a subsequent browse operation: the older retry must not clear its busy flag.
        model.browseBusy = true
        try blocker.input.fileHandleForWriting.close()
        await retry.value
        blocker.process.waitUntilExit()
        try check(blocker.process.terminationStatus == 0, "临时写锁未正常释放")
        try check(model.selectedSource?.id == otherID, "旧重试响应覆盖了新选中的资料")
        try check(model.browseMessage == switchedMessage, "旧重试响应覆盖了新资料提示")
        try check(model.browseBusy, "旧重试响应清除了后续操作的忙碌状态")
        model.browseBusy = false
        print("PASS: SQLite 写锁延迟真实重试返回，切换资料后旧响应不覆盖选择、提示及后续忙碌状态")

        // The unchanged corrupt bytes fail again without replacing original or supplement.
        try await waitForParse(model, id: id, status: "failed")
        try check(model.selectedSource?.revision == revision, "失败重试改变了已有版本")
        try check(model.selectedSource?.text.contains("合成用户补充：必须保留") == true, "失败重试丢失补充")
        let download = try await api.downloadFile("v1/sources/\(id)/file", into: root.appendingPathComponent("downloads"), named: "failed.docx")
        try check(try Data(contentsOf: download) == broken, "失败重试修改了原件")
        print("PASS: Swift 模型提交真实解析重试，失败后原件、用户补充和旧版本保留")

        // Repair only this synthetic fixture; production retry itself never replaces an original.
        let result = try python(runtime, arguments: [data.path, id], script: #"""
import io, json, pathlib, sqlite3, sys, zipfile
root = pathlib.Path(sys.argv[1])
assert root.parent.parent.parent.parent.name.startswith('pka-document-retry-test-')
db = sqlite3.connect(f'file:{root / "knowledge.db"}?mode=ro', uri=True)
asset = db.execute('SELECT id FROM file_assets WHERE source_id=?', (sys.argv[2],)).fetchone()[0]
attempts = db.execute('SELECT attempts FROM document_jobs WHERE source_id=?', (sys.argv[2],)).fetchone()[0]
db.close()
payload = io.BytesIO()
with zipfile.ZipFile(payload, 'w', zipfile.ZIP_DEFLATED) as package:
    package.writestr('[Content_Types].xml', '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
    package.writestr('_rels/.rels', '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
    package.writestr('word/document.xml', '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Synthetic repaired document</w:t></w:r></w:p><w:sectPr/></w:body></w:document>')
target = root / 'assets' / asset
assert target.resolve().parent == (root / 'assets').resolve()
target.write_bytes(payload.getvalue())
print(json.dumps({'attempts': attempts}))
"""#)
        try check(result["attempts"] as? Int == 2, "首次重试没有实际增加解析次数")
        await model.retryDocumentParse()
        try check(model.browseMessage.contains("已提交重新解析"), model.browseMessage)
        try await waitForParse(model, id: id, status: "completed")
        try check(model.selectedSource?.text.contains("Synthetic repaired document") == true, "重试未产生解析正文")
        try check(model.selectedSource?.text.contains("合成用户补充：必须保留") == true, "成功重试丢失用户补充")
        try check((model.selectedSource?.revision ?? 0) > (revision ?? 0), "成功重试没有新版本")
        let after = try await api.call("v1/sources/\(id)/revisions")
        try check(oldEvidence.isSubset(of: evidenceIDs(after)), "成功重试丢失旧证据")
        for evidence in oldEvidence {
            let detail = try await api.call("v1/evidence/\(evidence)")
            try check((detail["text"] as? String)?.contains("合成用户补充：必须保留") == true, "旧证据不可读")
        }
        let attempts = try python(runtime, arguments: [data.path, id], script: #"""
import json, pathlib, sqlite3, sys
db = sqlite3.connect(f'file:{pathlib.Path(sys.argv[1]) / "knowledge.db"}?mode=ro', uri=True)
print(json.dumps({'attempts': db.execute('SELECT attempts FROM document_jobs WHERE source_id=?', (sys.argv[2],)).fetchone()[0]}))
db.close()
"""#)
        try check(attempts["attempts"] as? Int == 3, "第二次重试没有实际执行")
        print("PASS: 修复临时合成原件后经 Swift 模型重试成功，正文、用户补充及旧证据全部可读")
        model.cleanup()
        service.stop()
        try await Task.sleep(nanoseconds: 1_500_000_000)
    }

    @MainActor private static func waitForParse(_ model: CaptureModel, id: String, status: String) async throws {
        for _ in 0..<180 {
            await model.openSource(id, force: true)
            if model.selectedSource?.documentParseStatus == status { return }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        throw failure("等待解析状态 \(status) 超时：\(model.selectedSource?.documentParseStatus ?? model.browseMessage)")
    }

    private static func evidenceIDs(_ response: [String: Any]) -> Set<String> {
        Set((response["items"] as? [[String: Any]] ?? []).flatMap {
            ($0["evidence"] as? [[String: Any]] ?? []).compactMap { $0["evidence_id"] as? String }
        })
    }

    private static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }

    private static func holdWriteLock(_ runtime: URL, data: URL) throws -> (process: Process, input: Pipe) {
        let process = Process()
        process.executableURL = runtime.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", "-B", "-c", #"""
import pathlib, sqlite3, sys
root = pathlib.Path(sys.argv[1])
assert root.parent.parent.parent.parent.name.startswith('pka-document-retry-test-')
db = sqlite3.connect(f'file:{root / "knowledge.db"}?mode=rw', uri=True, timeout=10)
db.execute('BEGIN IMMEDIATE')
print('ready', flush=True)
try:
    sys.stdin.buffer.read(1)
finally:
    db.rollback()
    db.close()
"""#, data.path]
        let output = Pipe()
        let input = Pipe()
        process.standardOutput = output
        process.standardInput = input
        try process.run()
        guard output.fileHandleForReading.readData(ofLength: 6) == Data("ready\n".utf8) else {
            try? input.fileHandleForWriting.close()
            if process.isRunning { process.terminate() }
            throw failure("临时测试库写锁未就绪")
        }
        return (process, input)
    }

    private static func python(_ runtime: URL, arguments: [String], script: String) throws -> [String: Any] {
        let process = Process()
        process.executableURL = runtime.appendingPathComponent("python/bin/python3")
        process.arguments = ["-I", "-B", "-c", script] + arguments
        let output = Pipe()
        process.standardOutput = output
        try process.run()
        let bytes = output.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        guard process.terminationStatus == 0,
              let result = try JSONSerialization.jsonObject(with: bytes) as? [String: Any] else {
            throw failure("临时测试库检查失败")
        }
        return result
    }
}

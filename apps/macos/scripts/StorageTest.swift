import Foundation
import SQLite3
import Darwin

@main struct StorageTest {
    nonisolated static func waitForGate(_ gate: DispatchSemaphore) { gate.wait() }
    static func main() async throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("pka-storage-test-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: root) }
        let home = root.appendingPathComponent("home")
        let app = root.appendingPathComponent("知识收集箱.app")
        let locations = StorageScanLocations(home: home, application: app)
        let legacy = MacPaths().library(home: home, credentialService: "", id: nil)
        let restored = MacPaths().library(home: home, credentialService: "", id: UUID().uuidString.lowercased())
        var expected: Int64 = 0
        func file(_ url: URL, _ size: Int, counted: Bool = true) throws {
            try manager.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data(repeating: 65, count: size).write(to: url)
            if counted { expected += Int64(size) }
        }
        try file(app.appendingPathComponent("Contents/MacOS/Binary"), 1000)
        try file(app.appendingPathComponent("Contents/Resources/runtime/model/weights.onnx"), 2000)
        try file(app.appendingPathComponent("Contents/Resources/runtime/ocr-models/model.bin"), 2100)
        try file(legacy.data.appendingPathComponent("assets/original"), 3000)
        let image = legacy.data.appendingPathComponent("assets/images/picture")
        try file(image, 4000)
        try manager.linkItem(at: image, to: image.deletingLastPathComponent().appendingPathComponent("hardlink"))
        try file(legacy.data.appendingPathComponent("capture-queue/files/pending"), 5000)
        try file(legacy.data.appendingPathComponent("staging/pending"), 100)
        try file(legacy.data.appendingPathComponent("backups/knowledge.db"), 6000)
        try file(legacy.cache.appendingPathComponent("models/weights"), 7000)
        try file(legacy.logs.appendingPathComponent("local-service.log"), 8000)
        try file(MacPaths().configuration(home: home).appendingPathComponent("settings.json"), 9000)
        let extensions = MacPaths().configuration(home: home).appendingPathComponent("extensions")
        try file(extensions.appendingPathComponent("packages/semantic/hash/packages/engine.so"), 1200)
        try file(extensions.appendingPathComponent("packages/semantic/hash/model/weights.onnx"), 1300)
        try file(extensions.appendingPathComponent("staging/install-test/partial"), 1400)
        try file(extensions.appendingPathComponent("download-test.pkaext"), 1500)
        try file(restored.data.appendingPathComponent("assets/another-original"), 3100)
        let external = root.appendingPathComponent("external/huge-file")
        try file(external, 1_000_000, counted: false)
        let link = legacy.data.appendingPathComponent("assets/outside-link")
        try manager.createSymbolicLink(at: link, withDestinationURL: external.deletingLastPathComponent())
        expected += Int64(external.deletingLastPathComponent().path.utf8.count)
        // An unrelated App/archive and externally exported backup must not be counted.
        try file(root.appendingPathComponent("archive/old.app/Binary"), 900_000, counted: false)
        try file(home.appendingPathComponent("Desktop/exported.pka-backup"), 800_000, counted: false)
        let database = legacy.data.appendingPathComponent("knowledge.db")
        var db: OpaquePointer?
        precondition(sqlite3_open(database.path, &db) == SQLITE_OK)
        defer { sqlite3_close(db) }
        let sql = """
            PRAGMA journal_mode=WAL; PRAGMA wal_autocheckpoint=0;
            CREATE TABLE source_revisions (body BLOB);
            CREATE TABLE source_images (info BLOB);
            CREATE TABLE ai_artifacts (analysis BLOB);
            CREATE TABLE evidence_embeddings (vector BLOB);
            CREATE TABLE other_state (value BLOB);
            CREATE INDEX custom_original_index ON source_revisions(body);
            INSERT INTO source_revisions VALUES (zeroblob(9000));
            INSERT INTO source_images VALUES (zeroblob(10000));
            INSERT INTO ai_artifacts VALUES (zeroblob(11000));
            INSERT INTO evidence_embeddings VALUES (zeroblob(12000));
            INSERT INTO other_state VALUES (zeroblob(13000));
            """
        precondition(sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK)
        let mainBefore = try Data(contentsOf: database)
        let walBefore = try Data(contentsOf: URL(fileURLWithPath: database.path + "-wal"))
        for suffix in ["", "-wal", "-shm"] {
            expected += (try manager.attributesOfItem(atPath: database.path + suffix)[.size] as! NSNumber).int64Value
        }
        let snapshot = try await MacStorageScanner.snapshot(locations)
        precondition(snapshot.totalBytes == expected, "总计必须等于所有受管理文件，不能重复或遗漏：\(snapshot.totalBytes) / \(expected)")
        precondition(snapshot.unreadableItems == 0 && snapshot.unclassifiedDatabases == 0)
        precondition(snapshot[.application] == 2200 && snapshot[.models] == 12400)
        precondition(snapshot[.drafts] == 5100 && snapshot[.backups] == 6000 && snapshot[.cache] == 10900)
        precondition(snapshot[.documents] > 6100 && snapshot[.images] > 4000)
        precondition(snapshot[.analysis] > 0 && snapshot[.indexes] > 0 && snapshot[.other] >= 9000)
        let mainAfter = try Data(contentsOf: database)
        let walAfter = try Data(contentsOf: URL(fileURLWithPath: database.path + "-wal"))
        precondition(mainBefore == mainAfter && walBefore == walAfter, "统计不得更改数据库或 WAL")
        let malformed = restored.data.appendingPathComponent("knowledge.db")
        try file(malformed, 32)
        let degraded = try MacStorageScanner.scan(locations)
        precondition(degraded.totalBytes == expected && degraded.unclassifiedDatabases == 1)
        let empty = try MacStorageScanner.scan(StorageScanLocations(roots: []))
        precondition(empty.totalBytes == 0 && empty.unreadableItems == 0)
        let gate = DispatchSemaphore(value: 0)
        let cancelled = Task.detached { waitForGate(gate); return try MacStorageScanner.scan(locations) }
        cancelled.cancel(); gate.signal()
        do { _ = try await cancelled.value; preconditionFailure("取消任务不应继续扫描") }
        catch is CancellationError { }
        precondition(StorageRules.databaseCategory(name: "sqlite_autoindex_ai_artifacts_1", type: "index") == .indexes)
        print("PASS: 文件/数据库合计一致、WAL 有效页分类、原件/图片/分析/索引/模型、历史库、备份、草稿、硬链接去重、不遍历外部符号链接、只读及损坏数据库回退、空状态、取消")
    }
}

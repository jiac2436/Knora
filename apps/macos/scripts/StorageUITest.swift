import AppKit
import SwiftUI
import SQLite3

@main struct StorageUITest: App {
    @StateObject private var model: CaptureModel
    init() {
        precondition(Bundle.main.bundleIdentifier == "local.pka.storage-ui-test")
        let manager = FileManager.default
        let home = manager.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("pka-storage-ui-\(UUID().uuidString)")
        let library = MacPaths().library(home: home, credentialService: "fixture.storage", id: nil)
        func file(_ url: URL, bytes: Int) {
            try! manager.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try! Data(repeating: 65, count: bytes).write(to: url)
        }
        file(library.data.appendingPathComponent("assets/document"), bytes: 25_000_000)
        file(library.data.appendingPathComponent("assets/images/image"), bytes: 16_000_000)
        file(library.cache.appendingPathComponent("models/weights"), bytes: 10_000_000)
        file(library.cache.appendingPathComponent("preview"), bytes: 3_000_000)
        file(library.logs.appendingPathComponent("local-service.log"), bytes: 10_000)
        file(library.data.appendingPathComponent("capture-queue/queue.json"), bytes: 12_000)
        file(library.data.appendingPathComponent("backups/backup"), bytes: 7_000_000)
        var db: OpaquePointer?
        precondition(sqlite3_open(library.data.appendingPathComponent("knowledge.db").path, &db) == SQLITE_OK)
        precondition(sqlite3_exec(db, "CREATE TABLE ai_artifacts(data BLOB); INSERT INTO ai_artifacts VALUES(zeroblob(3000000)); CREATE TABLE evidence_embeddings(data BLOB); INSERT INTO evidence_embeddings VALUES(zeroblob(2000000));", nil, nil, nil) == SQLITE_OK)
        sqlite3_close(db)
        let model = CaptureModel(localService: LocalService(home: home, credentialService: "fixture.storage"), runtime: nil)
        _model = StateObject(wrappedValue: model)
    }
    var body: some Scene {
        Window("存储设置 · 隔离验收", id: "storage-test") {
            SettingsView(model: model).preferredColorScheme(.dark).tint(InboxStyle.accent)
        }
    }
}

import Foundation
import Darwin
import SQLite3

/// Read-only, cancellable work kept off the main actor. Contents and credentials
/// are never read; SQLite exposes page sizes rather than user rows.
enum MacStorageScanner {
    static func snapshot(_ locations: StorageScanLocations) async throws -> StorageSnapshot {
        let worker = Task.detached(priority: .utility) { try scan(locations) }
        return try await withTaskCancellationHandler {
            try await worker.value
        } onCancel: { worker.cancel() }
    }

    static func scan(_ locations: StorageScanLocations) throws -> StorageSnapshot {
        let scan = Scan()
        for root in locations.roots { try scan.walk(root) }
        for database in scan.databases { try scan.classify(database) }
        return StorageSnapshot(bytes: scan.bytes, unreadableItems: scan.unreadable,
                               unclassifiedDatabases: scan.unclassified, scannedAt: Date())
    }

    private final class Scan {
        var bytes: [StorageCategory: Int64] = [:]
        var unreadable = 0
        var unclassified = 0
        var databases: [URL] = []
        var fileLengths: [String: Int64] = [:]
        private var visited: Set<FileIdentity> = []
        private struct FileIdentity: Hashable { let device: Int32; let inode: UInt64 }

        func walk(_ root: StorageScanRoot) throws {
            try Task.checkCancellation()
            // Refuse linked ancestors too; an app-managed directory must not
            // accidentally turn this scan into a traversal of another volume.
            var ancestor = root.url.deletingLastPathComponent()
            while ancestor.path != "/" {
                var info = stat()
                if lstat(ancestor.path, &info) == 0 && info.st_mode & S_IFMT == S_IFLNK &&
                    !["/var", "/tmp", "/etc"].contains(ancestor.path) {
                    unreadable += 1; return
                }
                ancestor.deleteLastPathComponent()
            }
            var info = stat()
            guard lstat(root.url.path, &info) == 0 else {
                if errno != ENOENT { unreadable += 1 }
                return
            }
            if info.st_mode & S_IFMT != S_IFDIR {
                try measure(root.url, path: [], kind: root.kind)
                return
            }
            guard let physical = realpath(root.url.path, nil) else { unreadable += 1; return }
            let directory = URL(fileURLWithPath: String(cString: physical), isDirectory: true)
            free(physical)
            guard let iterator = FileManager.default.enumerator(at: directory, includingPropertiesForKeys: nil,
                options: [], errorHandler: { _, _ in self.unreadable += 1; return true }) else {
                unreadable += 1; return
            }
            for case let url as URL in iterator {
                try Task.checkCancellation()
                let path = Array(url.pathComponents.dropFirst(directory.pathComponents.count))
                try measure(url, path: path, kind: root.kind)
            }
        }

        private func measure(_ url: URL, path: [String], kind: StorageScanRoot.Kind) throws {
            try Task.checkCancellation()
            var info = stat()
            guard lstat(url.path, &info) == 0 else { unreadable += 1; return }
            let type = info.st_mode & S_IFMT
            guard type == S_IFREG || type == S_IFLNK else { return }
            let identity = FileIdentity(device: info.st_dev, inode: info.st_ino)
            guard visited.insert(identity).inserted else { return }
            let length = max(0, Int64(info.st_size))
            bytes[StorageRules.category(path: path, kind: kind), default: 0] += length
            var databasePath = path
            if databasePath.last == "knowledge.db-wal" { databasePath[databasePath.count - 1] = "knowledge.db" }
            if type == S_IFREG && StorageRules.isDatabase(path: databasePath, kind: kind) {
                fileLengths[url.path] = length
                if path.last == "knowledge.db" { databases.append(url) }
            }
            // Directory enumeration does not descend into symbolic links.
        }

        func classify(_ database: URL) throws {
            try Task.checkCancellation()
            do {
                let pages = try DatabasePages.read(database)
                // Current valid pages can reside in either the main file or WAL.
                // All remaining bytes (free pages, old WAL frames, SHM) stay in other.
                let container = [database.path, database.path + "-wal"].reduce(Int64(0)) {
                    $0 + fileLengths[$1, default: 0]
                }
                guard pages.values.reduce(0, +) <= container else { unclassified += 1; return }
                for (category, size) in pages where category != .other {
                    bytes[category, default: 0] += size
                    bytes[.other, default: 0] -= size
                }
            } catch is CancellationError { throw CancellationError() }
            catch { unclassified += 1 }
        }
    }

    private enum DatabasePages {
        static func read(_ url: URL) throws -> [StorageCategory: Int64] {
            var db: OpaquePointer?
            guard sqlite3_open_v2(url.path, &db, SQLITE_OPEN_READONLY | SQLITE_OPEN_NOMUTEX, nil) == SQLITE_OK else {
                sqlite3_close(db); throw ScanError.database
            }
            defer { sqlite3_close(db) }
            sqlite3_busy_timeout(db, 100)
            sqlite3_progress_handler(db, 2000, { _ in Task.isCancelled ? 1 : 0 }, nil)
            guard sqlite3_exec(db, "PRAGMA query_only=ON; BEGIN", nil, nil, nil) == SQLITE_OK else { throw ScanError.database }
            defer { sqlite3_exec(db, "ROLLBACK", nil, nil, nil) }
            let sql = """
                SELECT d.name, COALESCE(s.type, 'table'), SUM(d.pgsize)
                FROM dbstat AS d LEFT JOIN sqlite_schema AS s ON s.name = d.name
                GROUP BY d.name, s.type
                """
            var statement: OpaquePointer?
            guard sqlite3_prepare_v2(db, sql, -1, &statement, nil) == SQLITE_OK else { throw ScanError.database }
            defer { sqlite3_finalize(statement) }
            var bytes: [StorageCategory: Int64] = [:]
            while true {
                try Task.checkCancellation()
                let result = sqlite3_step(statement)
                if result == SQLITE_DONE { return bytes }
                guard result == SQLITE_ROW, let name = sqlite3_column_text(statement, 0),
                      let type = sqlite3_column_text(statement, 1) else { throw ScanError.database }
                let category = StorageRules.databaseCategory(name: String(cString: name), type: String(cString: type))
                bytes[category, default: 0] += sqlite3_column_int64(statement, 2)
            }
        }
    }
    private enum ScanError: Error { case database }
}

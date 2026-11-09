import Foundation
import Darwin

@main struct LocalLibraryStoreTest {
    static func main() throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent("pka-library-store-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: root) }
        let home = root.appendingPathComponent("home")
        let store = LocalLibraryStore(home: home, credentialService: "synthetic.credential")
        let original = try store.current()
        try check(original.id == nil && original.credentialService == "synthetic.credential", "缺省凭证改变")
        try check(original.data.path == home.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox").path, "缺省路径改变")
        let library = try store.allocate()
        try check(!manager.fileExists(atPath: library.data.path), "allocate 创建了恢复目标")
        try manager.createDirectory(at: library.data, withIntermediateDirectories: false)
        try Data("synthetic database".utf8).write(to: library.data.appendingPathComponent("knowledge.db"))
        try store.activate(library)
        try check(try LocalLibraryStore(home: home, credentialService: "synthetic.credential").current() == library, "活动库没有持久化")
        let pointer = home.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox-state/active-library.json")
        let saved = try Data(contentsOf: pointer)
        let state = pointer.deletingLastPathComponent()
        try check((try manager.attributesOfItem(atPath: pointer.path)[.posixPermissions] as? NSNumber)?.intValue == 0o600, "指针权限不是 0600")
        try check((try manager.attributesOfItem(atPath: state.path)[.posixPermissions] as? NSNumber)?.intValue == 0o700, "状态目录权限不是 0700")
        print("PASS: 缺省路径、分配不创建目标、活动库持久化、私有权限")

        for value in ["broken", "{\"version\":2,\"id\":\"\(library.id!)\"}", "{\"version\":1,\"id\":\"../escape\"}",
                      "{\"version\":1}", "{\"version\":1,\"id\":null,\"path\":\"/outside\"}",
                      "{\"version\":1,\"id\":\"\(UUID().uuidString)\"}"] {
            try Data(value.utf8).write(to: pointer)
            try rejected { _ = try store.current() }
        }
        try saved.write(to: pointer)
        let outside = LocalLibraryDescriptor(id: library.id, data: root.appendingPathComponent("outside"),
            cache: library.cache, logs: library.logs, credentialService: library.credentialService)
        try rejected { try store.activate(outside) }
        let wrongCredential = LocalLibraryDescriptor(id: library.id, data: library.data,
            cache: library.cache, logs: library.logs, credentialService: "wrong")
        try rejected { try store.activate(wrongCredential) }
        let absent = try store.allocate()
        try rejected { try store.activate(absent) }
        try check(try Data(contentsOf: pointer) == saved, "非法激活改变了旧指针")
        print("PASS: 损坏及未知版本拒绝，非法路径及凭证 namespace 拒绝")

        try manager.removeItem(at: pointer)
        try manager.createSymbolicLink(at: pointer, withDestinationURL: library.data.appendingPathComponent("knowledge.db"))
        try rejected { _ = try store.current() }
        try rejected { try store.activate(library) }
        try manager.removeItem(at: pointer)
        try saved.write(to: pointer)
        let symbolicHome = root.appendingPathComponent("symbolic-home")
        try manager.createDirectory(at: symbolicHome, withIntermediateDirectories: true)
        try manager.createSymbolicLink(at: symbolicHome.appendingPathComponent("Library"), withDestinationURL: home.appendingPathComponent("Library"))
        let symbolicStore = LocalLibraryStore(home: symbolicHome, credentialService: "synthetic.credential")
        try rejected { _ = try symbolicStore.current() }
        try rejected { _ = try symbolicStore.allocate() }
        let db = library.data.appendingPathComponent("knowledge.db")
        try manager.removeItem(at: db)
        try manager.createSymbolicLink(at: db, withDestinationURL: pointer)
        try rejected { try store.activate(library) }
        try manager.removeItem(at: db)
        try Data("synthetic database".utf8).write(to: db)
        print("PASS: 指针、管理目录及数据库符号链接拒绝")

        let next = try store.allocate()
        try manager.createDirectory(at: next.data, withIntermediateDirectories: false)
        try Data("another synthetic database".utf8).write(to: next.data.appendingPathComponent("knowledge.db"))
        try manager.setAttributes([.posixPermissions: 0o500], ofItemAtPath: state.path)
        defer { try? manager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: state.path) }
        try rejected { try store.activate(next) }
        try check(try Data(contentsOf: pointer) == saved, "原子写失败改变了旧指针")
        try check(try store.current() == library, "写失败后活动库改变")
        try manager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: state.path)
        try store.activate(next)
        try check(try store.current() == next, "第二次切换失败")
        try check(try Data(contentsOf: db) == Data("synthetic database".utf8), "切换改写了旧库")
        try manager.createDirectory(at: original.data, withIntermediateDirectories: true)
        try Data("default synthetic database".utf8).write(to: original.data.appendingPathComponent("knowledge.db"))
        try store.activate(original)
        try check(try store.current() == original, "默认库回切失败")
        let object = try JSONSerialization.jsonObject(with: Data(contentsOf: pointer)) as? [String: Any]
        try check(object?["id"] is NSNull, "默认库指针缺少显式空 ID")
        print("PASS: 原子写入失败保留旧指针，成功切换保留旧库")
    }

    private static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw NSError(domain: "LocalLibraryStoreTest", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    }
    private static func rejected(_ action: () throws -> Void) throws {
        do { try action() }
        catch { return }
        try check(false, "预期拒绝但操作成功")
    }
}

import Foundation
import CryptoKit

enum CaptureKind: String, Codable {
    case file, link, note

    init(serverValue: String) {
        switch serverValue {
        case "file": self = .file
        case "link", "url", "browser_page", "feishu_document": self = .link
        default: self = .note
        }
    }

    var label: String {
        switch self {
        case .file: "文件"
        case .link: "链接"
        case .note: "笔记"
        }
    }

    var symbol: String {
        switch self {
        case .file: "doc"
        case .link: "link"
        case .note: "note.text"
        }
    }
}

struct CaptureItem: Identifiable, Codable {
    var id = UUID().uuidString
    let kind: CaptureKind
    var title: String
    var text: String?
    var file: URL?
    var fileDigest: String?
    var status = "待发送"
    var sent = false
    var domainID: String?
    var projectID: String?
    var destination: String?
    var sourceID: String?
    var feishuDocument: FeishuDocument?
    var uploadedImageIDs: Set<String> = []
    var kernelJobID: String?
    var kernelState: String?
    var kernelReceipt: String?
}

struct CaptureQueueSnapshot: Codable {
    var version = 1
    var draft: String
    var items: [CaptureItem]
    var domainID: String
    var projectID: String
}

/// Private queue state is committed before requests; files outlive the app process.
struct CaptureQueueStore {
    let root: URL
    var writeSnapshot: (Data, URL) throws -> Void = { data, url in
        try data.write(to: url, options: [.atomic, .completeFileProtectionUnlessOpen])
    }
    var manifest: URL { root.appendingPathComponent("queue.json") }
    var files: URL { root.appendingPathComponent("files") }

    static var local: CaptureQueueStore {
        CaptureQueueStore(root: MacPaths().library(home: FileManager.default.homeDirectoryForCurrentUser,
            credentialService: LocalService.credentialService, id: nil).data.appendingPathComponent("capture-queue"))
    }

    func checkStorage() throws {
        let expected = root.resolvingSymlinksInPath().appendingPathComponent("files").path
        guard files.resolvingSymlinksInPath().path == expected else { throw failure("采集原件目录不能是符号链接") }
        if FileManager.default.fileExists(atPath: manifest.path),
           try manifest.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink == true {
            throw failure("采集队列文件不能是符号链接")
        }
    }

    func prepare() throws {
        try checkStorage()
        try FileManager.default.createDirectory(at: files, withIntermediateDirectories: true,
                                                 attributes: [.posixPermissions: 0o700])
    }

    func owns(_ url: URL) -> Bool {
        url.isFileURL && url.resolvingSymlinksInPath().path.hasPrefix(files.resolvingSymlinksInPath().path + "/")
    }

    func load() throws -> CaptureQueueSnapshot? {
        try checkStorage()
        guard FileManager.default.fileExists(atPath: manifest.path) else { return nil }
        let size = try manifest.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? Int.max
        guard size <= 32 * 1024 * 1024 else { throw failure("本地采集队列过大，已保留原文件。") }
        let data = try Data(contentsOf: manifest)
        guard data.count <= 32 * 1024 * 1024 else { throw failure("本地采集队列过大，已保留原文件。") }
        var snapshot = try JSONDecoder().decode(CaptureQueueSnapshot.self, from: data)
        guard snapshot.version == 1 else { throw failure("采集队列版本不受支持，已保留原文件。") }
        guard snapshot.items.allSatisfy({ UUID(uuidString: $0.id) != nil }),
              Set(snapshot.items.map(\.id)).count == snapshot.items.count else { throw failure("采集队列编号重复，已保留原文件。") }
        for index in snapshot.items.indices {
            let item = snapshot.items[index]
            try validatePaths(item)
            if !item.sent && item.kernelJobID == nil { snapshot.items[index].status = "已恢复 · 待发送或重试" }
            if let file = item.file, !FileManager.default.fileExists(atPath: file.path) {
                snapshot.items[index].status = "暂存文件缺失，请移除后重新添加"
            }
        }
        return snapshot
    }

    func validatePaths(_ item: CaptureItem) throws {
        let urls = [item.file, item.feishuDocument?.imageDirectory].compactMap { $0 }
            + (item.feishuDocument?.images.compactMap(\.file) ?? [])
        guard urls.allSatisfy(owns) else { throw failure("采集队列包含不安全的原件路径，已保留原文件。") }
    }

    func digest(_ file: URL) throws -> String {
        guard owns(file) else { throw failure("暂存文件路径无效") }
        let handle = try FileHandle(forReadingFrom: file)
        defer { try? handle.close() }
        var hash = SHA256()
        while let block = try handle.read(upToCount: 1024 * 1024), !block.isEmpty { hash.update(data: block) }
        return hash.finalize().map { String(format: "%02x", $0) }.joined()
    }

    func stage(_ original: URL, name: String = UUID().uuidString) throws -> URL {
        try prepare()
        guard UUID(uuidString: name) != nil else { throw failure("暂存文件编号无效") }
        let destination = files.appendingPathComponent(name)
        try FileManager.default.copyItem(at: original, to: destination)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: destination.path)
        return destination
    }

    func archive(_ document: FeishuDocument, itemID: String) throws -> FeishuDocument {
        guard UUID(uuidString: itemID) != nil else { throw failure("采集编号无效") }
        var result = document
        for index in result.images.indices {
            if let file = result.images[index].file, !owns(file) {
                result.images[index].file = try stage(file)
            }
        }
        // Each image is now separately owned; never persist a temporary directory.
        result.imageDirectory = nil
        return result
    }

    func save(_ snapshot: CaptureQueueSnapshot) throws {
        try prepare()
        for item in snapshot.items { try validatePaths(item) }
        let data = try JSONEncoder().encode(snapshot)
        guard data.count <= 32 * 1024 * 1024 else { throw failure("本地采集队列超过保存上限") }
        try writeSnapshot(data, manifest)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: manifest.path)
    }

    func removeAssets(_ item: CaptureItem) {
        let urls = [item.file].compactMap { $0 } + (item.feishuDocument?.images.compactMap(\.file) ?? [])
        for url in urls where owns(url) { try? FileManager.default.removeItem(at: url) }
    }
}

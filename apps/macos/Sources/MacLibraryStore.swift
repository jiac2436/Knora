import Foundation
import Darwin

/// The pointer contains only a managed library ID; paths and credentials are derived locally.
struct MacLibraryStore: LibraryStoring {
    let home: URL
    let credentialService: String
    private var manager: FileManager { FileManager.default }

    private var state: URL {
        MacPaths().configuration(home: home)
    }
    private var pointer: URL { state.appendingPathComponent("active-library.json") }

    func current() throws -> LocalLibraryDescriptor {
        try checkParents(pointer)
        guard let kind = try fileKind(pointer) else {
            let result = descriptor(nil)
            try checkDescriptorPaths(result)
            return result
        }
        guard kind == S_IFREG else { throw failure("活动库指针必须是普通文件。") }
        let attributes = try manager.attributesOfItem(atPath: pointer.path)
        guard (attributes[.size] as? NSNumber)?.intValue ?? Int.max <= 4096 else {
            throw failure("活动库指针大小异常。")
        }
        let bytes = try Data(contentsOf: pointer)
        guard let object = try JSONSerialization.jsonObject(with: bytes) as? [String: Any],
              Set(object.keys) == Set(["version", "id"]) else {
            throw failure("活动库指针格式无效。")
        }
        let value = try JSONDecoder().decode(Pointer.self, from: bytes)
        guard value.version == 1 else { throw failure("活动库指针版本不受支持。") }
        let id = try value.id.map(canonicalID)
        let result = descriptor(id)
        try checkDescriptorPaths(result)
        if id != nil, try fileKind(result.data.appendingPathComponent("knowledge.db")) != S_IFREG {
            throw failure("活动知识库缺少有效数据库，请恢复原库后重试。")
        }
        return result
    }

    func allocate() throws -> LocalLibraryDescriptor {
        let result = descriptor(UUID().uuidString.lowercased())
        for directory in [result.data, result.cache, result.logs] {
            try createParents(directory.deletingLastPathComponent())
            guard try fileKind(directory) == nil else { throw failure("新库目标已存在，请重试。") }
        }
        return result
    }

    func activate(_ library: LocalLibraryDescriptor) throws {
        let id = try library.id.map(canonicalID)
        guard library == descriptor(id) else { throw failure("知识库不属于当前客户端的管理目录。") }
        try checkDescriptorPaths(library)
        let database = library.data.appendingPathComponent("knowledge.db")
        guard try fileKind(database) == S_IFREG else { throw failure("目标知识库缺少有效数据库。") }
        try createParents(state)
        if let kind = try fileKind(pointer), kind != S_IFREG {
            throw failure("活动库指针必须是普通文件。")
        }
        let bytes = try JSONSerialization.data(withJSONObject: ["version": 1, "id": id as Any? ?? NSNull()])
        let temporary = state.appendingPathComponent(".active-library-\(UUID().uuidString).tmp")
        let fd = open(temporary.path, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, mode_t(0o600))
        guard fd >= 0 else { throw failure("无法写入活动库指针。") }
        defer { close(fd); try? manager.removeItem(at: temporary) }
        try bytes.withUnsafeBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                let count = Darwin.write(fd, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw failure("无法写入活动库指针。") }
                offset += count
            }
        }
        guard fsync(fd) == 0 else { throw failure("无法保存活动库指针。") }
        // Recheck before publishing so existing links and non-file pointers are refused.
        try checkParents(pointer)
        if let kind = try fileKind(pointer), kind != S_IFREG {
            throw failure("活动库指针必须是普通文件。")
        }
        guard rename(temporary.path, pointer.path) == 0 else { throw failure("无法切换活动知识库。") }
    }

    private struct Pointer: Codable {
        let version: Int
        let id: String?
    }

    private func canonicalID(_ id: String) throws -> String {
        guard let value = UUID(uuidString: id), value.uuidString.lowercased() == id.lowercased() else {
            throw failure("活动库 ID 无效。")
        }
        return value.uuidString.lowercased()
    }

    private func descriptor(_ id: String?) -> LocalLibraryDescriptor {
        MacPaths().library(home: home, credentialService: credentialService, id: id)
    }

    private func checkDescriptorPaths(_ value: LocalLibraryDescriptor) throws {
        for directory in [value.data, value.cache, value.logs] {
            try checkParents(directory)
            if let kind = try fileKind(directory), kind != S_IFDIR {
                throw failure("知识库管理目录必须是普通目录。")
            }
        }
    }

    private func components(_ url: URL) throws -> [URL] {
        let root = home.standardizedFileURL
        let target = url.standardizedFileURL
        guard root.path.hasPrefix("/"), target.path.hasPrefix(root.path + "/") else {
            throw failure("知识库路径超出客户端管理范围。")
        }
        var values = [root]
        let relative = target.path.dropFirst(root.path.count + 1)
        for component in relative.split(separator: "/") {
            values.append(values.last!.appendingPathComponent(String(component)))
        }
        return values
    }

    private func checkParents(_ url: URL) throws {
        for parent in try components(url).dropLast() {
            if let kind = try fileKind(parent), kind != S_IFDIR {
                throw failure("知识库管理路径包含符号链接或非目录。")
            }
        }
    }

    private func createParents(_ directory: URL) throws {
        for component in try components(directory) {
            if let kind = try fileKind(component) {
                guard kind == S_IFDIR else { throw failure("知识库管理路径包含符号链接或非目录。") }
            } else {
                guard mkdir(component.path, mode_t(0o700)) == 0 else {
                    throw failure("无法创建知识库管理目录。")
                }
            }
        }
    }

    private func fileKind(_ url: URL) throws -> mode_t? {
        var info = stat()
        if lstat(url.path, &info) == 0 { return info.st_mode & S_IFMT }
        if errno == ENOENT { return nil }
        throw failure("无法读取知识库管理路径。")
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "LocalLibrary", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}

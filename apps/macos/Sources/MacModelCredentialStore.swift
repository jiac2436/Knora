import CryptoKit
import Darwin
import Foundation

/// Model API keys belong to this macOS user, outside libraries and exported backups.
/// Legacy Keychain access is read-only and must never request authentication UI.
struct MacModelCredentialStore: CredentialStore {
    let home: URL
    let legacy: any CredentialStore

    init(home: URL = FileManager.default.homeDirectoryForCurrentUser,
         legacy: any CredentialStore = KeychainCredentialStore()) {
        self.home = home
        self.legacy = legacy
    }

    private struct Saved: Codable {
        let version: Int
        let value: String?
    }

    func read(service: String) -> CredentialRead { readWithoutInteraction(service: service) }

    func readWithoutInteraction(service: String) -> CredentialRead {
        do {
            return try locked(service: service) { directory, name in
                if let saved = try load(name, in: directory) {
                    return saved.value.map(CredentialRead.available) ?? .missing
                }
                let previous = legacy.readWithoutInteraction(service: service)
                if case .available(let value) = previous {
                    guard valid(value) else { return .unavailable }
                    try publish(value, named: name, in: directory)
                }
                return previous
            }
        } catch { return .unavailable }
    }

    func write(_ value: String, service: String) throws {
        guard valid(value) else { throw failure("API Key 格式无效。") }
        try locked(service: service) { directory, name in
            try publish(value, named: name, in: directory)
        }
    }

    func remove(service: String) throws {
        try locked(service: service) { directory, name in
            // Keep a non-secret tombstone: an old Keychain entry must not return
            // after clearing the model configuration or restarting the app.
            try publish(nil, named: name, in: directory)
        }
    }

    private func valid(_ value: String) -> Bool {
        (1...4096).contains(value.utf8.count) && value.utf8.allSatisfy { (33...126).contains($0) }
    }

    private func locked<T>(service: String, _ body: (Int32, String) throws -> T) throws -> T {
        guard service.hasSuffix(".model"), service.utf8.count <= 2048 else {
            throw failure("模型密钥标识无效。")
        }
        let directory = try openDirectory()
        defer { close(directory) }
        let lock = try lockDirectory(directory)
        defer { flock(lock, LOCK_UN); close(lock) }
        let name = SHA256.hash(data: Data(service.utf8)).map { String(format: "%02x", $0) }.joined() + ".json"
        return try body(directory, name)
    }

    private func load(_ name: String, in directory: Int32) throws -> Saved? {
        let file = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        if file < 0 {
            if errno == ENOENT { return nil }
            throw invalidFile()
        }
        defer { close(file) }
        let before = try checkFile(file)
        guard (1...16_384).contains(before.st_size) else { throw invalidFile() }
        var bytes = [UInt8](repeating: 0, count: Int(before.st_size) + 1)
        var total = 0
        while total < bytes.count {
            let count = bytes.withUnsafeMutableBytes {
                Darwin.read(file, $0.baseAddress!.advanced(by: total), $0.count - total)
            }
            if count < 0 && errno == EINTR { continue }
            guard count >= 0 else { throw invalidFile() }
            if count == 0 { break }
            total += count
        }
        let after = try checkFile(file)
        guard total == before.st_size, after.st_size == before.st_size,
              let saved = try? JSONDecoder().decode(Saved.self, from: Data(bytes.prefix(total))),
              saved.version == 1, saved.value.map(valid) ?? true else { throw invalidFile() }
        return saved
    }

    private func publish(_ value: String?, named name: String, in directory: Int32) throws {
        // A damaged payload can be repaired by re-entering the key; unsafe file
        // types, ownership or permissions cannot be silently replaced.
        let existing = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        if existing >= 0 {
            defer { close(existing) }
            _ = try checkFile(existing)
        } else if errno != ENOENT { throw invalidFile() }
        let temporary = ".\(name).\(UUID().uuidString).tmp"
        let file = openat(directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, mode_t(0o600))
        guard file >= 0 else { throw failure("无法创建本机模型密钥文件。") }
        defer { close(file); unlinkat(directory, temporary, 0) }
        guard fchmod(file, mode_t(0o600)) == 0 else { throw invalidFile() }
        _ = try checkFile(file)
        let bytes = try JSONEncoder().encode(Saved(version: 1, value: value))
        try bytes.withUnsafeBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                let count = Darwin.write(file, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw failure("无法写入本机模型密钥文件。") }
                offset += count
            }
        }
        guard fsync(file) == 0, renameat(directory, temporary, directory, name) == 0,
              fsync(directory) == 0, let verified = try load(name, in: directory), verified.value == value else {
            throw failure("模型密钥未能完整保存，请重新填写后重试。")
        }
    }

    private func lockDirectory(_ directory: Int32) throws -> Int32 {
        var file: Int32 = -1
        for _ in 0..<4 {
            file = openat(directory, ".lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, mode_t(0o600))
            if file >= 0 || errno != ENOENT { break }
            usleep(1_000)
        }
        guard file >= 0 else { throw failure("无法锁定本机模型密钥目录。") }
        do {
            let info = try checkFile(file)
            guard info.st_size == 0 else { throw invalidFile() }
            let flags = fcntl(file, F_GETFL)
            guard flags >= 0, fcntl(file, F_SETFL, flags & ~O_NONBLOCK) == 0 else { throw invalidFile() }
            while flock(file, LOCK_EX) != 0 {
                if errno != EINTR { throw failure("无法锁定本机模型密钥目录。") }
            }
            return file
        } catch { close(file); throw error }
    }

    private func checkFile(_ file: Int32) throws -> stat {
        var info = stat()
        guard fstat(file, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == geteuid(), info.st_nlink == 1, info.st_mode & 0o7777 == 0o600 else {
            throw invalidFile()
        }
        return info
    }

    private func openDirectory() throws -> Int32 {
        guard home.isFileURL, home.path.hasPrefix("/"), home.standardizedFileURL.path != "/" else {
            throw failure("本机模型密钥目录超出当前用户范围。")
        }
        let base = home.standardizedFileURL
        let target = MacPaths().configuration(home: base).appendingPathComponent("model-credentials")
        var directory = open(base.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard directory >= 0 else { throw failure("无法打开本机模型密钥目录。") }
        do {
            try checkDirectory(directory, isPrivate: false)
            for component in target.pathComponents.dropFirst(base.pathComponents.count) {
                if mkdirat(directory, component, mode_t(0o700)) != 0 && errno != EEXIST {
                    throw failure("无法创建本机模型密钥目录。")
                }
                let child = openat(directory, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                guard child >= 0 else { throw failure("本机模型密钥路径包含符号链接或不可用目录。") }
                close(directory)
                directory = child
                try checkDirectory(directory, isPrivate:
                    component == "local.pka.knowledge-inbox-state" || component == "model-credentials")
            }
            return directory
        } catch { close(directory); throw error }
    }

    private func checkDirectory(_ directory: Int32, isPrivate: Bool) throws {
        var info = stat()
        guard fstat(directory, &info) == 0, info.st_mode & S_IFMT == S_IFDIR,
              info.st_uid == geteuid(), info.st_mode & 0o022 == 0,
              !isPrivate || info.st_mode & 0o7777 == 0o700 else {
            throw failure("本机模型密钥目录的所有者或权限不安全。")
        }
    }

    private func invalidFile() -> NSError {
        failure("本机模型密钥文件权限不安全或内容已损坏，请重新填写密钥；若仍无法保存，请检查本地应用状态。")
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "MacModelCredentialStore", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}

import CryptoKit
import Darwin
import Foundation

/// The local owner's credential belongs to the macOS user, not to an account login.
/// Keep it outside the knowledge library and its exported backups.
struct MacDeviceCredentialStore {
    let home: URL
    let legacy: any CredentialStore

    func loadOrCreate(for library: LocalLibraryDescriptor) throws -> String {
        guard let directory = try openDirectory(create: true) else { throw failure("无法打开本地启动凭证目录。") }
        defer { close(directory) }
        let lock = try lockDirectory(directory)
        defer { flock(lock, LOCK_UN); close(lock) }
        let name = filename(library)
        if let saved = try read(name, in: directory) { return saved }
        let value: String
        if case .available(let previous) = legacy.readWithoutInteraction(service: library.credentialService), valid(previous) {
            value = previous
        } else {
            value = try MacPrivateFiles.randomCredential()
        }
        return try publish(value, named: name, in: directory)
    }

    /// Used only when an isolated restoration fails before its library is published.
    func remove(for library: LocalLibraryDescriptor) throws {
        guard let directory = try openDirectory(create: false) else { return }
        defer { close(directory) }
        let lock = try lockDirectory(directory)
        defer { flock(lock, LOCK_UN); close(lock) }
        let name = filename(library)
        guard try read(name, in: directory) != nil else { return }
        guard unlinkat(directory, name, 0) == 0, fsync(directory) == 0 else {
            throw failure("无法移除本地启动凭证。")
        }
    }

    private func filename(_ library: LocalLibraryDescriptor) -> String {
        SHA256.hash(data: Data(library.credentialService.utf8))
            .map { String(format: "%02x", $0) }.joined() + ".token"
    }

    private func valid(_ value: String) -> Bool {
        (32...256).contains(value.utf8.count) && value.utf8.allSatisfy { (33...126).contains($0) }
    }

    private func read(_ name: String, in directory: Int32) throws -> String? {
        // Non-blocking open makes special files fail validation without hanging startup.
        let file = openat(directory, name, O_RDONLY | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK)
        if file < 0 {
            if errno == ENOENT { return nil }
            throw failure("本地启动凭证不是可安全读取的普通文件。")
        }
        defer { close(file) }
        let before = try checkFile(file)
        guard (32...256).contains(before.st_size) else { throw invalidFile() }
        var bytes = [UInt8](repeating: 0, count: 257)
        var total = 0
        while total < bytes.count {
            let count = bytes.withUnsafeMutableBytes {
                Darwin.read(file, $0.baseAddress!.advanced(by: total), $0.count - total)
            }
            if count < 0 && errno == EINTR { continue }
            guard count >= 0 else { throw failure("无法读取本地启动凭证。") }
            if count == 0 { break }
            total += count
        }
        let after = try checkFile(file)
        guard total == before.st_size, after.st_size == before.st_size,
              let value = String(bytes: bytes.prefix(total), encoding: .ascii), valid(value) else {
            throw invalidFile()
        }
        return value
    }

    private func publish(_ value: String, named name: String, in directory: Int32) throws -> String {
        let temporary = ".\(name).\(UUID().uuidString).tmp"
        let file = openat(directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, mode_t(0o600))
        guard file >= 0 else { throw failure("无法创建本地启动凭证。") }
        defer { close(file); unlinkat(directory, temporary, 0) }
        guard fchmod(file, mode_t(0o600)) == 0 else { throw failure("无法保护本地启动凭证。") }
        _ = try checkFile(file)
        let bytes = Array(value.utf8)
        try bytes.withUnsafeBytes { buffer in
            var offset = 0
            while offset < buffer.count {
                let count = Darwin.write(file, buffer.baseAddress!.advanced(by: offset), buffer.count - offset)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw failure("无法写入本地启动凭证。") }
                offset += count
            }
        }
        guard fsync(file) == 0 else { throw failure("无法保存本地启动凭证。") }
        // Refuse replacement even if another process created the target before our lock.
        if renameatx_np(directory, temporary, directory, name, UInt32(RENAME_EXCL)) != 0 {
            if errno == EEXIST, let saved = try read(name, in: directory) { return saved }
            throw failure("无法发布本地启动凭证。")
        }
        guard fsync(directory) == 0 else { throw failure("无法保存本地启动凭证目录。") }
        return value
    }

    private func lockDirectory(_ directory: Int32) throws -> Int32 {
        var file: Int32 = -1
        // APFS can briefly return ENOENT when O_CREAT races another first opener.
        for _ in 0..<4 {
            file = openat(directory, ".lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC | O_NONBLOCK, mode_t(0o600))
            if file >= 0 || errno != ENOENT { break }
            usleep(1_000)
        }
        guard file >= 0 else { throw failure("无法锁定本地启动凭证。") }
        do {
            let info = try checkFile(file)
            guard info.st_size == 0 else { throw invalidFile() }
            let flags = fcntl(file, F_GETFL)
            guard flags >= 0, fcntl(file, F_SETFL, flags & ~O_NONBLOCK) == 0 else {
                throw failure("无法准备本地启动凭证锁。")
            }
            while flock(file, LOCK_EX) != 0 {
                if errno != EINTR { throw failure("无法锁定本地启动凭证。") }
            }
            return file
        } catch {
            close(file)
            throw error
        }
    }

    private func checkFile(_ file: Int32) throws -> stat {
        var info = stat()
        guard fstat(file, &info) == 0, info.st_mode & S_IFMT == S_IFREG,
              info.st_uid == geteuid(), info.st_nlink == 1, info.st_mode & 0o7777 == 0o600 else {
            throw invalidFile()
        }
        return info
    }

    private func openDirectory(create: Bool) throws -> Int32? {
        guard home.isFileURL, home.path.hasPrefix("/"), home.standardizedFileURL.path != "/" else {
            throw failure("本地启动凭证目录超出当前用户范围。")
        }
        let base = home.standardizedFileURL
        let target = MacPaths().configuration(home: base).appendingPathComponent("device-credentials")
        guard target.path.hasPrefix(base.path + "/") else { throw failure("本地启动凭证目录超出当前用户范围。") }
        // The OS-provided home is the trusted boundary; its ancestors can include
        // system aliases such as /var. Refuse a linked home or any managed child.
        var descriptor = open(base.path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
        guard descriptor >= 0 else { throw failure("无法打开本地启动凭证目录。") }
        do {
            try checkDirectory(descriptor, privateDirectory: false)
            let managedComponents = target.pathComponents.dropFirst(base.pathComponents.count)
            for component in managedComponents {
                if create && mkdirat(descriptor, component, mode_t(0o700)) != 0 && errno != EEXIST {
                    throw failure("无法创建本地启动凭证目录。")
                }
                let child = openat(descriptor, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
                if child < 0 {
                    if errno == ENOENT && !create { close(descriptor); return nil }
                    throw failure("本地启动凭证路径包含符号链接或不可用目录。")
                }
                close(descriptor)
                descriptor = child
                try checkDirectory(descriptor, privateDirectory:
                    component == "local.pka.knowledge-inbox-state" || component == "device-credentials")
            }
            return descriptor
        } catch {
            close(descriptor)
            throw error
        }
    }

    private func checkDirectory(_ directory: Int32, privateDirectory: Bool) throws {
        var info = stat()
        guard fstat(directory, &info) == 0, info.st_mode & S_IFMT == S_IFDIR,
              info.st_uid == geteuid(), info.st_mode & 0o022 == 0 else {
            throw failure("本地启动凭证目录的所有者或权限不安全。")
        }
        if privateDirectory && info.st_mode & 0o7777 != 0o700 {
            throw failure("本地启动凭证目录必须仅允许当前用户访问。")
        }
    }

    private func invalidFile() -> NSError {
        failure("本地启动凭证文件权限不安全或内容已损坏，请修复本地应用状态后重试。")
    }

    private func failure(_ message: String) -> NSError {
        NSError(domain: "MacDeviceCredentialStore", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
    }
}

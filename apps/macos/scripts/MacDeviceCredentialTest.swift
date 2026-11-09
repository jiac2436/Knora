import Foundation
import CryptoKit
import Darwin

/// Every credential in this test is synthetic; no real Keychain API is called.
private final class DeviceLegacyCredentials: CredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var result: CredentialRead
    private var quietReads = 0
    private var interactiveCalls = 0

    init(_ result: CredentialRead) { self.result = result }
    func read(service: String) -> CredentialRead {
        lock.lock(); defer { lock.unlock() }
        interactiveCalls += 1; return .denied
    }
    func readWithoutInteraction(service: String) -> CredentialRead {
        lock.lock(); defer { lock.unlock() }
        quietReads += 1; return result
    }
    func write(_ value: String, service: String) throws {
        lock.lock(); defer { lock.unlock() }; interactiveCalls += 1
        throw failure("测试禁止写入钥匙串")
    }
    func remove(service: String) throws {
        lock.lock(); defer { lock.unlock() }; interactiveCalls += 1
        throw failure("测试禁止删除钥匙串")
    }
    func change(to value: CredentialRead) {
        lock.lock(); defer { lock.unlock() }; result = value
    }
    var counts: (quiet: Int, interactive: Int) {
        lock.lock(); defer { lock.unlock() }; return (quietReads, interactiveCalls)
    }
}

@main struct MacDeviceCredentialTest {
    static func main() async throws {
        setbuf(stdout, nil)
        let manager = FileManager.default
        let root = manager.temporaryDirectory.resolvingSymlinksInPath()
            .appendingPathComponent("pka-device-credential-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: root) }

        let old = String(repeating: "a", count: 64)
        let legacy = DeviceLegacyCredentials(.available(old))
        let home = root.appendingPathComponent("migration")
        let library = try descriptor(home: home, service: "synthetic.migration")
        let store = MacDeviceCredentialStore(home: home, legacy: legacy)
        try check(try store.loadOrCreate(for: library) == old, "旧凭证未原值无交互迁移")
        let path = file(home: home, library: library)
        let originalBytes = try Data(contentsOf: path)
        try check(try mode(path) == 0o600 && mode(path.deletingLastPathComponent()) == 0o700,
                  "本地凭证或目录权限不够严格")
        legacy.change(to: .available(String(repeating: "b", count: 64)))
        try check(try MacDeviceCredentialStore(home: home, legacy: legacy).loadOrCreate(for: library) == old,
                  "已有文件不应被钥匙串新值覆盖")
        try check(try Data(contentsOf: path) == originalBytes && legacy.counts.quiet == 1,
                  "文件存在后仍访问旧钥匙串或改写了凭证")
        try check(legacy.counts.interactive == 0, "迁移触发交互式钥匙串访问")
        print("PASS: 旧凭证仅首次无交互迁移，文件权限 0700/0600，后续文件权威且不覆写")

        for (index, state) in [CredentialRead.missing, .denied, .unavailable,
                               .available("short"), .available(String(repeating: "中", count: 40)),
                               .available(String(repeating: "x", count: 32) + "\n"),
                               .available(String(repeating: "x", count: 257))].enumerated() {
            let fallbackHome = root.appendingPathComponent("fallback-\(index)")
            let fallbackLibrary = try descriptor(home: fallbackHome, service: "synthetic.fallback.\(index)")
            let denied = DeviceLegacyCredentials(state)
            let fallback = MacDeviceCredentialStore(home: fallbackHome, legacy: denied)
            let generated = try fallback.loadOrCreate(for: fallbackLibrary)
            try check(valid(generated), "旧凭证不可用时未生成有效随机凭证")
            try check(try fallback.loadOrCreate(for: fallbackLibrary) == generated, "回退凭证没有稳定保存")
            try check(denied.counts.quiet == 1 && denied.counts.interactive == 0,
                      "回退生成触发交互或重复读取旧凭证")
        }
        print("PASS: 旧钥匙串缺失、拒绝、锁定或非法值时安全生成，后续稳定复用且无交互")

        for (index, bytes) in [Data(), Data("short".utf8), Data((old + "\n").utf8),
                               Data(String(repeating: "中", count: 40).utf8), Data(repeating: 0, count: 64),
                               Data(repeating: 120, count: 257)].enumerated() {
            let unsafeHome = root.appendingPathComponent("corrupt-\(index)")
            let unsafeLibrary = try descriptor(home: unsafeHome, service: "synthetic.corrupt.\(index)")
            let sentinel = DeviceLegacyCredentials(.available(old))
            let unsafeStore = MacDeviceCredentialStore(home: unsafeHome, legacy: sentinel)
            _ = try unsafeStore.loadOrCreate(for: unsafeLibrary)
            let unsafeFile = file(home: unsafeHome, library: unsafeLibrary)
            try bytes.write(to: unsafeFile)
            try rejected { _ = try unsafeStore.loadOrCreate(for: unsafeLibrary) }
            try check(try Data(contentsOf: unsafeFile) == bytes && sentinel.counts.quiet == 1,
                      "损坏文件被静默替换或回退钥匙串")
        }
        print("PASS: 已有凭证损坏、非 ASCII、空白与超限时拒绝，保留原文件")

        let linkHome = root.appendingPathComponent("symlink")
        let linkLibrary = try descriptor(home: linkHome, service: "synthetic.symlink")
        let linkStore = MacDeviceCredentialStore(home: linkHome, legacy: legacy)
        _ = try linkStore.loadOrCreate(for: linkLibrary)
        let linkFile = file(home: linkHome, library: linkLibrary)
        let target = root.appendingPathComponent("external-secret")
        try Data(old.utf8).write(to: target)
        try manager.removeItem(at: linkFile)
        try manager.createSymbolicLink(at: linkFile, withDestinationURL: target)
        try rejected { _ = try linkStore.loadOrCreate(for: linkLibrary) }
        try check(try Data(contentsOf: target) == Data(old.utf8), "链接目标被修改")

        let parentHome = root.appendingPathComponent("parent-symlink")
        let parentLibrary = try descriptor(home: parentHome, service: "synthetic.parent-symlink")
        let parentStore = MacDeviceCredentialStore(home: parentHome, legacy: legacy)
        _ = try parentStore.loadOrCreate(for: parentLibrary)
        let parent = file(home: parentHome, library: parentLibrary).deletingLastPathComponent()
        let moved = parent.deletingLastPathComponent().appendingPathComponent("moved-credentials")
        try manager.moveItem(at: parent, to: moved)
        try manager.createSymbolicLink(at: parent, withDestinationURL: moved)
        try rejected { _ = try parentStore.loadOrCreate(for: parentLibrary) }
        let actualHome = root.appendingPathComponent("actual-home")
        let linkedHome = root.appendingPathComponent("linked-home")
        try manager.createDirectory(at: actualHome, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try manager.createSymbolicLink(at: linkedHome, withDestinationURL: actualHome)
        let linkedLibrary = MacPaths().library(home: linkedHome, credentialService: "synthetic.linked-home", id: nil)
        try rejected { _ = try MacDeviceCredentialStore(home: linkedHome, legacy: legacy).loadOrCreate(for: linkedLibrary) }
        print("PASS: 凭证文件、父目录及home符号链接均拒绝，不读取或修改链接目标")

        for (index, directory) in [false, true].enumerated() {
            let unsafeHome = root.appendingPathComponent("permissions-\(index)")
            let unsafeLibrary = try descriptor(home: unsafeHome, service: "synthetic.permissions.\(index)")
            let unsafeStore = MacDeviceCredentialStore(home: unsafeHome, legacy: legacy)
            _ = try unsafeStore.loadOrCreate(for: unsafeLibrary)
            let credentialFile = file(home: unsafeHome, library: unsafeLibrary)
            let unsafe = directory ? credentialFile.deletingLastPathComponent() : credentialFile
            try manager.setAttributes([.posixPermissions: directory ? 0o755 : 0o644], ofItemAtPath: unsafe.path)
            try rejected { _ = try unsafeStore.loadOrCreate(for: unsafeLibrary) }
            try check(try mode(unsafe) == (directory ? 0o755 : 0o644), "不安全权限被静默修复而非拒绝")
        }
        let hardHome = root.appendingPathComponent("hardlink")
        let hardLibrary = try descriptor(home: hardHome, service: "synthetic.hardlink")
        let hardStore = MacDeviceCredentialStore(home: hardHome, legacy: legacy)
        _ = try hardStore.loadOrCreate(for: hardLibrary)
        let hardFile = file(home: hardHome, library: hardLibrary)
        try manager.linkItem(at: hardFile, to: root.appendingPathComponent("hardlink-alias"))
        try rejected { _ = try hardStore.loadOrCreate(for: hardLibrary) }
        let fifoHome = root.appendingPathComponent("fifo")
        let fifoLibrary = try descriptor(home: fifoHome, service: "synthetic.fifo")
        let fifoStore = MacDeviceCredentialStore(home: fifoHome, legacy: legacy)
        _ = try fifoStore.loadOrCreate(for: fifoLibrary)
        let fifoFile = file(home: fifoHome, library: fifoLibrary)
        try manager.removeItem(at: fifoFile)
        try check(mkfifo(fifoFile.path, mode_t(0o600)) == 0, "不能创建合成FIFO")
        let began = Date()
        try rejected { _ = try fifoStore.loadOrCreate(for: fifoLibrary) }
        try check(Date().timeIntervalSince(began) < 1, "FIFO凭证导致阻塞")
        print("PASS: 宽权限文件/目录、硬链接与FIFO拒绝，不静默改变已有凭证保护或阻塞")

        let concurrentHome = root.appendingPathComponent("concurrent")
        let concurrentLibrary = try descriptor(home: concurrentHome, service: "synthetic.concurrent")
        let concurrentLegacy = DeviceLegacyCredentials(.denied)
        let tokens = try await withThrowingTaskGroup(of: String.self) { group in
            for _ in 0..<16 {
                group.addTask {
                    try MacDeviceCredentialStore(home: concurrentHome, legacy: concurrentLegacy)
                        .loadOrCreate(for: concurrentLibrary)
                }
            }
            var results: [String] = []
            for try await result in group { results.append(result) }
            return results
        }
        try check(tokens.count == 16 && Set(tokens).count == 1 && concurrentLegacy.counts.quiet == 1,
                  "并发首次创建产生多个凭证或重复迁移")
        try check(concurrentLegacy.counts.interactive == 0, "并发创建触发交互")
        let otherLibrary = try descriptor(home: concurrentHome, service: "synthetic.concurrent.other")
        let concurrentStore = MacDeviceCredentialStore(home: concurrentHome, legacy: concurrentLegacy)
        let other = try concurrentStore.loadOrCreate(for: otherLibrary)
        try check(other != tokens[0] && file(home: concurrentHome, library: otherLibrary) != file(home: concurrentHome, library: concurrentLibrary),
                  "不同知识库共用了设备凭证")
        try concurrentStore.remove(for: otherLibrary)
        try check(!manager.fileExists(atPath: file(home: concurrentHome, library: otherLibrary).path), "删除未清理指定库文件")
        try check(try concurrentStore.loadOrCreate(for: concurrentLibrary) == tokens[0] && concurrentLegacy.counts.interactive == 0,
                  "删除另一库影响当前库或触发钥匙串")
        print("PASS: 16 路首次创建复用同一凭证，按库隔离，定向删除不触碰其他库或钥匙串")

        #if !DEVICE_CREDENTIAL_CORE_TESTS
        let modelHome = root.appendingPathComponent("quiet-model")
        let modelLibrary = try descriptor(home: modelHome, service: "synthetic.quiet-model." + UUID().uuidString)
        let modelLegacy = DeviceLegacyCredentials(.unavailable)
        defer {
            for suffix in ["", ".mode", ".profile.v1"] {
                UserDefaults.standard.removeObject(forKey: LocalModelConfiguration.key(modelLibrary) + suffix)
            }
        }
        UserDefaults.standard.set(try JSONEncoder().encode(LocalModelOptions(model: "synthetic-text")),
                                  forKey: LocalModelConfiguration.key(modelLibrary))
        UserDefaults.standard.set("direct", forKey: LocalModelConfiguration.key(modelLibrary) + ".mode")
        let quiet = LocalModelStartup.prepare(modelLibrary, credentials: modelLegacy, allowInteraction: false)
        try check(quiet.profile.status == "unavailable" && quiet.provider_key.isEmpty && modelLegacy.counts.interactive == 0,
                  "静默启动模型密钥需要授权时应保持不可用，不弹系统授权")
        modelLegacy.change(to: .available("synthetic-provider-key"))
        let ready = LocalModelStartup.prepare(modelLibrary, credentials: modelLegacy, allowInteraction: false)
        try check(ready.profile.status == "ready" && ready.provider_key == "synthetic-provider-key" && modelLegacy.counts.interactive == 0,
                  "无需交互可读的模型密钥不能正常准备")
        _ = LocalModelStartup.prepare(modelLibrary, credentials: modelLegacy)
        try check(modelLegacy.counts.interactive == 1, "设置主动应用仍应保留现有交互式授权路径")
        print("PASS: 启动模型密钥静默读取，不可用不弹窗；主动设置仍保留原授权路径")
        #endif
    }

    private static func descriptor(home: URL, service: String) throws -> LocalLibraryDescriptor {
        try FileManager.default.createDirectory(at: home, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        return MacPaths().library(home: home, credentialService: service, id: nil)
    }
    private static func file(home: URL, library: LocalLibraryDescriptor) -> URL {
        let name = SHA256.hash(data: Data(library.credentialService.utf8)).map { String(format: "%02x", $0) }.joined()
        return MacPaths().configuration(home: home).appendingPathComponent("device-credentials/" + name + ".token")
    }
    private static func valid(_ value: String) -> Bool {
        (32...256).contains(value.utf8.count) && value.utf8.allSatisfy { (33...126).contains($0) }
    }
    private static func mode(_ url: URL) throws -> Int {
        (try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as! NSNumber).intValue & 0o777
    }
    private static func check(_ value: Bool, _ message: String) throws {
        if !value { throw failure(message) }
    }
    private static func rejected(_ action: () throws -> Void) throws {
        do { try action() } catch { return }
        throw failure("不安全或损坏的凭证应当拒绝")
    }
}

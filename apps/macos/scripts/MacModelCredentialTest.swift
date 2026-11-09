import CryptoKit
import Darwin
import Foundation

private final class ModelLegacyCredentials: CredentialStore, @unchecked Sendable {
    private let lock = NSLock()
    private var result: CredentialRead
    private var quiet = 0
    private var forbidden = 0
    init(_ result: CredentialRead) { self.result = result }
    func read(service: String) -> CredentialRead {
        lock.lock(); defer { lock.unlock() }; forbidden += 1; return .denied
    }
    func readWithoutInteraction(service: String) -> CredentialRead {
        lock.lock(); defer { lock.unlock() }; quiet += 1; return result
    }
    func write(_ value: String, service: String) throws {
        lock.lock(); defer { lock.unlock() }; forbidden += 1
        throw failure("测试禁止写入旧钥匙串")
    }
    func remove(service: String) throws {
        lock.lock(); defer { lock.unlock() }; forbidden += 1
        throw failure("测试禁止删除旧钥匙串")
    }
    func change(_ value: CredentialRead) { lock.lock(); defer { lock.unlock() }; result = value }
    var calls: (quiet: Int, forbidden: Int) {
        lock.lock(); defer { lock.unlock() }; return (quiet, forbidden)
    }
}

private final class ModelCredentialResults: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [Bool] = []
    func append(_ value: Bool) { lock.lock(); defer { lock.unlock() }; values.append(value) }
    var passed: Bool { lock.lock(); defer { lock.unlock() }; return values.count == 40 && values.allSatisfy { $0 } }
}

@main struct MacModelCredentialTest {
    static func main() async throws {
        setbuf(stdout, nil)
        let manager = FileManager.default
        let root = manager.temporaryDirectory.resolvingSymlinksInPath()
            .appendingPathComponent("pka-model-file-test-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        defer { try? manager.removeItem(at: root) }
        let home = try newHome(root, "normal")
        let service = "synthetic.model-file.\(UUID().uuidString).model"
        let legacy = ModelLegacyCredentials(.available("synthetic-old-model-key"))
        let store = MacModelCredentialStore(home: home, legacy: legacy)
        try check(store.read(service: service) == .available("synthetic-old-model-key"), "旧密钥未静默迁移")
        try check(legacy.calls == (quiet: 1, forbidden: 0), "迁移发生交互或修改旧钥匙串")
        let file = keyFile(home, service)
        try check(try permissions(file) == 0o600 && permissions(file.deletingLastPathComponent()) == 0o700,
                  "密钥文件或目录权限不正确")
        legacy.change(.denied)
        let restarted = MacModelCredentialStore(home: home, legacy: legacy)
        try check(restarted.read(service: service) == .available("synthetic-old-model-key") && legacy.calls.quiet == 1,
                  "重启后仍访问旧钥匙串")
        try restarted.write("synthetic-replacement-key", service: service)
        try check(store.readWithoutInteraction(service: service) == .available("synthetic-replacement-key"), "替换密钥未生效")
        try store.remove(service: service)
        legacy.change(.available("synthetic-old-model-key"))
        try check(MacModelCredentialStore(home: home, legacy: legacy).read(service: service) == .missing && legacy.calls.quiet == 1,
                  "清除后旧钥匙串密钥复活")
        try check(!(try String(contentsOf: file, encoding: .utf8)).contains("synthetic-replacement-key"), "清除标记仍含旧密钥")
        try store.write("synthetic-key-after-clear", service: service)
        try check(store.read(service: service) == .available("synthetic-key-after-clear") && legacy.calls.forbidden == 0,
                  "清除后重新配置失败或修改了钥匙串")
        print("PASS: 静默迁移、0600/0700权限、重启复用、替换及清除防复活；零交互/写入/删除钥匙串")

        for (index, state) in [CredentialRead.missing, .denied, .unavailable, .available("invalid key")].enumerated() {
            let isolatedHome = try newHome(root, "legacy-\(index)")
            let denied = ModelLegacyCredentials(state)
            let target = MacModelCredentialStore(home: isolatedHome, legacy: denied)
            let expected: CredentialRead = index == 3 ? .unavailable : state
            try check(target.read(service: service) == expected && denied.calls.forbidden == 0, "旧凭证失败状态处理错误")
            try target.write("synthetic-manually-entered-key", service: service)
            try check(target.read(service: service) == .available("synthetic-manually-entered-key") && denied.calls.quiet == 1,
                      "旧凭证拒绝时不能重新填写，或保存后仍读旧凭证")
        }
        let other = service + ".other.model"
        try store.write("synthetic-other-library-key", service: other)
        try check(store.read(service: service) == .available("synthetic-key-after-clear"), "跨知识库覆盖密钥")
        try check(keyFile(home, service) != keyFile(home, other), "知识库密钥文件未隔离")
        print("PASS: 旧密钥缺失/拒绝/不可用/无效可重新填写，多个知识库隔离")

        for scenario in ["file-symlink", "file-hardlink", "fifo", "file-permissions", "directory-permissions", "linked-parent"] {
            let isolatedHome = try newHome(root, scenario)
            let source = ModelLegacyCredentials(.denied)
            let target = MacModelCredentialStore(home: isolatedHome, legacy: source)
            try target.write("synthetic-safe-key", service: service)
            let path = keyFile(isolatedHome, service)
            let external = root.appendingPathComponent(scenario + "-external")
            try Data("synthetic-external-sentinel".utf8).write(to: external)
            switch scenario {
            case "file-symlink":
                try manager.removeItem(at: path)
                try manager.createSymbolicLink(at: path, withDestinationURL: external)
            case "file-hardlink": try manager.linkItem(at: path, to: external.appendingPathExtension("link"))
            case "fifo":
                try manager.removeItem(at: path)
                guard mkfifo(path.path, mode_t(0o600)) == 0 else { throw failure("无法准备FIFO测试") }
            case "file-permissions": guard chmod(path.path, mode_t(0o644)) == 0 else { throw failure("chmod失败") }
            case "directory-permissions": guard chmod(path.deletingLastPathComponent().path, mode_t(0o755)) == 0 else { throw failure("chmod失败") }
            default:
                let parent = path.deletingLastPathComponent()
                let moved = parent.appendingPathExtension("moved")
                try manager.moveItem(at: parent, to: moved)
                try manager.createSymbolicLink(at: parent, withDestinationURL: moved)
            }
            try check(target.read(service: service) == .unavailable && source.calls.quiet == 0,
                      "不安全文件触发旧钥匙串回退：\(scenario)")
            try rejected { try target.write("synthetic-unsafe-write", service: service) }
            try rejected { try target.remove(service: service) }
            try check(try String(contentsOf: external, encoding: .utf8) == "synthetic-external-sentinel", "外部文件被修改")
        }
        try Data("broken-json".utf8).write(to: file)
        guard chmod(file.path, mode_t(0o600)) == 0 else { throw failure("chmod失败") }
        try check(store.read(service: service) == .unavailable && legacy.calls.quiet == 1, "损坏文件回退读取旧密钥")
        try store.write("synthetic-repaired-key", service: service)
        try check(store.read(service: service) == .available("synthetic-repaired-key"), "损坏文件无法重新配置")
        for value in ["", "has spaces", "line\nbreak", String(repeating: "x", count: 4097)] {
            try rejected { try store.write(value, service: service) }
        }
        let maximum = String(repeating: "\\\"", count: 2048)
        try store.write(maximum, service: service)
        try check(store.read(service: service) == .available(maximum), "4096字节转义密钥不能往返")
        print("PASS: 符号链接/硬链接/FIFO/不安全权限拒绝，损坏可修复，边界与转义密钥往返")

        let concurrentHome = try newHome(root, "concurrent")
        let concurrentLegacy = ModelLegacyCredentials(.available("synthetic-concurrent-key"))
        let results = ModelCredentialResults()
        DispatchQueue.concurrentPerform(iterations: 40) { _ in
            let value = MacModelCredentialStore(home: concurrentHome, legacy: concurrentLegacy).read(service: service)
            results.append(value == .available("synthetic-concurrent-key"))
        }
        try check(results.passed && concurrentLegacy.calls == (quiet: 1, forbidden: 0), "并发首次迁移不一致")
        print("PASS: 40路并发首次读取仅迁移一次，没有半写文件或多次钥匙串访问")

        try configurationAndRestart(home: home, legacy: legacy)
        if CommandLine.arguments.count == 2 {
            try await hostAndBackup(home: try newHome(root, "host"), runtime: URL(fileURLWithPath: CommandLine.arguments[1]))
        }
    }

    private static func configurationAndRestart(home: URL, legacy: ModelLegacyCredentials) throws {
        let library = MacPaths().library(home: home, credentialService: "synthetic.model-config.\(UUID().uuidString)", id: nil)
        defer { clearDefaults(library) }
        let store = MacModelCredentialStore(home: home, legacy: legacy)
        let options = LocalModelOptions(model: "synthetic-model")
        try LocalModelConfiguration.save(options, replacementKey: "synthetic-model-profile-key", library: library, credentials: store)
        let original = LocalModelConfiguration.profile(library)
        try LocalModelConfiguration.save(options, replacementKey: "", library: library, credentials: store)
        try check(LocalModelConfiguration.profile(library).credential_generation == original.credential_generation,
                  "留空保存改变凭证身份")
        let reopened = MacModelCredentialStore(home: home, legacy: legacy)
        try check(LocalModelConfiguration.hasCredential(library, credentials: reopened), "已有文件未显示已保存")
        let startup = LocalModelStartup.prepare(library, credentials: reopened, allowInteraction: false)
        try check(startup.profile.status == "ready" && startup.provider_key == "synthetic-model-profile-key", "重启未从文件加载模型密钥")
        let metadata = UserDefaults.standard.data(forKey: LocalModelConfiguration.key(library) + ".profile.v1")!
        try check(!String(data: metadata, encoding: .utf8)!.contains("synthetic-model-profile-key"), "配置元数据泄漏密钥")
        try LocalModelConfiguration.clear(library, credentials: reopened)
        try check(LocalModelStartup.prepare(library, credentials: reopened).profile.mode == "disabled", "清除后模型仍启用")
        try check(!LocalModelConfiguration.hasCredential(library, credentials: reopened), "清除后仍显示已保存")
        print("PASS: 配置保存/留空保持/重启加载/移除，密钥不进入UserDefaults元数据")
    }

    private static func hostAndBackup(home: URL, runtime: URL) async throws {
        let legacy = ModelLegacyCredentials(.available("synthetic-host-key-for-migration"))
        let namespace = "synthetic.model-host.\(UUID().uuidString)"
        let first = LocalService(home: home, credentialService: namespace, credentials: legacy)
        let library = try first.currentLibrary()
        defer { first.stopAndWait(); clearDefaults(library) }
        let store = MacModelCredentialStore(home: home, legacy: legacy)
        try LocalModelConfiguration.save(LocalModelOptions(model: "synthetic-no-network-model"),
                                         replacementKey: "synthetic-host-private-file-key", library: library, credentials: store)
        let connection = try first.start(runtime: runtime)
        let api = try API(address: connection.address, token: connection.token)
        let state = try await api.call("v1/ai/configuration/state")
        try check((state["profile"] as? [String: Any])?["status"] as? String == "ready", "真实宿主未加载私有文件密钥")
        let dump = try JSONSerialization.data(withJSONObject: state)
        try check(!String(data: dump, encoding: .utf8)!.contains("synthetic-host-private-file-key"), "状态接口暴露密钥")
        first.stopAndWait()
        let backup = home.appendingPathComponent("synthetic-backup.zip")
        _ = try MacBackupTool.export(runtime: runtime, data: library.data, cache: library.cache,
                                     logs: library.logs, output: backup)
        let inspect = Process()
        inspect.executableURL = runtime.appendingPathComponent("python/bin/python3")
        inspect.arguments = ["-I", "-B", "-c", "import zipfile,sys; z=zipfile.ZipFile(sys.argv[1]); assert not any('model-credentials' in n for n in z.namelist()); assert not any(b'synthetic-host-private-file-key' in z.read(n) for n in z.namelist() if not n.endswith('/')); print('PASS: 备份内容与路径均不含模型密钥')", backup.path]
        try inspect.run(); inspect.waitUntilExit()
        try check(inspect.terminationStatus == 0, "真实知识库备份包含模型密钥")
        legacy.change(.denied)
        let restarted = LocalService(home: home, credentialService: namespace, credentials: legacy)
        defer { restarted.stopAndWait() }
        let next = try restarted.start(runtime: runtime)
        let nextState = try await API(address: next.address, token: next.token).call("v1/ai/configuration/state")
        try check((nextState["profile"] as? [String: Any])?["status"] as? String == "ready", "真实宿主重启丢失文件密钥")
        try check(legacy.calls.forbidden == 0, "真实宿主触发钥匙串交互或写入")
        print("PASS: 真实内核启动/重启模型就绪、状态脱敏和知识库备份隔离；零模型调用")
    }

    private static func newHome(_ root: URL, _ name: String) throws -> URL {
        let path = root.appendingPathComponent(name)
        try FileManager.default.createDirectory(at: path, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        return path
    }
    private static func keyFile(_ home: URL, _ service: String) -> URL {
        let name = SHA256.hash(data: Data(service.utf8)).map { String(format: "%02x", $0) }.joined() + ".json"
        return MacPaths().configuration(home: home).appendingPathComponent("model-credentials/" + name)
    }
    private static func permissions(_ path: URL) throws -> Int {
        try FileManager.default.attributesOfItem(atPath: path.path)[.posixPermissions] as! Int
    }
    private static func rejected(_ action: () throws -> Void) throws {
        do { try action() } catch { return }
        throw failure("不安全操作被接受")
    }
    private static func check(_ value: @autoclosure () throws -> Bool, _ message: String) throws {
        guard try value() else { throw failure(message) }
    }
    private static func clearDefaults(_ library: LocalLibraryDescriptor) {
        for suffix in ["", ".mode", ".profile.v1"] {
            UserDefaults.standard.removeObject(forKey: LocalModelConfiguration.key(library) + suffix)
        }
    }
}

import Foundation

extension Notification.Name {
    static let localServiceStopped = Notification.Name("local.pka.service-stopped")
}

/// Owns only processes launched for this app; the open pipe defines their lifetime.
final class MacKernelHost: @unchecked Sendable {
    static let shared = LocalService()
    static let credentialService = "local.pka.knowledge-inbox.local-device"
    private let credentialService: String
    private let credentials: any CredentialStore
    private let home: URL
    private var deviceCredentials: MacDeviceCredentialStore {
        MacDeviceCredentialStore(home: home, legacy: credentials)
    }
    private var selectedLibrary: LocalLibraryDescriptor?
    private let lock = NSLock()
    private let startLock = NSLock()
    private var generation = UUID()
    private var restorationProbe: LocalService?
    var operationToken: UUID {
        lock.lock()
        defer { lock.unlock() }
        return generation
    }

    init(home: URL = FileManager.default.homeDirectoryForCurrentUser,
         credentialService: String = LocalService.credentialService,
         library: LocalLibraryDescriptor? = nil,
         credentials: any CredentialStore = KeychainCredentialStore()) {
        self.home = home
        self.credentialService = credentialService
        self.selectedLibrary = library
        self.credentials = credentials
    }
    private var process: Process?
    private var input: Pipe?
    private var log: FileHandle?
    private var connection: (address: String, token: String)?
    private var webAllowFakeIPAtStart: Bool?
    var activeWebAllowFakeIP: Bool? {
        lock.lock()
        defer { lock.unlock() }
        return process?.isRunning == true ? webAllowFakeIPAtStart : nil
    }

    static var runtime: URL? {
        guard let resources = Bundle.main.resourceURL else { return nil }
        let url = resources.appendingPathComponent("runtime")
        return FileManager.default.isExecutableFile(atPath: MacPaths().runtime(url).python.path) ? url : nil
    }

    func start(runtime: URL, expectedGeneration: UUID? = nil) throws -> (address: String, token: String) {
        guard #available(macOS 14.5, *) else {
            throw failure("当前内置运行环境需要 macOS 14.5 或更新版本。")
        }
        startLock.lock()
        defer { startLock.unlock() }
        lock.lock()
        if let expectedGeneration, generation != expectedGeneration {
            lock.unlock()
            throw failure("本地服务启动已取消。")
        }
        if let connection, process?.isRunning == true { lock.unlock(); return connection }
        lock.unlock()
        guard let attempt = cleanup(expectedGeneration: expectedGeneration) else {
            throw failure("本地服务启动已取消。")
        }
        let libraryDescriptor = try currentLibrary()
        let token = try deviceCredentials.loadOrCreate(for: libraryDescriptor)
        let data = libraryDescriptor.data
        let cache = libraryDescriptor.cache
        let logs = libraryDescriptor.logs
        try FileManager.default.createDirectory(at: logs, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let logURL = logs.appendingPathComponent("local-service.log")
        try LocalServiceLog.rotate(logURL)
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil, attributes: [.posixPermissions: 0o600])
        }
        let logHandle = try FileHandle(forWritingTo: logURL)
        try logHandle.seekToEnd()
        let child = Process()
        let parentPipe = Pipe()
        let output = Pipe()
        let layout = MacPaths().runtime(runtime)
        child.executableURL = layout.python
        child.arguments = ["-I", "-B", layout.backend.appendingPathComponent("app/local_host.py").path]
        child.currentDirectoryURL = runtime
        child.environment = ["PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": home.path,
                             "TMPDIR": NSTemporaryDirectory(), "PKA_ENVIRONMENT": "local",
                             "PKA_DATA_DIR": data.path, "PKA_CACHE_DIR": cache.path, "PKA_LOG_DIR": logs.path,
                             "PKA_SEMANTIC_MODEL_PATH": layout.model.path,
                             "PKA_OCR_MODELS_DIR": layout.ocrModels.path]
        child.environment?["PKA_EXTENSIONS_ROOT"] = MacPaths().configuration(home: home).appendingPathComponent("extensions").path
        child.environment?["PKA_PLATFORM_HELPER"] = layout.platformHelper.path
        let modelCredentials: any CredentialStore = LocalModelConfiguration.profile(libraryDescriptor).mode == "direct" ?
            MacModelCredentialStore(home: home, legacy: credentials) : credentials
        let startup = LocalHostStartup(owner_credential: token,
            ai: LocalModelStartup.prepare(libraryDescriptor, credentials: modelCredentials, allowInteraction: false))
        let startupBytes = try startup.encoded()
        let expectedRuntimeID = try LocalKernelContract.runtimeID(runtime)
        let webEnvironment = LocalWebConfiguration.environment(libraryDescriptor)
        for (key, value) in webEnvironment { child.environment?[key] = value }
        child.terminationHandler = { [weak self] exited in
            DispatchQueue.main.async {
                guard let self else { return }
                self.lock.lock()
                let current = self.process === exited
                if current { self.connection = nil }
                self.lock.unlock()
                if current { NotificationCenter.default.post(name: .localServiceStopped, object: self) }
            }
        }
        child.standardInput = parentPipe
        child.standardOutput = output
        child.standardError = logHandle
        do {
            lock.lock()
            guard generation == attempt else {
                lock.unlock()
                try? logHandle.close()
                throw failure("本地服务启动已取消。")
            }
            process = child
            input = parentPipe
            log = logHandle
            do { try child.run() }
            catch { lock.unlock(); throw error }
            lock.unlock()
            // Close the parent's duplicate read end, so EOF accurately follows its write end.
            try parentPipe.fileHandleForReading.close()
            try output.fileHandleForWriting.close()
            try parentPipe.fileHandleForWriting.write(contentsOf: startupBytes)
            let deadline = Date().addingTimeInterval(75)
            let timeout = DispatchWorkItem { Self.terminate(child, pipe: parentPipe) }
            DispatchQueue.global().asyncAfter(deadline: .now() + 75, execute: timeout)
            defer { timeout.cancel(); try? output.fileHandleForReading.close() }
            var line = Data()
            while Date() < deadline {
                let bytes = output.fileHandleForReading.readData(ofLength: 1)
                if bytes.isEmpty { break }
                line.append(bytes)
                guard line.count < 16_384 else { throw failure("本地服务启动响应异常。") }
                if bytes[0] != 10 { continue }
                defer { line.removeAll(keepingCapacity: true) }
                guard let event = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any] else { continue }
                if event["event"] as? String == "failed" {
                    throw failure(event["message"] as? String ?? "本地服务启动失败。")
                }
                if event["event"] as? String == "ready", let address = event["address"] as? String,
                   address.hasPrefix("http://127.0.0.1:") {
                    guard let kernel = event["kernel"],
                          let data = try? JSONSerialization.data(withJSONObject: kernel),
                          let info = try? JSONDecoder().decode(LocalKernelInfo.self, from: data) else {
                        throw failure("本地内核缺少版本信息，请使用同一安装包重新启动。")
                    }
                    try LocalKernelContract.validate(info, expectedRuntimeID: expectedRuntimeID)
                    let result = (address: address, token: token)
                    lock.lock()
                    guard generation == attempt && process === child && child.isRunning else {
                        lock.unlock()
                        throw failure("本地服务启动已取消。")
                    }
                    connection = result
                    webAllowFakeIPAtStart = webEnvironment["PKA_WEB_ALLOW_FAKE_IP"] == "1"
                    selectedLibrary = libraryDescriptor
                    lock.unlock()
                    return result
                }
            }
            throw failure("本地服务未能启动，请查看本地服务日志后重试。")
        } catch {
            _ = cleanup(expectedGeneration: attempt)
            throw error
        }
    }

    var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        return process?.isRunning == true && connection != nil
    }

    func stop() { _ = cleanup() }

    func storageLocations(application: URL = Bundle.main.bundleURL) -> StorageScanLocations {
        StorageScanLocations(home: home, application: application)
    }

    var backupDirectories: (data: URL, cache: URL, logs: URL) {
        // Callers with recoverable errors use currentLibrary() rather than this convenience.
        if let descriptor = try? currentLibrary() { return (descriptor.data, descriptor.cache, descriptor.logs) }
        let layout = MacPaths().library(home: home, credentialService: credentialService, id: nil)
        return (layout.data, layout.cache, layout.logs)
    }

    func currentLibrary() throws -> LocalLibraryDescriptor {
        lock.lock()
        let selected = selectedLibrary
        lock.unlock()
        return try selected ?? LocalLibraryStore(home: home, credentialService: credentialService).current()
    }

    func activateLibrary(_ descriptor: LocalLibraryDescriptor) throws {
        startLock.lock()
        defer { startLock.unlock() }
        try LocalLibraryStore(home: home, credentialService: credentialService).activate(descriptor)
        selectLibrary(descriptor)
    }

    /// Used to reopen the untouched old library if pointer publication fails.
    func selectLibrary(_ descriptor: LocalLibraryDescriptor) {
        lock.lock()
        selectedLibrary = descriptor
        lock.unlock()
    }

    func prepareRestoredLibrary(runtime: URL, package: URL, expectedGeneration: UUID) throws -> LocalLibraryDescriptor {
        guard operationToken == expectedGeneration else { throw failure("知识库恢复已取消。") }
        let descriptor = try LocalLibraryStore(home: home, credentialService: credentialService).allocate()
        let token = try deviceCredentials.loadOrCreate(for: descriptor)
        do {
            _ = try LocalBackupTool.restore(runtime: runtime, package: package, destination: descriptor.data,
                                            cache: descriptor.cache, logs: descriptor.logs, credential: token)
            let probe = LocalService(home: home, credentialService: credentialService, library: descriptor, credentials: credentials)
            lock.lock()
            guard generation == expectedGeneration else {
                lock.unlock()
                throw failure("知识库恢复已取消，已恢复的独立目录仍保留。")
            }
            let probeGeneration = probe.operationToken
            restorationProbe = probe
            lock.unlock()
            defer {
                probe.stopAndWait()
                lock.lock()
                if restorationProbe === probe { restorationProbe = nil }
                lock.unlock()
            }
            _ = try probe.start(runtime: runtime, expectedGeneration: probeGeneration)
            guard operationToken == expectedGeneration else { throw failure("知识库恢复已取消。") }
            return descriptor
        } catch {
            if !FileManager.default.fileExists(atPath: descriptor.data.appendingPathComponent("knowledge.db").path) {
                try? deviceCredentials.remove(for: descriptor)
            }
            throw error
        }
    }

    #if CAPTURE_QUEUE_TESTS
    var credentialServicesForTesting: [String] {
        let root = MacPaths().librariesRoot(home: home)
        let children = (try? FileManager.default.contentsOfDirectory(atPath: root.path)) ?? []
        return [credentialService] + children.compactMap { UUID(uuidString: $0).map { credentialService + ".library." + $0.uuidString.lowercased() } }
    }
    #endif

    /// Blocking: wait for our host's graceful worker shutdown before taking a backup.
    @discardableResult func stopAndWait(expectedGeneration: UUID? = nil) -> UUID? {
        startLock.lock()
        defer { startLock.unlock() }
        lock.lock()
        let child = process
        lock.unlock()
        guard let operation = cleanup(expectedGeneration: expectedGeneration) else { return nil }
        child?.waitUntilExit()
        return operation
    }

    #if CAPTURE_QUEUE_TESTS
    func interruptOwnedHostForTesting() {
        lock.lock()
        let child = process
        lock.unlock()
        if let child, child.isRunning { MacProcessTermination.kill(child) }
    }
    #endif

    private static func terminate(_ child: Process, pipe: Pipe?) {
        try? pipe?.fileHandleForWriting.close()
        MacProcessTermination.stop(child, grace: 8)
    }

    @discardableResult private func cleanup(expectedGeneration: UUID? = nil) -> UUID? {
        lock.lock()
        if let expectedGeneration, generation != expectedGeneration { lock.unlock(); return nil }
        generation = UUID()
        let operation = generation
        let probe = restorationProbe
        restorationProbe = nil
        let child = process
        let pipe = input
        let handle = log
        process = nil
        input = nil
        connection = nil
        webAllowFakeIPAtStart = nil
        log = nil
        lock.unlock()
        probe?.stop()
        if let child { Self.terminate(child, pipe: pipe) }
        else { try? pipe?.fileHandleForWriting.close() }
        try? handle?.close()
        return operation
    }
}

/// Rotate only this app's diagnostic logs; knowledge and staged files are unrelated.
enum LocalServiceLog {
    static func rotate(_ file: URL, maximumBytes: Int = 5 * 1024 * 1024) throws {
        let manager = FileManager.default
        guard manager.fileExists(atPath: file.path) else { return }
        let values = try file.resourceValues(forKeys: [.fileSizeKey, .isSymbolicLinkKey])
        guard values.isSymbolicLink != true else { throw failure("服务日志不能是符号链接。") }
        guard (values.fileSize ?? 0) >= maximumBytes else { return }
        for index in stride(from: 3, through: 1, by: -1) {
            let target = URL(fileURLWithPath: file.path + ".\(index)")
            if manager.fileExists(atPath: target.path) {
                let targetValues = try target.resourceValues(forKeys: [.isRegularFileKey, .isSymbolicLinkKey])
                guard targetValues.isRegularFile == true, targetValues.isSymbolicLink != true else {
                    throw failure("日志归档路径不是普通文件，已保留原内容。")
                }
                try manager.removeItem(at: target)
            }
            let source = index == 1 ? file : URL(fileURLWithPath: file.path + ".\(index - 1)")
            if manager.fileExists(atPath: source.path) { try manager.moveItem(at: source, to: target) }
        }
    }
}

struct LocalRestartPolicy {
    private var attempts: [Date] = []

    mutating func nextDelay(now: Date = Date()) -> TimeInterval? {
        attempts.removeAll { now.timeIntervalSince($0) >= 60 }
        guard attempts.count < 3 else { return nil }
        let delay = [1.0, 2.0, 4.0][attempts.count]
        attempts.append(now)
        return delay
    }
}

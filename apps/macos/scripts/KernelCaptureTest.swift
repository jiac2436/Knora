import Foundation

private final class CaptureCredentials: CredentialStore {
    var values: [String: String] = [:]
    func read(service: String) -> CredentialRead { values[service].map(CredentialRead.available) ?? .missing }
    func write(_ value: String, service: String) throws { values[service] = value }
    func remove(service: String) throws { values.removeValue(forKey: service) }
}

private final class SnapshotGate {
    var rejectReceipt = false
    func write(_ data: Data, _ url: URL) throws {
        if rejectReceipt, let snapshot = try JSONSerialization.jsonObject(with: data) as? [String: Any],
           let items = snapshot["items"] as? [[String: Any]], items.contains(where: { $0["kernelJobID"] != nil }) {
            throw CocoaError(.fileWriteOutOfSpace)
        }
        try data.write(to: url, options: .atomic)
    }
}

@main struct KernelCaptureTest {
    @MainActor static func main() async throws {
        let arguments = CommandLine.arguments
        guard arguments.count == 4 else { fatalError("需要隔离runtime、root和合成CLI路径") }
        let manager = FileManager.default
        let runtime = URL(fileURLWithPath: arguments[1])
        let root = URL(fileURLWithPath: arguments[2])
        let home = root.appendingPathComponent("home")
        try manager.createDirectory(at: home, withIntermediateDirectories: true,
                                    attributes: [.posixPermissions: 0o700])
        let credentials = CaptureCredentials()
        let service = LocalService(home: home.standardizedFileURL, credentialService: "synthetic.m4." + UUID().uuidString, credentials: credentials)
        defer { service.stopAndWait() }
        let library = try service.currentLibrary()
        let gate = SnapshotGate()
        let store = CaptureQueueStore(root: library.data.appendingPathComponent("capture-queue"), writeSnapshot: gate.write)
        let first = CaptureModel(queueStore: store, localService: service, runtime: runtime)
        await first.startLocalService()
        precondition(first.connected, first.authMessage)
        first.domainName = "合成采集域"
        await first.createDomain()
        first.draft = "合成笔记正文"
        first.addDraft()
        let file = root.appendingPathComponent("synthetic.csv")
        try Data("name,value\nsynthetic,137\n".utf8).write(to: file)
        first.addFiles([file])
        let oldCopy = first.items[1].file!
        gate.rejectReceipt = true
        await first.send()
        precondition(manager.fileExists(atPath: oldCopy.path), "客户端回执落盘失败时不得清理原件")
        let api = try API(address: first.address, token: first.token)
        let queued = try await api.call("v1/capture-queue")
        precondition((queued["items"] as? [[String: Any]])?.count == 2)
        gate.rejectReceipt = false
        await first.refreshKernelCaptures()
        precondition(first.items.allSatisfy { $0.kernelJobID != nil })
        precondition(!manager.fileExists(atPath: oldCopy.path), "回执落盘成功后释放客户端原件")
        first.draft = "尚未提交的草稿"
        first.cleanup()
        service.stopAndWait()

        let second = CaptureModel(queueStore: store, localService: service, runtime: runtime)
        precondition(second.draft == "尚未提交的草稿")
        await second.startLocalService()
        precondition(second.connected, second.authMessage)
        for _ in 0..<150 {
            await second.refreshKernelCaptures()
            if second.items.allSatisfy(\.sent) { break }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        precondition(second.items.count == 2 && second.items.allSatisfy(\.sent), second.queueMessage)
        let sourceAPI = try API(address: second.address, token: second.token)
        let rows = try await sourceAPI.call("v1/sources")
        precondition((rows["items"] as? [[String: Any]])?.count == 2, "重启不得重复入库")
        precondition(second.draft == "尚未提交的草稿", "后台恢复不得提交草稿")
        print("PASS: 回执落盘失败保留原件、成功后清理、重启继续归档、草稿保留和幂等")

        second.feishu.executablePath = arguments[3]
        second.feishu.profile = "synthetic"
        second.draft = "https://synthetic.feishu.cn/docx/Document"
        await second.send()
        for _ in 0..<250 {
            await second.refreshKernelCaptures()
            if second.items.last?.sent == true || second.items.last?.kernelState == "failed" { break }
            try await Task.sleep(nanoseconds: 100_000_000)
        }
        precondition(second.items.last?.sent == true, second.items.last?.status ?? second.queueMessage)
        let identifier = second.items.last!.sourceID!
        let detail = try await sourceAPI.call("v1/sources/\(identifier)")
        let images = detail["images"] as? [[String: Any]] ?? []
        precondition(detail["capture_type"] as? String == "feishu_document" && images.count == 1)
        precondition(images[0]["status"] as? String == "saved")
        precondition((detail["extracted_text"] as? String ?? "").contains("合成正文"))
        let sourceID = second.items[0].sourceID!
        let count = second.items.count
        await second.removeKernelCaptures([second.items[0]])
        precondition(second.items.count == count - 1)
        _ = try await sourceAPI.call("v1/sources/\(sourceID)") // Queue removal retains the archived source.
        print("PASS: 原生客户端→内核队列→系统CLI/Vision→图文归档，移除队列保留知识")
    }
}

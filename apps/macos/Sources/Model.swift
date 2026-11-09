import AppKit
import PDFKit
import SwiftUI
import UniformTypeIdentifiers

private let supportedFileExtensions: Set<String> = [
    "pdf", "doc", "docx", "docm", "ppt", "pps", "pot", "pptx", "pptm", "ppsx", "ppsm",
    "xls", "xlsx", "xlsm", "xlsb", "odt", "ods", "odp", "rtf", "epub", "csv", "md", "txt"
]

struct DomainItem: Identifiable {
    let id: String
    let name: String
}

struct ProjectItem: Identifiable {
    let id: String
    let name: String
}

@MainActor final class CaptureModel: ObservableObject {
    let feishu = FeishuConnection()
    @Published var address = ""
    @Published var token = ""
    @Published var ocrModel = "small"
    @Published var ocrEngine = "pdfium"
    @Published var ocrMessage = "设置对新上传的 PDF 和重新扫描生效。"
    @Published var webReadEngine = "legacy"
    @Published var webCleaningEnabled = false
    @Published var webReadMessage = "设置对新保存和重新读取的网页生效。"
    @Published var webAllowFakeIP = false
    @Published var webNetworkRequiresRestart = false
    @Published var webNetworkMessage = "设置自动保存，重启客户端后生效。"
    @Published var domains: [DomainItem] = []
    @Published var projects: [ProjectItem] = []
    @Published var accountName = ""
    @Published var authMessage = ""
    @Published var domainID = ""
    @Published var domainName = ""
    @Published var projectID = ""
    @Published var projectName = ""
    @Published var draft = "" { didSet { if !restoringQueue { _ = persistQueue() } } }
    @Published var items: [CaptureItem] = []
    @Published var busy = false
    @Published var statusBusy = false
    @Published var message = "粘贴文本、链接，或从 Finder 复制文件后粘贴。"
    var draftKind: CaptureKind? {
        guard !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return nil }
        return standaloneLink(draft) == nil ? .note : .link
    }
    @Published var knowledgeSearchMode: KnowledgeSearchMode = .exact
    @Published var searchQuery = ""
    @Published var searchDomainID = ""
    @Published var knowledgeRows: [KnowledgeRow] = []
    @Published var allKnowledgeRows: [KnowledgeRow] = []
    @Published var nextSourceOffset: Int?
    @Published var searchProjectID = ""
    @Published var selectedSource: KnowledgeDetail? {
        didSet {
            if let selectedSource { readingSessionStore?.select(sourceID: selectedSource.id, domainID: selectedSource.domainID) }
        }
    }
    var readingSessionStore: ReadingSessionStore?
    var pendingArticleLink: ArticleLink?
    var articleLinkGeneration = UUID()
    var articleLinkInFlight: UUID?
    @Published var articleNavigationID = UUID()
    @Published var articleLinkError: String?
    @Published var sourceRevisions: [RevisionItem] = []
    @Published var selectedEvidence: EvidenceDetail?
    @Published var outdatedSourceIDs: Set<String> = []
    @Published var delayedWebReadIDs: Set<String> = []
    var cachedKnowledge: [String: CachedKnowledge] = [:]
    let textReaderStore = TextReaderStore()
    var knowledgeCacheOrder: [String] = []
    @Published var supplementDraft = ""
    @Published var browseBusy = false
    @Published var browseMessage = "连接后台后可搜索已保存的资料。"
    private var connectedAddress = ""
    private var connectedToken = ""
    private var ownerID = ""
    var loadingSourceID = ""
    var sourceLoadGeneration = UUID()
    var sourceListGeneration = UUID()
    private let staging = FileManager.default.temporaryDirectory.appendingPathComponent("pka-\(UUID().uuidString)")
    private var pdfDocumentCache: [String: PDFDocument] = [:]
    private var pdfViewCache: [String: PDFView] = [:]
    private var pdfCacheOrder: [String] = []
    var pdfCacheDirectory: URL { staging.appendingPathComponent("pdf-reader") }
    let localService: LocalService
    private let serviceRuntime: URL?
    private var queueStore: CaptureQueueStore?
    private var launchGeneration = UUID()
    private var wakeCheckTask: Task<Void, Never>?
    private var recoveryTask: Task<Void, Never>?
    private var restartPolicy = LocalRestartPolicy()
    @Published var recoveringService = false
    private var restoringQueue = false
    private var queueReadFailed = false
    @Published var queueMessage = ""
    var captureDraftStore: CaptureQueueStore? { queueStore }
    var captureDraftsReadable: Bool { !queueReadFailed }
    var captureTarget: String? { connected ? (usesLocalService ? "local|\(ownerID)" : "\(address)|\(ownerID)") : nil }
    @Published var trashItems: [LocalTrashItem] = []
    @Published var trashBusy = false
    @Published var trashMessage = "回收站不会自动清理；永久删除后无法恢复。"
    @Published var nextTrashOffset: Int?
    var trashGeneration = UUID()
    @Published var searchTagQuery = ""
    @Published var summaryIndexBusy = false
    @Published var ragBusy = false
    @Published var agentConnectionsBusy = false
    @Published var aiGatewayAddress = ""
    @Published var aiTokenSaved = false
    @Published var aiJobs: [LocalAIJob] = []
    @Published var aiArtifacts: [LocalAIArtifact] = []
    @Published var aiReadStatus: AIReadStatus?
    var aiReadGeneration = UUID()
    @Published var aiBusy = false
    @Published var aiConfigurationPending = false
    @Published var aiMessage = "手动分析会把所选资料正文、个人补充与上下文、领域和项目名称发送到 AI 网关，产物保存在本机。"
    var aiGeneration = UUID()
    @Published var localTags: [LocalTag] = []
    @Published var sourceTags: [LocalSourceTag] = []
    @Published var legacyRuleTags: [String] = []
    @Published var tagsBusy = false
    @Published var tagsMessage = "人工标签保存在本机；AI 标签需由模型分析生成。"
    var tagsGeneration = UUID()
    var tagsReadGeneration = UUID()
    @Published var backupBusy = false
    private var backupGeneration = UUID()
    @Published private var previousLocalLibrary: LocalLibraryDescriptor?
    var canReturnToPreviousLocalLibrary: Bool { previousLocalLibrary != nil }
    @Published var backupMessage = "导出已入库资料及原件；草稿与采集队列不包含在备份中。"

    init(queueStore: CaptureQueueStore? = nil,
         localService: LocalService = .shared, runtime: URL? = LocalService.runtime) {
        self.queueStore = queueStore
        self.localService = localService
        self.serviceRuntime = runtime
        restoringQueue = true
        defer { restoringQueue = false }
        do {
            if runtime != nil {
                let library = try localService.currentLibrary()
                readingSessionStore = ReadingSessionStore(directory: library.data)
                if self.queueStore == nil { self.queueStore = CaptureQueueStore(root: library.data.appendingPathComponent("capture-queue")) }
            }
            if let snapshot = try self.queueStore?.load() {
                draft = snapshot.draft
                items = snapshot.items
                domainID = snapshot.domainID
                projectID = snapshot.projectID
            }
        } catch {
            queueReadFailed = true
            queueMessage = "采集队列无法恢复，原文件已保留：\(error.localizedDescription)"
        }
    }

    @discardableResult
    func persistQueue() -> Bool {
        guard let queueStore else { return true }
        guard !queueReadFailed else { return false }
        do {
            try queueStore.save(CaptureQueueSnapshot(draft: draft, items: items, domainID: domainID, projectID: projectID))
            queueMessage = ""
            return true
        } catch {
            let detail = error as NSError
            if detail.domain == NSCocoaErrorDomain && detail.code == CocoaError.Code.fileWriteOutOfSpace.rawValue {
                queueMessage = "磁盘空间不足，待发送内容尚未安全保存。请释放空间后重试保存。"
            } else {
                queueMessage = "待发送内容尚未安全保存：\(error.localizedDescription)"
            }
            return false
        }
    }


    func cachedPDF(_ id: String) -> PDFDocument? {
        guard let document = pdfDocumentCache[id] else { return nil }
        pdfCacheOrder.removeAll { $0 == id }
        pdfCacheOrder.append(id)
        return document
    }

    func rememberPDF(_ document: PDFDocument, id: String) {
        if pdfDocumentCache[id] !== document { pdfViewCache.removeValue(forKey: id) }
        pdfDocumentCache[id] = document
        pdfCacheOrder.removeAll { $0 == id }
        pdfCacheOrder.append(id)
        while pdfCacheOrder.count > 4 {
            let removed = pdfCacheOrder.removeFirst()
            pdfDocumentCache.removeValue(forKey: removed)
            pdfViewCache.removeValue(forKey: removed)
            try? FileManager.default.removeItem(at: pdfCacheDirectory.appendingPathComponent("\(removed).pdf"))
        }
    }

    func clearPDFCache() {
        pdfViewCache.removeAll()
        pdfDocumentCache.removeAll()
        pdfCacheOrder.removeAll()
    }

    func forgetPDF(_ id: String) {
        pdfViewCache.removeValue(forKey: id)
        pdfDocumentCache.removeValue(forKey: id)
        pdfCacheOrder.removeAll { $0 == id }
        try? FileManager.default.removeItem(at: pdfCacheDirectory.appendingPathComponent("\(id).pdf"))
    }

    func pdfView(for id: String) -> PDFView {
        if let view = pdfViewCache[id] { return view }
        let view = PDFView()
        pdfViewCache[id] = view
        return view
    }

    func cachedPDFPage(_ id: String) -> Int? {
        guard let view = pdfViewCache[id], let document = view.document,
              let page = view.currentPage else { return nil }
        let index = document.index(for: page)
        return index >= 0 ? index + 1 : nil
    }

    var connected: Bool { !ownerID.isEmpty && address == connectedAddress && token == connectedToken }

    var usesLocalService: Bool { serviceRuntime != nil }

    func localServiceStopped(service: LocalService?) {
        guard service === localService, usesLocalService && connected, !backupBusy else { return }
        summaryIndexBusy = false
        ragBusy = false
        agentConnectionsBusy = false
        resetLocalAI()
        resetLocalTags()
        resetLocalTrash()
        ownerID = ""
        connectedToken = ""
        authMessage = "本地服务已停止，正在尝试恢复。"
        message = authMessage
        guard recoveryTask == nil else { return }
        recoveringService = true
        recoveryTask = Task { [weak self] in
            guard let self else { return }
            defer { self.recoveringService = false; self.recoveryTask = nil }
            while let delay = self.restartPolicy.nextDelay() {
                do { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) }
                catch { return }
                // Let an interrupted capture finish its failure path before reconnecting.
                var waits = 0
                while self.busy && waits < 100 && !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 100_000_000)
                    waits += 1
                }
                guard !Task.isCancelled, !self.busy else { break }
                await self.startLocalService(automatic: true)
                if self.connected || Task.isCancelled { return }
            }
            self.authMessage = "本地服务连续恢复失败，请查看日志后手动重试。"
            self.message = self.authMessage
        }
    }

    func cancelLocalServiceStart() {
        summaryIndexBusy = false
        ragBusy = false
        agentConnectionsBusy = false
        resetLocalAI()
        resetLocalTags()
        resetLocalTrash()
        backupGeneration = UUID()
        wakeCheckTask?.cancel()
        recoveryTask?.cancel()
        launchGeneration = UUID()
        localService.stop()
        busy = false
        ownerID = ""
        connectedToken = ""
        authMessage = "已取消本地服务启动。"
        message = authMessage
    }

    func checkLocalServiceAfterWake() async {
        guard usesLocalService, connected, !busy, !recoveringService, !trashBusy else { return }
        let attempt = launchGeneration
        if !localService.isRunning {
            localServiceStopped(service: localService)
            return
        }
        do {
            let api = try API(address: address, token: token)
            let health = try await api.call("health", timeout: 8)
            guard launchGeneration == attempt, connected, !Task.isCancelled else { return }
            guard health["status"] as? String == "ok" else { throw failure("本地服务尚未就绪。") }
            let user = try await api.call("v1/me", timeout: 8)
            guard launchGeneration == attempt, connected, !Task.isCancelled else { return }
            guard user["id"] as? String == ownerID else { throw failure("本地服务身份不匹配。") }
        } catch {
            guard launchGeneration == attempt, connected, !Task.isCancelled else { return }
            let expired = (error as NSError).userInfo["HTTPStatus"] as? Int == 401
            if expired || !localService.isRunning {
                localService.stop()
                localServiceStopped(service: localService)
            } else {
                message = "本地服务暂未响应，请稍后重试；已保留采集队列。"
            }
        }
    }

    func scheduleLocalServiceWakeCheck() {
        guard wakeCheckTask == nil else { return }
        wakeCheckTask = Task { [weak self] in
            guard let self else { return }
            defer { self.wakeCheckTask = nil }
            do { try await Task.sleep(nanoseconds: 1_000_000_000) }
            catch { return }
            await self.checkLocalServiceAfterWake()
        }
    }

    func exportLocalBackup(to output: URL) async {
        await exportLocalKnowledge(to: output, readable: false)
    }

    func exportReadableKnowledge(to output: URL) async {
        await exportLocalKnowledge(to: output, readable: true)
    }

    private func exportLocalKnowledge(to output: URL, readable: Bool) async {
        guard let runtime = serviceRuntime, connected, !busy, !browseBusy, !statusBusy,
              !recoveringService, !backupBusy, !trashBusy, !tagsBusy, !summaryIndexBusy, !ragBusy, !agentConnectionsBusy, !aiConfigurationPending else {
            backupMessage = "请等待当前操作完成，并确认本地知识库已打开后再导出。"
            return
        }
        backupBusy = true
        busy = true
        wakeCheckTask?.cancel()
        recoveryTask?.cancel()
        let attempt = UUID()
        launchGeneration = attempt
        ownerID = ""
        connectedToken = ""
        backupMessage = readable ? "正在暂停本地服务并导出 Markdown 与原件，请保持客户端打开…" :
            "正在暂停本地服务并导出备份，请保持客户端打开…"
        defer { backupBusy = false; busy = false }
        let service = localService
        do {
            let directories = try service.currentLibrary()
            let serviceOperation = service.operationToken
            _ = try await Task.detached {
                guard service.stopAndWait(expectedGeneration: serviceOperation) != nil else {
                    throw failure("备份导出已取消。")
                }
                if readable {
                    return try LocalBackupTool.exportReadable(runtime: runtime, data: directories.data,
                                                              cache: directories.cache, logs: directories.logs, output: output)
                }
                return try LocalBackupTool.export(runtime: runtime, data: directories.data,
                                                   cache: directories.cache, logs: directories.logs, output: output)
            }.value
            guard launchGeneration == attempt, !Task.isCancelled else { return }
            backupMessage = readable ? "已导出 Markdown 与原件：\(output.lastPathComponent)。此 ZIP 不能用于知识库恢复，包含私人知识，请妥善保管。" :
                "已导出备份：\(output.lastPathComponent)。请妥善保管，其中包含私人知识及文件原件。"
        } catch {
            guard launchGeneration == attempt, !Task.isCancelled else { return }
            backupMessage = "导出失败：\(error.localizedDescription)"
        }
        guard launchGeneration == attempt, !Task.isCancelled else { return }
        await startLocalService()
        if !connected {
            backupMessage += " 本地服务未能重新打开，请关闭设置后手动重试。"
        }
    }

    func restoreLocalBackup(from package: URL) async {
        await switchLocalLibrary(package: package, destination: nil)
    }

    func returnToPreviousLocalLibrary() async {
        guard let previousLocalLibrary else { return }
        await switchLocalLibrary(package: nil, destination: previousLocalLibrary)
    }

    private func bindLocalQueue(_ library: LocalLibraryDescriptor) {
        readingSessionStore?.flush()
        readingSessionStore = ReadingSessionStore(directory: library.data)
        summaryIndexBusy = false
        ragBusy = false
        agentConnectionsBusy = false
        resetLocalAI()
        resetLocalTags()
        loadAIConfiguration()
        loadWebNetworkConfiguration()
        resetLocalTrash()
        restoringQueue = true
        defer { restoringQueue = false }
        queueStore = CaptureQueueStore(root: library.data.appendingPathComponent("capture-queue"))
        queueReadFailed = false
        queueMessage = ""
        draft = ""
        items = []
        domainID = ""
        projectID = ""
        domains = []
        projects = []
        domainName = ""
        projectName = ""
        searchQuery = ""
        searchTagQuery = ""
        supplementDraft = ""
        sourceLoadGeneration = UUID()
        sourceListGeneration = UUID()
        loadingSourceID = ""
        clearKnowledgeCache()
        knowledgeRows = []
        allKnowledgeRows = []
        nextSourceOffset = nil
        selectedSource = nil
        sourceRevisions = []
        selectedEvidence = nil
        pdfViewCache.removeAll()
        pdfDocumentCache.removeAll()
        pdfCacheOrder.removeAll()
        try? FileManager.default.removeItem(at: pdfCacheDirectory)
        do {
            if let snapshot = try queueStore?.load() {
                draft = snapshot.draft
                items = snapshot.items
                domainID = snapshot.domainID
                projectID = snapshot.projectID
            }
        } catch {
            queueReadFailed = true
            queueMessage = "采集队列无法恢复，原文件已保留：\(error.localizedDescription)"
        }
    }

    private func switchLocalLibrary(package: URL?, destination: LocalLibraryDescriptor?) async {
        guard let runtime = serviceRuntime, !busy, !browseBusy, !statusBusy,
              !recoveringService, !backupBusy, !trashBusy, !tagsBusy, !summaryIndexBusy, !ragBusy, !agentConnectionsBusy, !aiConfigurationPending else {
            backupMessage = "请等待当前操作完成后再恢复或切换知识库。"
            return
        }
        if !queueReadFailed && !persistQueue() {
            backupMessage = "当前草稿和采集队列尚未安全保存，暂不能切换知识库。\(queueMessage)"
            return
        }
        let service = localService
        let original = try? service.currentLibrary()
        let previous = previousLocalLibrary
        let operation = UUID()
        backupGeneration = operation
        backupBusy = true
        busy = true
        wakeCheckTask?.cancel()
        recoveryTask?.cancel()
        launchGeneration = UUID()
        sourceLoadGeneration = UUID()
        sourceListGeneration = UUID()
        ownerID = ""
        connectedToken = ""
        backupMessage = package == nil ? "正在打开恢复前的知识库…" : "正在验证并恢复备份，请保持客户端打开…"
        defer { backupBusy = false; busy = false }
        var activated = false
        do {
            let serviceOperation = service.operationToken
            let restored = try await Task.detached {
                guard let stoppedOperation = service.stopAndWait(expectedGeneration: serviceOperation) else {
                    throw failure("知识库恢复已取消。")
                }
                if let package { return try service.prepareRestoredLibrary(runtime: runtime, package: package,
                                                                         expectedGeneration: stoppedOperation) }
                guard let destination else { throw failure("未指定要打开的知识库。") }
                return destination
            }.value
            guard backupGeneration == operation, !Task.isCancelled else { return }
            try service.activateLibrary(restored)
            activated = true
            bindLocalQueue(restored)
            await startLocalService()
            guard backupGeneration == operation, !Task.isCancelled else { return }
            guard connected else { throw failure(authMessage) }
            previousLocalLibrary = package == nil ? nil : original
            backupMessage = package == nil ? "已回到恢复前的知识库，原资料与采集队列已保留。" :
                "已恢复并打开新知识库。原知识库及其草稿、采集队列已保留，可在本次会话中返回。"
        } catch {
            guard backupGeneration == operation, !Task.isCancelled else { return }
            backupMessage = "恢复或切换失败：\(error.localizedDescription)。原知识库已保留。"
            previousLocalLibrary = previous
            if let original {
                let fallbackOperation = service.operationToken
                let stopped = await Task.detached { service.stopAndWait(expectedGeneration: fallbackOperation) }.value
                guard stopped != nil, backupGeneration == operation, !Task.isCancelled else { return }
                if activated {
                    do { try service.activateLibrary(original) }
                    catch { backupMessage += " 活动库设置未能回退，下次启动请检查设置文件。" }
                    bindLocalQueue(original)
                }
                service.selectLibrary(original)
                await startLocalService()
                if !connected { backupMessage += " 原库未能打开，请查看本地服务提示。" }
            } else {
                authMessage = "活动库设置不可用，请从有效备份恢复。"
            }
        }
    }

    func startLocalService(automatic: Bool = false) async {
        guard let runtime = serviceRuntime, !trashBusy else { return }
        resetLocalTrash()
        if !automatic {
            recoveryTask?.cancel()
            restartPolicy = LocalRestartPolicy()
        }
        let attempt = UUID()
        launchGeneration = attempt
        busy = true
        defer { if launchGeneration == attempt { busy = false } }
        do {
            let service = localService
            let serviceOperation = service.operationToken
            let connection = try await Task.detached { try service.start(runtime: runtime, expectedGeneration: serviceOperation) }.value
            guard launchGeneration == attempt && !Task.isCancelled else { return }
            address = connection.address
            token = connection.token
            await connect(localAttempt: attempt)
            if launchGeneration == attempt { loadWebNetworkConfiguration() }
            if launchGeneration == attempt && connected && !service.isRunning {
                localServiceStopped(service: service)
            }
        } catch {
            guard launchGeneration == attempt && !Task.isCancelled else { return }
            authMessage = error.localizedDescription
            message = error.localizedDescription
        }
    }

    func openLocalLibrary() async {
        guard usesLocalService else {
            authMessage = "客户端缺少本地运行包，请使用完整的 \(AppBrand.name) App。"
            message = authMessage
            return
        }
        await startLocalService()
    }

    func connect(localAttempt: UUID? = nil) async {
        guard !trashBusy else { return }
        resetLocalTrash()
        busy = true
        defer { if localAttempt == nil || localAttempt == launchGeneration { busy = false } }
        do {
            let api = try API(address: address, token: token)
            let user = try await api.call("v1/me")
            let result = try await api.call("v1/domains")
            let projectResult = try await api.call("v1/projects")
            let preference = try await api.call("v1/ocr/preferences")
            let webPreference = try await api.call("v1/web/preferences")
            guard localAttempt == nil || localAttempt == launchGeneration else { return }
            guard let id = user["id"] as? String else { throw failure("本地知识库响应无效") }
            domains = (result["items"] as? [[String: Any]] ?? []).compactMap {
                guard let id = $0["id"] as? String, let name = $0["name"] as? String else { return nil }
                return DomainItem(id: id, name: name)
            }
            projects = (projectResult["items"] as? [[String: Any]] ?? []).compactMap {
                guard let id = $0["id"] as? String, let name = $0["name"] as? String else { return nil }
                return ProjectItem(id: id, name: name)
            }
            if !usesLocalService || !domains.contains(where: { $0.id == domainID }) { domainID = domains.first?.id ?? "" }
            if !usesLocalService || !projects.contains(where: { $0.id == projectID }) { projectID = "" }
            ocrModel = preference["model"] as? String ?? "small"
            ocrEngine = preference["engine"] as? String ?? "pdfium"
            webReadEngine = webPreference["engine"] as? String ?? "legacy"
            webCleaningEnabled = webPreference["cleaning_enabled"] as? Bool ?? false
            ownerID = id
            clearKnowledgeCache()
            knowledgeRows = []
            allKnowledgeRows = []
            selectedSource = nil
            sourceRevisions = []
            selectedEvidence = nil
            if let savedDomain = readingSessionStore?.current?.domainID, domains.contains(where: { $0.id == savedDomain }) {
                domainID = savedDomain
            }
            searchDomainID = domainID
            searchProjectID = ""
            connectedAddress = address
            connectedToken = token
            accountName = user["display_name"] as? String ?? "本地用户"
            authMessage = ""
            message = domains.isEmpty ? "本地知识库已打开，可开始采集资料。" : "本地知识库已就绪"
            if usesLocalService { await restoreKernelCaptures() }
        } catch {
            guard localAttempt == nil || localAttempt == launchGeneration else { return }
            ownerID = ""
            clearKnowledgeCache()
            knowledgeRows = []
            allKnowledgeRows = []
            selectedSource = nil
            sourceRevisions = []
            selectedEvidence = nil
            message = error.localizedDescription
            authMessage = error.localizedDescription
        }
    }

    func saveOCRPreference() async {
        guard !trashBusy else { return }
        guard connected else { ocrMessage = "请先连接后台"; return }
        busy = true
        defer { busy = false }
        do {
            let result = try await API(address: address, token: token).call(
                "v1/ocr/preferences", method: "PUT", json: ["model": ocrModel, "engine": ocrEngine])
            ocrModel = result["model"] as? String ?? ocrModel
            ocrEngine = result["engine"] as? String ?? ocrEngine
            ocrMessage = "已保存 PDF 识别设置；新上传和重新扫描时生效。"
        } catch { ocrMessage = error.localizedDescription }
    }

    func saveWebReadPreference() async {
        guard !trashBusy else { return }
        guard connected else { webReadMessage = "请先连接后台"; return }
        busy = true
        defer { busy = false }
        do {
            let result = try await API(address: address, token: token).call(
                "v1/web/preferences", method: "PUT",
                json: ["engine": webReadEngine, "cleaning_enabled": webCleaningEnabled])
            webReadEngine = result["engine"] as? String ?? webReadEngine
            webCleaningEnabled = result["cleaning_enabled"] as? Bool ?? webCleaningEnabled
            webReadMessage = "已保存网页设置；新保存和重新读取时生效。"
        } catch { webReadMessage = error.localizedDescription }
    }

    func createDomain() async {
        guard !trashBusy else { return }
        guard connected else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await API(address: address, token: token).call("v1/domains", method: "POST", json: ["name": domainName])
            guard let id = result["id"] as? String, let name = result["name"] as? String else { throw failure("知识域响应无效") }
            domains.append(DomainItem(id: id, name: name)); domainID = id; domainName = ""
            searchDomainID = id; searchProjectID = ""; projectID = ""
            message = "已创建知识域：\(name)"
        } catch { message = error.localizedDescription }
    }

    func createProject() async {
        guard !trashBusy else { return }
        let name = projectName.trimmingCharacters(in: .whitespacesAndNewlines)
        guard connected, !name.isEmpty else { return }
        busy = true
        defer { busy = false }
        do {
            let result = try await API(address: address, token: token).call(
                "v1/projects", method: "POST", json: ["name": name])
            guard let id = result["id"] as? String, let savedName = result["name"] as? String else {
                throw failure("项目响应无效")
            }
            projects.append(ProjectItem(id: id, name: savedName))
            projectID = id
            projectName = ""
            message = "已创建项目：\(savedName)"
        } catch { message = error.localizedDescription }
    }

    func addDraft() {
        guard !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        guard draft.count <= 102400 else { message = "文本不能超过 102400 字符"; return }
        let text = draft
        let link = standaloneLink(text)
        let kind: CaptureKind = link == nil ? .note : .link
        let title = link?.absoluteString ?? "笔记"
        items.append(CaptureItem(kind: kind, title: title, text: text, file: nil))
        draft = ""
        _ = persistQueue()
    }

    func paste() {
        guard !busy else { return }
        let desktop = DesktopIntegration.shared
        let urls = desktop.clipboardFiles()
        if !urls.isEmpty {
            addFiles(urls)
        } else if let text = desktop.clipboardText() {
            draft += text
        } else { message = "剪贴板中没有文本、链接或文件。暂不支持直接粘贴截图位图。" }
    }

    func chooseFiles() {
        let urls = DesktopIntegration.shared.chooseFiles(extensions: supportedFileExtensions.sorted(), multiple: true)
        if !urls.isEmpty { addFiles(urls) }
    }

    func addFiles(_ urls: [URL]) {
        for url in urls {
            do {
                let values = try url.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey, .isSymbolicLinkKey])
                guard values.isRegularFile == true, values.isSymbolicLink != true else { throw failure("只支持普通文件，请先压缩文件夹") }
                guard supportedFileExtensions.contains(url.pathExtension.lowercased()) else {
                    throw failure("不支持该文件类型；请选择 PDF、Office、OpenDocument、RTF、EPUB、CSV、TXT 或 Markdown")
                }
                guard (values.fileSize ?? Int.max) <= 50 * 1024 * 1024 else { throw failure("单个文件不能超过 50 MB") }
                try FileManager.default.createDirectory(at: staging, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
                let copy: URL
                if let queueStore { copy = try queueStore.stage(url) }
                else {
                    copy = staging.appendingPathComponent(UUID().uuidString)
                    try FileManager.default.copyItem(at: url, to: copy)
                }
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: copy.path)
                var item = CaptureItem(kind: .file, title: url.lastPathComponent, text: nil, file: copy)
                item.fileDigest = try queueStore?.digest(copy)
                items.append(item)
                _ = persistQueue()
                message = "文件已加入待发送列表，原件尚未上传。"
            } catch { message = "\(url.lastPathComponent)：\(error.localizedDescription)" }
        }
    }

    func clearQueue(_ filter: CaptureQueueFilter) {
        guard !busy, filter != .processing else { return }
        let removed = items.filter { $0.queueFilter == filter }
        guard !removed.isEmpty else { return }
        if removed.contains(where: { $0.kernelJobID != nil }) {
            Task { await removeKernelCaptures(removed) }
            return
        }
        let previous = items
        items.removeAll { $0.queueFilter == filter }
        guard persistQueue() else { items = previous; return }
        for item in removed {
            if let queueStore { queueStore.removeAssets(item) }
            else {
                if let file = item.file { try? FileManager.default.removeItem(at: file) }
                if let directory = item.feishuDocument?.imageDirectory {
                    try? FileManager.default.removeItem(at: directory)
                }
            }
        }
    }

    func remove(_ item: CaptureItem) {
        if item.kernelJobID != nil {
            Task { await removeKernelCaptures([item]) }
            return
        }
        let previous = items
        items.removeAll { $0.id == item.id }
        guard persistQueue() else { items = previous; return }
        if let queueStore { queueStore.removeAssets(item) }
        else {
            if let file = item.file { try? FileManager.default.removeItem(at: file) }
            if let directory = item.feishuDocument?.imageDirectory { try? FileManager.default.removeItem(at: directory) }
        }
    }

    func cleanup() {
        readingSessionStore?.flush()
        _ = persistQueue()
        for item in items where queueStore == nil {
            if let directory = item.feishuDocument?.imageDirectory { try? FileManager.default.removeItem(at: directory) }
        }
        pdfViewCache.removeAll()
        pdfDocumentCache.removeAll()
        pdfCacheOrder.removeAll()
        try? FileManager.default.removeItem(at: staging)
    }

    func ensureCaptureDomain() async throws {
        if domains.isEmpty {
            let api = try API(address: address, token: token)
            let result = try await api.call("v1/domains")
            domains = (result["items"] as? [[String: Any]] ?? []).compactMap {
                guard let id = $0["id"] as? String, let name = $0["name"] as? String else { return nil }
                return DomainItem(id: id, name: name)
            }
            if domains.isEmpty {
                let created = try await api.call("v1/domains", method: "POST", json: ["name": "默认知识库"])
                guard let id = created["id"] as? String, !id.isEmpty,
                      let name = created["name"] as? String else { throw failure("创建默认知识库失败：响应无效") }
                domains = [DomainItem(id: id, name: name)]
            }
        }
        if !domains.contains(where: { $0.id == domainID }) {
            domainID = domains.first?.id ?? ""
            if let savedDomain = readingSessionStore?.current?.domainID, domains.contains(where: { $0.id == savedDomain }) {
                domainID = savedDomain
            }
            searchDomainID = domainID
            searchProjectID = ""
            projectID = ""
        }
        guard !domainID.isEmpty else { throw failure("请选择知识库后重试") }
    }

    func send() async {
        if usesLocalService { await sendKernelCaptures(); return }
        await sendCompatibilityCaptures()
    }

    /// Compatibility for explicitly selected external development API sessions.
    /// The bundled local client always submits durable kernel jobs above.
    private func sendCompatibilityCaptures() async {
        guard !trashBusy, !busy else { return }
        guard connected else { message = "请先连接后台"; return }
        addDraft()
        guard persistQueue() else { message = queueMessage; return }
        busy = true
        defer { busy = false }
        do {
            guard items.contains(where: { !$0.sent }) else { return }
            try await ensureCaptureDomain()
            guard persistQueue() else { throw failure(queueMessage) }
            let api = try API(address: address, token: token)
            let destination = usesLocalService ? "local|\(ownerID)" : "\(api.base.absoluteString)|\(ownerID)"
            for index in items.indices where !items[index].sent {
                if let previous = items[index].destination, previous != destination {
                    items[index].status = "账户或后台已变更，请移除后重新添加"; continue
                }
                items[index].destination = destination
                if items[index].domainID?.isEmpty != false { items[index].domainID = domainID }
                if items[index].projectID == nil { items[index].projectID = projectID }
                guard persistQueue() else { throw failure(queueMessage) }
                let item = items[index]
                items[index].status = "正在发送…"
                do {
                    let result: [String: Any]
                    if let sourceID = item.sourceID {
                        result = try await api.call("v1/sources/\(sourceID)")
                    } else if let file = item.file {
                        if let queueStore, let expected = item.fileDigest, try queueStore.digest(file) != expected {
                            throw failure("暂存文件已变更，请移除后重新添加，避免修改重试请求。")
                        }
                        result = try await api.call("v1/file-captures", method: "POST", key: item.id, file: file, query: [
                            URLQueryItem(name: "domain_id", value: item.domainID), URLQueryItem(name: "filename", value: item.title)
                        ], progress: { [weak self] fraction in
                            Task { @MainActor in
                                guard let self, let current = self.items.firstIndex(where: { $0.id == item.id }),
                                      !self.items[current].sent else { return }
                                let percent = min(100, Int(fraction * 20) * 5)
                                self.items[current].status = "上传中 \(percent)%"
                            }
                        })
                    } else {
                        let text = item.text ?? ""
                        let link = item.kind == .link ? standaloneLink(text) : nil
                        if item.kind == .link && link == nil { throw failure("链接格式无效，请重新加入") }
                        var context: [String: Any] = ["domain_id": item.domainID!]
                        if let projectID = item.projectID, !projectID.isEmpty { context["project_id"] = projectID }
                        var payload: [String: Any] = ["capture_type": item.kind == .link ? "url" : "text",
                            "title": item.title,
                            "content": ["text": link == nil ? text : ""], "coverage": link == nil ? "full_text" : "link_only",
                            "context": context, "client": ["kind": "desktop", "version": "0.1.0"]]
                        if let link { payload["source_url"] = link.absoluteString }
                        if let link, FeishuCLI.isFeishuLink(link) {
                            guard !feishu.busy else { throw failure("飞书连接正在处理，请完成后重试导入。") }
                            items[index].status = "正在读取飞书文档…"
                            let document: FeishuDocument
                            if let cached = items[index].feishuDocument { document = cached }
                            else {
                                let cli = try feishu.client()
                                let fetched = try await cli.fetch(link)
                                let archived = try await FeishuImages.archive(fetched, cli: cli) { current, total in
                                    self.items[index].status = "正在获取飞书图片 \(current) / \(total)…"
                                }
                                document = try queueStore?.archive(archived, itemID: item.id) ?? archived
                                items[index].feishuDocument = document
                                guard persistQueue() else { throw failure(queueMessage) }
                                if queueStore != nil, let directory = archived.imageDirectory {
                                    try? FileManager.default.removeItem(at: directory)
                                }
                                items[index].title = document.title
                            }
                            payload["capture_type"] = "feishu_document"
                            payload["title"] = document.title
                            payload["content"] = ["text": FeishuImages.markdown(document)]
                            payload["coverage"] = document.partial || document.images.contains(where: { $0.file == nil })
                                ? "partial_text" : "full_text"
                            payload["feishu_document"] = document.metadata
                            payload["feishu_images"] = document.images.map(\.metadata)
                            items[index].status = "正在保存飞书正文…"
                        }
                        result = try await api.call("v1/captures", method: "POST", json: payload, key: item.id)
                    }
                    guard let sourceID = result["id"] as? String else { throw failure("后台响应缺少资料编号，请重试") }
                    items[index].sourceID = sourceID
                    guard persistQueue() else { throw failure(queueMessage) }
                    if let document = items[index].feishuDocument {
                        let savedIDs = Set((result["images"] as? [[String: Any]] ?? []).compactMap {
                            $0["status"] as? String == "saved" ? $0["id"] as? String : nil
                        })
                        items[index].uploadedImageIDs.formUnion(savedIDs)
                        for image in document.images {
                            guard let file = image.file, !items[index].uploadedImageIDs.contains(image.id) else { continue }
                            items[index].status = "正在保存飞书图片 \(image.ordinal + 1) / \(document.images.count)…"
                            _ = try await api.call("v1/sources/\(sourceID)/images/\(image.id)", method: "PUT", file: file)
                            items[index].uploadedImageIDs.insert(image.id)
                            guard persistQueue() else { throw failure(queueMessage) }
                        }
                        if !document.images.isEmpty {
                            cachedKnowledge.removeValue(forKey: sourceID)
                            knowledgeCacheOrder.removeAll { $0 == sourceID }
                            if selectedSource?.id == sourceID { await openSource(sourceID, force: true) }
                        }
                    }
                    if item.file != nil, let projectID = item.projectID, !projectID.isEmpty {
                        _ = try await api.call("v1/sources/\(sourceID)/context", method: "PATCH",
                                               json: ["project_id": projectID])
                    }
                    items[index].sourceID = sourceID
                    items[index].sent = true
                    if item.file != nil {
                        items[index].status = fileStatus(result)
                    } else {
                        if let document = items[index].feishuDocument {
                            let missing = document.images.filter { $0.file == nil }.count
                            items[index].status = "飞书正文已保存 · 图片 \(document.images.count - missing) / \(document.images.count)"
                                + (missing > 0 ? " · 部分图片未获取" : "") + (document.partial ? " · 正文可能有缺失" : "")
                            if let directory = document.imageDirectory { try? FileManager.default.removeItem(at: directory) }
                        } else if item.kind == .link {
                            items[index].status = "链接已保存 · 等待读取网页"
                        } else {
                            items[index].status = "笔记已保存"
                        }
                    }
                    if queueStore == nil, let file = item.file { try? FileManager.default.removeItem(at: file) }
                } catch { items[index].status = "发送失败：\(error.localizedDescription)" }
                guard persistQueue() else { throw failure(queueMessage) }
            }
            message = "已保存 \(items.filter(\.sent).count) / \(items.count) 条；失败项可点击发送重试。"
        } catch { message = error.localizedDescription }
    }

    private func fileStatus(_ detail: [String: Any]) -> String {
        if let document = detail["document_parse"] as? [String: Any] {
            switch document["status"] as? String {
            case "pending": return "文档已上传 · 等待后台解析"
            case "running": return "文档解析中…"
            case "completed": return "文档已解析 · 可搜索"
            case "failed": return "文档解析失败 · 请在阅读器查看原因"
            default: return "文档已上传 · 等待后台解析"
            }
        }
        guard let ocr = detail["ocr"] as? [String: Any] else { return "原文件已保存 · 未解析" }
        switch ocr["status"] as? String {
        case "pending": return "PDF 已上传 · 等待后台识别"
        case "running": return "PDF 识别中…"
        case "completed":
            return detail["processing_status"] as? String == "usable"
                ? "PDF 已识别 · 可搜索" : "PDF 已处理 · 未识别到文字"
        case "failed": return "PDF 识别失败 · 请在“搜索与证据”查看原因"
        case "cancelled": return "PDF 识别已取消"
        default: return "PDF 已上传 · 等待后台识别"
        }
    }

    /// Capture page owns polling, so leaving it cancels requests and stops animation.
    /// Identity is checked again after every await to avoid updating another library.
    func refreshFileStatuses(quiet: Bool = false, includeFinished: Bool = false) async {
        guard !trashBusy, !backupBusy, !busy, !statusBusy, connected else { return }
        if usesLocalService {
            statusBusy = true
            defer { statusBusy = false }
            await refreshKernelCaptures()
            return
        }
        statusBusy = true
        defer { statusBusy = false }
        do {
            let api = try API(address: address, token: token)
            let destination = usesLocalService ? "local|\(ownerID)" : "\(api.base.absoluteString)|\(ownerID)"
            let candidates = items.filter {
                $0.sent && $0.sourceID != nil && $0.kind != .note && $0.destination == destination &&
                (!quiet || includeFinished || $0.stage.active || $0.status.contains("状态暂不可用"))
            }
            for item in candidates {
                try Task.checkCancellation()
                do {
                    let detail = try await api.call("v1/sources/\(item.sourceID!)", timeout: 12)
                    try Task.checkCancellation()
                    let currentDestination = usesLocalService ? "local|\(ownerID)" : "\(address)|\(ownerID)"
                    guard connected, token == api.token, destination == currentDestination,
                          let index = items.firstIndex(where: { $0.id == item.id && $0.destination == destination }) else { return }
                    if item.kind == .file {
                        items[index].status = fileStatus(detail)
                    } else {
                        switch detail["processing_status"] as? String {
                        case "usable": items[index].status = "网页正文已读取 · 可搜索"
                        case "failed": items[index].status = "网页读取失败 · 原始链接已保留"
                        case "processing": items[index].status = "正在读取网页…"
                        case "pending": items[index].status = "链接已保存 · 等待后台读取网页"
                        default: items[index].status = "链接已保存 · 可在阅读器查看"
                        }
                    }
                } catch {
                    if Task.isCancelled { return }
                    if let index = items.firstIndex(where: { $0.id == item.id && $0.destination == destination }) {
                        items[index].status = "原件已保存 · 状态暂不可用，请刷新重试"
                    }
                    if !quiet { message = error.localizedDescription }
                }
            }
            if !candidates.isEmpty { _ = persistQueue() }
            if !quiet { message = "资料状态已刷新。" }
        } catch {
            if !quiet && !Task.isCancelled { message = error.localizedDescription }
        }
    }
}

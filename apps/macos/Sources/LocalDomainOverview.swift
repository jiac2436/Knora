import Foundation
import Combine

struct LocalDomainPoint: Codable { let text: String; let citations: [String] }
struct LocalDomainResult: Codable {
    let overview: LocalDomainPoint
    let insights: [LocalDomainPoint]
    let limitations: [LocalDomainPoint]
}
struct LocalDomainCoverage: Codable {
    let source_count: Int
    let processed_source_count: Int?
    let cited_source_count: Int?
    let original_text_complete: Bool?
    let excluded: [String: String]?
    var complete: Bool { source_count > 0 && processed_source_count == source_count }
}
struct LocalDomainNode: Codable { let id: String; let kind: String; let name: String; let parent_id: String? }
struct LocalDomainTopic: Codable, Identifiable { let id: String; let name: String; let parent_id: String?; let source_count: Int }
struct LocalDomainSource: Codable, Identifiable { let id: String; let title: String; let revision_id: String? }
struct LocalDomainPage<Item: Codable>: Codable { var items: [Item]; var next_offset: Int? }
struct LocalDomainOverview: Codable {
    let available: Bool
    let id: String?
    let reason: String?
    let result: LocalDomainResult?
    let coverage: LocalDomainCoverage?
    let created_at: String?
}
struct LocalDomainNavigation: Codable {
    let domain_id: String
    let scope: String
    let node: LocalDomainNode
    let overview: LocalDomainOverview
    let index_status: String
    var topics: LocalDomainPage<LocalDomainTopic>
    var sources: LocalDomainPage<LocalDomainSource>
}
struct LocalDomainPolicy: Codable {
    var enabled = true
    var paused = false
    var automatic_updates = false
    var automatic_max_sources = 0
    var automatic_max_steps = 0
    var max_calls = 0
    var max_input_tokens = 0
    var debounce_seconds = 30
    var max_wait_seconds = 300
}
struct LocalDomainBudget: Codable {
    let calls: Int
    let input_token_reservation: Int
    let total_steps: Int
    let reused_batches: Int?
}
struct LocalDomainSchedule: Codable {
    let phase: String
    let automatic_status: String
    let policy: LocalDomainPolicy
    let remaining_calls: Int
    let remaining_input_tokens: Int
    let latest_build: LocalDomainBuild?
}
struct LocalDomainBuildPreview: Codable {
    let preview_hash: String
    let coverage: LocalDomainCoverage
    let calls: Int
    let input_token_reservation: Int
    let total_steps: Int
    let reused_batches: Int?
}
struct LocalDomainBuildStep: Codable, Identifiable {
    let key: String
    let status: String
    let artifact_id: String?
    var id: String { key }
}
struct LocalDomainBuild: Codable, Identifiable {
    let id: String
    let status: String
    let topic_id: String?
    let coverage: LocalDomainCoverage
    let budget: LocalDomainBudget
    let steps: [LocalDomainBuildStep]
    let last_error: String?
}
struct LocalDomainProcessingChild: Codable, Identifiable {
    let artifact_id: String
    let node_id: String
    var id: String { artifact_id }
}
struct LocalDomainProcessing: Codable {
    let available: Bool
    let artifact_id: String?
    let reason: String?
    let result: LocalDomainResult?
    let coverage: LocalDomainCoverage?
    let children: [LocalDomainProcessingChild]?
}

/// Only owner/app scope is available here. Reads never authorize model use.
@MainActor final class LocalDomainOverviewStore: ObservableObject {
    typealias Transport = (API, String, String, [String: Any]?) async throws -> [String: Any]
    @Published private(set) var navigation: LocalDomainNavigation?
    @Published private(set) var schedule: LocalDomainSchedule?
    @Published private(set) var preview: LocalDomainBuildPreview?
    @Published private(set) var build: LocalDomainBuild?
    @Published private(set) var processing: LocalDomainProcessing?
    @Published private(set) var selectedTopicID: String?
    @Published private(set) var selectedSection: LocalDomainSection = .overview
    @Published private(set) var directory: LocalDomainDirectory?
    @Published private(set) var information: LocalDomainInformation?
    @Published private(set) var treeSources: [String: LocalDomainSourcePage] = [:]
    @Published private(set) var treeLoading: Set<String> = []
    @Published private(set) var treeErrors: [String: String] = [:]
    @Published private(set) var directoryLoading = false
    @Published private(set) var informationLoading = false
    @Published private(set) var directoryError: String?
    @Published private(set) var informationError: String?
    @Published private(set) var isLoading = false
    @Published private(set) var isMutating = false
    @Published private(set) var message: String?
    @Published private(set) var error: String?
    @Published private(set) var requiresReconnect = false
    @Published private(set) var pendingRequestID: String?
    private var api: API?
    private var domainID = ""
    private var epoch = UUID()
    private var navigationRead = UUID()
    private var previewRead = UUID()
    private var processingRead = UUID()
    private var scheduleRead = UUID()
    private var directoryRead = UUID()
    private var informationRead = UUID()
    private var navigationCache: [String: LocalDomainNavigation] = [:]
    private var navigationDates: [String: Date] = [:]
    private var navigationReads: [String: UUID] = [:]
    private var navigationTasks: [String: Task<Void, Never>] = [:]
    private var directoryTask: Task<Void, Never>?
    private var informationTask: Task<Void, Never>?
    private var treeTasks: [String: Task<Void, Never>] = [:]
    private var treeReads: [String: UUID] = [:]
    private var treeDates: [String: Date] = [:]
    private var directoryDate: Date?
    private var informationDate: Date?
    private let cacheLifetime: TimeInterval
    private let transport: Transport
    private struct Operation {
        let path: String
        let method: String
        let body: [String: Any]
        let id: String
        let buildResult: Bool
    }
    private var pending: [Operation] = []
    var needsBuildAuthorization: Bool {
        guard let preview, let schedule else { return true }
        return !schedule.policy.enabled || schedule.remaining_calls < preview.calls
            || schedule.remaining_input_tokens < preview.input_token_reservation
    }
    init(transport: Transport? = nil, cacheLifetime: TimeInterval = 20) {
        self.cacheLifetime = cacheLifetime
        self.transport = transport ?? { api, path, method, body in
            try await api.call(path, method: method, json: body)
        }
    }
    func reset() {
        epoch = UUID(); navigationRead = UUID(); previewRead = UUID(); processingRead = UUID()
        api = nil; domainID = ""; selectedTopicID = nil
        navigation = nil; schedule = nil; preview = nil; build = nil; processing = nil
        pending = []; pendingRequestID = nil; isMutating = false; isLoading = false
        error = nil; message = nil; requiresReconnect = false
        clearReadCaches()
    }
    private func clearReadCaches() {
        navigationTasks.values.forEach { $0.cancel() }; navigationTasks = [:]
        directoryTask?.cancel(); directoryTask = nil
        informationTask?.cancel(); informationTask = nil
        treeTasks.values.forEach { $0.cancel() }; treeTasks = [:]
        navigationCache = [:]; navigationDates = [:]; navigationReads = [:]
        treeSources = [:]; treeLoading = []; treeErrors = [:]; treeReads = [:]; treeDates = [:]
        directory = nil; information = nil; directoryDate = nil; informationDate = nil
        directoryLoading = false; informationLoading = false
        directoryError = nil; informationError = nil
        directoryRead = UUID(); informationRead = UUID(); scheduleRead = UUID()
        selectedSection = .overview
    }
    func configure(address: String, token: String, domainID: String) async {
        do {
            let next = try API(address: address, token: token)
            if api?.base != next.base || api?.token != next.token || self.domainID != domainID {
                epoch = UUID(); navigationRead = UUID(); previewRead = UUID(); processingRead = UUID()
                api = next; self.domainID = domainID; selectedTopicID = nil
                navigation = nil; schedule = nil; preview = nil; build = nil; processing = nil
                pending = []; pendingRequestID = nil; isMutating = false; isLoading = false
                error = nil; message = nil; requiresReconnect = false
                clearReadCaches()
            }
            await refresh()
        } catch {
            reset()
            requiresReconnect = true
            self.error = errorText(error)
        }
    }
    private func errorText(_ error: Error, unsupportedEndpoint: Bool = false) -> String {
        let status = (error as NSError).userInfo["HTTPStatus"] as? Int ?? 0
        if unsupportedEndpoint && (status == 404 || status == 405) {
            return "当前后台不支持知识库概览，请更新 API 与 Worker 运行包。"
        }
        return error.localizedDescription
    }
    private var path: String { "/v1/domains/\(domainID)/analysis" }
    private func selection() -> [String: Any] {
        var body: [String: Any] = ["scope": "app"]
        if let selectedTopicID { body["topic_id"] = selectedTopicID }
        return body
    }
    private func decode<T: Decodable>(_ type: T.Type, _ body: [String: Any]) throws -> T {
        try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: body))
    }
    private func fresh(_ date: Date?) -> Bool { date.map { Date().timeIntervalSince($0) < cacheLifetime } ?? false }
    private func navigationKey(_ topicID: String?) -> String { topicID ?? "__root" }
    static func treeKey(topicID: String?, unclassified: Bool = false) -> String {
        unclassified ? "__unclassified" : topicID ?? "__all"
    }
    private func rejectExpiredAccess(_ failure: Error) -> Bool {
        let status = (failure as NSError).userInfo["HTTPStatus"] as? Int
        guard status == 401 || status == 403 else { return false }
        expireAccess()
        return true
    }
    func expireAccess() {
        reset(); requiresReconnect = true
        error = "访问授权已失效，请重新连接本地知识库。"
    }
    func selectSection(_ section: LocalDomainSection) async {
        guard !requiresReconnect else { return }
        selectedSection = section
        switch section {
        case .overview: await loadNavigation(force: false)
        case .topics:
            async let catalog: Void = loadDirectory()
            async let files: Void = refreshLoadedBranches(force: false)
            _ = await (catalog, files)
        case .information: await loadInformation()
        }
    }
    func refreshSelected() async {
        switch selectedSection {
        case .overview: await refresh()
        case .topics:
            async let catalog: Void = loadDirectory(force: true)
            async let files: Void = refreshLoadedBranches()
            _ = await (catalog, files)
        case .information: await loadInformation(force: true)
        }
    }
    func refresh() async {
        guard let api, !domainID.isEmpty else { return }
        let identity = epoch, read = UUID(), root = path, buildID = build?.id
        scheduleRead = read; error = nil
        async let nav: Void = loadNavigation(force: true)
        async let state: Void = refreshSchedule(api: api, root: root, identity: identity, read: read, buildID: buildID)
        _ = await (nav, state)
    }
    private func loadNavigation(force: Bool) async {
        guard let api, !domainID.isEmpty else { return }
        let topicID = selectedTopicID, key = navigationKey(topicID)
        if !force, navigationCache[key] != nil, fresh(navigationDates[key]) { return }
        if let pending = navigationTasks[key] { await pending.value; return }
        let identity = epoch, read = UUID(), root = path, body = selection()
        navigationReads[key] = read
        isLoading = true
        let task = Task { [weak self] in
            guard let self else { return }
            await self.fetchNavigation(api: api, root: root, body: body, topicID: topicID, key: key, identity: identity, read: read)
        }
        navigationTasks[key] = task
        await task.value
        if epoch == identity, navigationReads[key] == read {
            navigationTasks[key] = nil
            isLoading = navigationTasks[navigationKey(selectedTopicID)] != nil
        }
    }
    private func fetchNavigation(api: API, root: String, body: [String: Any], topicID: String?, key: String,
                                 identity: UUID, read: UUID) async {
        do {
            let value = try await transport(api, root + "/navigation", "POST", body)
            guard epoch == identity, navigationReads[key] == read, !Task.isCancelled else { return }
            let result = try decode(LocalDomainNavigation.self, value)
            guard result.domain_id == domainID, result.scope == "app", result.node.id == (topicID ?? domainID) else {
                throw failure("概览响应范围不符，请重新读取")
            }
            navigationCache[key] = result; navigationDates[key] = Date()
            if selectedTopicID == topicID { navigation = result }
        } catch {
            guard epoch == identity, navigationReads[key] == read, !Task.isCancelled else { return }
            if rejectExpiredAccess(error) { return }
            if selectedTopicID == topicID { self.error = errorText(error, unsupportedEndpoint: true) }
        }
    }
    private func refreshSchedule(api: API, root: String, identity: UUID, read: UUID, buildID: String?) async {
        do {
            let value = try await transport(api, root + "/schedule-state", "POST", ["scope": "app"])
            guard epoch == identity, scheduleRead == read else { return }
            let current = try decode(LocalDomainSchedule.self, value)
            schedule = current
            if let latest = current.latest_build { build = latest }
            else if let buildID {
                let job = try await transport(api, root + "/build/jobs/" + buildID, "GET", nil)
                guard epoch == identity, scheduleRead == read else { return }
                build = try decode(LocalDomainBuild.self, job)
            }
        } catch {
            if epoch == identity && scheduleRead == read && !Task.isCancelled {
                if !rejectExpiredAccess(error) { self.error = errorText(error) }
            }
        }
    }

    func selectTopic(_ id: String?) async {
        guard !isMutating, pending.isEmpty else { return }
        selectedTopicID = id; navigation = navigationCache[navigationKey(id)]; preview = nil; processing = nil
        navigationRead = UUID(); isLoading = navigationTasks[navigationKey(id)] != nil; error = nil
        previewRead = UUID(); processingRead = UUID()
        await loadNavigation(force: false)
    }
    func loadMoreTopics() async { await loadMore(topics: true) }
    func loadMoreSources() async { await loadMore(topics: false) }
    private func loadMore(topics: Bool) async {
        guard !isLoading, let api, let current = navigation,
              let offset = topics ? current.topics.next_offset : current.sources.next_offset else { return }
        let identity = epoch, read = navigationRead
        var body = selection(); body[topics ? "topic_offset" : "source_offset"] = offset
        isLoading = true
        defer { if epoch == identity && navigationRead == read { isLoading = false } }
        do {
            let result = try await transport(api, path + "/navigation", "POST", body)
            guard epoch == identity, navigationRead == read else { return }
            let page = try decode(LocalDomainNavigation.self, result)
            if topics {
                let ids = Set(current.topics.items.map(\.id))
                navigation?.topics.items += page.topics.items.filter { !ids.contains($0.id) }
                navigation?.topics.next_offset = page.topics.next_offset
            } else {
                let ids = Set(current.sources.items.map(\.id))
                navigation?.sources.items += page.sources.items.filter { !ids.contains($0.id) }
                navigation?.sources.next_offset = page.sources.next_offset
            }
            if let navigation { navigationCache[navigationKey(selectedTopicID)] = navigation }
        } catch {
            if epoch == identity && navigationRead == read && !Task.isCancelled {
                if !rejectExpiredAccess(error) { self.error = errorText(error) }
            }
        }
    }
    func loadDirectory(force: Bool = false) async {
        guard let api, !domainID.isEmpty else { return }
        if !force, directory != nil, fresh(directoryDate) { return }
        if let pending = directoryTask { await pending.value; return }
        let identity = epoch, read = UUID(), root = path
        directoryRead = read; directoryLoading = true; directoryError = nil
        let task = Task { [weak self] in
            guard let self else { return }
            do {
                var offset = 0, items: [LocalDomainDirectoryTopic] = [], seen: Set<String> = []
                while true {
                    let value = try await self.transport(api, root + "/library", "POST", ["scope": "app", "offset": offset, "limit": 100])
                    guard self.epoch == identity, self.directoryRead == read, !Task.isCancelled else { return }
                    var page = try self.decode(LocalDomainDirectory.self, value)
                    guard page.domain_id == self.domainID, page.scope == "app" else { throw failure("分类目录响应范围不符") }
                    for item in page.items where seen.insert(item.id).inserted { items.append(item) }
                    guard let next = page.next_offset else {
                        page.items = items; self.directory = page; self.directoryDate = Date()
                        return
                    }
                    guard next > offset else { throw failure("分类分页响应无效，请重新读取") }
                    offset = next
                }
            } catch {
                guard self.epoch == identity, self.directoryRead == read, !Task.isCancelled else { return }
                if !self.rejectExpiredAccess(error) { self.directoryError = self.errorText(error, unsupportedEndpoint: true) }
            }
        }
        directoryTask = task; await task.value
        if epoch == identity, directoryRead == read { directoryTask = nil; directoryLoading = false }
    }
    func loadInformation(force: Bool = false) async {
        guard let api, !domainID.isEmpty else { return }
        if !force, information != nil, fresh(informationDate) { return }
        if let pending = informationTask { await pending.value; return }
        let identity = epoch, read = UUID(), root = path
        informationRead = read; informationLoading = true; informationError = nil
        let task = Task { [weak self] in
            guard let self else { return }
            do {
                let value = try await self.transport(api, root + "/information", "POST", ["scope": "app"])
                guard self.epoch == identity, self.informationRead == read, !Task.isCancelled else { return }
                let result = try self.decode(LocalDomainInformation.self, value)
                guard result.domain_id == self.domainID, result.scope == "app" else { throw failure("知识域信息响应范围不符") }
                self.information = result; self.informationDate = Date()
            } catch {
                guard self.epoch == identity, self.informationRead == read, !Task.isCancelled else { return }
                if !self.rejectExpiredAccess(error) { self.informationError = self.errorText(error, unsupportedEndpoint: true) }
            }
        }
        informationTask = task; await task.value
        if epoch == identity, informationRead == read { informationTask = nil; informationLoading = false }
    }
    func loadTreeSources(topicID: String?, unclassified: Bool = false, more: Bool = false, force: Bool = false) async {
        guard let api, !domainID.isEmpty, !(topicID != nil && unclassified) else { return }
        let key = Self.treeKey(topicID: topicID, unclassified: unclassified)
        let previous = treeSources[key]
        if more, previous?.next_offset == nil { return }
        if !force, !more, previous != nil, fresh(treeDates[key]) { return }
        if let pending = treeTasks[key] { await pending.value; return }
        let identity = epoch, read = UUID(), root = path
        treeReads[key] = read; treeLoading.insert(key); treeErrors[key] = nil
        let task = Task { [weak self] in
            guard let self else { return }
            do {
                var offset = more ? previous?.next_offset ?? 0 : 0
                var items = more ? previous?.items ?? [] : []
                var ids = Set(items.map(\.id))
                let keepCount = more ? 0 : max(previous?.items.count ?? 0, 40)
                while true {
                    var body: [String: Any] = ["scope": "app", "unclassified": unclassified, "offset": offset, "limit": 40]
                    if let topicID { body["topic_id"] = topicID }
                    let value = try await self.transport(api, root + "/library/sources", "POST", body)
                    guard self.epoch == identity, self.treeReads[key] == read, !Task.isCancelled else { return }
                    var page = try self.decode(LocalDomainSourcePage.self, value)
                    guard page.domain_id == self.domainID, page.scope == "app", page.topic_id == topicID,
                          page.unclassified == unclassified else { throw failure("分类文件响应范围不符") }
                    for item in page.items where ids.insert(item.id).inserted { items.append(item) }
                    if let next = page.next_offset, next <= offset { throw failure("文件分页响应无效，请重新读取") }
                    if !more, items.count < keepCount, let next = page.next_offset {
                        offset = next; continue
                    }
                    page.items = items; self.treeSources[key] = page; self.treeDates[key] = Date()
                    return
                }
            } catch {
                guard self.epoch == identity, self.treeReads[key] == read, !Task.isCancelled else { return }
                if !self.rejectExpiredAccess(error) { self.treeErrors[key] = self.errorText(error, unsupportedEndpoint: true) }
            }
        }
        treeTasks[key] = task; await task.value
        if epoch == identity, treeReads[key] == read { treeTasks[key] = nil; treeLoading.remove(key) }
    }
    private func refreshLoadedBranches(force: Bool = true) async {
        let keys = Array(treeSources.keys)
        await withTaskGroup(of: Void.self) { group in
            for key in keys {
                group.addTask { @MainActor [weak self] in
                    await self?.loadTreeSources(topicID: key.hasPrefix("__") ? nil : key,
                        unclassified: key == "__unclassified", force: force)
                }
            }
        }
    }
    /// Invalidate freshness, not presentation. Visible rows stay mounted until replacements arrive.
    func invalidateLibrary() {
        navigationDates = [:]; directoryDate = nil; informationDate = nil; treeDates = [:]
        navigationTasks.values.forEach { $0.cancel() }; navigationTasks = [:]; navigationReads = [:]
        directoryTask?.cancel(); directoryTask = nil; directoryRead = UUID(); directoryLoading = false
        informationTask?.cancel(); informationTask = nil; informationRead = UUID(); informationLoading = false
        treeTasks.values.forEach { $0.cancel() }; treeTasks = [:]; treeReads = [:]; treeLoading = []
        navigationRead = UUID(); isLoading = false
    }
    /// Polling only reads metadata. It never creates or retries a model task.
    func refreshProgress() async {
        guard let api, !domainID.isEmpty else { return }
        let identity = epoch, read = UUID(), previousID = build?.id, previousStatus = build?.status
        scheduleRead = read
        await refreshSchedule(api: api, root: path, identity: identity, read: read, buildID: previousID)
        guard epoch == identity, scheduleRead == read else { return }
        if previousID != build?.id || previousStatus != build?.status {
            invalidateLibrary()
            await loadNavigation(force: true)
            if selectedSection != .overview { await refreshSelected() }
        }
    }
    func previewBuild() async {
        guard !isMutating, pending.isEmpty, let api else { return }
        let identity = epoch, read = UUID(); previewRead = read; preview = nil
        do {
            let result = try await transport(api, path + "/build/preview", "POST", selection())
            guard epoch == identity, previewRead == read else { return }
            preview = try decode(LocalDomainBuildPreview.self, result); error = nil
        } catch {
            if epoch == identity && previewRead == read && !Task.isCancelled {
                if !rejectExpiredAccess(error) { self.error = errorText(error) }
            }
        }
    }
    private func policy(automaticUpdates: Bool, calls: Int, tokens: Int, sources: Int, steps: Int) throws -> LocalDomainPolicy {
        guard calls >= 0, tokens >= 0, (0...5000).contains(sources), (0...256).contains(steps),
              !automaticUpdates || (sources > 0 && steps > 0) else { throw failure("请输入有效预算与自动更新范围上限") }
        var value = LocalDomainPolicy()
        value.automatic_updates = automaticUpdates; value.max_calls = calls; value.max_input_tokens = tokens
        value.automatic_max_sources = sources; value.automatic_max_steps = steps
        return value
    }
    private func authorization(_ policy: LocalDomainPolicy) throws -> Operation {
        let id = UUID().uuidString.lowercased()
        let data = try JSONEncoder().encode(policy)
        let body: [String: Any] = ["scope": "app", "authorization_id": id,
            "policy": try JSONSerialization.jsonObject(with: data)]
        return Operation(path: path + "/authorization", method: "PUT", body: body, id: id, buildResult: false)
    }
    private func buildOperation(confirmedUnknown: Bool) throws -> Operation {
        guard let preview else { throw failure("请先预览本次加工范围与预算") }
        guard schedule?.phase != "unknown" || confirmedUnknown else { throw failure("上次结果未知，请明确确认可能重复产生用量") }
        let id = UUID().uuidString.lowercased()
        var body = selection(); body["request_id"] = id; body["preview_hash"] = preview.preview_hash
        body["confirmed_unknown"] = confirmedUnknown
        return Operation(path: path + "/build/jobs", method: "POST", body: body, id: id, buildResult: true)
    }
    func createBuild(confirmedUnknown: Bool = false) async {
        guard canMutate else { return }
        do { pending = [try buildOperation(confirmedUnknown: confirmedUnknown)]; await runPending() }
        catch { self.error = errorText(error) }
    }
    func authorizeAndBuild(confirmed: Bool, automaticUpdates: Bool = false, maxCalls: Int? = nil,
                           maxInputTokens: Int? = nil, maxSources: Int? = nil, maxSteps: Int? = nil) async {
        guard canMutate else { return }
        do {
            guard confirmed, let preview else { throw failure("请明确确认新的预算授权周期") }
            guard schedule?.phase != "unknown" else { throw failure("结果未知时不能重置预算，请先处理原请求") }
            guard !automaticUpdates || (maxSources != nil && maxSteps != nil) else {
                throw failure("开启自动更新前，请明确设置每次资料数与步骤数上限")
            }
            let value = try policy(automaticUpdates: automaticUpdates, calls: maxCalls ?? preview.calls,
                tokens: maxInputTokens ?? preview.input_token_reservation,
                sources: maxSources ?? 0, steps: maxSteps ?? 0)
            guard value.max_calls >= preview.calls, value.max_input_tokens >= preview.input_token_reservation else {
                throw failure("授权预算不足以执行本次预览")
            }
            pending = [try authorization(value), try buildOperation(confirmedUnknown: false)]
            await runPending()
        } catch { self.error = errorText(error) }
    }
    func saveAuthorization(confirmed: Bool, automaticUpdates: Bool, maxCalls: Int, maxInputTokens: Int,
                           maxSources: Int, maxSteps: Int) async {
        guard canMutate else { return }
        do {
            guard confirmed else { throw failure("更改策略将开启新的预算周期，请明确确认") }
            guard schedule?.phase != "unknown" else { throw failure("结果未知时不能重置授权预算") }
            let value = try policy(automaticUpdates: automaticUpdates, calls: maxCalls, tokens: maxInputTokens,
                                   sources: maxSources, steps: maxSteps)
            pending = [try authorization(value)]; await runPending()
        } catch { self.error = errorText(error) }
    }
    private var canMutate: Bool { !isMutating && pending.isEmpty && api != nil && !domainID.isEmpty }
    func setPaused(_ paused: Bool) async {
        guard canMutate else { return }
        pending = [Operation(path: path + "/pause", method: "PATCH", body: ["scope": "app", "paused": paused],
                             id: UUID().uuidString, buildResult: false)]
        await runPending()
    }
    func rebuildIndex() async {
        guard canMutate else { return }
        pending = [Operation(path: path + "/index/rebuild", method: "POST", body: selection(),
                             id: UUID().uuidString, buildResult: false)]
        await runPending()
    }
    func retryPendingMutation() async { if !isMutating { await runPending() } }
    private func runPending() async {
        guard let api, !pending.isEmpty else { return }
        let identity = epoch
        isMutating = true; error = nil
        defer { if epoch == identity { isMutating = false } }
        while let operation = pending.first {
            pendingRequestID = operation.id
            do {
                let value = try await transport(api, operation.path, operation.method, operation.body)
                guard epoch == identity else { return }
                if operation.buildResult { build = try decode(LocalDomainBuild.self, value); preview = nil }
                pending.removeFirst()
            } catch {
                guard epoch == identity else { return }
                if rejectExpiredAccess(error) { return }
                self.error = errorText(error)
                let status = (error as NSError).userInfo["HTTPStatus"] as? Int ?? 0
                if (400..<500).contains(status) {
                    pending = []; pendingRequestID = nil
                    if status == 409 { preview = nil }
                } else { self.error = "请求结果尚未确认。请重试原请求，不会另建任务。\n" + error.localizedDescription }
                return
            }
        }
        pendingRequestID = nil; message = "操作已提交，可继续浏览。"
        invalidateLibrary()
        await refresh()
        if selectedSection != .overview { await refreshSelected() }
    }
    func readProcessing(_ artifactID: String) async {
        guard let api else { return }
        let identity = epoch, read = UUID(); processingRead = read; processing = nil
        do {
            let value = try await transport(api, path + "/processing-nodes/" + artifactID, "POST", ["scope": "app"])
            guard epoch == identity, processingRead == read else { return }
            processing = try decode(LocalDomainProcessing.self, value)
        } catch {
            if epoch == identity && processingRead == read && !Task.isCancelled {
                if !rejectExpiredAccess(error) { self.error = errorText(error) }
            }
        }
    }
}

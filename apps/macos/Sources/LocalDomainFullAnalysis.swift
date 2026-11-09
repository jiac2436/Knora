import Foundation
import Combine
import CryptoKit

struct LocalDomainFullDocument: Codable, Identifiable {
    let source_id: String
    let title: String
    let status: String
    let job_id: String?
    let last_error: String?
    var id: String { source_id }
}
struct LocalDomainFullBlockedSource: Codable, Identifiable {
    let source_id: String?
    let title: String
    let reason: String
    var id: String { source_id ?? "scope-\(title)-\(reason)" }
}
struct LocalDomainFullPreview: Codable {
    let preview_hash: String
    let total_sources: Int
    let reused_sources: Int
    let analyze_sources: Int
    var stale_sources: Int? = nil
    var skipped_sources: Int? = nil
    var reanalyze: Bool? = nil
    let blocked_sources: [LocalDomainFullBlockedSource]
    let calls: Int
    let input_token_reservation: Int
    let documents: [LocalDomainFullDocument]?
    let requires_unknown_confirmation: Bool?
    let configured: Bool?
    let budget_basis: String?
}
struct LocalDomainFullBudget: Codable {
    let calls: Int
    let input_token_reservation: Int
    let used_calls: Int
    let used_input_tokens: Int
    let actual_input_tokens: Int?
}
struct LocalDomainFullOverviewProgress: Codable {
    let completed_steps: Int
    let total_steps: Int
    let current_step_title: String?
}
struct LocalDomainFullTokenUsage: Codable {
    let input_tokens: Int
    let output_tokens: Int
    let total_tokens: Int
}
struct LocalDomainFullJob: Codable, Identifiable {
    let id: String
    let request_id: String?
    let status: String
    let phase: String
    let total_sources: Int
    let reused_sources: Int
    let analyzed_sources: Int
    var stale_sources: Int? = nil
    var skipped_sources: Int? = nil
    var reanalyze: Bool? = nil
    var token_usage: LocalDomainFullTokenUsage? = nil
    let completed_sources: Int
    let current_source_title: String?
    let build_id: String?
    let overview_progress: LocalDomainFullOverviewProgress?
    let last_error: String?
    let created_at: String
    let completed_at: String?
    let budget: LocalDomainFullBudget
    let documents: [LocalDomainFullDocument]
    var isActive: Bool { ["pending", "running", "cancelling"].contains(status) }
    var resultsKey: String { "\(id)|\(status)|\(phase)|\(completed_sources)|\(build_id ?? "")" }
}

/// Reads and polling never authorize model use. A lost create reply retains its original idempotent request.
@MainActor final class LocalDomainFullAnalysisStore: ObservableObject {
    typealias Transport = (API, String, String, [String: Any]?) async throws -> [String: Any]
    @Published private(set) var reanalyze = false
    @Published private(set) var preview: LocalDomainFullPreview?
    @Published private(set) var job: LocalDomainFullJob?
    @Published private(set) var isReading = false
    @Published private(set) var isPreviewing = false
    @Published private(set) var isMutating = false
    @Published private(set) var readError: String?
    @Published private(set) var previewError: String?
    @Published private(set) var mutationError: String?
    @Published private(set) var requiresReconnect = false
    @Published private(set) var pendingRequestID: String?
    private struct Pending: Codable {
        let requestID: String
        let previewHash: String
        let confirmedUnknown: Bool
        var reanalyze: Bool? = nil
    }
    private struct Latest: Decodable { let job: LocalDomainFullJob? }
    private let transport: Transport
    private let defaults: UserDefaults?
    private let cacheLifetime: TimeInterval
    private var api: API?
    private var domainID = ""
    private var epoch = UUID()
    private var readID = UUID()
    private var previewID = UUID()
    private var readTask: Task<Void, Never>?
    private var previewTask: Task<Void, Never>?
    private var readDate: Date?
    private var pending: Pending?
    private var pendingKey: String?

    init(transport: Transport? = nil, defaults: UserDefaults? = .standard, cacheLifetime: TimeInterval = 3) {
        self.transport = transport ?? { api, path, method, body in try await api.call(path, method: method, json: body) }
        self.defaults = defaults; self.cacheLifetime = cacheLifetime
    }
    func reset() {
        epoch = UUID(); readID = UUID(); previewID = UUID()
        readTask?.cancel(); readTask = nil; previewTask?.cancel(); previewTask = nil
        api = nil; domainID = ""; readDate = nil; pending = nil; pendingKey = nil
        reanalyze = false; preview = nil; job = nil; isReading = false; isPreviewing = false; isMutating = false
        readError = nil; previewError = nil; mutationError = nil; pendingRequestID = nil; requiresReconnect = false
    }
    func configure(address: String, token: String, domainID: String) async {
        do {
            let next = try API(address: address, token: token)
            if api?.base != next.base || api?.token != next.token || self.domainID != domainID || requiresReconnect {
                reset(); api = next; self.domainID = domainID
                let scope = "\(next.base.absoluteString)|\(next.token)|\(domainID)"
                let digest = SHA256.hash(data: Data(scope.utf8)).map { String(format: "%02x", $0) }.joined()
                pendingKey = "local-domain-full-pending.\(digest)"
                if let key = pendingKey, let data = defaults?.data(forKey: key), let saved = try? JSONDecoder().decode(Pending.self, from: data) {
                    pending = saved; pendingRequestID = saved.requestID
                }
            }
            await refreshLatest()
        } catch { reset(); readError = error.localizedDescription }
    }
    func refreshLatest(force: Bool = false) async {
        guard let api, !domainID.isEmpty, !requiresReconnect, !isMutating else { return }
        if let task = readTask { await task.value; return }
        if !force, let readDate, Date().timeIntervalSince(readDate) < cacheLifetime { return }
        let current = epoch, request = UUID(), path = basePath + "/latest"
        readID = request; isReading = true; readError = nil
        let task = Task { [weak self] in
            guard let self else { return }
            defer { if self.epoch == current && self.readID == request { self.isReading = false; self.readTask = nil } }
            do {
                let raw = try await self.transport(api, path, "GET", nil)
                let result: Latest = try self.decode(raw)
                guard self.epoch == current, self.readID == request, !Task.isCancelled else { return }
                self.job = result.job; self.readDate = Date()
                if let job = result.job { self.confirmPending(job) }
            } catch {
                guard self.epoch == current, self.readID == request, !Task.isCancelled else { return }
                self.receive(error, target: .read)
            }
        }
        readTask = task; await task.value
    }
    func refreshJob() async {
        guard !isMutating else { return }
        guard let api, let id = job?.id, !requiresReconnect else { await refreshLatest(force: true); return }
        if let task = readTask { await task.value; return }
        let current = epoch, request = UUID(), path = basePath + "/jobs/\(id)"
        readID = request; isReading = true; readError = nil
        let task = Task { [weak self] in
            guard let self else { return }
            defer { if self.epoch == current && self.readID == request { self.isReading = false; self.readTask = nil } }
            do {
                let raw = try await self.transport(api, path, "GET", nil)
                let result: LocalDomainFullJob = try self.decode(raw)
                guard self.epoch == current, self.readID == request, !Task.isCancelled else { return }
                self.job = result; self.readDate = Date(); self.confirmPending(result)
            } catch {
                guard self.epoch == current, self.readID == request, !Task.isCancelled else { return }
                self.receive(error, target: .read)
            }
        }
        readTask = task; await task.value
    }
    func setReanalyze(_ value: Bool) async {
        guard !isMutating, pending == nil, job?.isActive != true, !requiresReconnect else { return }
        guard reanalyze != value else { return }
        reanalyze = value
        previewID = UUID(); previewTask?.cancel(); previewTask = nil
        preview = nil; isPreviewing = false
        await loadPreview()
    }
    func loadPreview() async {
        guard let api, !requiresReconnect, !isMutating, pending == nil, job?.isActive != true else { return }
        if let task = previewTask { await task.value; return }
        let current = epoch, request = UUID(), path = basePath + "/preview"
        let selectedMode = reanalyze
        previewID = request; isPreviewing = true; previewError = nil; mutationError = nil
        let task = Task { [weak self] in
            guard let self else { return }
            defer { if self.epoch == current && self.previewID == request { self.isPreviewing = false; self.previewTask = nil } }
            do {
                let raw = try await self.transport(api, path, "POST", ["scope": "app", "reanalyze": selectedMode])
                let result: LocalDomainFullPreview = try self.decode(raw)
                guard self.epoch == current, self.previewID == request, !Task.isCancelled else { return }
                self.preview = result
            } catch {
                guard self.epoch == current, self.previewID == request, !Task.isCancelled else { return }
                self.receive(error, target: .preview)
            }
        }
        previewTask = task; await task.value
    }
    func create(confirmed: Bool, confirmedUnknown: Bool = false) async {
        guard confirmed, !requiresReconnect, !isMutating, !isPreviewing, pending == nil, job?.isActive != true,
              let preview, (preview.reanalyze ?? false) == reanalyze, preview.blocked_sources.isEmpty, preview.total_sources > 0,
              !(preview.configured == false && preview.calls > 0), readError == nil, previewError == nil else { return }
        guard (job?.status != "unknown" && preview.requires_unknown_confirmation != true) || confirmedUnknown else {
            mutationError = "上次任务结果未知，请先核对已产生的结果，再明确确认新的模型调用。"; return
        }
        let operation = Pending(requestID: UUID().uuidString.lowercased(), previewHash: preview.preview_hash, confirmedUnknown: confirmedUnknown, reanalyze: reanalyze)
        savePending(operation)
        await submit(operation)
    }
    func restorePendingRequest() async {
        guard let pending, !requiresReconnect, !isMutating else { return }
        await submit(pending)
    }
    func cancel() async {
        guard let api, let job, job.isActive, !isMutating, !requiresReconnect else { return }
        let current = epoch, path = basePath + "/jobs/\(job.id)/cancel"
        isMutating = true; mutationError = nil
        readID = UUID(); readTask?.cancel(); readTask = nil; isReading = false
        defer { if epoch == current { isMutating = false } }
        do {
            let raw = try await transport(api, path, "POST", ["scope": "app"])
            let result: LocalDomainFullJob = try decode(raw)
            guard epoch == current else { return }
            self.job = result; readDate = Date()
        } catch { guard epoch == current else { return }; receive(error, target: .mutation) }
    }
    private func submit(_ operation: Pending) async {
        guard let api else { return }
        let current = epoch, path = basePath + "/jobs"
        isMutating = true; mutationError = nil
        // Supersede pre-create reads so a delayed latest response cannot erase this newly created task.
        readID = UUID(); readTask?.cancel(); readTask = nil; isReading = false
        defer { if epoch == current { isMutating = false } }
        do {
            let raw = try await transport(api, path, "POST", ["request_id": operation.requestID,
                "preview_hash": operation.previewHash, "confirmed": true, "confirmed_unknown": operation.confirmedUnknown, "reanalyze": operation.reanalyze ?? false])
            let result: LocalDomainFullJob = try decode(raw)
            guard epoch == current else { return }
            job = result; preview = nil; readDate = Date(); confirmPending(result)
        } catch {
            guard epoch == current else { return }
            let status = (error as NSError).userInfo["HTTPStatus"] as? Int ?? 0
            if status >= 400 && status < 500 && ![401, 403].contains(status) { savePending(nil); preview = nil }
            receive(error, target: .mutation)
            if !requiresReconnect && pending != nil { mutationError = "创建响应尚未确认。请刷新查看最新任务，或手动恢复原请求确认；不会自动重发或另建任务。" }
        }
    }
    private var basePath: String { "/v1/domains/\(domainID)/analysis/full" }
    private enum ErrorTarget { case read, preview, mutation }
    private func receive(_ error: Error, target: ErrorTarget) {
        let status = (error as NSError).userInfo["HTTPStatus"] as? Int ?? 0
        if status == 401 || status == 403 {
            reset(); requiresReconnect = true; readError = "连接凭证已失效，请重新连接后读取知识域分析。"; return
        }
        switch target {
        case .read: readError = error.localizedDescription
        case .preview: previewError = error.localizedDescription
        case .mutation: mutationError = error.localizedDescription
        }
    }
    private func decode<T: Decodable>(_ raw: [String: Any]) throws -> T {
        try JSONDecoder().decode(T.self, from: JSONSerialization.data(withJSONObject: raw))
    }
    private func confirmPending(_ job: LocalDomainFullJob) {
        if let pending, job.id == pending.requestID || job.request_id == pending.requestID { savePending(nil) }
    }
    private func savePending(_ value: Pending?) {
        pending = value; pendingRequestID = value?.requestID
        guard let key = pendingKey else { return }
        if let value, let data = try? JSONEncoder().encode(value) { defaults?.set(data, forKey: key) }
        else { defaults?.removeObject(forKey: key) }
    }
}

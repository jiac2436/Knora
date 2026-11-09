import Foundation
import SwiftUI

struct LocalRAGSession: Decodable, Identifiable {
    let id: String
    let title: String
    let created_at: String
}

struct LocalRAGCitation: Decodable, Identifiable {
    let evidence_id: String
    let source_id: String
    let revision_id: String
    let title: String
    let locator_kind: String
    let locator_start: Int
    let locator_end: Int
    let stale: Bool
    var id: String { evidence_id }
}

struct LocalRAGTurn: Decodable, Identifiable {
    let id: String
    let session_id: String
    let question: String
    let status: String
    let last_error: String?
    let answer: String?
    let citations: [LocalRAGCitation]
    let insufficient_evidence: Bool
    let warnings: [String]
    let scope: LocalRAGScope?
    var isActive: Bool { status == "pending" || status == "running" }
    var statusText: String {
        switch status {
        case "pending": "等待回答"
        case "running": "正在检索和生成回答"
        case "succeeded": insufficient_evidence ? "资料不足" : "回答完成"
        case "failed": "回答失败"
        case "unknown": "结果未知；新建请求可能重复消耗额度"
        case "cancelled": "已取消"
        default: status
        }
    }
}

struct LocalRAGList<Item: Decodable>: Decodable { let items: [Item] }
struct LocalRAGConfiguration: Decodable { let configured: Bool }

struct LocalRAGScope: Decodable, Equatable {
    let domain_ids: [String]
    let topic_id: String?
}

struct LocalRAGRequest {
    let sessionID: String
    let question: String
    let tagQuery: String
    let key: String
    var domainID: String? = nil
    var topicID: String? = nil
    var body: [String: Any] {
        var value: [String: Any] = ["session_id": sessionID, "question": question, "confirm_send": true]
        if !tagQuery.isEmpty { value["tag_query"] = tagQuery }
        if let domainID { value["domain_ids"] = [domainID] }
        if let topicID { value["topic_id"] = topicID }
        return value
    }
    func matches(sessionID: String, question: String, tagQuery: String, domainID: String? = nil, topicID: String? = nil) -> Bool {
        self.sessionID == sessionID && self.question == question && self.tagQuery == tagQuery &&
        self.domainID == domainID && self.topicID == topicID
    }
}

/// Reads and submissions share the existing authenticated local API. No provider credential lives here.
struct LocalRAGAPI {
    let api: API
    init(address: String, token: String) throws { api = try API(address: address, token: token) }
    private func scopeQuery(domainID: String?, topicID: String?) -> [URLQueryItem] {
        var query: [URLQueryItem] = []
        if let domainID { query.append(URLQueryItem(name: "domain_id", value: domainID)) }
        if let topicID { query.append(URLQueryItem(name: "topic_id", value: topicID)) }
        return query
    }
    func sessions(domainID: String? = nil, topicID: String? = nil) async throws -> [LocalRAGSession] {
        try decodeLocalAI(LocalRAGList<LocalRAGSession>.self, body: await api.call("v1/rag/sessions", query: scopeQuery(domainID: domainID, topicID: topicID))).items
    }
    func configuration() async throws -> Bool {
        try decodeLocalAI(LocalRAGConfiguration.self, body: await api.call("v1/rag/config")).configured
    }
    func createSession(title: String) async throws -> LocalRAGSession {
        try decodeLocalAI(LocalRAGSession.self, body: await api.call("v1/rag/sessions", method: "POST", json: ["title": title]))
    }
    func turns(sessionID: String, domainID: String? = nil, topicID: String? = nil) async throws -> [LocalRAGTurn] {
        try decodeLocalAI(LocalRAGList<LocalRAGTurn>.self, body: await api.call("v1/rag/turns", query:
            [URLQueryItem(name: "session_id", value: sessionID)] + scopeQuery(domainID: domainID, topicID: topicID))).items
    }
    func submit(_ request: LocalRAGRequest) async throws -> LocalRAGTurn {
        try decodeLocalAI(LocalRAGTurn.self, body: await api.call("v1/rag/turns", method: "POST", json: request.body, key: request.key))
    }
}

private struct LocalRAGConnection: Equatable {
    let address: String
    let token: String
    let connected: Bool
    let library: LocalLibraryDescriptor?
    let domainID: String?
    let topicID: String?
}

struct LocalRAGView: View {
    @ObservedObject var model: CaptureModel
    var domainID: String? = nil
    var topicID: String? = nil
    var scopeTitle: String? = nil
    @Environment(\.dismiss) private var dismiss
    @State private var sessions: [LocalRAGSession] = []
    @State private var selectedSessionID: String?
    @State private var automaticallyCreatedSessionID: String?
    @State private var turns: [LocalRAGTurn] = []
    @State private var question = ""
    @State private var tagQuery = ""
    @State private var configured = false
    @State private var configurationLoaded = false
    @State private var loading = true
    @State private var submitting = false
    @State private var message = ""
    @State private var confirmSend = false
    @State private var request: LocalRAGRequest?
    @State private var readGeneration = UUID()
    @State private var cachedTurns: [String: [LocalRAGTurn]] = [:]

    private var connection: LocalRAGConnection {
        LocalRAGConnection(address: model.address, token: model.token, connected: model.connected,
                           library: try? model.localService.currentLibrary(), domainID: domainID, topicID: topicID)
    }
    private var serviceAvailable: Bool { model.usesLocalService && model.connected && !model.recoveringService }
    private var writeAllowed: Bool {
        serviceAvailable && !model.busy && !model.backupBusy && !model.trashBusy &&
        !model.aiBusy && !model.tagsBusy && !model.summaryIndexBusy && !model.ragBusy && !model.agentConnectionsBusy && !submitting && !loading
    }
    private var trimmedQuestion: String { question.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var trimmedTag: String { tagQuery.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var scopeDescription: String {
        if let scopeTitle, !scopeTitle.isEmpty { return scopeTitle }
        if topicID != nil { return "当前主题及其子分类" }
        return domainID == nil ? "全部知识域" : "当前知识域"
    }
    private var validScope: Bool { topicID == nil || domainID != nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(domainID == nil ? "知识库问答" : "\(scopeDescription) · 问答").font(.title2)
                Spacer()
                Button("完成") { dismiss() }.disabled(submitting)
            }
            Text("检索在本机完成。确认提问后，问题和检索到的原文将发送给配置的 AI 网关，可能消耗模型额度。问答历史保存在本地；每次提问独立检索，历史回答不作为事实依据。")
                .font(.callout).foregroundStyle(.secondary)
            Text("检索范围：\(scopeDescription)\(topicID == nil ? "" : "（包含子分类，资料去重）")")
                .font(.callout).foregroundStyle(.secondary)
            HStack(alignment: .bottom) {
                InboxSelect("本地会话", selection: $selectedSessionID, options:
                    [InboxSelectOption<String?>(nil, "新会话")] + sessions.map {
                        InboxSelectOption<String?>($0.id, $0.title)
                    })
                .disabled(loading || submitting)
                Button("刷新") { Task { await refresh() } }.disabled(!serviceAvailable || loading || submitting)
                if loading || submitting { ProgressView().controlSize(.small) }
            }
            if configurationLoaded && !configured { Text("尚未配置 AI 网关，请先在 AI 设置中完成配置。刷新后可提问。").font(.caption).foregroundStyle(.secondary) }
            if !message.isEmpty { Text(message).font(.callout).foregroundStyle(.secondary).textSelection(.enabled) }
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 18) {
                    if turns.isEmpty && !loading { Text("当前范围尚无问答记录。可输入一个问题，查找本地原文依据。").foregroundStyle(.secondary) }
                    ForEach(turns) { turn in turnView(turn) }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
            TextField("可选：按正式标签名或别名筛选资料", text: $tagQuery).disabled(submitting)
            TextEditor(text: $question).font(.body).frame(height: 90).border(Color.secondary.opacity(0.3))
                .disabled(submitting).accessibilityLabel("知识库问题")
            HStack {
                Text("待处理任务请点击刷新查看结果；不会自动重新调用模型。").font(.caption).foregroundStyle(.secondary)
                Spacer()
                Button("提问…") { confirmSend = true }
                    .disabled(!writeAllowed || !configured || !validScope || trimmedQuestion.isEmpty || trimmedQuestion.count > 2000 || trimmedTag.count > 120 || turns.contains(where: \.isActive))
            }
        }
        .padding(20).frame(minWidth: 760, minHeight: 620)
        .task(id: connection) {
            reset()
            await refresh()
        }
        .onChange(of: selectedSessionID) { _, _ in
            if let created = automaticallyCreatedSessionID, created == selectedSessionID {
                automaticallyCreatedSessionID = nil
                return
            }
            guard !submitting else { return }
            readGeneration = UUID()
            turns = selectedSessionID.flatMap { cachedTurns[$0] } ?? []; request = nil; message = ""
            loading = serviceAvailable
            Task { await refresh() }
        }
        .onDisappear { readGeneration = UUID() }
        .confirmationDialog("发送问题及检索原文给 AI 网关？", isPresented: $confirmSend, titleVisibility: .visible) {
            Button("确认发送并提问") { Task { await submit() } }
            Button("取消", role: .cancel) {}
        } message: {
            Text("检索范围：\(scopeDescription)\(topicID == nil ? "" : "（包含子分类，资料去重）")。仅发送此范围内检索到的原文，可能消耗模型额度。若没有可用资料，将直接提示资料不足，不调用模型。结果未知时应先检查历史，再决定是否创建新请求。")
        }
    }

    private func turnView(_ turn: LocalRAGTurn) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(turn.question).font(.headline).textSelection(.enabled)
            Text(turn.statusText).font(.caption).foregroundStyle(.secondary)
            if let answer = turn.answer, !answer.isEmpty { Text(answer).textSelection(.enabled) }
            if turn.insufficient_evidence { Text("现有资料不足以支持完整回答，请补充资料或调整问题。").font(.caption).foregroundStyle(.secondary) }
            if let error = turn.last_error { Text(error).font(.caption).foregroundStyle(.secondary).textSelection(.enabled) }
            ForEach(Array(turn.warnings.enumerated()), id: \.offset) { _, warning in
                Text(warning).font(.caption).foregroundStyle(.secondary)
            }
            ForEach(Array(turn.citations.enumerated()), id: \.offset) { index, citation in
                VStack(alignment: .leading, spacing: 3) {
                    Button("来源 [\(index + 1)] · \(citation.title) · \(citation.locator_kind) \(citation.locator_start)–\(citation.locator_end)") {
                        Task { await openCitation(citation) }
                    }.disabled(citation.stale || !writeAllowed)
                    if citation.stale { Text("原资料已修改、删除或移出检索范围；该引用仅属于历史回答，不可作为当前知识。").font(.caption).foregroundStyle(.secondary) }
                }
            }
            Divider()
        }
    }

    @MainActor private func reset() {
        readGeneration = UUID(); sessions = []; selectedSessionID = nil; automaticallyCreatedSessionID = nil; turns = []
        question = ""; tagQuery = ""; configured = false; configurationLoaded = false; loading = serviceAvailable; submitting = false
        message = ""; request = nil; confirmSend = false
        cachedTurns = [:]
    }
    @MainActor private func isCurrent(_ original: LocalRAGConnection) -> Bool {
        !Task.isCancelled && connection == original && serviceAvailable
    }
    @MainActor private func refresh() async {
        guard serviceAvailable, !submitting else { return }
        let original = connection
        let generation = UUID(); readGeneration = generation
        let sessionID = selectedSessionID
        loading = true
        defer { if connection == original && readGeneration == generation { loading = false } }
        do {
            let client = try LocalRAGAPI(address: original.address, token: original.token)
            async let sessionRead = client.sessions(domainID: original.domainID, topicID: original.topicID)
            async let configurationRead = client.configuration()
            let loadedTurns: [LocalRAGTurn]
            if let sessionID { loadedTurns = try await client.turns(sessionID: sessionID, domainID: original.domainID, topicID: original.topicID) }
            else { loadedTurns = [] }
            let (loadedSessions, ready) = try await (sessionRead, configurationRead)
            guard isCurrent(original), generation == readGeneration, sessionID == selectedSessionID else { return }
            sessions = loadedSessions; configured = ready; configurationLoaded = true; turns = loadedTurns; message = ""
            if let sessionID { cachedTurns[sessionID] = loadedTurns }
        } catch {
            guard isCurrent(original), generation == readGeneration, sessionID == selectedSessionID else { return }
            message = error.localizedDescription
        }
    }
    @MainActor private func submit() async {
        guard writeAllowed, configured, validScope, !trimmedQuestion.isEmpty, trimmedQuestion.count <= 2000,
              trimmedTag.count <= 120, !turns.contains(where: \.isActive) else { return }
        let original = connection
        let query = trimmedQuestion; let tag = trimmedTag
        submitting = true; model.ragBusy = true; readGeneration = UUID()
        defer {
            model.ragBusy = false
            if connection == original { submitting = false }
        }
        do {
            let client = try LocalRAGAPI(address: original.address, token: original.token)
            var sessionID = selectedSessionID
            if sessionID == nil {
                let session = try await client.createSession(title: String(query.prefix(80)))
                guard isCurrent(original) else { return }
                sessions.insert(session, at: 0); automaticallyCreatedSessionID = session.id
                selectedSessionID = session.id; sessionID = session.id
            }
            guard let sessionID, isCurrent(original) else { return }
            if request?.matches(sessionID: sessionID, question: query, tagQuery: tag, domainID: original.domainID, topicID: original.topicID) != true {
                request = LocalRAGRequest(sessionID: sessionID, question: query, tagQuery: tag, key: UUID().uuidString,
                    domainID: original.domainID, topicID: original.topicID)
            }
            guard let request else { return }
            let turn = try await client.submit(request)
            guard isCurrent(original), selectedSessionID == sessionID else { return }
            turns.removeAll { $0.id == turn.id }; turns.append(turn)
            cachedTurns[sessionID] = turns
            question = ""; self.request = nil
            message = turn.isActive ? "任务已创建，请稍后点击刷新查看结果。" : "任务状态：\(turn.statusText)"
        } catch {
            guard isCurrent(original) else { return }
            message = "\(error.localizedDescription)\n提交结果可能未确认，请先刷新历史。再次发送相同会话、问题和筛选条件时，将沿用同一请求标识；不会自动重试。"
        }
    }
    @MainActor private func openCitation(_ citation: LocalRAGCitation) async {
        guard !citation.stale, writeAllowed else { return }
        let original = connection
        loading = true
        defer { if connection == original { loading = false } }
        do {
            let value = try await API(address: original.address, token: original.token).call("v1/evidence/\(citation.evidence_id)")
            guard let evidence = EvidenceDetail(value), isCurrent(original) else { return }
            guard !evidence.hasNewer else {
                message = "原资料已有新版本，请刷新问答历史；此引用仅属于历史回答。"
                return
            }
            await model.openSource(citation.source_id, force: true)
            guard isCurrent(original), model.selectedSource?.id == citation.source_id else { return }
            if var cached = model.cachedKnowledge[citation.source_id] {
                cached.evidence[citation.evidence_id] = evidence
                model.cachedKnowledge[citation.source_id] = cached
            }
            await model.openEvidence(citation.evidence_id)
        } catch {
            if isCurrent(original) { message = error.localizedDescription }
            return
        }
        guard isCurrent(original), model.selectedEvidence?.id == citation.evidence_id else { return }
        guard model.selectedEvidence?.hasNewer != true else {
            message = "原资料已有新版本，请刷新问答历史；此引用仅属于历史回答。"
            return
        }
        dismiss()
    }
}


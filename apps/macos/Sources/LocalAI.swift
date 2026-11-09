import Foundation
import SwiftUI

/// Each library has its own gateway address and Keychain access credential.
/// Direct model keys are managed separately by LocalModelConfiguration.
enum LocalAIConfiguration {
    static func tokenService(_ library: LocalLibraryDescriptor) -> String { library.credentialService + ".ai-gateway" }
    static func addressKey(_ library: LocalLibraryDescriptor) -> String { tokenService(library) + ".url" }
    static func address(_ library: LocalLibraryDescriptor) -> String {
        UserDefaults.standard.string(forKey: addressKey(library)) ?? ""
    }
    static func hasCredential(_ library: LocalLibraryDescriptor) -> Bool {
        if case .available = KeychainCredentialStore().readWithoutInteraction(service: tokenService(library)) { return true }
        return false
    }
    static func validatedAddress(_ value: String) throws -> String {
        let value = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !value.isEmpty else { return "" }
        let api = try API(address: value, token: "")
        guard (api.base.port ?? 1) > 0, (api.base.port ?? 1) <= 65535 else {
            throw failure("AI 网关端口无效。")
        }
        return api.base.absoluteString
    }
    static func environment(_ library: LocalLibraryDescriptor) throws -> [String: String] {
        // Credentials travel exclusively through LocalHostStartup's private pipe.
        [:]
    }
    static func save(address: String, replacementToken: String, clear: Bool,
                     library: LocalLibraryDescriptor, credentials: any CredentialStore = KeychainCredentialStore()) throws {
        let url = try validatedAddress(address)
        if clear || url.isEmpty {
            try LocalModelConfiguration.beginUpdate(library, mode: "external", replacedCredential: true)
            try credentials.remove(service: tokenService(library))
            UserDefaults.standard.removeObject(forKey: addressKey(library))
            try LocalModelConfiguration.finishUpdate(library, mode: "external")
            return
        }
        let token = replacementToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard token.utf8.allSatisfy({ $0 > 32 && $0 < 127 }), token.count <= 4096 else {
            throw failure("网关访问凭证格式无效。")
        }
        if token.isEmpty {
            guard case .available = credentials.read(service: tokenService(library)) else { throw failure("网关访问凭证不可用，请检查钥匙串权限或重新填写。") }
        }
        try LocalModelConfiguration.beginUpdate(library, mode: "external", replacedCredential: !token.isEmpty)
        if !token.isEmpty { try credentials.write(token, service: tokenService(library)) }
        UserDefaults.standard.set(url, forKey: addressKey(library))
        try LocalModelConfiguration.finishUpdate(library, mode: "external")
    }
}

struct LocalAIPlan: Decodable {
    let plan_hash: String
    let calls: Int
    let sections: Int
    let coverage: String
    let covered_chars: Int
    let total_chars: Int
    let source_coverage: String
    let image_count: Int?
    var scopeText: String {
        "本次覆盖已解析证据约 \(covered_chars) / \(total_chars) 字符，\(coverage == "full" ? "已解析证据全部覆盖" : "仅覆盖部分证据")。原资料采集范围：\(source_coverage)。" + ((image_count ?? 0) > 0 ? "联合分析 \(image_count ?? 0) 张原图。" : "")
    }
}
struct LocalAIJob: Decodable, Identifiable {
    let id: String
    let source_id: String
    let kind: String
    let status: String
    let last_error: String?
    let artifact_id: String?
    let completed_steps: Int?
    let total_steps: Int?
    let can_resume: Bool?
    let contract_version: Int?
    let pipeline_status: String?
    let index_state: String?
    let index_retryable: Bool?
    var isActive: Bool { ["pending", "running"].contains(status) || ["analyzing", "indexing"].contains(pipeline_status ?? "") }
    var statusText: String {
        switch pipeline_status {
        case "completed": return "分析完成，已更新知识检索"
        case "indexing": return "正在更新知识检索"
        case "index_failed": return "分析已保存，检索更新失败"
        case "stale": return "资料已变更，请重新分析"
        default: break
        }
        switch status {
        case "pending": return "等待分析"
        case "running": return "正在分析"
        case "succeeded", "completed": return contract_version == 3 ? "分析已保存，等待确认检索状态" : "历史分析完成"
        case "failed": return "分析失败"
        case "cancelled": return "已取消"
        case "unknown": return "结果未知，请先检查后再创建新任务，可能重复计费"
        default: return status
        }
    }
}
struct LocalAIArtifact: Decodable, Identifiable {
    struct Insight: Decodable { let text: String; let citations: [String] }
    struct Suggestion: Decodable {
        let domain_id: String?; let project_ids: [String]
        let reason: String; let citations: [String]
    }
    struct Result: Decodable {
        let summary: String; let summary_citations: [String]; let insights: [Insight]
        let organizing_suggestion: Suggestion?; let warnings: [String]
        let contract_version: Int?
        let overview: Insight?
        let limitations: [Insight]?
    }
    struct TagCandidate: Decodable, Identifiable {
        let id: String
        let tag_id: String?
        let name: String
        let dimension: String
        let reason: String
        let citations: [String]
        let status: String
        let stale: Bool
    }
    struct Scope: Decodable {
        let coverage: String
        let source_coverage: String
        let covered_chars: Int
        let total_chars: Int
        let synthesis_evidence_coverage: String?
        let image_count: Int?
    }
    struct Evidence: Decodable, Identifiable {
        let id: String
        let text: String
        let locator_kind: String
        let locator_start: Int
        let locator_end: Int
        let original_evidence_id: String?
        let text_start: Int?
        let text_end: Int?
        let image_id: String?
    }
    let id: String
    let source_id: String
    let result: Result
    let evidence: [Evidence]
    let provider: String
    let model: String
    let stale: Bool
    let applied_at: String?
    let tag_suggestions: [TagCandidate]?
    let phase: String?
    let coverage: String?
    let section_index: Int?
    let analysis_plan: Scope?
    let pipeline_status: String?
    let index_state: String?
    let index_retryable: Bool?
}
func decodeLocalAI<T: Decodable>(_ type: T.Type, body: Any) throws -> T {
    try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: body))
}

@MainActor extension CaptureModel {
    func loadAIConfiguration() {
        guard let library = try? localService.currentLibrary() else { return }
        aiGatewayAddress = LocalAIConfiguration.address(library)
        aiTokenSaved = LocalAIConfiguration.hasCredential(library)
    }
    func refreshAIConfigurationState() async {
        guard connected, !aiConfigurationPending else { return }
        let endpoint = address, credential = token
        do {
            let state = try await API(address: endpoint, token: credential).call("v1/ai/configuration/state")
            guard connected, address == endpoint, token == credential, !aiConfigurationPending else { return }
            let profile = state["profile"] as? [String: Any]
            let direct = profile?["mode"] as? String == "direct"
            let messages = ["ready": "本机模型配置已就绪，尚未通过真实调用验证；保存配置不会调用模型。",
                "missing": "尚未配置模型密钥，AI 暂不可用；阅读、本地检索和备份可正常使用。",
                "denied": direct ? "旧模型密钥无法静默读取，请重新填写 API Key 后保存；基础功能可继续使用。" : "网关凭证需要授权，请重新填写凭证或允许钥匙串访问；基础功能可继续使用。",
                "unavailable": direct ? "模型密钥暂不可用，请重新填写 API Key 后保存；基础功能可继续使用。" : "网关凭证暂不可用，请重新填写凭证或完成钥匙串授权；基础功能可继续使用。",
                "invalid": "模型配置无效或上次保存未完成，请检查后重新保存；基础功能可继续使用。",
                "disabled": "模型已停用；阅读、本地检索和备份可正常使用。"]
            if let status = profile?["status"] as? String { aiMessage = messages[status] ?? "模型配置状态未知，请重新检查。" }
        } catch {
            if address == endpoint && token == credential { aiMessage = "无法读取模型配置状态，请检查本地服务连接。" }
        }
    }
    func resetLocalAI() {
        aiGeneration = UUID()
        aiReadGeneration = UUID()
        aiJobs = []; aiArtifacts = []; aiReadStatus = nil; aiBusy = false
        aiMessage = "分析将自动生成简短总结、AI 标签并更新本地知识检索，原文和人工标签保留。"
    }
    func saveAIConfiguration(replacementToken: String, clear: Bool = false, directOptions: LocalModelOptions? = nil, clearDirect: Bool = false) async {
        guard usesLocalService, !busy, !backupBusy, !trashBusy, !browseBusy, !recoveringService, !aiBusy, !tagsBusy, !summaryIndexBusy && !ragBusy && !agentConnectionsBusy && !aiConfigurationPending else {
            aiMessage = "请等待当前操作完成后保存 AI 配置。"; return
        }
        aiConfigurationPending = true
        defer { aiConfigurationPending = false }
        var prepared: (api: API, id: String)?
        var saved = false
        do {
            let library = try localService.currentLibrary()
            let serviceOperation = localService.operationToken
            if connected {
                let api = try API(address: address, token: token)
                let state = try await api.call("v1/ai/configuration/prepare", method: "POST")
                guard let identity = state["switch_id"] as? String else { throw failure("后台未返回配置切换状态，配置未保存。") }
                prepared = (api, identity)
            } else if localService.isRunning {
                throw failure("请恢复本地服务连接后再修改模型配置，避免中断后台任务。")
            }
            guard try localService.currentLibrary() == library, localService.operationToken == serviceOperation else {
                throw failure("知识库已切换，配置未保存。")
            }
            if clearDirect {
                try LocalModelConfiguration.clear(library)
            } else if let directOptions {
                try LocalModelConfiguration.save(directOptions, replacementKey: replacementToken, library: library)
            } else {
                try LocalAIConfiguration.save(address: aiGatewayAddress, replacementToken: replacementToken,
                                              clear: clear, library: library)
            }
            saved = true
            if let prepared {
                while true {
                    let state = try await prepared.api.call("v1/ai/configuration/state")
                    guard try localService.currentLibrary() == library, localService.operationToken == serviceOperation,
                          state["switch_id"] as? String == prepared.id else { throw failure("配置已保存，服务状态发生变化；将在下次安全启动时应用。") }
                    if state["can_apply"] as? Bool == true { break }
                    let tasks = state["tasks"] as? [String: Int] ?? [:]
                    aiMessage = "配置已保存，等待后台完成：执行中 \(tasks["running"] ?? 0) 项、排队 \(tasks["pending"] ?? 0) 项。完成后自动应用；原任务继续使用原配置。"
                    try await Task.sleep(nanoseconds: 2_000_000_000)
                }
            }
            let service = localService
            guard await Task.detached(operation: { service.stopAndWait(expectedGeneration: serviceOperation) }).value != nil else {
                throw failure("配置已保存，本地服务状态已变化，请重新打开知识库。")
            }
            resetLocalAI(); loadAIConfiguration()
            cancelLocalServiceStart()
            let generation = aiGeneration
            await startLocalService()
            guard aiGeneration == generation, try localService.currentLibrary() == library,
                  !Task.isCancelled else { return }
            aiMessage = connected ? "本机模型配置已应用；尚未通过真实调用验证。可在资料阅读器中打开 AI 分析。" : "配置已保存，本地服务尚未恢复，请重试连接。"
        } catch {
            if !saved, let prepared {
                _ = try? await prepared.api.call("v1/ai/configuration/cancel", method: "POST", json: ["switch_id": prepared.id])
            }
            aiMessage = saved ? "配置已保存，尚未应用：" + error.localizedDescription : error.localizedDescription
        }
    }
    private func aiRequestCurrent(_ generation: UUID, source: String, address: String, token: String, sourceGeneration: UUID? = nil) -> Bool {
        aiGeneration == generation && connected && selectedSource?.id == source && (sourceGeneration == nil || sourceLoadGeneration == sourceGeneration) && self.address == address && self.token == token && !Task.isCancelled
    }
    @discardableResult
    func refreshLocalAI(sourceID: String) async -> Bool {
        guard connected, selectedSource?.id == sourceID, !backupBusy, !trashBusy else { return false }
        let generation = aiGeneration, endpoint = address, credential = token
        let sourceGeneration = sourceLoadGeneration
        if aiSourceReadPhase != .ready {
            aiReadStatus = AIReadStatus(sourceID: sourceID, sourceGeneration: sourceGeneration, phase: .loading)
        }
        let readGeneration = UUID()
        aiReadGeneration = readGeneration
        do {
            let api = try API(address: endpoint, token: credential)
            let jobs = try await api.call("v1/ai/jobs", query: [URLQueryItem(name: "source_id", value: sourceID)])
            let artifacts = try await api.call("v1/ai/artifacts", query: [URLQueryItem(name: "source_id", value: sourceID)])
            guard aiRequestCurrent(generation, source: sourceID, address: endpoint, token: credential, sourceGeneration: sourceGeneration) else { return false }
            guard aiReadGeneration == readGeneration else { return false }
            let loadedJobs = try decodeLocalAI([LocalAIJob].self, body: jobs["items"] ?? [])
            let loadedArtifacts = try decodeLocalAI([LocalAIArtifact].self, body: artifacts["items"] ?? [])
            let previousArtifacts = Set(aiArtifacts.map(\.id))
            aiJobs = loadedJobs
            aiArtifacts = loadedArtifacts
            aiReadStatus = AIReadStatus(sourceID: sourceID, sourceGeneration: sourceGeneration, phase: .ready)
            if aiArtifacts.contains(where: { !previousArtifacts.contains($0.id) && $0.result.contract_version == 3 && !$0.stale && $0.phase != "segment" }) {
                await refreshLocalTags(source: true)
            }
            return true
        } catch {
            guard aiRequestCurrent(generation, source: sourceID, address: endpoint, token: credential, sourceGeneration: sourceGeneration) else { return false }
            guard aiReadGeneration == readGeneration else { return false }
            if aiSourceReadPhase != .ready {
                aiReadStatus = AIReadStatus(sourceID: sourceID, sourceGeneration: sourceGeneration, phase: .failed)
            }
            aiMessage = "无法读取 AI 任务，请检查本地服务连接。"
            return false
        }
    }
    func previewLocalAI(kind: String) async -> LocalAIPlan? {
        guard usesLocalService, connected, let sourceID = selectedSource?.id, !aiBusy, !backupBusy,
              !trashBusy, !busy, !recoveringService, !tagsBusy, !summaryIndexBusy && !ragBusy && !agentConnectionsBusy else { return nil }
        let generation = aiGeneration, endpoint = address, credential = token, sourceGeneration = sourceLoadGeneration
        aiBusy = true
        defer { if aiGeneration == generation { aiBusy = false } }
        do {
            let body = try await API(address: endpoint, token: credential).call("v1/ai/plan", method: "POST",
                json: ["source_id": sourceID, "kind": "analyze", "contract_version": 3])
            guard aiRequestCurrent(generation, source: sourceID, address: endpoint, token: credential, sourceGeneration: sourceGeneration) else { return nil }
            return try decodeLocalAI(LocalAIPlan.self, body: body)
        } catch {
            guard aiRequestCurrent(generation, source: sourceID, address: endpoint, token: credential, sourceGeneration: sourceGeneration) else { return nil }
            aiMessage = "无法预览分析范围，请检查资料处理状态和 AI 网关配置。"
            return nil
        }
    }
    func createLocalAI(kind: String, planHash: String? = nil, expectedSourceID: String? = nil) async {
        guard usesLocalService, connected, let sourceID = selectedSource?.id, !aiBusy, !backupBusy, !trashBusy, !busy, !recoveringService, !tagsBusy, !summaryIndexBusy && !ragBusy && !agentConnectionsBusy else { return }
        guard expectedSourceID == nil || expectedSourceID == sourceID else { return }
        guard !aiJobs.contains(where: { $0.source_id == sourceID && $0.isActive }) else {
            aiMessage = "当前资料已有分析任务，请查看进度。"; return
        }
        let generation = aiGeneration, endpoint = address, credential = token, sourceGeneration = sourceLoadGeneration
        aiBusy = true
        defer { if aiGeneration == generation { aiBusy = false } }
        do {
            let api = try API(address: endpoint, token: credential)
            let config = try await api.call("v1/ai/config")
            guard aiRequestCurrent(generation, source: sourceID, address: endpoint, token: credential, sourceGeneration: sourceGeneration) else { return }
            guard config["configured"] as? Bool == true else { aiMessage = "请先在设置 → AI 分析中配置 AI 网关。"; return }
            var body: [String: Any] = ["source_id": sourceID, "kind": "analyze", "contract_version": 3, "request_key": UUID().uuidString.lowercased()]
            if let planHash { body["plan_hash"] = planHash }
            _ = try await api.call("v1/ai/jobs", method: "POST", json: body)
            guard aiRequestCurrent(generation, source: sourceID, address: endpoint, token: credential, sourceGeneration: sourceGeneration) else { return }
            aiMessage = "任务已创建，正在生成总结与标签；完成后自动更新本地知识检索。"
            await refreshLocalAI(sourceID: sourceID)
        } catch {
            guard aiRequestCurrent(generation, source: sourceID, address: endpoint, token: credential, sourceGeneration: sourceGeneration) else { return }
            aiMessage = "无法确认任务是否创建，请先刷新任务列表。检查网关配置与资料解析状态后再重试。"
        }
    }
    func controlLocalAI(_ job: LocalAIJob, cancel: Bool) async {
        guard usesLocalService, connected, !aiBusy, !busy, !backupBusy, !trashBusy, !tagsBusy,
              !recoveringService, selectedSource?.id == job.source_id else { return }
        let generation = aiGeneration, endpoint = address, credential = token
        aiBusy = true
        aiReadGeneration = UUID()
        defer { if aiGeneration == generation { aiBusy = false } }
        do {
            _ = try await API(address: endpoint, token: credential).call("v1/ai/jobs/\(job.id)/\(cancel ? "cancel" : "resume")", method: "POST")
            guard aiRequestCurrent(generation, source: job.source_id, address: endpoint, token: credential) else { return }
            aiMessage = cancel ? "已停止后续分析，已发出的请求可能产生用量；已完成分段保留在本机。" : "继续处理已授权的任务，已完成调用不会重发。"
            await refreshLocalAI(sourceID: job.source_id)
        } catch {
            guard aiRequestCurrent(generation, source: job.source_id, address: endpoint, token: credential) else { return }
            aiMessage = "无法确认操作结果，请刷新任务列表；结果未知的请求不能自动重发。"
            await refreshLocalAI(sourceID: job.source_id)
        }
    }
    func applyLocalAI(_ artifact: LocalAIArtifact) async {
        guard usesLocalService, connected, !busy, !recoveringService, !aiBusy, !artifact.stale, artifact.applied_at == nil,
              selectedSource?.id == artifact.source_id, !backupBusy, !trashBusy, !tagsBusy, !summaryIndexBusy && !ragBusy && !agentConnectionsBusy else { return }
        let generation = aiGeneration, endpoint = address, credential = token
        aiBusy = true
        defer { if aiGeneration == generation { aiBusy = false } }
        do {
            _ = try await API(address: endpoint, token: credential).call("v1/ai/artifacts/\(artifact.id)/apply", method: "POST")
            guard aiRequestCurrent(generation, source: artifact.source_id, address: endpoint, token: credential) else { return }
            aiMessage = "已应用分类和项目建议。"
            await refreshLocalAI(sourceID: artifact.source_id)
            await openSource(artifact.source_id, force: true)
        } catch {
            guard aiRequestCurrent(generation, source: artifact.source_id, address: endpoint, token: credential) else { return }
            aiMessage = "无法应用建议。资料可能已变更，请刷新后重新分析。"
            await refreshLocalAI(sourceID: artifact.source_id)
        }
    }

    func decideAITag(_ candidate: LocalAIArtifact.TagCandidate, artifact: LocalAIArtifact, accept: Bool) async {
        guard usesLocalService, connected, !busy, !aiBusy, !tagsBusy, !backupBusy, !trashBusy,
              !recoveringService, selectedSource?.id == artifact.source_id,
              candidate.status == "pending", !candidate.stale, !artifact.stale else { return }
        let generation = aiGeneration, endpoint = address, credential = token
        aiBusy = true
        aiReadGeneration = UUID()
        defer { if aiGeneration == generation { aiBusy = false } }
        do {
            _ = try await API(address: endpoint, token: credential).call(
                "v1/ai/tag-suggestions/\(candidate.id)/\(accept ? "accept" : "reject")", method: "POST")
            guard aiRequestCurrent(generation, source: artifact.source_id, address: endpoint, token: credential) else { return }
            aiMessage = accept ? "AI 标签已确认，标注来源仍为 AI。" : "已拒绝，同一正文版本不会重复建议此标签。"
            await refreshLocalAI(sourceID: artifact.source_id)
            await refreshLocalTags(source: true)
        } catch {
            guard aiRequestCurrent(generation, source: artifact.source_id, address: endpoint, token: credential) else { return }
            aiMessage = "无法确认标签操作结果，请刷新；资料或标签目录可能已变化。"
            await refreshLocalAI(sourceID: artifact.source_id)
        }
    }

    func allowAITagSuggestionsAgain() async {
        guard usesLocalService, connected, let sourceID = selectedSource?.id, !busy, !aiBusy, !tagsBusy,
              !backupBusy, !trashBusy, !recoveringService, !summaryIndexBusy && !ragBusy && !agentConnectionsBusy else { return }
        let generation = aiGeneration, endpoint = address, credential = token
        aiBusy = true
        aiReadGeneration = UUID()
        defer { if aiGeneration == generation { aiBusy = false } }
        do {
            _ = try await API(address: endpoint, token: credential).call("v1/sources/\(sourceID)/ai-tag-rejections", method: "DELETE")
            guard aiRequestCurrent(generation, source: sourceID, address: endpoint, token: credential) else { return }
            aiMessage = "已清除当前版本的拒绝记录；再次手动分析时可重新建议，可能消耗模型用量。"
        } catch {
            guard aiRequestCurrent(generation, source: sourceID, address: endpoint, token: credential) else { return }
            aiMessage = "无法清除拒绝记录，请刷新后重试。"
        }
    }
}

struct LocalAISettingsView: View {
    @ObservedObject var model: CaptureModel
    @State private var gatewayToken = ""
    @State private var apiKey = ""
    @State private var direct = true
    @State private var options = LocalModelOptions()
    @State private var keySaved = false

    private var unavailable: Bool {
        model.busy || model.backupBusy || model.trashBusy || model.aiBusy || model.tagsBusy ||
        model.summaryIndexBusy || model.ragBusy || model.agentConnectionsBusy || model.recoveringService || model.browseBusy
        || model.aiConfigurationPending
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            configurationField("连接方式") {
                SettingsTabs(title: "连接方式", selection: $direct, options: [
                    InboxSelectOption(true, "直接配置模型"),
                    InboxSelectOption(false, "外部 AI 网关（高级）")
                ])
            }
            if direct {
                configurationField("接口类型") {
                    InboxSelect("接口类型", selection: $options.provider, options: [
                        InboxSelectOption("deepseek", "DeepSeek", symbol: "sparkles"),
                        InboxSelectOption("openai-compatible", "OpenAI 兼容（Chat Completions）", symbol: "network")
                    ], showsLabel: false)
                }
                configurationField("API 基础地址（高级）", hint: "DeepSeek 默认地址无需更改；兼容接口填写服务商地址，不包含 /chat/completions。") {
                    TextField("https://api.example.com/v1", text: $options.baseURL)
                        .accessibilityLabel("API 基础地址")
                }
                configurationField("文字模型", hint: "必填，使用服务商提供的模型名称。") {
                    TextField("模型名称", text: $options.model)
                        .accessibilityLabel("文字模型")
                }
                configurationField("图片模型", hint: "可选，需要支持图片输入。") {
                    TextField("图片模型名称", text: $options.visionModel)
                        .accessibilityLabel("图片模型")
                }
                configurationField("API Key", hint: "密钥保存在本机私有文件，不随知识库备份导出。更换服务时需重新填写。") {
                    SecureField(keySaved ? "已保存，留空保持" : "输入 API Key", text: $apiKey)
                        .accessibilityLabel("API Key")
                }
                Text("无需手动启动网关。分析时，所选资料及相关上下文会发送到模型服务；兼容接口需支持 JSON 输出。保存配置不会调用模型。")
                    .font(.system(size: 12)).foregroundStyle(InboxStyle.muted)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 10) {
                    Button("保存并应用") {
                        let secret = apiKey; apiKey = ""
                        Task { await model.saveAIConfiguration(replacementToken: secret, directOptions: options); reload() }
                    }
                    .buttonStyle(InboxButtonStyle(.primary))
                    Button("移除模型配置", role: .destructive) {
                        apiKey = ""
                        Task { await model.saveAIConfiguration(replacementToken: "", clearDirect: true); reload() }
                    }
                    .buttonStyle(InboxButtonStyle(.quiet))
                }.disabled(unavailable)
            } else {
                Text("连接独立 AI 网关，模型和厂商密钥由网关管理。")
                    .font(.system(size: 12)).foregroundStyle(InboxStyle.muted)
                configurationField("网关地址", hint: "使用 HTTPS；本机开发可使用 http://127.0.0.1:端口。") {
                    TextField("https://gateway.example.com", text: $model.aiGatewayAddress)
                        .accessibilityLabel("网关地址")
                }
                configurationField("网关访问凭证") {
                    SecureField(model.aiTokenSaved ? "已保存，留空保持" : "输入访问凭证", text: $gatewayToken)
                        .accessibilityLabel("网关访问凭证")
                }
                HStack(spacing: 10) {
                    Button("保存并应用") { let value = gatewayToken; gatewayToken = ""; Task { await model.saveAIConfiguration(replacementToken: value) } }
                        .buttonStyle(InboxButtonStyle(.primary))
                    Button("移除网关配置") { gatewayToken = ""; Task { await model.saveAIConfiguration(replacementToken: "", clear: true) } }
                        .buttonStyle(InboxButtonStyle(.quiet))
                }.disabled(unavailable)
            }
            Text("配置保存在当前知识库的本机设备上。后台任务会继续使用原配置，全部完成后再自动应用新配置；结果未知的请求不会自动重发。")
                .font(.system(size: 12)).foregroundStyle(InboxStyle.muted)
                .fixedSize(horizontal: false, vertical: true)
            if !model.aiMessage.isEmpty {
                Text(model.aiMessage).font(.callout).foregroundStyle(InboxStyle.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .textFieldStyle(InboxFieldStyle())
        .task {
            model.loadAIConfiguration(); reload()
            if let library = try? model.localService.currentLibrary() {
                direct = LocalModelConfiguration.isDirect(library) || LocalAIConfiguration.address(library).isEmpty
            }
            await model.refreshAIConfigurationState()
        }
    }

    private func configurationField<Content: View>(_ title: String, hint: String? = nil,
                                                    @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 7) {
            Text(title).font(.system(size: 12, weight: .medium)).foregroundStyle(InboxStyle.text)
            content()
            if let hint {
                Text(hint).font(.system(size: 11)).foregroundStyle(InboxStyle.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func reload() {
        guard let library = try? model.localService.currentLibrary() else { return }
        options = LocalModelConfiguration.load(library)
        keySaved = LocalModelConfiguration.hasCredential(library)
    }
}

struct LocalAIView: View {
    @ObservedObject var model: CaptureModel
    @Environment(\.dismiss) private var dismiss
    @State private var pendingApply: LocalAIArtifact?
    @State private var confirmApply = false
    @State private var overviewExpanded = false
    @State private var pendingCancel: LocalAIJob?
    @State private var confirmCancel = false
    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack { Text("AI 分析").font(.title2); Spacer(); Button("完成") { dismiss() } }
            Text(model.selectedSource?.title ?? "未选择资料").font(.headline)
            HStack {
                LocalAIAnalyzeButton(model: model, showOverview: { overviewExpanded.toggle() }, showDetails: {})
                Button("刷新状态") { if let id = model.selectedSource?.id { Task { await model.refreshLocalAI(sourceID: id) } } }
            }
            Text(model.aiMessage).font(.callout).foregroundStyle(.secondary)
            ScrollView {
                VStack(alignment: .leading, spacing: 18) {
                    LocalAIResultCard(model: model, isExpanded: $overviewExpanded)
                        .zIndex(10)
                    if let job = model.aiJobs.first(where: { $0.source_id == model.selectedSource?.id }) {
                        VStack(alignment: .leading, spacing: 8) {
                            Text(job.statusText).font(.callout)
                            if job.status == "pending" || job.status == "running" {
                                Button("停止后续分析…") { pendingCancel = job; confirmCancel = true }.disabled(model.aiBusy)
                            }
                            if let error = job.last_error, !error.isEmpty { Text(error).font(.caption).foregroundStyle(.secondary) }
                            if job.pipeline_status == "index_failed" && job.index_retryable != false {
                                Button("重试本地检索更新") {
                                    Task {
                                        if let message = await model.rebuildSummarySearchIndex(sourceID: job.source_id) { model.aiMessage = message }
                                        await model.refreshLocalAI(sourceID: job.source_id)
                                    }
                                }.disabled(model.aiBusy || model.summaryIndexBusy)
                            }
                            if job.can_resume == true {
                                Button("继续未发送部分") { Task { await model.controlLocalAI(job, cancel: false) } }.disabled(model.aiBusy)
                            }
                            DisclosureGroup("调用进度") {
                                Text("已完成 \(job.completed_steps ?? 0) / \(job.total_steps ?? 1) 次模型调用").font(.caption)
                            }
                        }
                    }
                    if !model.aiArtifacts.isEmpty {
                        DisclosureGroup("历史分析与详细依据") {
                            ForEach(model.aiArtifacts) { artifact in artifactView(artifact) }
                        }
                    }
                }.frame(maxWidth: .infinity, alignment: .leading)
            }
        }.padding(24).frame(minWidth: 520, idealWidth: 680, maxWidth: 820, minHeight: 520, idealHeight: 650)
        .task(id: model.selectedSource?.id) {
            guard let id = model.selectedSource?.id else { return }
            while !Task.isCancelled {
                await model.refreshLocalAI(sourceID: id)
                do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { return }
            }
        }
        .alert("停止后续分析？", isPresented: $confirmCancel) {
            Button("保留任务", role: .cancel) { pendingCancel = nil }
            Button("停止分析", role: .destructive) {
                if let job = pendingCancel { Task { await model.controlLocalAI(job, cancel: true) } }
                pendingCancel = nil
            }
        } message: { Text("已发送的模型请求无法撤回，可能产生用量。已完成分段保留；取消后不再继续或自动重发。") }
        .alert("应用分类和项目建议？", isPresented: $confirmApply) {
            Button("取消", role: .cancel) { pendingApply = nil }
            Button("应用") { if let artifact = pendingApply { Task { await model.applyLocalAI(artifact) } }; pendingApply = nil }
        } message: { Text("将按展示的建议更新当前资料的领域和关联项目，原文不变。请先确认需要的标签；应用分类后，本次尚未确认的标签建议会过期。资料发生变更时，过期建议无法应用。") }
    }
    @ViewBuilder private func artifactView(_ artifact: LocalAIArtifact) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            if artifact.phase == "segment" {
                Text("分段总结 · 第 \(artifact.section_index ?? 0) 段（非全文总结）").font(.headline)
            } else if artifact.phase == "synthesis" {
                Text(artifact.coverage == "full" ? "已解析证据汇总" : "部分证据汇总").font(.headline)
            }
            if let scope = artifact.analysis_plan {
                Text("原资料采集范围：\(scope.source_coverage)").font(.caption).foregroundStyle(.secondary)
                if artifact.phase != "segment" {
                    Text("已解析证据覆盖：\(scope.covered_chars) / \(scope.total_chars) 字符").font(.caption).foregroundStyle(.secondary)
                }
            }
            HStack {
                Text("\(artifact.provider) · \(artifact.model)").font(.caption).foregroundStyle(.secondary)
                if artifact.stale { Text("资料已变更，结果过期").foregroundStyle(.orange).font(.caption) }
                if artifact.applied_at != nil { Text("已应用").font(.caption) }
            }
            if let overview = artifact.result.overview {
                Text("一句话概述").font(.headline)
                Text(overview.text).textSelection(.enabled)
                citations(overview.citations, artifact: artifact)
            }
            Text(artifact.result.contract_version == 2 ? "文档摘要" : "历史分析摘要").font(.headline)
            Text(artifact.result.summary).textSelection(.enabled)
            citations(artifact.result.summary_citations, artifact: artifact)
            if !artifact.result.insights.isEmpty { Text("关键要点").font(.headline) }
            ForEach(Array(artifact.result.insights.enumerated()), id: \.offset) { _, insight in
                Text(insight.text).textSelection(.enabled)
                citations(insight.citations, artifact: artifact)
            }
            if let limitations = artifact.result.limitations, !limitations.isEmpty {
                Text("适用条件与限制").font(.headline)
                ForEach(Array(limitations.enumerated()), id: \.offset) { _, item in
                    Text(item.text).textSelection(.enabled)
                    citations(item.citations, artifact: artifact)
                }
            }
            if artifact.result.contract_version != 3, let candidates = artifact.tag_suggestions, !candidates.isEmpty {
                Text("AI 标签候选").font(.headline)
                ForEach(candidates) { candidate in
                    VStack(alignment: .leading, spacing: 5) {
                        Text("\(candidate.name) · \(LocalTag.dimensionText(candidate.dimension)) · AI")
                        Text(candidate.reason).font(.callout).foregroundStyle(.secondary)
                        citations(candidate.citations, artifact: artifact)
                        if candidate.status == "pending" {
                            HStack {
                                Button("确认标签") { Task { await model.decideAITag(candidate, artifact: artifact, accept: true) } }
                                Button("拒绝") { Task { await model.decideAITag(candidate, artifact: artifact, accept: false) } }
                            }.disabled(candidate.stale || artifact.stale || model.summaryIndexBusy || model.ragBusy || model.agentConnectionsBusy || model.aiBusy || model.tagsBusy || model.summaryIndexBusy || model.ragBusy || model.agentConnectionsBusy || model.busy || model.backupBusy || model.trashBusy)
                        } else { Text(candidate.status == "accepted" ? "已确认 · AI 来源" : "已拒绝").font(.caption) }
                    }
                }
            }
            if artifact.result.contract_version != 3, let suggestion = artifact.result.organizing_suggestion {
                Text("领域：\(suggestion.domain_id == nil ? "保留当前领域" : (model.domains.first { $0.id == suggestion.domain_id }?.name ?? "领域已不可用"))")
                Text("项目：\(suggestion.project_ids.isEmpty ? "清除项目关联" : suggestion.project_ids.map { id in model.projects.first { $0.id == id }?.name ?? "项目已不可用" }.joined(separator: "、"))")
                Text(suggestion.reason).foregroundStyle(.secondary)
                citations(suggestion.citations, artifact: artifact)
            }
            ForEach(Array(artifact.result.warnings.enumerated()), id: \.offset) { _, warning in Text(warning).font(.caption).foregroundStyle(.orange) }
            if artifact.result.contract_version != 3 && artifact.result.organizing_suggestion != nil {
                Button("应用分类和项目建议") { pendingApply = artifact; confirmApply = true }
                    .disabled(artifact.stale || artifact.applied_at != nil || model.aiBusy)
            }
        }.padding(16).background(InboxStyle.elevated, in: RoundedRectangle(cornerRadius: 10))
    }
    private func citations(_ ids: [String], artifact: LocalAIArtifact) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach(ids, id: \.self) { id in
                DisclosureGroup("查看依据") {
                    Text(artifact.evidence.first { $0.id == id }?.text ?? "该依据无法读取")
                        .font(.caption).textSelection(.enabled)
                    if let evidence = artifact.evidence.first(where: { $0.id == id }) {
                        Text("\(evidence.locator_kind) · \(evidence.locator_start)–\(evidence.locator_end)")
                            .font(.caption2).foregroundStyle(.secondary)
                        if let start = evidence.text_start, let end = evidence.text_end {
                            Text("原证据文字偏移：\(start)–\(end)").font(.caption2).foregroundStyle(.secondary)
                        }
                    }
                    Button("在资料证据中打开") {
                        let originalID = artifact.evidence.first(where: { $0.id == id })?.original_evidence_id ?? id
                        Task { await model.openEvidence(originalID); dismiss() }
                    }
                        .disabled(artifact.stale)
                }.font(.caption)
            }
        }
    }
}

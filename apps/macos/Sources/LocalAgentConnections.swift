import AppKit
import SwiftUI

struct LocalAgentConnection: Decodable, Identifiable {
    let id: String
    let client_name: String
    let status: String
    let created_at: String
    let expires_at: String
    let revoked_at: String?
    let domain_ids: [String]

    static func current(in connections: [LocalAgentConnection], client: LocalAgentClient) -> LocalAgentConnection? {
        connections.filter { $0.status == "active" && $0.client_name == client.rawValue }
            .sorted { $0.created_at == $1.created_at ? $0.id < $1.id : $0.created_at > $1.created_at }.first
    }

    static func reusable(in connections: [LocalAgentConnection], name: String,
                         domainIDs: [String]) -> LocalAgentConnection? {
        let domains = Set(domainIDs)
        return connections.filter {
            $0.status == "active" && $0.client_name == name && Set($0.domain_ids) == domains
        }.sorted {
            if $0.created_at != $1.created_at { return $0.created_at > $1.created_at }
            return $0.id < $1.id
        }.first
    }

    func expirationText(timeZone: TimeZone = .autoupdatingCurrent) -> String {
        // SQLite returns naive UTC timestamps; explicit server offsets remain authoritative.
        let hasZone = expires_at.range(of: "(?:[zZ]|[+-][0-9]{2}:?[0-9]{2})$", options: .regularExpression) != nil
        let value = hasZone ? expires_at : expires_at + "Z"
        let parser = ISO8601DateFormatter()
        parser.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var date = parser.date(from: value)
        if date == nil {
            parser.formatOptions = [.withInternetDateTime]
            date = parser.date(from: value)
        }
        guard let date else { return "时间不可用" }
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "zh_CN")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = timeZone
        formatter.dateFormat = "yyyy-MM-dd HH:mm:ss zzz"
        return formatter.string(from: date)
    }

    var statusText: String {
        switch status {
        case "active": return "有效"
        case "expired": return "已过期"
        case "revoked": return "已撤销"
        case "superseded": return "已替换"
        default: return "不可用"
        }
    }
}

struct LocalAgentCreated: Decodable {
    let id: String
    let access_token: String
}

struct LocalAgentConnectionsAPI {
    let api: API
    init(address: String, token: String) throws { api = try API(address: address, token: token) }
    func list() async throws -> [LocalAgentConnection] {
        try decodeLocalAI(LocalRAGList<LocalAgentConnection>.self, body: await api.call("v1/agent-connections")).items
    }
    func create(name: String, domainIDs: [String], days: Int) async throws -> LocalAgentCreated {
        try decodeLocalAI(LocalAgentCreated.self, body: await api.call("v1/agent-connections", method: "POST", json: [
            "client_name": name, "domain_ids": domainIDs, "expires_in_days": days,
        ]))
    }
    func update(id: String, domainIDs: [String]) async throws {
        guard UUID(uuidString: id) != nil else { throw failure("连接标识无效。") }
        _ = try await api.call("v1/agent-connections/\(id)", method: "PATCH", json: ["domain_ids": domainIDs])
    }
    func revoke(id: String) async throws {
        guard UUID(uuidString: id) != nil else { throw failure("连接标识无效。") }
        _ = try await api.call("v1/agent-connections/\(id)", method: "DELETE")
    }
}

enum LocalAgentClient: String, CaseIterable, Identifiable {
    case codex = "Codex"
    case claudeCode = "Claude Code"
    case workbuddy = "Workbuddy"

    var id: String { rawValue }
    var instructions: String {
        switch self {
        case .codex: return "将配置添加到 ~/.codex/config.toml，保留已有服务，再重新连接 MCP。"
        case .claudeCode: return "将 JSON 中的服务合并到 ~/.claude.json 的 mcpServers 下，保留文件中的其他内容；重启 Claude Code 后用 /mcp 检查连接。"
        case .workbuddy: return "在 Workbuddy 的 MCP 配置入口，将 JSON 中的服务合并到用户级 ~/.workbuddy/mcp.json 的 mcpServers 下，保留已有服务，保存并确认连接状态。当前尚未完成真实 Workbuddy 客户端验收。"
        }
    }
}

/// Generates a private local configuration; the token is the independent Agent grant, never the owner token.
enum LocalAgentConfiguration {
    static func credentialService(library: LocalLibraryDescriptor, connectionID: String) throws -> String {
        guard let id = UUID(uuidString: connectionID) else { throw failure("连接标识无效。") }
        return library.credentialService + ".agent." + id.uuidString.lowercased()
    }
    static func tomlString(_ value: String) -> String {
        var result = "\""
        for scalar in value.unicodeScalars {
            switch scalar.value {
            case 0x22: result += "\\\""
            case 0x5c: result += "\\\\"
            case 0x0a: result += "\\n"
            case 0x0d: result += "\\r"
            case 0x09: result += "\\t"
            case 0...0x1f, 0x7f: result += String(format: "\\u%04X", scalar.value)
            default: result.unicodeScalars.append(scalar)
            }
        }
        return result + "\""
    }
    static func codex(runtime: URL, library: LocalLibraryDescriptor, connectionID: String, agentToken: String) throws -> String {
        guard let id = UUID(uuidString: connectionID), !agentToken.isEmpty else { throw failure("缺少有效的 Agent 凭证。") }
        let name = "knowledge_" + id.uuidString.lowercased().replacingOccurrences(of: "-", with: "_")
        let executable = MacPaths().runtime(runtime).python.path
        let script = MacPaths().runtime(runtime).backend.appendingPathComponent("app/local_mcp.py").path
        let apiFile = library.data.appendingPathComponent("agent-api.json").path
        return """
        [mcp_servers.\(name)]
        command = \(tomlString(executable))
        args = ["-I", "-B", \(tomlString(script))]
        env = { PKA_API_FILE = \(tomlString(apiFile)), PKA_AGENT_TOKEN = \(tomlString(agentToken)) }
        """
    }
    static func configuration(for client: LocalAgentClient, runtime: URL, library: LocalLibraryDescriptor,
                              connectionID: String, agentToken: String) throws -> String {
        if client == .codex {
            return try codex(runtime: runtime, library: library, connectionID: connectionID, agentToken: agentToken)
        }
        guard let id = UUID(uuidString: connectionID), !agentToken.isEmpty else { throw failure("缺少有效的 Agent 凭证。") }
        let executable = MacPaths().runtime(runtime).python.path
        let script = MacPaths().runtime(runtime).backend.appendingPathComponent("app/local_mcp.py").path
        let endpoint = library.data.appendingPathComponent("agent-api.json").path
        // Claude expands ${VAR} even inside JSON strings; refuse ambiguous literal paths.
        if client == .claudeCode && [executable, script, endpoint, agentToken].contains(where: { $0.contains("${") }) {
            throw failure("Claude Code 会展开 ${…} 环境变量。应用或知识库路径包含该字符，请改用不含此字符的路径后重新复制配置。")
        }
        var server: [String: Any] = ["command": executable, "args": ["-I", "-B", script],
                                     "env": ["PKA_API_FILE": endpoint, "PKA_AGENT_TOKEN": agentToken]]
        if client == .claudeCode { server["type"] = "stdio" }
        let name = "knowledge_" + id.uuidString.lowercased().replacingOccurrences(of: "-", with: "_")
        let data = try JSONSerialization.data(withJSONObject: ["mcpServers": [name: server]], options: [.prettyPrinted, .sortedKeys, .withoutEscapingSlashes])
        guard let result = String(data: data, encoding: .utf8) else { throw failure("生成 Agent 配置失败。") }
        return result
    }
}

private struct LocalAgentServiceConnection: Equatable {
    let address: String
    let token: String
    let connected: Bool
    let library: LocalLibraryDescriptor?
}

struct LocalAgentConnectionsView: View {
    @ObservedObject var model: CaptureModel
    let installer: LocalAgentIntegration

    init(model: CaptureModel, installer: LocalAgentIntegration = LocalAgentIntegration()) {
        self.model = model; self.installer = installer
    }
    @State private var items: [LocalAgentConnection] = []
    @State private var configurationClient: LocalAgentClient = .codex
    @State private var selectedDomains: Set<String> = []
    private let days = 30
    @State private var loading = false
    @State private var writing = false
    @State private var message = ""
    @State private var generation = UUID()
    @State private var revokeTarget: LocalAgentConnection?
    @State private var transientTokens: [String: String] = [:]
    @State private var installationStatus: [String: String] = [:]
    @State private var connectionFeedback: [String: String] = [:]
    @State private var referenceStatus: [String: LocalAgentInstallResult] = [:]
    @State private var referenceGeneration = UUID()
    @State private var referenceLoading = false

    private var connection: LocalAgentServiceConnection {
        LocalAgentServiceConnection(address: model.address, token: model.token, connected: model.connected,
                                    library: try? model.localService.currentLibrary())
    }
    private var available: Bool { model.usesLocalService && model.connected && !model.recoveringService }
    private var writeAllowed: Bool {
        available && !loading && !referenceLoading && !writing && !model.busy && !model.backupBusy && !model.trashBusy &&
        !model.aiBusy && !model.tagsBusy && !model.summaryIndexBusy && !model.ragBusy && !model.agentConnectionsBusy
    }
    private var validDomains: Set<String> { Set(model.domains.map(\.id)) }
    private var currentItem: LocalAgentConnection? {
        LocalAgentConnection.current(in: items, client: configurationClient)
    }
    private var selectedIDs: [String] { selectedDomains.intersection(validDomains).sorted() }
    private var authorizationChanged: Bool { Set(currentItem?.domain_ids ?? []) != Set(selectedIDs) }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            SettingsTabs(title: "接入客户端", selection: $configurationClient, options:
                LocalAgentClient.allCases.map { InboxSelectOption($0, $0.rawValue) })
                .disabled(writing)
            HStack(alignment: .top) {
                Text("每个客户端使用一个独立的只读连接。请保持 \(AppBrand.name) 运行。")
                    .font(.callout).foregroundStyle(InboxStyle.muted)
                Spacer(minLength: 8)
                Button("刷新") { Task { await refresh() } }
                    .disabled(!available || loading || referenceLoading || writing)
            }
            Surface {
                AgentLibraryPicker(domains: model.domains, selection: $selectedDomains)
                    .disabled(!available || loading || writing)
            }
            HStack {
                if currentItem == nil {
                    Text(installer.detected(client: configurationClient) ? "已找到客户端或已有配置" : "未检测到客户端，可先手动创建连接")
                        .font(.caption).foregroundStyle(InboxStyle.muted)
                } else {
                    Text(authorizationChanged ? "授权范围有未保存的修改" : "授权范围已保存")
                        .font(.caption).foregroundStyle(InboxStyle.muted)
                }
                Spacer()
                if currentItem == nil, configurationClient != .workbuddy, !installer.detected(client: configurationClient) {
                    Button("手动创建连接") { Task { await create() } }
                        .disabled(!writeAllowed || selectedIDs.isEmpty)
                }
                Button(currentItem != nil ? "保存授权" : configurationClient == .codex ? "创建知识库插件" : configurationClient == .workbuddy ? "创建连接" : "连接 \(configurationClient.rawValue)") {
                    Task { await saveAuthorization() }
                }
                .buttonStyle(InboxButtonStyle(.primary))
                .disabled(!writeAllowed || selectedIDs.isEmpty ||
                    (currentItem != nil ? !authorizationChanged : configurationClient != .workbuddy && !installer.detected(client: configurationClient)))
            }
            if let item = currentItem { connectionCard(item) }
            else if !loading {
                Text(configurationClient == .workbuddy ? "先创建只读连接，再将配置复制到 Workbuddy 中完成接入。" :
                    configurationClient == .codex ? "创建后，到 Codex 插件页安装“个人知识库”，再回到这里完成接入。" :
                    "连接时会备份原配置并保留其他设置，完成后请在 Claude Code 中重新加载 MCP。")
                    .font(.callout).foregroundStyle(InboxStyle.muted)
            }
            if loading || referenceLoading || writing { ProgressView().controlSize(.small) }
            if !message.isEmpty {
                Text(message).font(.callout).foregroundStyle(InboxStyle.muted).textSelection(.enabled)
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
        .task(id: connection) { reset(); await refresh() }
        .onChange(of: configurationClient) { _, _ in
            message = ""; selectedDomains = Set(currentItem?.domain_ids ?? []).intersection(validDomains)
            Task { await refreshReference() }
        }
        .onChange(of: validDomains) { _, value in selectedDomains.formIntersection(value) }
        .onDisappear { generation = UUID(); referenceGeneration = UUID(); transientTokens = [:] }
        .confirmationDialog("撤销此 Agent 的访问权限？", isPresented: Binding(get: { revokeTarget != nil }, set: { if !$0 { revokeTarget = nil } }), titleVisibility: .visible) {
            if let target = revokeTarget { Button("撤销连接", role: .destructive) { Task { await revoke(target) } } }
            Button("取消", role: .cancel) { revokeTarget = nil }
        } message: { Text("该凭证将立即失效。需要重新接入时，请创建新连接并更新 Agent 配置。") }
    }

    private func connectionCard(_ item: LocalAgentConnection) -> some View {
        Surface {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Image(systemName: "checkmark.shield").foregroundStyle(InboxStyle.accent)
                    Text("\(configurationClient.rawValue) 当前连接").font(.headline)
                    Spacer()
                    Text(item.statusText).font(.caption).foregroundStyle(InboxStyle.accent)
                    if configurationClient != .workbuddy {
                        Menu {
                            Button("复制配置") { copyConfiguration(item) }
                            Button("撤销凭证", role: .destructive) { revokeTarget = item }
                        } label: { Image(systemName: "ellipsis").frame(width: 24, height: 24) }
                        .menuStyle(.borderlessButton).fixedSize()
                        .disabled(!writeAllowed)
                        .help("更多连接操作")
                        .accessibilityLabel("更多连接操作")
                    }
                }
                Text(installationStatus[statusKey(item.id)] ?? "连接已创建，尚未检查客户端配置")
                    .font(.callout).foregroundStyle(InboxStyle.muted)
                if configurationClient != .workbuddy {
                    Toggle(configurationClient == .codex ? "启用知识库插件" : "自动参考知识库", isOn: Binding(
                        get: { referenceStatus[statusKey(item.id)]?.auto_reference_enabled == true },
                        set: { enabled in Task { await setReference(item, enabled: enabled) } }
                    ))
                    .disabled(!writeAllowed || referenceStatus[statusKey(item.id)] == nil ||
                        referenceStatus[statusKey(item.id)]?.update_required == true ||
                        (configurationClient == .codex && referenceStatus[statusKey(item.id)]?.installed != true))
                    Text(referenceStatus[statusKey(item.id)]?.message ?? (referenceLoading ? "正在读取接入状态…" : "接入状态暂不可用，请刷新检查。"))
                        .font(.caption).foregroundStyle(InboxStyle.muted)
                    Text(configurationClient == .codex ? "在 Codex 对话中通过 @ 选择个人知识库 Skill，检索资料或依据原文问答。" :
                        "开启后对新会话生效，涉及个人资料时先检索，不保证每轮调用。")
                        .font(.caption).foregroundStyle(InboxStyle.muted)
                    ViewThatFits(in: .horizontal) {
                        HStack(spacing: 8) { installationButtons(item) }
                        VStack(alignment: .leading, spacing: 8) { installationButtons(item) }
                    }
                } else {
                    Text(configurationClient.instructions)
                        .font(.callout).foregroundStyle(InboxStyle.muted).textSelection(.enabled)
                    HStack(spacing: 8) {
                        Button("复制配置") { copyConfiguration(item) }.disabled(!writeAllowed)
                        Button("断开连接", role: .destructive) { revokeTarget = item }.disabled(!writeAllowed)
                    }
                    Text("配置包含独立访问凭证，请勿分享或提交仓库。")
                        .font(.caption).foregroundStyle(InboxStyle.muted)
                }
                if let feedback = connectionFeedback[statusKey(item.id)] {
                    Text(feedback).font(.caption).foregroundStyle(InboxStyle.muted).textSelection(.enabled)
                }
                Text("有效期至 \(item.expirationText())").font(.caption).foregroundStyle(InboxStyle.muted)
                Text("已授权：\(domainNames(item))").font(.caption).foregroundStyle(InboxStyle.muted).lineLimit(2)
            }
        }
    }

    @ViewBuilder private func installationButtons(_ item: LocalAgentConnection) -> some View {
        Button(configurationClient == .codex ? "创建/更新插件" : "接入") { Task { await manageInstallation(item, action: "install") } }
            .disabled(!writeAllowed || !installer.detected(client: configurationClient))
        if configurationClient == .codex {
            Button("完成接入") { Task { await manageInstallation(item, action: "complete") } }
                .disabled(!writeAllowed || referenceStatus[statusKey(item.id)]?.installed != true || referenceStatus[statusKey(item.id)]?.auto_reference_enabled != true)
        }
        Button("检查连接") { Task { await manageInstallation(item, action: "check") } }.disabled(!writeAllowed)
        Button("断开") { Task { await manageInstallation(item, action: "remove") } }.disabled(!writeAllowed)
    }

    @MainActor private func saveAuthorization() async {
        guard let item = currentItem else { await create(install: configurationClient != .workbuddy); return }
        guard writeAllowed, !selectedIDs.isEmpty else { return }
        let original = connection; let attempt = generation
        writing = true; model.agentConnectionsBusy = true; message = ""
        defer { if connection == original { writing = false; model.agentConnectionsBusy = false } }
        do {
            let client = try LocalAgentConnectionsAPI(address: original.address, token: original.token)
            try await client.update(id: item.id, domainIDs: selectedIDs)
            let loaded = try await client.list()
            guard isCurrent(original, attempt) else { return }
            items = loaded
            message = "授权已更新，保留原连接、凭证和有效期；其他客户端不受影响。"
        } catch {
            if isCurrent(original, attempt) { message = "授权保存未确认：\(error.localizedDescription)。请刷新检查后重试。" }
        }
    }

    private func domainNames(_ item: LocalAgentConnection) -> String {
        let ids = Set(item.domain_ids)
        let names = model.domains.filter { ids.contains($0.id) }.map(\.name)
        if !validDomains.isEmpty && ids == validDomains { return "全部 \(names.count) 个当前知识库" }
        if names.isEmpty { return "原授权知识库已不可用，请重新选择" }
        if names.count > 3 { return names.prefix(2).joined(separator: "、") + " 等 \(names.count) 个知识库" }
        return names.joined(separator: "、")
    }
    @MainActor private func reset() {
        generation = UUID(); items = []; selectedDomains = []; loading = false; writing = false
        message = ""; revokeTarget = nil; transientTokens = [:]; installationStatus = [:]; connectionFeedback = [:]
        referenceGeneration = UUID(); referenceStatus = [:]; referenceLoading = false
    }
    @MainActor private func isCurrent(_ original: LocalAgentServiceConnection, _ attempt: UUID) -> Bool {
        !Task.isCancelled && available && connection == original && generation == attempt
    }
    @MainActor private func refresh() async {
        guard available, !writing else { return }
        let original = connection; let attempt = UUID(); generation = attempt; loading = true
        defer { if connection == original && generation == attempt { loading = false } }
        do {
            let loaded = try await LocalAgentConnectionsAPI(address: original.address, token: original.token).list()
            guard isCurrent(original, attempt) else { return }
            items = loaded
            selectedDomains = Set(currentItem?.domain_ids ?? []).intersection(validDomains)
            await refreshReference()
        } catch { if isCurrent(original, attempt) { message = error.localizedDescription } }
    }
    private func statusKey(_ id: String) -> String { configurationClient.rawValue + ":" + id }


    @MainActor private func refreshReference() async {
        guard available, !writing, configurationClient != .workbuddy, let runtime = LocalService.runtime else { return }
        let original = connection; let attempt = generation; let targetClient = configurationClient
        let referenceAttempt = UUID(); referenceGeneration = referenceAttempt; referenceLoading = true
        defer { if referenceGeneration == referenceAttempt { referenceLoading = false } }
        for item in [currentItem].compactMap({ $0 }) {
            do {
                let result = try await Task.detached {
                    if targetClient == .codex {
                        guard let library = original.library else { throw failure("未找到本地知识库。") }
                        return try installer.runPlugin(runtime: runtime, action: "plugin_status", connectionID: item.id,
                            endpoint: library.data.appendingPathComponent("agent-api.json"))
                    }
                    return try installer.runReference(runtime: runtime, action: "reference_status", client: targetClient, connectionID: item.id)
                }.value
                guard isCurrent(original, attempt), configurationClient == targetClient, referenceGeneration == referenceAttempt else { return }
                referenceStatus[statusKey(item.id)] = result
                if targetClient == .codex && result.installed == true {
                    installationStatus[statusKey(item.id)] = result.update_required == true ? "接入路径需要更新" :
                        result.host_installed == true ? "Codex 已安装插件" : "插件已生成；等待 Codex 安装"
                }
            } catch {
                guard isCurrent(original, attempt), configurationClient == targetClient, referenceGeneration == referenceAttempt else { return }
                referenceStatus.removeValue(forKey: statusKey(item.id))
                message = "自动参考状态无法确认：\(error.localizedDescription)"
            }
        }
    }

    @MainActor private func setReference(_ item: LocalAgentConnection, enabled: Bool) async {
        guard writeAllowed, (!enabled || item.status == "active"), let runtime = LocalService.runtime else { return }
        let original = connection; let attempt = generation; let targetClient = configurationClient
        writing = true; model.agentConnectionsBusy = true; message = ""
        defer { if connection == original { writing = false; model.agentConnectionsBusy = false } }
        do {
            let result = try await Task.detached {
                if targetClient == .codex {
                    guard let library = original.library else { throw failure("未找到本地知识库。") }
                    return try installer.runPlugin(runtime: runtime, action: enabled ? "plugin_enable" : "plugin_disable", connectionID: item.id,
                        endpoint: library.data.appendingPathComponent("agent-api.json"))
                }
                return try installer.runReference(runtime: runtime, action: enabled ? "reference_enable" : "reference_disable", client: targetClient, connectionID: item.id)
            }.value
            guard isCurrent(original, attempt), configurationClient == targetClient else { return }
            referenceStatus[statusKey(item.id)] = result
            connectionFeedback[statusKey(item.id)] = result.message
            message = result.message + " 请在 \(targetClient.rawValue) 中开始新会话。"
        } catch {
            guard isCurrent(original, attempt), configurationClient == targetClient else { return }
            message = "自动参考设置未完成：\(error.localizedDescription)。请刷新检查实际状态；已修改的规则需要手动处理。"
            connectionFeedback[statusKey(item.id)] = message
        }
    }

    @MainActor private func disableReference(_ item: LocalAgentConnection, runtime: URL, client: LocalAgentClient,
                                              original: LocalAgentServiceConnection, attempt: UUID) async throws {
        guard client != .workbuddy else { return }
        let result = try await Task.detached {
            if client == .codex {
                guard let library = original.library else { throw failure("未找到本地知识库。") }
                _ = try installer.runPlugin(runtime: runtime, action: "plugin_remove", connectionID: item.id,
                    endpoint: library.data.appendingPathComponent("agent-api.json"))
            }
            return try installer.runReference(runtime: runtime, action: "reference_disable", client: client, connectionID: item.id)
        }.value
        guard isCurrent(original, attempt), configurationClient == client else { throw failure("连接状态已变化，请刷新后重试。") }
        guard result.auto_reference_enabled == false, result.reference_status == "disabled" else {
            throw failure("自动参考规则未完全移除，请先检查规则状态。")
        }
        referenceStatus[statusKey(item.id)] = result
    }

    @MainActor private func create(install: Bool = false) async {
        guard writeAllowed, let library = connection.library else { return }
        let name = configurationClient.rawValue; let domains = selectedIDs
        guard !name.isEmpty, name.count <= 160, !domains.isEmpty, (1...365).contains(days) else { return }
        let original = connection; let attempt = generation
        let targetClient = configurationClient
        writing = true; model.agentConnectionsBusy = true; message = ""
        defer { if connection == original { writing = false; model.agentConnectionsBusy = false } }
        do {
            let client = try LocalAgentConnectionsAPI(address: original.address, token: original.token)
            // Read current grant states before deciding; a displayed grant may have expired or been revoked.
            if install {
                let loaded = try await client.list()
                guard isCurrent(original, attempt) else { return }
                items = loaded
            }
            let existing = LocalAgentConnection.current(in: items, client: targetClient)
            let created: LocalAgentCreated
            if let existing {
                guard Set(existing.domain_ids) == Set(domains) else {
                    message = "请先保存授权范围，再更新客户端接入。"; return
                }
                let service = try LocalAgentConfiguration.credentialService(library: library, connectionID: existing.id)
                guard let token = transientTokens[existing.id] ?? TokenStore.load(service: service), !token.isEmpty else {
                    message = "已有相同授权的有效连接，但本机凭证已丢失。请在当前连接中撤销凭证后重新连接；不会重复创建凭证。"
                    return
                }
                created = LocalAgentCreated(id: existing.id, access_token: token)
            } else {
                created = try await client.create(name: name, domainIDs: domains, days: days)
                guard isCurrent(original, attempt) else { return }
                let service = try LocalAgentConfiguration.credentialService(library: library, connectionID: created.id)
                let saved = TokenStore.save(created.access_token, service: service)
                if !saved { transientTokens[created.id] = created.access_token }
                message = saved ? "连接已创建，凭证已保存在本机钥匙串。可复制 Agent 配置。" : "连接已创建，但钥匙串保存失败。请在关闭此窗口前复制配置；否则需撤销此连接后重新创建。"
            }
            if install {
                do {
                    guard let runtime = LocalService.runtime else { throw failure("未找到内置运行环境。") }
                    let configuration = try LocalAgentConfiguration.configuration(for: targetClient, runtime: runtime, library: library, connectionID: created.id, agentToken: created.access_token)
                    let result = try await Task.detached {
                        if targetClient == .codex {
                            return try installer.runPlugin(runtime: runtime, action: "plugin_install", connectionID: created.id,
                                endpoint: library.data.appendingPathComponent("agent-api.json"), configuration: configuration)
                        }
                        return try installer.run(runtime: runtime, action: "install", client: targetClient, connectionID: created.id, configuration: configuration)
                    }.value
                    guard isCurrent(original, attempt) else { return }
                    installationStatus[statusKey(created.id)] = targetClient == .codex ? "插件已生成；等待 Codex 安装" : "配置已安装，知识库服务检查通过"
                    let reference = try? await Task.detached {
                        if targetClient == .codex {
                            return try installer.runPlugin(runtime: runtime, action: "plugin_status", connectionID: created.id,
                                endpoint: library.data.appendingPathComponent("agent-api.json"))
                        }
                        return try installer.runReference(runtime: runtime, action: "reference_status", client: targetClient, connectionID: created.id)
                    }.value
                    guard isCurrent(original, attempt), configurationClient == targetClient else { return }
                    referenceStatus[statusKey(created.id)] = reference
                    message = (existing == nil ? "" : "已复用现有连接，原有效期保持不变。") + result.message + (targetClient == .codex ? " 在 Codex 插件页安装后，回到这里完成接入；然后开始新会话。" : " 请在 \(targetClient.rawValue) 中重新加载 MCP。")
                } catch {
                    guard isCurrent(original, attempt) else { return }
                    installationStatus[statusKey(created.id)] = "接入未完成"
                    message = "\(existing == nil ? "凭证已创建" : "已复用现有凭证")，自动接入未完成：\(error.localizedDescription)。请在当前连接中重试接入，或复制配置手动接入；无需重复创建凭证。"
                }
            }
            let loaded = try await client.list()
            guard isCurrent(original, attempt) else { return }
            items = loaded
        } catch {
            if isCurrent(original, attempt) { message = "创建或刷新未确认：\(error.localizedDescription)。请先刷新连接列表，不要连续重复创建。" }
        }
    }
    @MainActor private func manageInstallation(_ item: LocalAgentConnection, action: String) async {
        guard writeAllowed, let library = connection.library, let runtime = LocalService.runtime else { return }
        let original = connection; let attempt = generation; let targetClient = configurationClient
        writing = true; model.agentConnectionsBusy = true; message = ""
        let feedbackKey = targetClient.rawValue + ":" + item.id
        connectionFeedback[feedbackKey] = action == "remove" ? "正在移除配置…" : "正在处理…"
        defer { if connection == original { writing = false; model.agentConnectionsBusy = false } }
        do {
            if action == "remove" {
                try await disableReference(item, runtime: runtime, client: targetClient, original: original, attempt: attempt)
            }
            let service = try LocalAgentConfiguration.credentialService(library: library, connectionID: item.id)
            let result: LocalAgentInstallResult
            if action == "remove" {
                result = try await Task.detached {
                    try installer.removeConfiguration(runtime: runtime, client: targetClient, connectionID: item.id,
                        endpoint: library.data.appendingPathComponent("agent-api.json"))
                }.value
            } else if targetClient == .codex && (["check", "complete"].contains(action) ||
                (action == "install" && referenceStatus[statusKey(item.id)]?.installed == true)) {
                let pluginAction = action == "complete" ? "plugin_complete" : action == "install" ? "plugin_install" : "plugin_check"
                result = try await Task.detached {
                    try installer.runPlugin(runtime: runtime, action: pluginAction, connectionID: item.id,
                        endpoint: library.data.appendingPathComponent("agent-api.json"))
                }.value
            } else {
                guard let token = transientTokens[item.id] ?? TokenStore.load(service: service) else { throw failure("本机凭证已丢失，请先撤销此连接后重新接入。") }
                let configuration = try LocalAgentConfiguration.configuration(for: targetClient, runtime: runtime, library: library, connectionID: item.id, agentToken: token)
                result = try await Task.detached {
                    if targetClient == .codex && action == "install" {
                        return try installer.runPlugin(runtime: runtime, action: "plugin_install", connectionID: item.id,
                            endpoint: library.data.appendingPathComponent("agent-api.json"), configuration: configuration)
                    }
                    return try installer.run(runtime: runtime, action: action, client: targetClient, connectionID: item.id, configuration: configuration)
                }.value
            }
            guard isCurrent(original, attempt) else { return }
            if action == "remove" {
                installationStatus[statusKey(item.id)] = "配置已移除"
                try await LocalAgentConnectionsAPI(address: original.address, token: original.token).revoke(id: item.id)
                guard isCurrent(original, attempt) else { return }
                TokenStore.delete(service: service); transientTokens.removeValue(forKey: item.id)
                let loaded = try await LocalAgentConnectionsAPI(address: original.address, token: original.token).list()
                guard isCurrent(original, attempt) else { return }
                items = loaded
                message = "已断开连接并撤销凭证。" + (targetClient == .codex ? " 请在 Codex 插件页卸载该插件。" : "")
                connectionFeedback[feedbackKey] = result.message + "；访问凭证已撤销。" + (targetClient == .codex ? " 请在 Codex 插件页卸载该插件。" : "")
            } else {
                installationStatus[statusKey(item.id)] = targetClient == .codex ? (result.host_installed == true ? "Codex 已安装插件，服务检查通过" : "插件已生成；等待 Codex 安装") : (result.checked == true ? "配置已安装，知识库服务检查通过" : "配置未安装")
                let reference = try? await Task.detached {
                    if targetClient == .codex {
                        guard let library = original.library else { throw failure("未找到本地知识库。") }
                        return try installer.runPlugin(runtime: runtime, action: "plugin_status", connectionID: item.id,
                            endpoint: library.data.appendingPathComponent("agent-api.json"))
                    }
                    return try installer.runReference(runtime: runtime, action: "reference_status", client: targetClient, connectionID: item.id)
                }.value
                guard isCurrent(original, attempt), configurationClient == targetClient else { return }
                referenceStatus[statusKey(item.id)] = reference
                message = result.message + (targetClient == .codex ? (action == "install" ? " 安装后回到这里点击“完成接入”。" : action == "complete" ? " 旧接入已迁移，请重启 Codex 并开始新会话。" : " 请在 Codex 中开始新会话。") : " Agent 已运行时，请重新加载 MCP。")
                connectionFeedback[feedbackKey] = message
            }
        } catch {
            if isCurrent(original, attempt) {
                let prefix = action == "remove" && installationStatus[statusKey(item.id)] == "配置已移除" ? "配置已移除，凭证撤销或列表刷新未确认：" : ""
                message = prefix + error.localizedDescription + " 请先刷新连接列表；可在当前连接中复制配置或撤销凭证后重试。"
                connectionFeedback[feedbackKey] = message
            }
        }
    }
    @MainActor private func revoke(_ item: LocalAgentConnection) async {
        guard writeAllowed, let library = connection.library else { return }
        let original = connection; let attempt = generation; let targetClient = configurationClient
        writing = true; model.agentConnectionsBusy = true; revokeTarget = nil; message = ""
        defer { if connection == original { writing = false; model.agentConnectionsBusy = false } }
        do {
            if targetClient != .workbuddy {
                guard let runtime = LocalService.runtime else { throw failure("未找到内置运行环境，无法安全移除自动参考规则。") }
                try await disableReference(item, runtime: runtime, client: targetClient, original: original, attempt: attempt)
            }
            let client = try LocalAgentConnectionsAPI(address: original.address, token: original.token)
            try await client.revoke(id: item.id)
            guard isCurrent(original, attempt) else { return }
            TokenStore.delete(service: try LocalAgentConfiguration.credentialService(library: library, connectionID: item.id))
            transientTokens.removeValue(forKey: item.id)
            message = "连接已撤销，旧配置中的凭证已失效。"
            let loaded = try await client.list()
            guard isCurrent(original, attempt) else { return }
            items = loaded
        } catch { if isCurrent(original, attempt) { message = "撤销或刷新未确认：\(error.localizedDescription)。请先刷新检查连接状态。" } }
    }
    @MainActor private func copyConfiguration(_ item: LocalAgentConnection) {
        guard writeAllowed, item.status == "active", let library = connection.library else { return }
        do {
            let service = try LocalAgentConfiguration.credentialService(library: library, connectionID: item.id)
            guard let token = transientTokens[item.id] ?? TokenStore.load(service: service) else {
                message = "本机未保存此连接的凭证。凭证不能从服务端找回；请撤销后创建新连接。"; return
            }
            guard let runtime = LocalService.runtime else { message = "未找到内置运行环境，请使用打包后的客户端。"; return }
            let configuration = try LocalAgentConfiguration.configuration(for: configurationClient, runtime: runtime, library: library, connectionID: item.id, agentToken: token)
            guard DesktopIntegration.shared.copyText(configuration) else { message = "写入剪贴板失败，请重试。"; return }
            message = "已复制私有 \(configurationClient.rawValue) 配置，请勿分享或提交仓库。\(configurationClient.instructions) 请保持知识库客户端运行。"
        } catch { message = error.localizedDescription }
    }
}

import Foundation

struct LocalAgentInstallResult: Decodable {
    let installed: Bool?
    let checked: Bool?
    let backup_path: String?
    let auto_reference_enabled: Bool?
    let reference_status: String?
    let plugin_status: String?
    let plugin_path: String?
    let plugin_name: String?
    let marketplace_name: String?
    let host_installed: Bool?
    let update_required: Bool?
    let message: String
}

/// Explicit integration paths allow isolated verification without touching a user's Agent settings.
struct LocalAgentIntegration: Sendable {
    let home: URL
    let environment: [String: String]

    init(home: URL? = nil) {
        self.home = home?.resolvingSymlinksInPath() ?? FileManager.default.homeDirectoryForCurrentUser
        environment = home == nil ? ProcessInfo.processInfo.environment : [:]
    }
    func configurationPath(_ client: LocalAgentClient) -> URL {
        LocalAgentInstaller.configurationPath(client: client, home: home, environment: environment)
    }
    func detected(client: LocalAgentClient) -> Bool {
        LocalAgentInstaller.detected(client: client, home: home)
    }
    func runPlugin(runtime: URL, action: String, connectionID: String, endpoint: URL,
                   configuration: String? = nil) throws -> LocalAgentInstallResult {
        let paths = LocalAgentInstaller.referencePaths(client: .codex, home: home, environment: environment)
        return try LocalAgentInstaller.runPlugin(runtime: runtime, action: action, connectionID: connectionID,
            endpoint: endpoint, configuration: configuration, home: home, configPath: configurationPath(.codex),
            skillsRoot: paths.skillsRoot, instructionsPath: paths.instructionsPath)
    }
    func run(runtime: URL, action: String, client: LocalAgentClient, connectionID: String,
             configuration: String) throws -> LocalAgentInstallResult {
        try LocalAgentInstaller.run(runtime: runtime, action: action, client: client, connectionID: connectionID,
            configuration: configuration, configPath: configurationPath(client))
    }
    func runReference(runtime: URL, action: String, client: LocalAgentClient, connectionID: String) throws -> LocalAgentInstallResult {
        let paths = LocalAgentInstaller.referencePaths(client: client, home: home, environment: environment)
        return try LocalAgentInstaller.runReference(runtime: runtime, action: action, client: client,
            connectionID: connectionID, skillsRoot: paths.skillsRoot, instructionsPath: paths.instructionsPath,
            configPath: configurationPath(client))
    }
    func removeConfiguration(runtime: URL, client: LocalAgentClient, connectionID: String, endpoint: URL) throws -> LocalAgentInstallResult {
        try LocalAgentInstaller.removeConfiguration(runtime: runtime, client: client, connectionID: connectionID,
            endpoint: endpoint, configPath: configurationPath(client))
    }
}

enum LocalAgentInstaller {
    static func pluginPaths(home: URL = FileManager.default.homeDirectoryForCurrentUser) -> (root: URL, marketplace: URL) {
        (home.appendingPathComponent(".codex/pka-knowledge-plugins"),
         home.appendingPathComponent(".agents/plugins/marketplace.json"))
    }

    /// Plugin files contain no credentials. The helper stores the connection in a separate private file.
    static func runPlugin(runtime: URL, action: String, connectionID: String, endpoint: URL,
                          configuration: String? = nil, home: URL = FileManager.default.homeDirectoryForCurrentUser,
                          configPath: URL? = nil, skillsRoot: URL? = nil, instructionsPath: URL? = nil) throws -> LocalAgentInstallResult {
        guard UUID(uuidString: connectionID) != nil,
              ["plugin_install", "plugin_status", "plugin_check", "plugin_complete", "plugin_remove", "plugin_enable", "plugin_disable", "plugin_consolidate"].contains(action) else {
            throw failure("插件操作无效。")
        }
        let paths = pluginPaths(home: home)
        let config = configPath ?? configurationPath(client: .codex, home: home)
        let template = runtime.appendingPathComponent("backend/app/agent_skill.md")
        // Foundation normalizes /private/tmp back to /tmp; the helper requires a physical path.
        let physicalTemplate = try MacPrivateFiles.physicalPath(template)
        var request: [String: Any] = ["action": action == "plugin_complete" ? "plugin_host_check" : action, "client": "codex", "connection_id": connectionID,
            "runtime_path": runtime.path, "endpoint_path": endpoint.path,
            "plugin_root": paths.root.path, "marketplace_path": paths.marketplace.path, "config_path": config.path,
            "skill_template_path": physicalTemplate,
            "skill_metadata_path": physicalTemplate.replacingOccurrences(of: "agent_skill.md", with: "agent_skill.yaml")]
        if let configuration { request["configuration"] = configuration }
        // Host installation uses Codex's supported UI. Keep legacy access until that step is verified.
        if action != "plugin_complete" { return try runRequest(runtime: runtime, request: request) }

        let legacy = referencePaths(client: .codex, home: home)
        let skills = skillsRoot ?? legacy.skillsRoot, instructions = instructionsPath ?? legacy.instructionsPath
        let old = try runReference(runtime: runtime, action: "reference_status", client: .codex,
            connectionID: connectionID, skillsRoot: skills, instructionsPath: instructions, configPath: config)
        guard !["partial", "modified"].contains(old.reference_status ?? "") else {
            throw failure("旧自动参考规则已修改或不完整，请先手动处理；旧接入尚未迁移。")
        }
        var preflight = request; preflight["action"] = "migration_check"
        _ = try runRequest(runtime: runtime, request: preflight)
        var statusRequest = request; statusRequest["action"] = "plugin_status"
        let previous = try runRequest(runtime: runtime, request: statusRequest)
        let result = try runRequest(runtime: runtime, request: request)
        guard result.host_installed == true, result.auto_reference_enabled == true else {
            throw failure("请先在 Codex 安装并启用知识库插件，再完成接入；旧接入保持可用。")
        }
        do {
            _ = try runReference(runtime: runtime, action: "reference_disable", client: .codex,
                connectionID: connectionID, skillsRoot: skills, instructionsPath: instructions, configPath: config)
            _ = try removeConfiguration(runtime: runtime, client: .codex, connectionID: connectionID, endpoint: endpoint, configPath: config)
        } catch {
            // Preserve a previously installed plugin; avoid deleting it after a concurrent legacy edit.
            request["action"] = previous.installed == true ? "plugin_disable" : "plugin_remove"
            request.removeValue(forKey: "configuration")
            let rolledBack = (try? runRequest(runtime: runtime, request: request))?.auto_reference_enabled == false
            throw failure("旧连接迁移未完成：\(error.localizedDescription)。\(rolledBack ? "插件已停用或移除；旧规则可能已清理，请刷新检查。" : "插件回退未确认，请检查插件和旧连接状态。")")
        }
        return result
    }

    static func configurationPath(client: LocalAgentClient, home: URL = FileManager.default.homeDirectoryForCurrentUser,
                                  environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        switch client {
        case .codex:
            let directory = environment["CODEX_HOME"].flatMap { $0.hasPrefix("/") ? URL(fileURLWithPath: $0) : nil }
                ?? home.appendingPathComponent(".codex")
            return directory.appendingPathComponent("config.toml")
        case .claudeCode:
            if let directory = environment["CLAUDE_CONFIG_DIR"], directory.hasPrefix("/") {
                return URL(fileURLWithPath: directory).appendingPathComponent(".claude.json")
            }
            return home.appendingPathComponent(".claude.json")
        case .workbuddy: return home.appendingPathComponent(".workbuddy/mcp.json")
        }
    }

    static func detected(client: LocalAgentClient, home: URL = FileManager.default.homeDirectoryForCurrentUser) -> Bool {
        if FileManager.default.fileExists(atPath: configurationPath(client: client, home: home).path) { return true }
        let executable = client == .codex ? "codex" : client == .claudeCode ? "claude" : "workbuddy"
        let appName = client == .codex ? "Codex.app" : client == .claudeCode ? "Claude.app" : "Workbuddy.app"
        return MacCLIResolver.detected(executable: executable, appName: appName, home: home)
    }

    /// Run from a detached task. Credentials travel over stdin, never shell arguments or logs.
    static func run(runtime: URL, action: String, client: LocalAgentClient, connectionID: String,
                    configuration: String, configPath: URL? = nil) throws -> LocalAgentInstallResult {
        guard client != .workbuddy, ["install", "status", "remove", "check"].contains(action), UUID(uuidString: connectionID) != nil else {
            throw failure("该操作暂不支持，请使用高级设置中的手动配置。")
        }
        let request: [String: Any] = ["action": action, "client": client == .codex ? "codex" : "claudeCode",
                                      "connection_id": connectionID, "configuration": configuration,
                                      "config_path": (configPath ?? configurationPath(client: client)).path]
        return try runRequest(runtime: runtime, request: request)
    }

    /// Remove our exact library-bound configuration without retrieving its credential.
    static func removeConfiguration(runtime: URL, client: LocalAgentClient, connectionID: String,
                                    endpoint: URL, configPath: URL? = nil) throws -> LocalAgentInstallResult {
        guard client != .workbuddy, UUID(uuidString: connectionID) != nil else { throw failure("该操作暂不支持。") }
        return try runRequest(runtime: runtime, request: [
            "action": "remove_owned", "client": client == .codex ? "codex" : "claudeCode",
            "connection_id": connectionID, "runtime_path": runtime.path, "endpoint_path": endpoint.path,
            "config_path": (configPath ?? configurationPath(client: client)).path
        ])
    }

    static func referencePaths(client: LocalAgentClient, home: URL = FileManager.default.homeDirectoryForCurrentUser,
                               environment: [String: String] = ProcessInfo.processInfo.environment) -> (skillsRoot: URL, instructionsPath: URL) {
        if client == .codex {
            let directory = environment["CODEX_HOME"].flatMap { $0.hasPrefix("/") ? URL(fileURLWithPath: $0) : nil }
                ?? home.appendingPathComponent(".codex")
            let override = directory.appendingPathComponent("AGENTS.override.md")
            let instructions = FileManager.default.fileExists(atPath: override.path) ? override : directory.appendingPathComponent("AGENTS.md")
            return (home.appendingPathComponent(".agents/skills"), instructions)
        }
        let directory = environment["CLAUDE_CONFIG_DIR"].flatMap { $0.hasPrefix("/") ? URL(fileURLWithPath: $0) : nil }
            ?? home.appendingPathComponent(".claude")
        return (directory.appendingPathComponent("skills"), directory.appendingPathComponent("CLAUDE.md"))
    }

    /// Reference rules contain no credential or knowledge content.
    static func runReference(runtime: URL, action: String, client: LocalAgentClient, connectionID: String,
                             skillsRoot: URL? = nil, instructionsPath: URL? = nil, configPath: URL? = nil) throws -> LocalAgentInstallResult {
        guard client != .workbuddy, ["reference_status", "reference_enable", "reference_disable"].contains(action),
              UUID(uuidString: connectionID) != nil else { throw failure("该 Agent 暂不支持自动参考知识库。") }
        let paths = referencePaths(client: client)
        let request: [String: Any] = ["action": action, "client": client == .codex ? "codex" : "claudeCode",
                                      "connection_id": connectionID,
                                      "skills_root": (skillsRoot ?? paths.skillsRoot).path,
                                      "instructions_path": (instructionsPath ?? paths.instructionsPath).path,
                                      "config_path": (configPath ?? configurationPath(client: client)).path]
        return try runRequest(runtime: runtime, request: request)
    }

}

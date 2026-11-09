import Foundation

@main struct LocalAgentConnectionsTest {
    @MainActor static func main() async throws {
        let id = "8331a1c2-a6f8-4ba0-a92b-31c588c5b7dc"
        let body: [String: Any] = ["id": id, "client_name": "Codex 测试", "status": "active", "created_at": "2026-10-03T00:00:00Z",
                                   "expires_at": "2026-11-02T00:00:00Z", "revoked_at": NSNull(), "domain_ids": ["domain"]]
        let active = try decodeLocalAI(LocalAgentConnection.self, body: body)
        precondition(active.statusText == "有效" && active.domain_ids == ["domain"] && active.revoked_at == nil)
        let shanghai = TimeZone(identifier: "Asia/Shanghai")!
        let timestamp = LocalAgentConnection(id: id, client_name: "Codex", status: "revoked", created_at: "", expires_at: "2026-11-02T11:54:58.078603", revoked_at: nil, domain_ids: [])
        precondition(timestamp.expirationText(timeZone: shanghai).hasPrefix("2026-11-02 19:54:58"))
        let offsetTimestamp = LocalAgentConnection(id: id, client_name: "Codex", status: "active", created_at: "", expires_at: "2026-11-02T19:54:58+08:00", revoked_at: nil, domain_ids: [])
        precondition(offsetTimestamp.expirationText(timeZone: TimeZone(secondsFromGMT: 0)!).hasPrefix("2026-11-02 11:54:58"))
        let daylight = LocalAgentConnection(id: id, client_name: "Codex", status: "active", created_at: "", expires_at: "2026-07-01T12:00:00Z", revoked_at: nil, domain_ids: [])
        precondition(daylight.expirationText(timeZone: TimeZone(identifier: "America/New_York")!).hasPrefix("2026-07-01 08:00:00"))

        var changed = body; changed["status"] = "expired"
        let expired = try decodeLocalAI(LocalAgentConnection.self, body: changed)
        precondition(expired.statusText == "已过期")
        changed["status"] = "revoked"; changed["revoked_at"] = "2026-10-03T01:00:00Z"
        let revoked = try decodeLocalAI(LocalAgentConnection.self, body: changed)
        precondition(revoked.statusText == "已撤销")
        precondition(LocalAgentConnection.reusable(in: [expired, revoked], name: "Codex 测试", domainIDs: ["domain"]) == nil)
        precondition(LocalAgentConnection.reusable(in: [active], name: "Claude Code", domainIDs: ["domain"]) == nil)
        precondition(LocalAgentConnection.reusable(in: [active], name: "Codex 测试", domainIDs: ["other-domain"]) == nil)
        precondition(LocalAgentConnection.reusable(in: [active], name: "Codex 测试", domainIDs: ["domain", "other-domain"]) == nil)
        precondition(LocalAgentConnection.reusable(in: [revoked, active, expired], name: "Codex 测试", domainIDs: ["domain"])?.id == active.id)
        var newerBody = body
        newerBody["id"] = "76f1c5ce-a321-49e1-969c-f47f130d42ad"
        newerBody["created_at"] = "2026-10-03T01:00:00Z"
        newerBody["domain_ids"] = ["domain", "other-domain"]
        let newer = try decodeLocalAI(LocalAgentConnection.self, body: newerBody)
        var olderBody = newerBody
        olderBody["id"] = active.id
        olderBody["created_at"] = active.created_at
        let older = try decodeLocalAI(LocalAgentConnection.self, body: olderBody)
        precondition(LocalAgentConnection.reusable(in: [older, newer], name: "Codex 测试", domainIDs: ["other-domain", "domain"])?.id == newer.id)
        precondition(LocalAgentConnection.reusable(in: [newer, older], name: "Codex 测试", domainIDs: ["domain", "other-domain"])?.id == newer.id)
        var codexBody = newerBody
        codexBody["client_name"] = "Codex"
        let currentCodex = try decodeLocalAI(LocalAgentConnection.self, body: codexBody)
        codexBody["id"] = active.id; codexBody["created_at"] = active.created_at
        let previousCodex = try decodeLocalAI(LocalAgentConnection.self, body: codexBody)
        var claudeBody = codexBody; claudeBody["client_name"] = "Claude Code"
        let claude = try decodeLocalAI(LocalAgentConnection.self, body: claudeBody)
        precondition(LocalAgentConnection.current(in: [previousCodex, currentCodex, claude], client: .codex)?.id == currentCodex.id)
        precondition(LocalAgentConnection.current(in: [currentCodex, claude], client: .claudeCode)?.client_name == "Claude Code")
        precondition(LocalAgentConnection.current(in: [currentCodex], client: .workbuddy) == nil)
        codexBody["status"] = "superseded"
        let replaced = try decodeLocalAI(LocalAgentConnection.self, body: codexBody)
        precondition(replaced.statusText == "已替换" && LocalAgentConnection.current(in: [replaced], client: .codex) == nil)
        let update = try decodeLocalAI(LocalAgentInstallResult.self, body: ["installed": true, "update_required": true, "message": "路径需要更新"])
        precondition(update.update_required == true && update.installed == true)
        let legacyStatus = try decodeLocalAI(LocalAgentInstallResult.self, body: ["installed": true, "message": "旧版本状态"])
        precondition(legacyStatus.update_required == nil)
        let created = try decodeLocalAI(LocalAgentCreated.self, body: ["id": id, "access_token": "synthetic-agent-token"])
        precondition(created.access_token == "synthetic-agent-token")
        let library = LocalLibraryDescriptor(id: "synthetic-library", data: URL(fileURLWithPath: "/tmp/知识 \"库\"\\目录"),
                                            cache: URL(fileURLWithPath: "/tmp/cache"), logs: URL(fileURLWithPath: "/tmp/logs"),
                                            credentialService: "local.pka.synthetic." + UUID().uuidString)
        let service = try LocalAgentConfiguration.credentialService(library: library, connectionID: id.uppercased())
        precondition(service == library.credentialService + ".agent." + id)
        do {
            _ = try LocalAgentConfiguration.credentialService(library: library, connectionID: "../../owner")
            fatalError("无效连接标识应被拒绝")
        } catch {}
        precondition(LocalAgentConfiguration.tomlString("a\"b\\c\nd\re\tf\u{0000}\u{001F}\u{007F}") == "\"a\\\"b\\\\c\\nd\\re\\tf\\u0000\\u001F\\u007F\"")
        let runtime = URL(fileURLWithPath: "/tmp/应用 \"runtime\"\\目录")
        let syntheticHome = URL(fileURLWithPath: "/private/tmp/pka-synthetic-agent-home")
        precondition(LocalAgentInstaller.configurationPath(client: .codex, home: syntheticHome, environment: [:]).path == syntheticHome.appendingPathComponent(".codex/config.toml").path)
        precondition(LocalAgentInstaller.configurationPath(client: .claudeCode, home: syntheticHome, environment: ["CLAUDE_CONFIG_DIR": "/private/tmp/custom-claude"]).path == "/private/tmp/custom-claude/.claude.json")
        let config = try LocalAgentConfiguration.codex(runtime: runtime, library: library, connectionID: id, agentToken: created.access_token)
        precondition(config.contains("args = [\"-I\", \"-B\","))
        precondition(config.contains("PKA_API_FILE") && config.contains("agent-api.json") && config.contains("PKA_AGENT_TOKEN"))
        precondition(!config.contains("PKA_APP_TOKEN") && !config.contains("owner-token"))
        precondition(!config.contains("http://") && !config.contains("https://"))
        precondition(config.contains("mcp_servers.knowledge_8331a1c2_a6f8_4ba0_a92b_31c588c5b7dc"))
        do {
            _ = try LocalAgentConfiguration.codex(runtime: runtime, library: library, connectionID: id, agentToken: "")
            fatalError("空凭证应被拒绝")
        } catch {}
        for client in [LocalAgentClient.claudeCode, .workbuddy] {
            let token = "synthetic\"\\\n\t\u{0000}中文"
            let configuration = try LocalAgentConfiguration.configuration(for: client, runtime: runtime, library: library, connectionID: id, agentToken: token)
            let document = try JSONSerialization.jsonObject(with: Data(configuration.utf8)) as! [String: Any]
            let servers = document["mcpServers"] as! [String: [String: Any]]
            precondition(servers.count == 1 && servers.keys.first == "knowledge_8331a1c2_a6f8_4ba0_a92b_31c588c5b7dc")
            let server = servers.values.first!
            precondition(server["command"] as? String == runtime.appendingPathComponent("python/bin/python3").path)
            precondition(server["args"] as? [String] == ["-I", "-B", runtime.appendingPathComponent("backend/app/local_mcp.py").path])
            precondition(server["type"] as? String == (client == .claudeCode ? "stdio" : nil))
            let environment = server["env"] as! [String: String]
            precondition(environment == ["PKA_API_FILE": library.data.appendingPathComponent("agent-api.json").path, "PKA_AGENT_TOKEN": token])
            do {
                _ = try LocalAgentConfiguration.configuration(for: client, runtime: runtime, library: library, connectionID: "invalid", agentToken: token)
                fatalError("无效 JSON 连接标识应被拒绝")
            } catch {}
            do {
                _ = try LocalAgentConfiguration.configuration(for: client, runtime: runtime, library: library, connectionID: id, agentToken: "")
                fatalError("空 JSON 凭证应被拒绝")
            } catch {}
        }
        do {
            _ = try LocalAgentConfiguration.configuration(for: .claudeCode, runtime: URL(fileURLWithPath: "/tmp/${HOME}/runtime"), library: library, connectionID: id, agentToken: created.access_token)
            fatalError("Claude 环境变量展开路径应被拒绝")
        } catch {}
        if let index = CommandLine.arguments.firstIndex(of: "--toml-output"), CommandLine.arguments.indices.contains(index + 1) {
            try config.write(toFile: CommandLine.arguments[index + 1], atomically: true, encoding: .utf8)
        }
        let fakeHome = URL(fileURLWithPath: "/tmp/pka-reference-home")
        let codexPaths = LocalAgentInstaller.referencePaths(client: .codex, home: fakeHome, environment: [:])
        precondition(codexPaths.skillsRoot.path == fakeHome.appendingPathComponent(".agents/skills").path)
        precondition(codexPaths.instructionsPath.path == fakeHome.appendingPathComponent(".codex/AGENTS.md").path)
        let claudePaths = LocalAgentInstaller.referencePaths(client: .claudeCode, home: fakeHome, environment: ["CLAUDE_CONFIG_DIR": "/tmp/pka-claude-settings"])
        precondition(claudePaths.skillsRoot.path == "/tmp/pka-claude-settings/skills")
        precondition(claudePaths.instructionsPath.path == "/tmp/pka-claude-settings/CLAUDE.md")
        if CommandLine.arguments.contains("--keychain") {
            defer { TokenStore.delete(service: service) }
            precondition(TokenStore.save(created.access_token, service: service))
            precondition(TokenStore.load(service: service) == created.access_token)
            TokenStore.delete(service: service)
            precondition(TokenStore.load(service: service) == nil)
            print("PASS: 合成独立 Agent 凭证钥匙串保存、读取与删除；未使用用户凭证")
        }
        print("PASS: 连接 DTO、状态、同名有效连接按授权范围复用、独立凭证作用域、三种客户端配置、TOML/JSON 特殊字符还原、Claude 路径展开保护及无效输入边界")
        if let index = CommandLine.arguments.firstIndex(of: "--runtime"), CommandLine.arguments.indices.contains(index + 1) {
            try await endToEnd(runtime: URL(fileURLWithPath: CommandLine.arguments[index + 1]))
        }
    }
}

extension LocalAgentConnectionsTest {
    @MainActor static func endToEnd(runtime: URL) async throws {
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent("pka-agent-connections-e2e-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        try manager.createDirectory(at: root.appendingPathComponent("home"), withIntermediateDirectories: true,
                                    attributes: [.posixPermissions: 0o700])
        let ownerService = "synthetic.pka.agent-connections-e2e.\(UUID().uuidString)"
        let service = LocalService(home: root.appendingPathComponent("home"), credentialService: ownerService)
        let library = try service.currentLibrary()
        var agentServices: [String] = []
        defer {
            _ = service.stopAndWait()
            TokenStore.delete(service: ownerService)
            for item in agentServices { TokenStore.delete(service: item) }
            try? manager.removeItem(at: root)
        }
        let model = CaptureModel(queueStore: CaptureQueueStore(root: library.data.appendingPathComponent("capture-queue")), localService: service, runtime: runtime)
        await model.startLocalService()
        try check(model.connected, "本地服务启动失败：\(model.authMessage)")
        var ownerAPI = try API(address: model.address, token: model.token)
        var connections = try LocalAgentConnectionsAPI(address: model.address, token: model.token)
        let domain = try await ownerAPI.call("v1/domains", method: "POST", json: ["name": "合成 Agent 授权领域"])
        let privateDomain = try await ownerAPI.call("v1/domains", method: "POST", json: ["name": "合成 Agent 未授权领域"])
        guard let domainID = domain["id"] as? String, let privateDomainID = privateDomain["id"] as? String else { throw failure("合成领域创建失败") }
        let marker = "agentisolationsynthetic"
        func capture(domainID: String, title: String) async throws -> String {
            let result = try await ownerAPI.call("v1/captures", method: "POST", json: [
                "capture_type": "browser_selection", "title": title, "coverage": "partial_text",
                "content": ["text": marker + " 合成原文，仅用于领域授权验证。", "image_urls": []] as [String: Any],
                "context": ["domain_id": domainID] as [String: Any],
                "client": ["kind": "desktop", "version": "synthetic-agent-test"]], key: UUID().uuidString)
            guard let id = result["id"] as? String else { throw failure("合成资料创建失败") }
            return id
        }
        let sourceID = try await capture(domainID: domainID, title: "合成授权资料")
        let privateSourceID = try await capture(domainID: privateDomainID, title: "合成未授权资料")
        let created = try await connections.create(name: "Codex 合成连接", domainIDs: [domainID], days: 1)
        let keychainService = try LocalAgentConfiguration.credentialService(library: library, connectionID: created.id)
        agentServices.append(keychainService)
        try check(TokenStore.save(created.access_token, service: keychainService), "合成 Agent 凭证保存失败")
        try check(TokenStore.load(service: keychainService) == created.access_token, "合成 Agent 凭证不能恢复")
        let listed = try await connections.list()
        try check(listed.contains(where: { $0.id == created.id && $0.status == "active" && $0.domain_ids == [domainID] }), "连接列表缺失有效授权")
        let endpoint = library.data.appendingPathComponent("agent-api.json")
        let firstEndpoint = try JSONSerialization.jsonObject(with: Data(contentsOf: endpoint)) as? [String: Any]
        try check(firstEndpoint?["api_url"] as? String == model.address, "发布的 Agent 地址不匹配")
        try check(firstEndpoint?["instance_id"] as? String != nil, "缺少服务实例标识")
        try check(firstEndpoint?["token"] == nil && firstEndpoint?["access_token"] == nil, "端点文件不得包含凭证")
        let config = try LocalAgentConfiguration.codex(runtime: runtime, library: library, connectionID: created.id, agentToken: created.access_token)
        let configFile = root.appendingPathComponent("codex.toml")
        try config.write(to: configFile, atomically: true, encoding: .utf8)
        let parser = Process()
        parser.executableURL = runtime.appendingPathComponent("python/bin/python3")
        parser.arguments = ["-I", "-B", "-c", "import sys,tomli,pathlib; c=next(iter(tomli.loads(pathlib.Path(sys.argv[1]).read_text())['mcp_servers'].values())); assert c['args'][:2]==['-I','-B']; assert pathlib.Path(c['command']).is_file(); assert pathlib.Path(c['args'][2]).is_file(); assert c['env']['PKA_API_FILE']==sys.argv[2]; assert c['env']['PKA_AGENT_TOKEN']==sys.argv[3]", configFile.path, endpoint.path, created.access_token]
        parser.standardOutput = FileHandle.nullDevice; parser.standardError = FileHandle.nullDevice
        try parser.run(); parser.waitUntilExit()
        try check(parser.terminationStatus == 0, "生成配置不能按 TOML 解析或缺少实际执行文件")
        var agentAPI = try API(address: model.address, token: created.access_token)
        var found = false
        for _ in 0..<120 {
            let search = try await agentAPI.call("v1/mcp/tools/search_knowledge", method: "POST", json: ["query": marker, "limit": 20])
            let ids = (search["items"] as? [[String: Any]] ?? []).compactMap { $0["source_id"] as? String }
            try check(!ids.contains(privateSourceID), "Agent 读取了未授权领域")
            if ids.contains(sourceID) { found = true; break }
            try await Task.sleep(nanoseconds: 500_000_000)
        }
        try check(found, "Agent 未能检索授权资料")
        // Foundation normalizes /private/var back to /var; use the physical temp path for strict file checks.
        let physicalRoot = root.path.hasPrefix("/var/") ? URL(fileURLWithPath: "/private" + root.path) : root
        let privateConfigRoot = physicalRoot.appendingPathComponent("isolated-configs")
        try manager.createDirectory(at: privateConfigRoot, withIntermediateDirectories: true)
        var installedConfigs: [(LocalAgentClient, URL, String)] = []
        for client in [LocalAgentClient.codex, .claudeCode] {
            let file = privateConfigRoot.appendingPathComponent(client == .codex ? "config.toml" : ".claude.json")
            let original = client == .codex ? "model = \"synthetic-model\"\n" : "{\"theme\":\"synthetic-theme\",\"mcpServers\":{}}"
            try original.write(to: file, atomically: true, encoding: .utf8)
            let configuration = try LocalAgentConfiguration.configuration(for: client, runtime: runtime, library: library, connectionID: created.id, agentToken: created.access_token)
            let installed = try LocalAgentInstaller.run(runtime: runtime, action: "install", client: client, connectionID: created.id, configuration: configuration, configPath: file)
            try check(installed.installed == true && installed.checked == true && installed.backup_path != nil, "一键安装或真实 MCP 检查失败")
            try check(try String(contentsOfFile: installed.backup_path!, encoding: .utf8) == original, "原配置备份不一致")
            let status = try LocalAgentInstaller.run(runtime: runtime, action: "status", client: client, connectionID: created.id, configuration: configuration, configPath: file)
            try check(status.installed == true && status.checked == false, "配置状态检查不正确")
            let checked = try LocalAgentInstaller.run(runtime: runtime, action: "check", client: client, connectionID: created.id, configuration: configuration, configPath: file)
            try check(checked.checked == true, "连接检查未通过")
            let rules = privateConfigRoot.appendingPathComponent(client == .codex ? "AGENTS.md" : "CLAUDE.md")
            let skillRoot = privateConfigRoot.appendingPathComponent(client.rawValue + "-skills")
            let originalRules = "# 合成原指令\n保留这条规则。"
            try originalRules.write(to: rules, atomically: true, encoding: .utf8)
            let beforeReference = try LocalAgentInstaller.runReference(runtime: runtime, action: "reference_status", client: client, connectionID: created.id, skillsRoot: skillRoot, instructionsPath: rules, configPath: file)
            try check(beforeReference.auto_reference_enabled == false, "不应默认开启自动参考")
            let enabled = try LocalAgentInstaller.runReference(runtime: runtime, action: "reference_enable", client: client, connectionID: created.id, skillsRoot: skillRoot, instructionsPath: rules, configPath: file)
            try check(enabled.auto_reference_enabled == true, "Skill 和规则安装失败")
            let skillFile = skillRoot.appendingPathComponent("pka-knowledge-" + created.id).appendingPathComponent("SKILL.md")
            let skillText = try String(contentsOf: skillFile, encoding: .utf8)
            try check(!skillText.contains(created.access_token) && !skillText.contains("PKA_AGENT_TOKEN"), "Skill 不得包含凭证")
            let referenceStatus = try LocalAgentInstaller.runReference(runtime: runtime, action: "reference_status", client: client, connectionID: created.id, skillsRoot: skillRoot, instructionsPath: rules, configPath: file)
            try check(referenceStatus.auto_reference_enabled == true, "自动参考实际状态错误")
            let disabled = try LocalAgentInstaller.runReference(runtime: runtime, action: "reference_disable", client: client, connectionID: created.id, skillsRoot: skillRoot, instructionsPath: rules, configPath: file)
            try check(disabled.auto_reference_enabled == false && !manager.fileExists(atPath: skillFile.path), "Skill 关闭未清理")
            try check(try String(contentsOf: rules, encoding: .utf8) == originalRules, "关闭未精确保留原指令")
            // Removing a stale configuration must work after its local credential is gone.
            let removed = try LocalAgentInstaller.removeConfiguration(runtime: runtime, client: client, connectionID: created.id,
                endpoint: library.data.appendingPathComponent("agent-api.json"), configPath: file)
            try check(removed.installed == false, "移除本连接失败")
            let remaining = try String(contentsOf: file, encoding: .utf8)
            try check(remaining.contains(client == .codex ? "synthetic-model" : "synthetic-theme") && !remaining.contains(created.access_token), "断开未保留原设置或未删除凭证")
            _ = try LocalAgentInstaller.run(runtime: runtime, action: "install", client: client, connectionID: created.id, configuration: configuration, configPath: file)
            installedConfigs.append((client, file, configuration))
        }
        print("PASS: 随包自动参考 Skill/规则开关与原指令精确保留、一键安装/状态/真实 MCP 检查/私有备份/移除和原设置保留；未修改用户配置")
        let repository = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        var jsonConfigs: [URL] = []
        for client in [LocalAgentClient.claudeCode, .workbuddy] {
            let configuration = try LocalAgentConfiguration.configuration(for: client, runtime: runtime, library: library, connectionID: created.id, agentToken: created.access_token)
            let file = root.appendingPathComponent(client == .claudeCode ? "claude.json" : "workbuddy.json")
            try configuration.write(to: file, atomically: true, encoding: .utf8)
            jsonConfigs.append(file)
        }
        func verifyJSON(_ file: URL, revoked: Bool = false) throws {
            let process = Process()
            process.executableURL = runtime.appendingPathComponent("python/bin/python3")
            process.arguments = ["-I", "-B", repository.appendingPathComponent("scripts/verify_agent_json.py").path,
                                 "--config", file.path, "--query", marker, "--allowed-source", sourceID, "--forbidden-source", privateSourceID]
            if revoked { process.arguments?.append("--expect-revoked") }
            try process.run(); process.waitUntilExit()
            try check(process.terminationStatus == 0, "JSON 配置的真实 stdio 协议验收失败")
        }
        for file in jsonConfigs { try verifyJSON(file) }
        if CommandLine.arguments.contains("--claude-cli") {
            let process = Process()
            process.executableURL = runtime.appendingPathComponent("python/bin/python3")
            process.arguments = ["-I", "-B", repository.appendingPathComponent("scripts/verify_claude_mcp_config.py").path, "--config", jsonConfigs[0].path]
            try process.run(); process.waitUntilExit()
            try check(process.terminationStatus == 0, "隔离 Claude Code CLI 配置加载失败")
        }
        _ = service.stopAndWait()
        model.cancelLocalServiceStart()
        try check(!manager.fileExists(atPath: endpoint.path), "本地服务停止后端点文件未删除")
        await model.startLocalService()
        try check(model.connected, "本地服务重启失败")
        let nextEndpoint = try JSONSerialization.jsonObject(with: Data(contentsOf: endpoint)) as? [String: Any]
        try check(nextEndpoint?["api_url"] as? String == model.address && nextEndpoint?["instance_id"] as? String != firstEndpoint?["instance_id"] as? String, "重启未更新服务实例")
        agentAPI = try API(address: model.address, token: created.access_token)
        let restartedSearch = try await agentAPI.call("v1/mcp/tools/search_knowledge", method: "POST", json: ["query": marker, "limit": 20])
        try check((restartedSearch["items"] as? [[String: Any]] ?? []).contains(where: { $0["source_id"] as? String == sourceID }), "重启后独立连接凭证失效")
        for file in jsonConfigs { try verifyJSON(file) }
        for (client, file, configuration) in installedConfigs {
            let checked = try LocalAgentInstaller.run(runtime: runtime, action: "check", client: client, connectionID: created.id, configuration: configuration, configPath: file)
            try check(checked.checked == true, "已安装配置跨重启检查失败")
        }
        // Migrate a legacy connection using only synthetic configuration and original user instructions.
        let codexFile = privateConfigRoot.appendingPathComponent("config.toml")
        let rules = privateConfigRoot.appendingPathComponent("AGENTS.md")
        let skills = privateConfigRoot.appendingPathComponent("Codex-skills")
        let originalRules = try Data(contentsOf: rules)
        _ = try LocalAgentInstaller.runReference(runtime: runtime, action: "reference_enable", client: .codex,
            connectionID: created.id, skillsRoot: skills, instructionsPath: rules, configPath: codexFile)
        let pluginHome = privateConfigRoot.appendingPathComponent("plugin-home")
        func plugin(_ action: String, configuration: String? = nil) throws -> LocalAgentInstallResult {
            try LocalAgentInstaller.runPlugin(runtime: runtime, action: action, connectionID: created.id, endpoint: endpoint,
                configuration: configuration, home: pluginHome, configPath: codexFile, skillsRoot: skills, instructionsPath: rules)
        }
        let prepared = try plugin("plugin_install", configuration: config)
        try check(prepared.installed == true && prepared.checked == true && prepared.host_installed != true, "插件生成状态错误")
        try check(try Data(contentsOf: rules) != originalRules, "宿主安装前不应移除旧规则")
        var rejectedBeforeHostInstall = false
        do {
            _ = try plugin("plugin_complete")
        } catch let error as NSError {
            rejectedBeforeHostInstall = true
            try check(error.localizedDescription.contains("Codex"), "未安装提示不明确")
        }
        try check(rejectedBeforeHostInstall, "尚未安装到 Codex 就完成了迁移")
        // Simulate the host's package copy; real host discovery is verified separately by Codex itself.
        let cache = codexFile.deletingLastPathComponent().appendingPathComponent("plugins/cache")
            .appendingPathComponent(prepared.marketplace_name!).appendingPathComponent(prepared.plugin_name!).appendingPathComponent("local")
        try manager.createDirectory(at: cache.deletingLastPathComponent(), withIntermediateDirectories: true)
        try manager.copyItem(at: URL(fileURLWithPath: prepared.plugin_path!), to: cache)
        let migrated = try plugin("plugin_complete")
        try check(migrated.host_installed == true && migrated.checked == true, "宿主插件验证或迁移失败")
        try check(try Data(contentsOf: rules) == originalRules, "迁移未保留原始用户指令")
        try check(!manager.fileExists(atPath: skills.appendingPathComponent("pka-knowledge-" + created.id + "/SKILL.md").path), "旧独立 Skill 未清理")
        let pluginConfig = try String(contentsOf: codexFile, encoding: .utf8)
        try check(!pluginConfig.contains(created.access_token) && !pluginConfig.contains("[mcp_servers."), "插件迁移残留旧 MCP 凭证或重复连接")
        let pluginFiles = URL(fileURLWithPath: migrated.plugin_path!)
        for case let file as URL in manager.enumerator(at: pluginFiles, includingPropertiesForKeys: [.isRegularFileKey])! {
            if try file.resourceValues(forKeys: [.isRegularFileKey]).isRegularFile == true {
                try check(!(try String(contentsOf: file, encoding: .utf8)).contains(created.access_token), "插件包包含访问凭证")
            }
        }
        try check(try plugin("plugin_status").auto_reference_enabled == true, "插件未启用")
        try check(try plugin("plugin_disable").auto_reference_enabled == false, "插件停用未生效")
        try check(try plugin("plugin_enable").auto_reference_enabled == true, "插件恢复未生效")
        try check(try plugin("plugin_check").checked == true, "无钥匙串读取的插件连接检查失败")
        try check(try plugin("plugin_remove").installed == false, "插件未移除")
        try check(try String(contentsOf: codexFile, encoding: .utf8).contains("synthetic-model"), "插件卸载丢失其他设置")
        try check(try Data(contentsOf: rules) == originalRules, "插件启停或卸载改写 AGENTS.md")
        print("PASS: 原生插件安装与真实 MCP、旧接入迁移、原指令保留、凭证隔离、启停及卸载；未修改用户配置")
        connections = try LocalAgentConnectionsAPI(address: model.address, token: model.token)
        ownerAPI = try API(address: model.address, token: model.token)
        try await connections.revoke(id: created.id)
        do {
            _ = try await agentAPI.call("v1/mcp/tools/search_knowledge", method: "POST", json: ["query": marker])
            throw failure("撤销后 Agent 仍然可以访问")
        } catch let error as NSError {
            try check(error.userInfo["HTTPStatus"] as? Int == 401, "撤销未立即返回 401")
        }
        let revoked = try await connections.list()
        try check(revoked.contains(where: { $0.id == created.id && $0.status == "revoked" && $0.revoked_at != nil }), "撤销状态未持久保存")
        for file in jsonConfigs { try verifyJSON(file, revoked: true) }
        print("PASS: 真实本地服务连接创建/列表、合成独立凭证钥匙串恢复、实际 Codex TOML 解析、授权领域检索隔离、停止清理及重启端点更新、同凭证跨重启、撤销立即 401；无真实资料和模型调用")
    }
    static func check(_ condition: @autoclosure () throws -> Bool, _ message: String) throws {
        if try !condition() { throw failure(message) }
    }
}

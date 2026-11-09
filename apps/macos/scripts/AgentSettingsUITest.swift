import AppKit
import SwiftUI

@main struct AgentSettingsUITest: App {
    @StateObject private var model: CaptureModel
    private let home: URL
    @State private var ready = false
    @State private var failureMessage = ""

    init() {
        precondition(Bundle.main.bundleIdentifier == "local.pka.agent-settings-ui-test")
        let path = try! String(contentsOf: Bundle.main.resourceURL!.appendingPathComponent("state-path.txt"), encoding: .utf8)
        precondition(path.contains("/apps/macos/.build/agent-settings-ui-home-") && !path.contains("/../"))
        home = URL(fileURLWithPath: path).standardizedFileURL
        let service = LocalService(home: home, credentialService: "synthetic.agent-settings-ui")
        _model = StateObject(wrappedValue: CaptureModel(localService: service, runtime: MacKernelHost.runtime))
    }

    var body: some Scene {
        Window("Agent 接入 · 隔离验收", id: "agents") {
            VStack(spacing: 0) {
                HStack {
                    Text("Agent 接入").font(.headline)
                    Spacer()
                    Text("36 个合成知识库 · 不访问用户配置").font(.caption).foregroundStyle(InboxStyle.muted)
                }.padding(24)
                Divider()
                ScrollView {
                    if ready {
                        LocalAgentConnectionsView(model: model, installer: LocalAgentIntegration(home: home)).padding(24)
                    } else if !failureMessage.isEmpty { Text(failureMessage).padding() }
                    else { ProgressView("正在准备合成资料库…").padding() }
                }
            }
            .frame(width: 660, height: 760)
            .background(InboxStyle.canvas).foregroundStyle(InboxStyle.text).tint(InboxStyle.accent)
            .preferredColorScheme(.dark).font(.system(size: 13))
            .buttonStyle(InboxButtonStyle(.secondary)).textFieldStyle(InboxFieldStyle()).toggleStyle(.switch)
            .task { await seed() }
        }
    }

    @MainActor private func seed() async {
        guard !ready else { return }
        do {
            await model.startLocalService()
            guard model.connected else { throw failure(model.authMessage) }
            let api = try API(address: model.address, token: model.token)
            var domains: [DomainItem] = []
            for index in 1...36 {
                let result = try await api.call("v1/domains", method: "POST", json: ["name": String(format: "知识库 %02d · 合成资料", index)])
                domains.append(DomainItem(id: result["id"] as! String, name: result["name"] as! String))
            }
            model.domains = domains
            let agent = try LocalAgentConnectionsAPI(address: model.address, token: model.token)
            _ = try await agent.create(name: "Codex", domainIDs: [domains[0].id], days: 30)
            _ = try await agent.create(name: "Codex", domainIDs: [domains[1].id], days: 30)
            _ = try await agent.create(name: "Claude Code", domainIDs: [domains[2].id], days: 30)
            let config = home.appendingPathComponent(".codex/config.toml")
            try FileManager.default.createDirectory(at: config.deletingLastPathComponent(), withIntermediateDirectories: true)
            try "# synthetic configuration\n".write(to: config, atomically: true, encoding: .utf8)
            ready = true
        } catch { failureMessage = error.localizedDescription }
    }
}

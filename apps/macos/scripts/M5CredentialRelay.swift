import Foundation

/// Invoked only by the approved acceptance runner, with stdout connected to a
/// private pipe. Never run this program as an interactive terminal command.
@main struct M5CredentialRelay {
    static func main() {
        var stage = "library"
        do {
            let library = try MacLibraryStore(home: FileManager.default.homeDirectoryForCurrentUser,
                credentialService: LocalService.credentialService).current()
            stage = "configuration"
            guard let defaults = UserDefaults(suiteName: "local.pka.knowledge-inbox"),
                  defaults.string(forKey: LocalModelConfiguration.key(library) + ".mode") == "direct",
                  let data = defaults.data(forKey: LocalModelConfiguration.key(library)) else {
                throw failure("当前客户端没有可读取的直接模型配置。")
            }
            stage = "options"
            let options = try JSONDecoder().decode(LocalModelOptions.self, from: data).validated()
            stage = "provider"
            guard options.provider == "deepseek", URL(string: options.baseURL)?.host == "api.deepseek.com" else {
                throw failure("当前配置不是本次授权的官方DeepSeek接口，尚未读取密钥。")
            }
            stage = "credential"
            guard case .available(let secret) = KeychainCredentialStore().read(service: LocalModelConfiguration.key(library)) else {
                throw failure("模型凭证缺失或暂时无法读取，本轮没有调用模型。")
            }
            let profile = LocalModelProfile(mode: "direct", status: "ready", provider: options.provider,
                base_url: options.baseURL, model: options.model, vision_model: options.visionModel,
                credential_ref: "m5-isolated-memory")
            let startup = LocalModelStartup(profile: profile, provider_key: secret, gateway_token: UUID().uuidString)
            var output = try JSONEncoder().encode(startup)
            output.append(10)
            try FileHandle.standardOutput.write(contentsOf: output)
        } catch {
            // Fixed message only. Do not stringify a credential-bearing value.
            FileHandle.standardError.write(Data("configuration_unavailable stage=\(stage)\n".utf8))
            exit(1)
        }
    }
}

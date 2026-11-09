import Foundation

struct LocalModelOptions: Codable, Equatable {
    var provider = "deepseek"
    var baseURL = "https://api.deepseek.com"
    var model = ""
    var visionModel = ""

    func validated() throws -> Self {
        var result = self
        result.baseURL = baseURL.trimmingCharacters(in: .whitespacesAndNewlines)
        result.model = model.trimmingCharacters(in: .whitespacesAndNewlines)
        result.visionModel = visionModel.trimmingCharacters(in: .whitespacesAndNewlines)
        guard ["deepseek", "openai-compatible"].contains(provider),
              let url = URL(string: result.baseURL), url.scheme == "https", url.host != nil,
              url.user == nil, url.password == nil, url.query == nil, url.fragment == nil,
              !result.model.isEmpty, result.model.count <= 200, result.visionModel.count <= 200,
              !(result.model + result.visionModel).contains(where: { $0.isNewline || $0 == "\0" }) else {
            throw failure("请填写有效的 HTTPS API 地址和模型名称，地址不能包含密钥、查询参数或片段。")
        }
        guard !url.path.hasSuffix("/chat/completions") else { throw failure("API 地址请填写基础路径，不要包含 /chat/completions。") }
        if provider == "deepseek", !result.visionModel.isEmpty,
           !["deepseek-flash", "deepseek-v4-flash-vision-exp"].contains(result.visionModel) {
            throw failure("当前 DeepSeek 图片分析仅支持 deepseek-flash 或 deepseek-v4-flash-vision-exp。")
        }
        result.baseURL = result.baseURL.trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        return result
    }
}

enum LocalModelConfiguration {
    static func key(_ library: LocalLibraryDescriptor) -> String { library.credentialService + ".model" }
    static func isDirect(_ library: LocalLibraryDescriptor) -> Bool { UserDefaults.standard.string(forKey: key(library) + ".mode") == "direct" }
    static func load(_ library: LocalLibraryDescriptor) -> LocalModelOptions {
        guard let data = UserDefaults.standard.data(forKey: key(library)),
              let value = try? JSONDecoder().decode(LocalModelOptions.self, from: data) else { return LocalModelOptions() }
        return value
    }
    static func profile(_ library: LocalLibraryDescriptor) -> LocalModelProfile {
        let storage = key(library) + ".profile.v1"
        if let bytes = UserDefaults.standard.data(forKey: storage),
           let stored = try? JSONDecoder().decode(LocalModelProfile.self, from: bytes),
           UUID(uuidString: stored.profile_id) != nil, UUID(uuidString: stored.credential_generation) != nil,
           stored.config_revision > 0, stored.config_revision < 2_147_483_647,
           ["direct", "external", "disabled"].contains(stored.mode),
           stored.credential_ref == (stored.mode == "direct" ? key(library) : LocalAIConfiguration.tokenService(library)) { return stored }
        let legacyMode = UserDefaults.standard.string(forKey: key(library) + ".mode") ?? "external"
        let mode = ["direct", "external", "disabled"].contains(legacyMode) ? legacyMode : "disabled"
        let migrated = LocalModelProfile(mode: mode, credential_ref: mode == "direct" ? key(library) : LocalAIConfiguration.tokenService(library))
        persistProfile(migrated, library: library)
        return migrated
    }
    @discardableResult private static func persistProfile(_ profile: LocalModelProfile, library: LocalLibraryDescriptor) -> Bool {
        UserDefaults.standard.set(try? JSONEncoder().encode(profile), forKey: key(library) + ".profile.v1")
        return UserDefaults.standard.synchronize()
    }
    static func beginUpdate(_ library: LocalLibraryDescriptor, mode: String, replacedCredential: Bool) throws {
        var value = profile(library)
        value.config_revision += 1; value.mode = mode; value.status = "invalid"
        value.credential_ref = mode == "direct" ? key(library) : LocalAIConfiguration.tokenService(library)
        if replacedCredential { value.credential_generation = UUID().uuidString.lowercased() }
        guard persistProfile(value, library: library) else { throw failure("无法保存模型配置代次，凭证尚未修改。") }
    }
    static func finishUpdate(_ library: LocalLibraryDescriptor, mode: String) throws {
        var value = profile(library)
        value.status = "disabled"  // Credential availability is checked only at bootstrap.
        UserDefaults.standard.set(mode, forKey: key(library) + ".mode")
        guard persistProfile(value, library: library) else { throw failure("模型配置未能完整保存，请重新检查后应用。") }
    }
    static func hasCredential(_ library: LocalLibraryDescriptor,
                              credentials: any CredentialStore = MacModelCredentialStore()) -> Bool {
        if case .available = credentials.readWithoutInteraction(service: key(library)) { return true }
        return false
    }
    static func save(_ options: LocalModelOptions, replacementKey: String, library: LocalLibraryDescriptor,
                     credentials: any CredentialStore = MacModelCredentialStore()) throws {
        let validated = try options.validated()
        let bytes = try JSONEncoder().encode(validated)
        let secret = replacementKey.trimmingCharacters(in: .whitespacesAndNewlines)
        guard secret.count <= 4096, secret.utf8.allSatisfy({ $0 > 32 && $0 < 127 }) else { throw failure("API Key 格式无效。") }
        if secret.isEmpty {
            let old = load(library)
            guard old.baseURL == validated.baseURL, old.provider == validated.provider else {
                throw failure("首次配置或更换服务地址时，请填写 API Key。")
            }
            switch credentials.read(service: key(library)) {
            case .available: break
            case .missing: throw failure("请填写模型 API Key。")
            case .denied: throw failure("旧模型密钥无法静默读取，请重新填写 API Key 后保存。")
            case .unavailable: throw failure("模型密钥暂不可用，请重新填写 API Key 后保存。")
            }
        }
        // Publish a new, unavailable revision before replacing the credential. A
        // partial write/verification failure cannot reuse the old identity.
        try beginUpdate(library, mode: "direct", replacedCredential: !secret.isEmpty)
        if !secret.isEmpty { try credentials.write(secret, service: key(library)) }
        UserDefaults.standard.set(bytes, forKey: key(library))
        try finishUpdate(library, mode: "direct")
    }
    static func clear(_ library: LocalLibraryDescriptor, credentials: any CredentialStore = MacModelCredentialStore()) throws {
        try beginUpdate(library, mode: "disabled", replacedCredential: true)
        try credentials.remove(service: key(library))
        UserDefaults.standard.removeObject(forKey: key(library))
        try finishUpdate(library, mode: "disabled")
    }
}

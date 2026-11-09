import Foundation

/// Device-only metadata; no credential contents or key-derived identifiers.
struct LocalModelProfile: Codable, Equatable {
    var profile_id = UUID().uuidString.lowercased()
    var config_revision = 1
    var mode: String
    var status = "disabled"
    var provider = "deepseek"
    var base_url = "https://api.deepseek.com"
    var model = ""
    var vision_model = ""
    var credential_ref: String
    var credential_generation = UUID().uuidString.lowercased()
    var external_url = ""
}

struct LocalModelStartup: Encodable {
    var profile: LocalModelProfile
    var provider_key = ""
    var gateway_token = ""

    static func prepare(_ library: LocalLibraryDescriptor,
                        credentials: (any CredentialStore)? = nil,
                        allowInteraction: Bool = true) -> Self {
        var profile = LocalModelConfiguration.profile(library)
        var result = Self(profile: profile)
        guard profile.status != "invalid" else { return result }
        guard profile.mode != "disabled" else { return result }
        if profile.mode == "direct" {
            guard let options = try? LocalModelConfiguration.load(library).validated() else {
                result.profile.status = "invalid"; return result
            }
            profile.provider = options.provider; profile.base_url = options.baseURL
            profile.model = options.model; profile.vision_model = options.visionModel
        } else {
            guard let address = try? LocalAIConfiguration.validatedAddress(LocalAIConfiguration.address(library)), !address.isEmpty else {
                result.profile.status = "missing"; return result
            }
            profile.external_url = address
        }
        result.profile = profile
        let store: any CredentialStore = credentials ?? (profile.mode == "direct" ?
            MacModelCredentialStore() : KeychainCredentialStore())
        let saved = allowInteraction ? store.read(service: profile.credential_ref) :
            store.readWithoutInteraction(service: profile.credential_ref)
        switch saved {
        case .available(let value):
            guard value.count <= 4096, value.utf8.allSatisfy({ $0 > 32 && $0 < 127 }) else {
                result.profile.status = "invalid"; return result
            }
            result.profile.status = "ready"
            if profile.mode == "direct" {
                result.provider_key = value; result.gateway_token = UUID().uuidString
            } else { result.gateway_token = value }
        case .missing: result.profile.status = "missing"
        case .denied: result.profile.status = "denied"
        case .unavailable: result.profile.status = "unavailable"
        }
        return result
    }
}

struct LocalHostStartup: Encodable {
    let version = 1
    let client_contract_version = LocalKernelContract.version
    let client_schema_version = LocalKernelContract.schemaVersion
    let owner_credential: String
    let ai: LocalModelStartup

    func encoded() throws -> Data {
        var data = try JSONEncoder().encode(self)
        data.append(10)
        guard data.count <= 32 * 1024 else { throw failure("本地服务启动配置超过大小限制。") }
        return data
    }
}

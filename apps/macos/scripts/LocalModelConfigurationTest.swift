import Foundation
import Security

final class MemoryCredentials: CredentialStore {
    var values: [String: String] = [:]
    var unavailable: CredentialRead?
    var failAfterWrite = false
    func read(service: String) -> CredentialRead { unavailable ?? values[service].map(CredentialRead.available) ?? .missing }
    func write(_ value: String, service: String) throws {
        values[service] = value
        if failAfterWrite { throw NSError(domain: "synthetic", code: 1) }
    }
    func remove(service: String) throws { values.removeValue(forKey: service) }
}

@main struct LocalModelConfigurationTest {
    static func main() throws {
        var options = LocalModelOptions(provider: "openai-compatible", baseURL: " https://example.com/v1/ ", model: " custom-text ", visionModel: "custom-vision")
        let valid = try options.validated()
        precondition(valid.baseURL == "https://example.com/v1" && valid.model == "custom-text")
        for address in ["http://example.com", "https://user:secret@example.com", "https://example.com?key=secret", "https://example.com/chat/completions", "file:///tmp/model"] {
            options.baseURL = address
            do { _ = try options.validated(); fatalError("Unsafe address accepted") } catch {}
        }
        options = LocalModelOptions()
        do { _ = try options.validated(); fatalError("Missing model accepted") } catch {}
        options.model = "model\nheader"
        do { _ = try options.validated(); fatalError("Newline accepted") } catch {}
        print("PASS: model configuration validation")
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let library = LocalLibraryDescriptor(id: nil, data: root, cache: root, logs: root,
                                             credentialService: "synthetic.model." + UUID().uuidString)
        let second = LocalLibraryDescriptor(id: nil, data: root, cache: root, logs: root,
                                            credentialService: "synthetic.other." + UUID().uuidString)
        defer {
            for descriptor in [library, second] {
                for suffix in ["", ".mode", ".profile.v1"] {
                    UserDefaults.standard.removeObject(forKey: LocalModelConfiguration.key(descriptor) + suffix)
                }
            }
        }
        let credentials = MemoryCredentials()
        options = LocalModelOptions(model: "synthetic-text")
        try LocalModelConfiguration.save(options, replacementKey: "synthetic-secret", library: library, credentials: credentials)
        let first = LocalModelConfiguration.profile(library)
        precondition(first.credential_ref == LocalModelConfiguration.key(library))
        let ready = LocalModelStartup.prepare(library, credentials: credentials)
        precondition(ready.profile.status == "ready" && ready.provider_key == "synthetic-secret")
        precondition(LocalModelStartup.prepare(second, credentials: credentials).gateway_token.isEmpty)
        let encoded = try LocalHostStartup(owner_credential: String(repeating: "a", count: 48), ai: ready).encoded()
        precondition(encoded.count < 32 * 1024 && encoded.last == 10)
        precondition(String(data: encoded, encoding: .utf8)!.contains("synthetic-secret"))
        let metadata = UserDefaults.standard.data(forKey: LocalModelConfiguration.key(library) + ".profile.v1")!
        precondition(!String(data: metadata, encoding: .utf8)!.contains("synthetic-secret"))
        try LocalModelConfiguration.save(options, replacementKey: "", library: library, credentials: credentials)
        let retained = LocalModelConfiguration.profile(library)
        precondition(retained.profile_id == first.profile_id && retained.config_revision == first.config_revision + 1)
        precondition(retained.credential_generation == first.credential_generation)
        try LocalModelConfiguration.save(options, replacementKey: "new-synthetic-secret", library: library, credentials: credentials)
        precondition(LocalModelConfiguration.profile(library).credential_generation != first.credential_generation)
        for (read, expected) in [(CredentialRead.missing, "missing"), (.denied, "denied"), (.unavailable, "unavailable")] {
            credentials.unavailable = read
            let unavailable = LocalModelStartup.prepare(library, credentials: credentials)
            precondition(unavailable.profile.status == expected && unavailable.provider_key.isEmpty && unavailable.gateway_token.isEmpty)
        }
        precondition(KeychainCredentialStore.classify(errSecAuthFailed, data: nil) == .denied)
        precondition(KeychainCredentialStore.classify(errSecInteractionNotAllowed, data: nil) == .unavailable)
        precondition(KeychainCredentialStore.classify(errSecItemNotFound, data: nil) == .missing)
        credentials.unavailable = nil
        let previous = LocalModelConfiguration.profile(library)
        credentials.failAfterWrite = true
        do {
            try LocalModelConfiguration.save(options, replacementKey: "partially-written-key", library: library, credentials: credentials)
            fatalError("Partial credential write accepted")
        } catch {}
        let failed = LocalModelStartup.prepare(library, credentials: credentials)
        precondition(failed.profile.status == "invalid" && failed.provider_key.isEmpty)
        precondition(failed.profile.config_revision > previous.config_revision)
        credentials.failAfterWrite = false
        try LocalModelConfiguration.save(options, replacementKey: "repaired-synthetic-key", library: library, credentials: credentials)
        precondition(LocalModelStartup.prepare(library, credentials: credentials).profile.status == "ready")
        try LocalModelConfiguration.clear(library, credentials: credentials)
        precondition(LocalModelStartup.prepare(library, credentials: credentials).profile.mode == "disabled")
        print("PASS: private startup, credential states, library isolation, stable profile and revision")
    }
}

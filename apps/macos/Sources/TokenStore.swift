import Foundation

/// Compatibility facade for explicitly scoped service credentials. Native storage is isolated.
enum TokenStore {
    static func load(service: String) -> String? {
        if case .available(let value) = KeychainCredentialStore().read(service: service) { return value }
        return nil
    }

    @discardableResult static func save(_ token: String, service: String) -> Bool {
        do { try KeychainCredentialStore().write(token, service: service); return true }
        catch { return false }
    }

    static func delete(service: String) { try? KeychainCredentialStore().remove(service: service) }
}

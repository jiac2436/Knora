import Foundation
import Security
import LocalAuthentication

/// Keeps the existing Keychain service/account so upgrades need no new key.
struct KeychainCredentialStore: CredentialStore {
    private static let accessLock = NSRecursiveLock()
    static func classify(_ status: OSStatus, data: Data?) -> CredentialRead {
        if status == errSecItemNotFound { return .missing }
        if status == errSecAuthFailed || status == errSecUserCanceled { return .denied }
        guard status == errSecSuccess, let data, let text = String(data: data, encoding: .utf8), !text.isEmpty else { return .unavailable }
        return .available(text)
    }

    private func query(_ service: String) -> [String: Any] {
        [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service,
         kSecAttrAccount as String: "session"]
    }

    func read(service: String) -> CredentialRead {
        Self.accessLock.lock()
        defer { Self.accessLock.unlock() }
        var request = query(service)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        return Self.classify(status, data: result as? Data)
    }

    func readWithoutInteraction(service: String) -> CredentialRead {
        Self.accessLock.lock()
        defer { Self.accessLock.unlock() }
        // File-based login keychains can ignore SecItem's newer UI attributes.
        // Serialize all accesses through this adapter and restore the prior policy.
        var previous: DarwinBoolean = false
        guard SecKeychainGetUserInteractionAllowed(&previous) == errSecSuccess,
              SecKeychainSetUserInteractionAllowed(false) == errSecSuccess else { return .unavailable }
        defer { SecKeychainSetUserInteractionAllowed(previous.boolValue) }
        let context = LAContext()
        context.interactionNotAllowed = true
        defer { context.invalidate() }
        var request = query(service)
        request[kSecReturnData as String] = true
        request[kSecMatchLimit as String] = kSecMatchLimitOne
        request[kSecUseAuthenticationContext as String] = context
        // Legacy login-keychain ACLs also need the per-query no-UI policy.
        request[kSecUseAuthenticationUI as String] = kSecUseAuthenticationUIFail
        var result: CFTypeRef?
        let status = SecItemCopyMatching(request as CFDictionary, &result)
        return Self.classify(status, data: result as? Data)
    }

    func write(_ value: String, service: String) throws {
        Self.accessLock.lock()
        defer { Self.accessLock.unlock() }
        let request = query(service)
        let data = Data(value.utf8)
        let status = SecItemUpdate(request as CFDictionary, [kSecValueData as String: data] as CFDictionary)
        if status == errSecItemNotFound {
            var item = request; item[kSecValueData as String] = data
            guard SecItemAdd(item as CFDictionary, nil) == errSecSuccess else { throw failure("无法保存凭证，请检查钥匙串访问权限。") }
        } else if status != errSecSuccess { throw failure("无法更新凭证，请检查钥匙串访问权限。") }
        guard read(service: service) == .available(value) else { throw failure("凭证保存后无法读取，请检查钥匙串访问权限。") }
    }

    func remove(service: String) throws {
        Self.accessLock.lock()
        defer { Self.accessLock.unlock() }
        let status = SecItemDelete(query(service) as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else { throw failure("无法移除凭证，请检查钥匙串访问权限。") }
    }
}

import Foundation
import LocalAuthentication
import Security

/// Compile twice with distinct code identities. Only the writer may access the synthetic item.
@main enum KeychainNoInteractionTest {
    static func main() throws {
        guard CommandLine.arguments.count == 3,
              CommandLine.arguments[2].hasPrefix("synthetic.pka.noninteractive.") else {
            throw failure("仅允许使用专属合成测试命名空间")
        }
        let service = CommandLine.arguments[2]
        var query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service, kSecAttrAccount as String: "session"]
        let context = LAContext()
        context.interactionNotAllowed = true
        defer { context.invalidate() }
        query[kSecUseAuthenticationContext as String] = context
        #if CREDENTIAL_WRITER
        if CommandLine.arguments[1] == "cleanup" {
            let status = SecItemDelete(query as CFDictionary)
            guard status == errSecSuccess || status == errSecItemNotFound else { throw failure("无法清理合成测试项") }
            print("PASS: 合成钥匙串项已清理")
            return
        }
        guard CommandLine.arguments[1] == "create" else { throw failure("无效的合成测试操作") }
        var application: SecTrustedApplication?
        guard SecTrustedApplicationCreateFromPath(nil, &application) == errSecSuccess,
              let application else { throw failure("无法创建合成测试身份") }
        var access: SecAccess?
        guard SecAccessCreate("Synthetic startup access test" as CFString,
                              [application] as CFArray, &access) == errSecSuccess,
              let access else { throw failure("无法创建合成测试访问控制") }
        query[kSecAttrAccess as String] = access
        query[kSecValueData as String] = Data("synthetic-private-item-not-a-user-credential".utf8)
        guard SecItemAdd(query as CFDictionary, nil) == errSecSuccess else { throw failure("无法准备合成钥匙串项") }
        print("PASS: 已创建仅信任写入程序的合成钥匙串项")
        #else
        guard CommandLine.arguments[1] == "read" else { throw failure("无效的合成测试操作") }
        var before: DarwinBoolean = false
        guard SecKeychainGetUserInteractionAllowed(&before) == errSecSuccess else {
            throw failure("无法读取合成测试前的交互策略")
        }
        let start = Date()
        let result = KeychainCredentialStore().readWithoutInteraction(service: service)
        var after: DarwinBoolean = false
        guard SecKeychainGetUserInteractionAllowed(&after) == errSecSuccess,
              before.boolValue == after.boolValue else { throw failure("静默读取未恢复原交互策略") }
        switch result {
        case .denied, .unavailable, .missing:
            guard Date().timeIntervalSince(start) < 5 else { throw failure("无交互读取未及时返回") }
            print("PASS: 不受信任的新代码身份读取受保护项立即返回，无授权弹窗，原交互策略已恢复")
        case .available:
            throw failure("合成访问控制未形成拒绝条件，不能据此验收无交互读取")
        }
        #endif
    }
}

func failure(_ message: String) -> NSError {
    NSError(domain: "KeychainNoInteractionTest", code: 1, userInfo: [NSLocalizedDescriptionKey: message])
}

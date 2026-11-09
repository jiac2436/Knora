import Foundation

enum CredentialRead: Equatable {
    case available(String)
    case missing
    case denied
    case unavailable
}

protocol CredentialStore {
    func read(service: String) -> CredentialRead
    func readWithoutInteraction(service: String) -> CredentialRead
    func write(_ value: String, service: String) throws
    func remove(service: String) throws
}

extension CredentialStore {
    /// A store must explicitly support silent reads; never fall back to an interactive read.
    func readWithoutInteraction(service: String) -> CredentialRead { .unavailable }
}

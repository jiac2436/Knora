import Foundation

struct LocalLibraryDescriptor: Equatable, Sendable {
    let id: String?
    let data: URL
    let cache: URL
    let logs: URL
    let credentialService: String
}

/// Platform contract; the compatibility name preserves the existing callers.
protocol LibraryStoring: Sendable {
    func current() throws -> LocalLibraryDescriptor
    func allocate() throws -> LocalLibraryDescriptor
    func activate(_ library: LocalLibraryDescriptor) throws
}

typealias LocalLibraryStore = MacLibraryStore

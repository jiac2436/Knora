import Foundation

struct NativeRuntimeLayout: Sendable {
    let root: URL
    let python: URL
    let backend: URL
    let model: URL
    let ocrModels: URL
    let platformHelper: URL
}

protocol PlatformPaths {
    func runtime(_ root: URL) -> NativeRuntimeLayout
    func library(home: URL, credentialService: String, id: String?) -> LocalLibraryDescriptor
    func configuration(home: URL) -> URL
}

struct RestrictedCommandResult: Sendable {
    let output: Data
    let errors: Data
    let exitCode: Int32
}

enum RestrictedCommandFailure: Error {
    case start, timeout, outputLimit
}

protocol RestrictedCommandRunning: Sendable {
    func run(executable: URL, arguments: [String], timeout: TimeInterval,
             workingDirectory: URL?, maximumDirectoryBytes: Int?) async throws -> RestrictedCommandResult
}

protocol ImageTextRecognizing: Sendable {
    /// A valid image remains usable when OCR finds no text or is unavailable.
    func recognize(file: URL) throws -> String
}

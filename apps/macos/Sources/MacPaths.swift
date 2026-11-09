import Foundation

struct MacPaths: PlatformPaths {
    func runtime(_ root: URL) -> NativeRuntimeLayout {
        NativeRuntimeLayout(root: root, python: root.appendingPathComponent("python/bin/python3"),
            backend: root.appendingPathComponent("backend"), model: root.appendingPathComponent("model"),
            ocrModels: root.appendingPathComponent("ocr-models"),
            platformHelper: root.appendingPathComponent("platform/macos/KnowledgePlatform"))
    }

    func configuration(home: URL) -> URL {
        home.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox-state")
    }

    func librariesRoot(home: URL) -> URL {
        home.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox-libraries")
    }

    func library(home: URL, credentialService: String, id: String?) -> LocalLibraryDescriptor {
        let library = home.appendingPathComponent("Library")
        let suffix = id.map { "-libraries/\($0)" } ?? ""
        return LocalLibraryDescriptor(id: id,
            data: library.appendingPathComponent("Application Support/local.pka.knowledge-inbox\(suffix)", isDirectory: true),
            cache: library.appendingPathComponent("Caches/local.pka.knowledge-inbox\(suffix)", isDirectory: true),
            logs: library.appendingPathComponent("Logs/local.pka.knowledge-inbox\(suffix)", isDirectory: true),
            credentialService: credentialService + (id.map { ".library.\($0)" } ?? ""))
    }
}

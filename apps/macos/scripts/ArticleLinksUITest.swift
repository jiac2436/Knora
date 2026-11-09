import AppKit
import SwiftUI

final class ArticleLinkTestDelegate: NSObject, NSApplicationDelegate {
    func applicationWillTerminate(_ notification: Notification) {
        if let name = Bundle.main.bundleIdentifier, name.hasPrefix("local.pka.article-links-test.") {
            TokenStore.delete(service: name)
        }
    }
}

/// The production Workspace receives real macOS URL events in an isolated library.
@main struct ArticleLinksUITest: App {
    @NSApplicationDelegateAdaptor(ArticleLinkTestDelegate.self) private var delegate
    @StateObject private var model: CaptureModel
    private let root: URL

    init() {
        guard let path = Bundle.main.object(forInfoDictionaryKey: "PKATestRoot") as? String,
              let runtimePath = Bundle.main.object(forInfoDictionaryKey: "PKATestRuntime") as? String,
              let name = Bundle.main.bundleIdentifier, name.hasPrefix("local.pka.article-links-test.") else {
            fatalError("缺少独立测试配置")
        }
        root = URL(fileURLWithPath: path).resolvingSymlinksInPath()
        precondition(root.path.hasPrefix(FileManager.default.temporaryDirectory.resolvingSymlinksInPath().path + "/pka-ui-test.article-links."))
        let home = root.appendingPathComponent("home")
        let service = LocalService(home: home, credentialService: name)
        _model = StateObject(wrappedValue: CaptureModel(localService: service, runtime: URL(fileURLWithPath: runtimePath)))
    }

    var body: some Scene {
        Window("文章链接隔离验收", id: "main") {
            WorkspaceView(model: model)
                .onReceive(model.$articleNavigationID.dropFirst()) { event in
                    guard let source = model.selectedSource else { return }
                    save(["event": event.uuidString, "source_id": source.id,
                          "visible": NSApp.windows.contains(where: { $0.isVisible && $0.canBecomeMain })], "opened.json")
                }
                .onReceive(model.$articleLinkError) { error in
                    if let error { save(["error": error], "error.json") }
                }
                .onAppear { save(["pid": ProcessInfo.processInfo.processIdentifier], "ready.json") }
                .onReceive(Timer.publish(every: 0.2, on: .main, in: .common).autoconnect()) { _ in
                    let manager = FileManager.default
                    if manager.fileExists(atPath: root.appendingPathComponent("stop").path) { NSApp.terminate(nil) }
                    if manager.fileExists(atPath: root.appendingPathComponent("close").path) {
                        try? manager.removeItem(at: root.appendingPathComponent("close"))
                        NSApp.windows.first(where: { $0.canBecomeMain })?.close()
                        save(["closed": true], "closed.json")
                    }
                }
        }
    }

    private func save(_ value: [String: Any], _ name: String) {
        if let data = try? JSONSerialization.data(withJSONObject: value) {
            try? data.write(to: root.appendingPathComponent(name), options: .atomic)
        }
    }
}

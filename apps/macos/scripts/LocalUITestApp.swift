import SwiftUI

// The UI harness uses the production views with an isolated home and Keychain namespace.
// Configuration lives in the test bundle so Finder relaunches preserve the same queue.
@main struct LocalUITestApp: App {
    @StateObject private var model: CaptureModel

    init() {
        guard let rootPath = Bundle.main.object(forInfoDictionaryKey: "PKATestRoot") as? String,
              let runtimePath = Bundle.main.object(forInfoDictionaryKey: "PKATestRuntime") as? String,
              let serviceName = Bundle.main.bundleIdentifier,
              serviceName.hasPrefix("local.pka.ui-test.") else {
            fatalError("缺少独立界面测试配置")
        }
        let root = URL(fileURLWithPath: rootPath).resolvingSymlinksInPath()
        let temporary = FileManager.default.temporaryDirectory.resolvingSymlinksInPath()
        precondition(root.path.hasPrefix(temporary.path + "/pka-ui-test."), "测试数据必须位于临时目录")
        let home = root.appendingPathComponent("home")
        let queue = home.appendingPathComponent("Library/Application Support/local.pka.knowledge-inbox/capture-queue")
        let service = LocalService(home: home, credentialService: serviceName)
        _model = StateObject(wrappedValue: CaptureModel(
            queueStore: CaptureQueueStore(root: queue), localService: service,
            runtime: URL(fileURLWithPath: runtimePath)))
    }

    var body: some Scene {
        Window("知识收集箱 · 临时界面测试", id: "main") {
            WorkspaceView(model: model)
        }
        .commands {
            CommandGroup(replacing: .newItem) {}
            CommandGroup(replacing: .appSettings) {
                Button("设置…") {
                    NotificationCenter.default.post(name: .openSettings, object: nil)
                }
                .keyboardShortcut(",", modifiers: .command)
            }
        }
    }
}

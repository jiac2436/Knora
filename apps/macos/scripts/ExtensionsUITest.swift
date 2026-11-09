import AppKit
import SwiftUI

@main struct ExtensionsUITest: App {
    @StateObject private var model: CaptureModel
    @StateObject private var manager: LocalExtensionManager

    init() {
        precondition(Bundle.main.bundleIdentifier == "local.pka.extensions-ui-test")
        let file = Bundle.main.resourceURL!.appendingPathComponent("state-path.txt")
        let path = try! String(contentsOf: file, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
        precondition(path.hasPrefix("/private/tmp/pka-extensions-ui-"))
        let home = URL(fileURLWithPath: path)
        let service = LocalService(home: home, credentialService: "synthetic.extensions-ui")
        _model = StateObject(wrappedValue: CaptureModel(localService: service, runtime: nil))
        _manager = StateObject(wrappedValue: LocalExtensionManager(home: home))
    }

    var body: some Scene {
        Window("扩展功能 · 隔离验收", id: "extensions") {
            VStack(spacing: 0) {
                HStack {
                    Text("扩展功能").font(.headline)
                    Spacer()
                    Text("合成验收 · 不访问真实知识库").font(.caption).foregroundStyle(.secondary)
                }.padding(24)
                Divider()
                ScrollView {
                    LocalExtensionsView(model: model, extensionManager: manager).padding(24)
                }
            }
            .frame(width: 800, height: 780)
            .background(InboxStyle.canvas).preferredColorScheme(.dark)
            .tint(InboxStyle.accent).foregroundStyle(InboxStyle.text)
            .font(.system(size: 13)).buttonStyle(InboxButtonStyle(.secondary))
            .textFieldStyle(InboxFieldStyle())
        }
    }
}

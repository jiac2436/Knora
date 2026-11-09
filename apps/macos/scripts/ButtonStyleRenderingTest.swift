import AppKit
import SwiftUI

// Verify environment inheritance and explicit overrides without opening a library or invoking actions.
private struct ActionSample: View {
    var body: some View {
        Button {} label: { Text("普通操作").frame(width: 150, height: 16) }
    }
}

@main struct ButtonStyleRenderingTest {
    @MainActor static func main() throws {
        _ = NSApplication.shared
        let output = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        func render<V: View>(_ view: V, name: String) throws -> Data {
            let renderer = ImageRenderer(content: view
                .frame(width: 200, height: 48)
                .background(InboxStyle.canvas)
                .environment(\.colorScheme, .dark))
            renderer.scale = 2
            guard let image = renderer.cgImage,
                  let pixels = image.dataProvider?.data,
                  let png = NSBitmapImageRep(cgImage: image).representation(using: .png, properties: [:]) else {
                throw NSError(domain: "ButtonStyleRenderingTest", code: 1)
            }
            try png.write(to: output.appendingPathComponent(name + ".png"))
            return pixels as Data
        }
        let ordinary = try render(ActionSample().buttonStyle(InboxButtonStyle(.secondary)), name: "secondary")
        let inherited = try render(VStack { ActionSample() }.buttonStyle(InboxButtonStyle(.secondary)), name: "inherited-secondary")
        precondition(ordinary == inherited, "Ordinary buttons must inherit the shared baseline")
        let primary = try render(ActionSample().buttonStyle(InboxButtonStyle(.primary)), name: "primary")
        let override = try render(VStack { ActionSample().buttonStyle(InboxButtonStyle(.primary)) }
            .buttonStyle(InboxButtonStyle(.secondary)), name: "primary-override")
        precondition(primary == override && primary != ordinary, "Explicit primary hierarchy must survive baseline")
        let plain = try render(ActionSample().buttonStyle(.plain), name: "plain")
        let plainOverride = try render(VStack { ActionSample().buttonStyle(.plain) }
            .buttonStyle(InboxButtonStyle(.secondary)), name: "plain-override")
        precondition(plain == plainOverride && plain != ordinary, "List/icon overrides must remain intact")
        let disabled = try render(ActionSample().disabled(true).buttonStyle(InboxButtonStyle(.secondary)), name: "disabled")
        precondition(disabled != ordinary, "Disabled actions must remain visually distinct")
        print("PASS: inherited secondary, explicit primary, plain override and disabled appearance")
    }
}

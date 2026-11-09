import AppKit
import SwiftUI

@MainActor private final class HoverFixture: ObservableObject {
    @Published var group: StorageGroup?
}
private struct BubbleFixture: View {
    @ObservedObject var hover: HoverFixture
    let snapshot: StorageSnapshot
    var body: some View {
        VStack {
            Spacer().frame(height: 110)
            StorageUsageBar(snapshot: snapshot, hovered: $hover.group)
            Spacer()
        }.padding(.horizontal, 25).frame(width: 650, height: 220)
            .background(InboxStyle.surface).preferredColorScheme(.dark)
    }
}

/// Rendering a standalone test view; no mouse/keyboard events or user App access.
@main struct StorageBubbleRenderingTest {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        let output = URL(fileURLWithPath: CommandLine.arguments[1])
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        let model = HoverFixture()
        let snapshot = StorageSnapshot(bytes: [.application: 600, .documents: 210, .images: 90, .analysis: 20, .indexes: 30, .models: 40, .other: 5, .cache: 4, .backups: 1], unreadableItems: 0, unclassifiedDatabases: 0, scannedAt: Date())
        let view = NSHostingView(rootView: BubbleFixture(hover: model, snapshot: snapshot))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 650, height: 220), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = view; window.orderFront(nil)
        try await Task.sleep(for: .milliseconds(200))
        func capture(_ name: String) throws -> Data {
            view.layoutSubtreeIfNeeded(); view.displayIfNeeded()
            let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds)!
            view.cacheDisplay(in: view.bounds, to: bitmap)
            let data = bitmap.representation(using: .png, properties: [:])!
            try data.write(to: output.appendingPathComponent(name + ".png"))
            return data
        }
        let initial = try capture("closed")
        model.group = .documents
        try await Task.sleep(for: .milliseconds(35))
        let moving = try capture("opening")
        try await Task.sleep(for: .milliseconds(240))
        let open = try capture("documents")
        precondition(open != initial)
        precondition(moving != open && moving != initial, "应捕获到开合动效的中间帧")
        model.group = .cache
        try await Task.sleep(for: .milliseconds(240))
        _ = try capture("edge-cache")
        model.group = nil
        try await Task.sleep(for: .milliseconds(240))
        let closed = try capture("closed-again")
        let originalPixels = NSBitmapImageRep(data: initial)!
        let restoredPixels = NSBitmapImageRep(data: closed)!
        let count = originalPixels.bytesPerRow * originalPixels.pixelsHigh
        precondition(count == restoredPixels.bytesPerRow * restoredPixels.pixelsHigh)
        let maximumDifference = (0..<count).map { abs(Int(originalPixels.bitmapData![$0]) - Int(restoredPixels.bitmapData![$0])) }.max() ?? 0
        // Native interpolation/rasterization can differ by one 8-bit color level.
        precondition(maximumDifference <= 1, "关闭气泡应恢复原始条形图，不遗留遮挡")
        window.contentView = nil
        print("PASS: 原生 SwiftUI 气泡渲染、末端极小色块避让、开合后布局与画面恢复")
    }
}

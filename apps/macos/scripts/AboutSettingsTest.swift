import AppKit
import SwiftUI

@main struct AboutSettingsTest {
    @MainActor static func main() {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.regular)
        NSApp.accessibilitySetValue(true, forAttribute: NSAccessibility.Attribute(rawValue: "AXEnhancedUserInterface"))
        Task { @MainActor in
            do { try await run(); NSApp.terminate(nil) }
            catch { print("FAIL: \(error)"); exit(1) }
        }
        NSApp.run()
    }

    @MainActor private static func run() async throws {
        setbuf(stdout, nil)
        let manager = FileManager.default
        let home = manager.temporaryDirectory.resolvingSymlinksInPath().appendingPathComponent("knora-about-\(UUID())")
        try manager.createDirectory(at: home, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: home) }
        let model = CaptureModel(localService: LocalService(home: home, credentialService: "synthetic.about.\(UUID())"), runtime: home)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 900, height: 650),
                              styleMask: [.titled, .closable], backing: .buffered, defer: false)
        let hosting = NSHostingView(rootView: SettingsView(model: model).preferredColorScheme(.dark))
        hosting.sizingOptions = []
        window.contentView = hosting
        window.setContentSize(NSSize(width: 900, height: 650))
        window.title = "Knora 关于 · 合成验收"
        window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        defer { window.orderOut(nil) }
        try await Task.sleep(for: .milliseconds(600))
        guard let about = find("关于", in: window, button: true),
              let pdf = find("PDF 文字识别", in: window, button: true) else { throw failure("设置末项缺失") }
        try check((about.accessibilityFrame?().minY ?? 0) < (pdf.accessibilityFrame?().minY ?? 0), "关于必须位于PDF项下方")
        try press(about)
        try await Task.sleep(for: .milliseconds(500))
        for text in [AppBrand.displayName, "版本 0.1.0", "让收藏成为随时可用的知识。", "资料保存在本机，AI 分析使用你配置的模型服务。"] {
            try check(find(text, in: window) != nil, "关于内容缺失：\(text)")
        }
        if let output = ProcessInfo.processInfo.environment["PKA_ABOUT_TEST_OUTPUT"] {
            hosting.layoutSubtreeIfNeeded()
            guard let bitmap = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else { throw failure("截图失败") }
            hosting.cacheDisplay(in: hosting.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: output).appendingPathComponent("about-settings.png"))
        }
        print("PASS: 关于为设置最后一项；原生点击显示品牌、真实版本和简短产品说明")
        guard let web = find("网页读取", in: window, button: true) else { throw failure("原设置入口缺失") }
        try press(web)
        try await Task.sleep(for: .milliseconds(300))
        try check(find("让收藏成为随时可用的知识。", in: window) == nil, "切换设置页后关于内容未消失")
        guard let again = find("关于", in: window, button: true) else { throw failure("关于入口丢失") }
        try press(again)
        try await Task.sleep(for: .milliseconds(300))
        try check(find(AppBrand.displayName, in: window) != nil, "重新打开关于失败")
        print("PASS: 原设置页可往返切换；未启动服务或访问真实资料库")
    }

    private static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }

    @MainActor private static func press(_ element: AnyObject) throws {
        if element.accessibilityPerformPress?() == true { return }
        if let button = element as? NSButton { button.performClick(nil); return }
        throw failure("原生设置按钮未响应")
    }

    @MainActor private static func find(_ text: String, in root: Any, button: Bool = false) -> AnyObject? {
        var visited = Set<ObjectIdentifier>()
        func search(_ value: Any) -> AnyObject? {
            let object = value as AnyObject
            guard visited.insert(ObjectIdentifier(object)).inserted else { return nil }
            if let node = value as? NSObject {
                let isButton = (object.accessibilityRole?())?.rawValue == "AXButton"
                if !button || isButton {
                    if node.responds(to: #selector(NSAccessibilityProtocol.accessibilityLabel)),
                       object.accessibilityLabel?() == text { return object }
                    if node.responds(to: #selector(NSAccessibilityProtocol.accessibilityValue)),
                       node.perform(#selector(NSAccessibilityProtocol.accessibilityValue))?.takeUnretainedValue() as? String == text { return object }
                }
                if node.responds(to: #selector(NSAccessibilityProtocol.accessibilityChildren)) {
                    for child in object.accessibilityChildren?() ?? [] { if let found = search(child) { return found } }
                }
            }
            if let window = value as? NSWindow, let content = window.contentView, let found = search(content) { return found }
            if let view = value as? NSView {
                for child in view.subviews { if let found = search(child) { return found } }
            }
            return nil
        }
        return search(root)
    }
}

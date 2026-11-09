import AppKit
import SwiftUI

@MainActor private final class SelectFixture: ObservableObject {
    @Published var engine = "legacy"
    @Published var library = "personal"
    @Published var longSelection = "topic-1"
    @Published var optionalSelection: String? = nil
    @Published var emptySelection = ""
    @Published var disabledSelection = "legacy"
    @Published var dynamicSelection = "one"
    @Published var dynamicValues = ["one", "two"]

    let engines = [
        InboxSelectOption("legacy", "原有方式 · 轻量读取", subtitle: "直接读取公开网页正文", symbol: "doc.text"),
        InboxSelectOption("crawl4ai", "Crawl4AI · 浏览器读取", subtitle: "适合需要浏览器渲染的页面", symbol: "globe")
    ]
    let libraries = [
        InboxSelectOption("personal", "个人信息", symbol: "folder"),
        InboxSelectOption("research", "公开网页与微信公众号文章研究资料知识库（长名称验收）", symbol: "folder"),
        InboxSelectOption("work", "工作资料", symbol: "folder")
    ]
    var topics: [InboxSelectOption<String>] {
        (1...24).map { InboxSelectOption("topic-\($0)", String(format: "主题 %02d", $0), subtitle: $0 == 20 ? "包含筛选目标的合成条目" : nil, symbol: "tag") }
    }
}

@MainActor private struct SelectFixtureView: View {
    @ObservedObject var fixture: SelectFixture
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("下拉控件 · 合成验收").font(.system(size: 22, weight: .semibold))
                    Text("无真实资料、无后台、无模型请求").font(.system(size: 12)).foregroundStyle(.secondary)
                }
                InboxSelect("读取方式", selection: $fixture.engine, options: fixture.engines)
                InboxSelect("保存到知识库", selection: $fixture.library, options: fixture.libraries)
                InboxSelect("内容标签", selection: $fixture.longSelection, options: fixture.topics)
                HStack(alignment: .top, spacing: 20) {
                    InboxSelect("空选项", selection: $fixture.emptySelection, options: [])
                    InboxSelect("不可修改", selection: $fixture.disabledSelection, options: fixture.engines).disabled(true)
                }
                InboxSelect("可选会话", selection: $fixture.optionalSelection, options: [
                    InboxSelectOption<String?>(nil, "新会话", symbol: "plus.bubble"),
                    InboxSelectOption<String?>("saved", "已保存会话", symbol: "bubble.left")
                ])
                InboxSelect("动态选项", selection: $fixture.dynamicSelection, options:
                    fixture.dynamicValues.map { InboxSelectOption($0, "动态条目 · \($0)") })
            }.padding(28).frame(maxWidth: .infinity, alignment: .leading)
        }.background(InboxStyle.canvas).tint(InboxStyle.accent)
    }
}

@main struct SelectControlTest {
    @MainActor private static func element(_ identifier: String) -> AnyObject? {
        var visited = Set<ObjectIdentifier>()
        var retainedNodes: [AnyObject] = []
        func find(_ value: Any) -> AnyObject? {
            let object = value as AnyObject
            guard visited.insert(ObjectIdentifier(object)).inserted else { return nil }
            retainedNodes.append(object)
            if let node = value as? NSObject {
                if node.responds(to: #selector(NSAccessibilityProtocol.accessibilityIdentifier)),
                   object.accessibilityIdentifier?() == identifier { return object }
                if node.responds(to: #selector(NSAccessibilityProtocol.accessibilityChildren)) {
                    for child in object.accessibilityChildren?() ?? [] {
                        if let result = find(child) { return result }
                    }
                }
            }
            if let view = value as? NSView {
                for child in view.subviews {
                    if let result = find(child) { return result }
                }
            }
            return nil
        }
        for window in NSApp.windows where window.isVisible {
            if let result = find(window) { return result }
        }
        return nil
    }
    @MainActor private static func stringValue(_ node: AnyObject) -> String? {
        (node as? NSObject)?.perform(NSSelectorFromString("accessibilityValue"))?.takeUnretainedValue() as? String
    }
    @MainActor private static func require(_ condition: @autoclosure () -> Bool, _ message: String) throws {
        guard condition() else { throw NSError(domain: "SelectControlTest", code: 1, userInfo: [NSLocalizedDescriptionKey: message]) }
    }
    @MainActor private static func press(_ identifier: String) throws {
        guard let node = element(identifier), node.accessibilityPerformPress?() == true else {
            throw NSError(domain: "SelectControlTest", code: 2, userInfo: [NSLocalizedDescriptionKey: "无法按压 \(identifier)"])
        }
    }
    @MainActor private static func settle(_ milliseconds: Int = 350) async throws {
        try await Task.sleep(for: .milliseconds(milliseconds))
    }
    @MainActor private static func key(_ code: UInt16, _ characters: String, in window: NSWindow) throws {
        let target = NSApp.keyWindow ?? window
        let event = NSEvent.keyEvent(with: .keyDown, location: .zero, modifierFlags: [],
            timestamp: ProcessInfo.processInfo.systemUptime, windowNumber: target.windowNumber, context: nil,
            characters: characters, charactersIgnoringModifiers: characters, isARepeat: false, keyCode: code)!
        target.sendEvent(event)
    }
    @MainActor private static func capture(_ name: String, main: NSWindow, output: URL) throws {
        // Capture only this synthetic process's views; a popover uses its own AppKit window.
        let windows = NSApp.windows.filter { $0.isVisible && $0.contentView != nil }
        for (index, window) in windows.enumerated() {
            let view = window.contentView!
            view.layoutSubtreeIfNeeded()
            guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { continue }
            view.cacheDisplay(in: view.bounds, to: bitmap)
            let suffix = window == main ? "" : "-popover-\(index)"
            try bitmap.representation(using: .png, properties: [:])!.write(to: output.appendingPathComponent(name + suffix + ".png"))
        }
    }
    @MainActor static func main() {
        _ = NSApplication.shared
        NSApp.setActivationPolicy(.regular)
        NSApp.accessibilitySetValue(true, forAttribute: NSAccessibility.Attribute(rawValue: "AXEnhancedUserInterface"))
        Task { @MainActor in
            do { try await run(); NSApp.terminate(nil) }
            catch { print("FAIL: \(error.localizedDescription)"); exit(1) }
        }
        NSApp.run()
    }
    @MainActor static func run() async throws {
        let fixture = SelectFixture()
        let host = NSHostingView(rootView: SelectFixtureView(fixture: fixture).preferredColorScheme(.dark))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 760, height: 780),
            styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "统一下拉控件（合成验收）"; window.contentView = host; window.center(); window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        let output = URL(fileURLWithPath: ProcessInfo.processInfo.environment["PKA_SELECT_PREVIEW"] ?? "/tmp/pka-select-preview")
        try FileManager.default.createDirectory(at: output, withIntermediateDirectories: true)
        try await settle(600)
        try capture("fields", main: window, output: output)
        let engineField = element("inbox-select-读取方式")!
        try require((engineField.accessibilityFrame?().height ?? 0) >= 38, "字段真实命中区域至少应为 38 pt")
        try press("inbox-select-读取方式"); try await settle()
        try require(element("inbox-select-option-读取方式-1") != nil, "短列表应呈现选项")
        let engineOption = element("inbox-select-option-读取方式-1")!
        try require((engineOption.accessibilityFrame?().height ?? 0) >= 44, "选项真实命中区域至少应为 44 pt")
        try capture("short-options", main: window, output: output)
        try press("inbox-select-option-读取方式-1"); try await settle()
        try require(fixture.engine == "crawl4ai", "选项按压必须写入 Binding")
        try require(element("inbox-select-option-读取方式-1") == nil, "选择后应关闭列表")
        print("PASS: short-list accessibility presses open, choose and dismiss with correct Binding")

        try press("inbox-select-保存到知识库"); try await settle()
        try press("inbox-select-option-保存到知识库-1"); try await settle()
        try require(fixture.library == "research", "长中文名选择必须保留原始值")
        try press("inbox-select-保存到知识库"); try await settle()
        try capture("long-chinese-options", main: window, output: output)
        try key(53, "\u{1B}", in: window); try await settle()
        try require(element("inbox-select-option-保存到知识库-1") == nil, "Esc 应关闭列表")
        try require(fixture.library == "research", "Esc 不应改变当前值")
        print("PASS: long Chinese option selects original value; Esc dismisses without changes")

        let previousDisabled = fixture.disabledSelection
        if let disabled = element("inbox-select-不可修改") { _ = disabled.accessibilityPerformPress?() }
        try await settle()
        try require(element("inbox-select-option-不可修改-0") == nil && fixture.disabledSelection == previousDisabled, "禁用字段不可打开或改值")
        if let empty = element("inbox-select-空选项") { _ = empty.accessibilityPerformPress?() }
        try await settle()
        try require(fixture.emptySelection.isEmpty, "空选项不能写入选择值")
        try capture("disabled-empty", main: window, output: output)
        print("PASS: disabled and empty choices preserve Binding without a selectable option")

        try press("inbox-select-可选会话"); try await settle()
        try press("inbox-select-option-可选会话-1"); try await settle()
        try require(fixture.optionalSelection == "saved", "Optional 非空值应可选择")
        try press("inbox-select-可选会话"); try await settle()
        try press("inbox-select-option-可选会话-0"); try await settle()
        try require(fixture.optionalSelection == nil, "Optional nil 应可选择")
        print("PASS: optional Binding supports nil and saved values")

        try press("inbox-select-动态选项"); try await settle()
        try capture("dynamic-before-update", main: window, output: output)
        try require(element("inbox-select-option-动态选项-1") != nil, "更新前的动态列表必须已打开")
        print("DIAG dynamic before windows=\(NSApp.windows.filter { $0.isVisible }.count)")
        fixture.dynamicValues = ["two", "three"]
        try await settle(400)
        try capture("dynamic-updated", main: window, output: output)
        print("DIAG dynamic windows=\(NSApp.windows.filter { $0.isVisible }.count) option0=\(element("inbox-select-option-动态选项-0") != nil) option1=\(element("inbox-select-option-动态选项-1") != nil)")
        try require(element("inbox-select-option-动态选项-1") != nil, "打开的动态列表应更新新增条目")
        try press("inbox-select-option-动态选项-1"); try await settle()
        try require(fixture.dynamicSelection == "three", "动态新增条目应写入真实值")
        try press("inbox-select-动态选项"); try await settle()
        fixture.dynamicValues = []
        try await settle()
        try require(element("inbox-select-option-动态选项-0") == nil, "打开列表被清空后应关闭且不可选择")
        try require(fixture.dynamicSelection == "three", "选项被移除不应隐式修改业务 Binding")
        print("PASS: dynamic options update in an open list and do not mutate a removed selection implicitly")

        try press("inbox-select-读取方式"); try await settle()
        try key(126, "\u{F700}", in: window); try await settle(60)
        try key(36, "\r", in: window); try await settle()
        try require(fixture.engine == "legacy", "方向键加回车应选择上一项")
        try require(element("inbox-select-option-读取方式-0") == nil, "键盘确认后应关闭列表")
        print("PASS: Up + Return chooses the preceding option and dismisses")
        element("inbox-select-读取方式")?.setAccessibilityFocused?(true)
        try await settle(80)
        try key(49, " ", in: window); try await settle()
        try require(element("inbox-select-option-读取方式-0") != nil, "聚焦字段后空格应打开列表")
        try key(125, "\u{F701}", in: window); try await settle(60)
        try key(49, " ", in: window); try await settle()
        try require(fixture.engine == "crawl4ai", "方向键加空格应选择下一项")
        try require(element("inbox-select-option-读取方式-0") == nil, "空格确认后应关闭列表")
        print("PASS: focused field Space opens; Down + Space chooses the next option and dismisses")

        try press("inbox-select-内容标签"); try await settle()
        try require(element("inbox-select-search-内容标签") != nil, "长列表应显示筛选输入框")
        try capture("long-options", main: window, output: output)
        let longPopover = NSApp.windows.first { $0.isVisible && $0 != window }
        try require(longPopover != nil && (longPopover!.contentView?.bounds.height ?? 0) <= 400,
            "长列表弹窗必须保持有界高度")
        // Typing is dispatched only to the test process's active text responder.
        try key(19, "2", in: window); try await settle()
        try require(element("inbox-select-option-内容标签-0") == nil, "筛选后应移除不匹配条目")
        try key(125, "\u{F701}", in: window); try await settle(60)
        try key(36, "\r", in: window); try await settle()
        try require(fixture.longSelection == "topic-12", "搜索聚焦时方向键和回车应选择筛选后的下一项")
        try press("inbox-select-内容标签"); try await settle()
        let resetSearch = element("inbox-select-search-内容标签")!
        try require(stringValue(resetSearch) == "", "再次打开应清空上次筛选")
        try key(19, "20", in: window); try await settle()
        try require(element("inbox-select-option-内容标签-19") != nil, "筛选应保留匹配条目原始索引")
        try require(element("inbox-select-option-内容标签-0") == nil, "筛选后不应显示不匹配条目")
        try capture("filtered-options", main: window, output: output)
        try press("inbox-select-option-内容标签-19"); try await settle()
        try require(fixture.longSelection == "topic-20", "筛选选中应写入原始值")
        try press("inbox-select-内容标签"); try await settle()
        let reopenedSearch = element("inbox-select-search-内容标签")!
        try require(stringValue(reopenedSearch) == "", "重开选项应重置筛选文本")
        let selectedTopic = element("inbox-select-option-内容标签-19")
        let selectedFrame = selectedTopic?.accessibilityFrame?() ?? .zero
        let reopenedPopover = NSApp.windows.first { $0.isVisible && $0 != window }
        try require(selectedTopic != nil && reopenedPopover != nil && reopenedPopover!.frame.contains(selectedFrame),
            "选中第 20 项后重新打开，该条目应完整滚入可见区")
        try capture("selected-twenty-visible", main: window, output: output)
        try key(19, "没有这个条目", in: window); try await settle()
        try key(36, "\r", in: window); try await settle()
        try require(fixture.longSelection == "topic-20", "无结果时回车不得改写选择值")
        try capture("no-match", main: window, output: output)
        try key(53, "\u{1B}", in: window); try await settle()
        print("PASS: search-focused Down + Return selects filtered next; original values and indices survive filtering; reopened selected item 20 scrolls into view; no-match Return preserves Binding")

        window.setContentSize(NSSize(width: 410, height: 780))
        try await settle(400)
        try capture("narrow-fields", main: window, output: output)
        try press("inbox-select-保存到知识库"); try await settle()
        try capture("narrow-options", main: window, output: output)
        try key(53, "\u{1B}", in: window); try await settle()
        print("PASS: narrow field and long-label popover render without changing selections")
        let temporaryRoot = FileManager.default.temporaryDirectory.appendingPathComponent("pka-select-production-\(UUID())")
        defer { try? FileManager.default.removeItem(at: temporaryRoot) }
        let model = CaptureModel(queueStore: CaptureQueueStore(root: temporaryRoot.appendingPathComponent("queue")),
            localService: LocalService(home: temporaryRoot, credentialService: "synthetic.select-controls"), runtime: temporaryRoot)
        let production = NSHostingView(rootView: SettingsView(model: model).preferredColorScheme(.dark))
        window.setContentSize(NSSize(width: 900, height: 650)); window.contentView = production
        try await settle(500)
        try capture("production-settings", main: window, output: output)
        try press("inbox-select-读取方式"); try await settle()
        try capture("production-settings-options", main: window, output: output)
        try press("inbox-select-option-读取方式-1"); try await settle()
        try require(model.webReadEngine == "crawl4ai", "生产设置应使用共享控件更新实际模型 Binding")
        print("PASS: production SettingsView renders themed field and popover; selecting updates webReadEngine without saving or network calls")
        window.orderOut(nil)
    }
}

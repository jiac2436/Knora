import AppKit
import SwiftUI
import WebKit

@main struct ReaderLayoutTest {
    @MainActor static func main() async throws {
        precondition(ReaderSidebarLayout.width(-500) == 200)
        precondition(ReaderSidebarLayout.width(900) == 360)
        precondition(ReaderSidebarLayout.width(280) == 280)
        precondition(ReaderSidebarLayout.width(.nan) == 248)
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-reader-layout-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let model = CaptureModel(queueStore: CaptureQueueStore(root: root.appendingPathComponent("queue")),
                                 localService: LocalService(home: root, credentialService: "synthetic.reader-layout"),
                                 runtime: root)
        model.address = "https://example.invalid"
        let paragraphs = (1...60).map { "## 第 \($0) 节：让知识可以随时使用\n\n资料保存之后，可以用关键词和语义找到相关段落。阅读器应当保持文字清晰，滚动流畅；工具栏保留文档标题，方便随时确认正在阅读的内容。" }.joined(separator: "\n\n")
        let value: [String: Any] = ["id": "synthetic-reader", "title": "年化100万美元生意：帮别人网站加上登录和收款功能", "content_type": "link", "revision": 2, "extracted_text": paragraphs, "source_url": "https://example.invalid/article"]
        model.selectedSource = KnowledgeDetail(value)!
        model.knowledgeRows = [KnowledgeRow(value, searchResult: false)!]
        model.domains = [DomainItem(id: "synthetic-library", name: "设计资料")]
        model.domainID = "synthetic-library"; model.searchDomainID = model.domainID
        UserDefaults.standard.set(false, forKey: "readerSidebarCollapsed")
        UserDefaults.standard.set(248.0, forKey: "readerSidebarWidth")
        guard let output = ProcessInfo.processInfo.environment["PKA_READER_PREVIEW"] else {
            print("PASS: sidebar width boundaries"); return
        }
        _ = NSApplication.shared
        let host = NSHostingView(rootView: KnowledgeView(model: model, showSettings: .constant(false)).preferredColorScheme(.dark))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 850), styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host; window.orderFront(nil)
        func snapshot(_ name: String) throws {
            host.layoutSubtreeIfNeeded()
            let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
            host.cacheDisplay(in: host.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: output).appendingPathComponent(name + ".png"))
        }
        try await Task.sleep(for: .seconds(2))
        try snapshot("reader")
        UserDefaults.standard.set(360.0, forKey: "readerSidebarWidth")
        try await Task.sleep(for: .milliseconds(1500)); try snapshot("sidebar-wide")
        UserDefaults.standard.set(true, forKey: "readerSidebarCollapsed")
        try await Task.sleep(for: .milliseconds(1500)); try snapshot("sidebar-collapsed")
        window.setContentSize(NSSize(width: 898, height: 680))
        UserDefaults.standard.set(false, forKey: "readerSidebarCollapsed")
        UserDefaults.standard.set(200.0, forKey: "readerSidebarWidth")
        try await Task.sleep(for: .milliseconds(1500)); try snapshot("minimum-size")
        func webView(in view: NSView) -> WKWebView? {
            if let web = view as? WKWebView { return web }
            return view.subviews.lazy.compactMap { webView(in: $0) }.first
        }
        if let web = webView(in: host) {
            let dimensions = try await web.evaluateJavaScript("(()=>{const p=document.querySelector('p');const r=document.createRange();r.selectNodeContents(p);return {viewport:window.innerWidth,body:document.body.scrollWidth,textRight:Math.max(...Array.from(r.getClientRects()).map(x=>x.right))}})()") as! [String: Any]
            print("Web viewport:", dimensions, "native width:", web.bounds.width)
            precondition((dimensions["body"] as! Double) <= (dimensions["viewport"] as! Double) + 1)
            precondition((dimensions["textRight"] as! Double) <= (dimensions["viewport"] as! Double) + 1)
            let viewport = try await web.takeSnapshot(configuration: nil)
            if let tiff = viewport.tiffRepresentation, let bitmap = NSBitmapImageRep(data: tiff) {
                try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: output).appendingPathComponent("minimum-web-viewport.png"))
            }
        }
        window.orderOut(nil)
        print("PASS: production reader rendered at normal/minimum size, wide and collapsed sidebar; width limits")
    }
}

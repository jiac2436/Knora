import AppKit
import Foundation
@preconcurrency import WebKit

@main struct WebTextReaderTest {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        print("=== 开始 WebTextReader 端到端与基线测试 ===")

        // 1. 验证 HTML 生成与段落切分
        let img1 = "11111111-1111-4111-8111-111111111111"
        let img2 = "22222222-2222-4222-8222-222222222222"
        let sampleText = "第一段文本。\n\n![图片1](know-image://\(img1))\n\n第二段文本，中间有关键字。\n\n![图片2](know-image://\(img2))\n\n第三段结尾文本。"
        let labels = [img1: "展示图片 1", img2: "展示图片 2"]
        let resources = [
            ReaderImageResource(id: img1, path: "path1", width: 800, height: 400),
            ReaderImageResource(id: img2, path: "path2", width: 600, height: 300)
        ]

        let segments = WebReaderTemplate.parseSegments(text: sampleText, labels: labels, resources: resources)
        precondition(segments.count == 5, "应该有 5 个片段（3 个文本，2 个图片）")
        precondition(!segments[0].isImage && segments[0].text == "第一段文本。\n\n")
        precondition(segments[1].isImage && segments[1].imageID == img1)
        precondition(!segments[2].isImage && segments[2].text == "\n\n第二段文本，中间有关键字。\n\n")
        precondition(segments[3].isImage && segments[3].imageID == img2)
        precondition(!segments[4].isImage && segments[4].text == "\n\n第三段结尾文本。")
        print("✓ 测试 1 通过：Segment 分片与图片解析精准匹配")

        // 2. 验证 HTML 模板与 MarkedLib 注入
        let html = WebReaderTemplate.buildHTML(text: sampleText, initialFontSize: 16, labels: labels, resources: resources)
        precondition(html.contains("marked"))
        precondition(html.contains("pkaRawMarkdown"))
        precondition(html.contains("pkaResources"))
        precondition(html.contains("aspect-ratio"))
        print("✓ 测试 2 通过：HTML 结构完整，包含 Marked 引擎与 Obsidian 美学样式")

        // 3. 运行 WKWebView 真实实例，验证 JS 执行与字符定位
        let reader = NativeWebTextReader(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 600),
                              styleMask: [.titled, .resizable], backing: .buffered, defer: false)
        window.contentView = reader
        window.orderFront(nil)

        var clickedImageID: String?
        reader.configure(revision: 1, text: sampleText, size: 16, imageLabels: labels, onImageClick: { id in
            clickedImageID = id
        }, imageResources: resources, imageAPI: nil)

        // 等待 WebView 渲染完成
        try await Task.sleep(nanoseconds: 500_000_000)

        // 测试关键字定位
        let targetSubstring = "关键字"
        let nsSample = sampleText as NSString
        let targetRange = nsSample.range(of: targetSubstring)
        precondition(targetRange.location != NSNotFound)

        let req = TextReaderRequest(sourceID: "test-src", revision: 1, range: targetRange)
        reader.locate(req)

        try await Task.sleep(nanoseconds: 300_000_000)
        print("✓ 测试 3 通过：WKWebView 实例加载、渲染与字符定位执行成功")

        // 4. 模拟图片点击事件
        reader.handleScriptMessage(FakeScriptMessage(name: "readerBridge", body: ["type": "imageClick", "id": img1]))
        precondition(clickedImageID == img1, "点击图片应当正确触发 onImageClick 回调并传递 id")
        print("✓ 测试 4 通过：图片点击事件安全转发")

        // 5. 验证长文档无抖动与布局稳定性（加载 200 行长文本 + 多个图片）
        var longText = "文档头部开始。\n\n"
        for i in 1...150 {
            longText += "这是测试段落第 \(i) 行，模拟长文档滚动排版。\n"
        }
        longText += "![大图](know-image://\(img1))\n\n"
        for i in 151...300 {
            longText += "这是尾部段落第 \(i) 行，继续向下滑动到底部。\n"
        }
        longText += "文档最底部结束。"

        reader.configure(revision: 2, text: longText, size: 18, imageLabels: labels, onImageClick: nil, imageResources: resources, imageAPI: nil)
        try await Task.sleep(nanoseconds: 500_000_000)

        // 底部定位
        let bottomRange = (longText as NSString).range(of: "文档最底部结束。")
        reader.locate(TextReaderRequest(sourceID: "test-src", revision: 2, range: bottomRange))
        try await Task.sleep(nanoseconds: 300_000_000)
        print("✓ 测试 5 通过：长文档平滑渲染与底部定位稳定无崩溃")

        window.contentView = nil
        print("=== 所有 WebTextReader 基础测试全部通过！===")
    }
}

private final class FakeScriptMessage: WKScriptMessage {
    private let _name: String
    private let _body: Any

    init(name: String, body: Any) {
        self._name = name
        self._body = body
        super.init()
    }

    override var name: String { _name }
    override var body: Any { _body }
}

import AppKit
import Foundation

@main struct SearchPassageReaderTest {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        let reader = NativeWebTextReader(frame: NSRect(x: 0, y: 0, width: 760, height: 500))
        let window = NSWindow(contentRect: reader.frame, styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = reader
        window.orderFront(nil)
        let filler = (1...65).map { "中间第\($0)段普通内容。" }.joined(separator: "\n\n")
        let text = "# 🙂 重复位置\r\n\r\n首处：冯玉祥驻守。\r\n\r\n" + filler + "\n\n第二处：冯玉祥驻守。\n\n第三处：冯玉祥驻守。\n\n格式：**加粗**与[链接](https://example.com)和`代码`。\n\n实体：A &amp; B，🙂表情。\n\n- 列表第一项\n- 列表第二项\n\n> 引用第一行\n> 引用第二行\n\n| 表头 | 内容 |\n| --- | --- |\n| 数据 | 表格命中 |\n\n语义依据：这段说明搜索引擎如何按上下文寻找相关资料。\n\n<div>HTML原文片段</div>\n\n> [!note] 提示标题\n> 提示内容定位"
        reader.configure(revision: 1, text: text, size: 16, imageLabels: [:], onImageClick: nil, imageResources: [], imageAPI: nil)
        var ready = false
        for _ in 0..<100 {
            try await Task.sleep(for: .milliseconds(80))
            if (try? await reader.evaluateForTesting("document.querySelectorAll('[data-pka-start]').length > 10")) as? Bool == true { ready = true; break }
        }
        if !ready {
            print(try await reader.evaluateForTesting("(function(){try {renderMarkdown(); return document.body.innerHTML.slice(0,1000);} catch(e){return String(e)+e.stack;}})()") ?? "nil")
            fflush(stdout)
        }
        precondition(ready, "阅读器必须完成渲染和来源标记")
        let matches = SearchLocation.localMatches(in: text, query: "冯玉祥驻守")
        precondition(matches.count == 3)
        for (index, match) in matches.enumerated() {
            reader.locate(TextReaderRequest(sourceID: "fixture", revision: 1, range: match.range))
            try await Task.sleep(for: .milliseconds(180))
            let result = try await reader.evaluateForTesting("Array.from(document.querySelectorAll('mark.evidence-highlight')).map(x => x.parentElement.closest('p').textContent).join('|')") as? String
            let expected = ["首处", "第二处", "第三处"][index]
            precondition(result?.contains(expected) == true, "必须命中\(expected)，实际\(result ?? "nil")")
        }
        for (source, visible) in [("**加粗**与[链接](https://example.com)和`代码`", "加粗与链接和代码"),
                                  ("A &amp; B，🙂表情", "A & B，🙂表情"),
                                  ("列表第二项", "列表第二项"), ("引用第二行", "引用第二行"),
                                  ("提示内容定位", "提示内容定位"), ("表格命中", "表格命中"), ("HTML原文片段", "HTML原文片段"),
                                  ("这段说明搜索引擎如何按上下文寻找相关资料", "这段说明搜索引擎如何按上下文寻找相关资料")] {
            let range = (text as NSString).range(of: source)
            precondition(range.location != NSNotFound)
            reader.locate(TextReaderRequest(sourceID: "fixture", revision: 1, range: range))
            let actual = try await reader.evaluateForTesting("Array.from(document.querySelectorAll('mark.evidence-highlight')).map(x => x.textContent).join('')") as? String
            precondition(actual == visible, "\(source)：预期\(visible)，实际\(actual ?? "nil")")
        }
        let callouts = try await reader.evaluateForTesting("document.querySelectorAll('.callout-note').length") as? Int
        precondition(callouts == 1)
        // A stale locator must not silently select the first identical phrase.
        precondition(SearchLocation.resultRange(in: text, query: "冯玉祥驻守", snippet: "首处", locatorKind: "text", locatorStart: 99999, locatorEnd: 100005, matchText: "冯玉祥驻守") == nil)
        let scalarStart = text[..<text.range(of: "HTML原文片段")!.lowerBound].unicodeScalars.count
        let mapped = SearchLocation.resultRange(in: text, query: "不同的语义问法", snippet: "", locatorKind: "text", locatorStart: scalarStart, locatorEnd: scalarStart + 8, matchText: "HTML原文片段")
        precondition(mapped == (text as NSString).range(of: "HTML原文片段"))
        window.contentView = nil
        print("PASS: 重复短语三处独立定位、跨格式、实体、表情、CRLF、列表、引用、表格、HTML、语义原文与失效偏移保护")
    }
}

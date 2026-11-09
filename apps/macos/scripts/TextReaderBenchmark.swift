import AppKit
import SwiftUI

@main struct TextReaderBenchmark {
    @MainActor static func main() {
        print("starting AppKit"); fflush(stdout)
        _ = NSApplication.shared
        print("AppKit ready"); fflush(stdout)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 700),
                              styleMask: [.titled, .resizable], backing: .buffered, defer: false)
        let store = TextReaderStore()
        let paragraph = "北京历史与城市发展。这里包含中文、English、数字 2026 和表情 🙂，用于验证长文排版与跨段选择。\n\n"
        let searchSample = "🙂我喜欢写代码。\n第二段：我的爱好是编程。\n🙂我喜欢写代码。"
        let localMatches = SearchLocation.localMatches(in: searchSample, query: "我喜欢写代码")
        precondition(localMatches.count == 2)
        precondition(localMatches[0].range.location == 2, "Emoji must count as two UTF-16 units")
        precondition((searchSample as NSString).substring(with: localMatches[1].range) == "我喜欢写代码")
        let scalarStart = searchSample.unicodeScalars.distance(from: searchSample.unicodeScalars.startIndex,
                                                               to: searchSample.range(of: "我的爱好")!.lowerBound)
        let globalRange = SearchLocation.resultRange(in: searchSample, query: "爱好", snippet: "我的爱好是编程。",
                                                      locatorKind: "text", locatorStart: scalarStart,
                                                      locatorEnd: scalarStart + 9)!
        precondition((searchSample as NSString).substring(with: globalRange) == "爱好")
        let semanticRange = SearchLocation.resultRange(in: searchSample, query: "兴趣", snippet: "我的爱好是编程。",
                                                        locatorKind: "text", locatorStart: scalarStart,
                                                        locatorEnd: scalarStart + 9)!
        precondition((searchSample as NSString).substring(with: semanticRange).hasPrefix("我的爱好"))
        let historyText = "🙂八旗驻守内城。\n1926年冯玉祥国民军驻守天津。"
        let historyPhrase = "冯玉祥国民军驻守"
        let historyStart = historyText.range(of: historyPhrase)!.lowerBound
            .utf16Offset(in: historyText) - 1 // One emoji adds one UTF-16 unit beyond Python's scalar offset.
        let historyRange = SearchLocation.resultRange(in: historyText, query: "冯玉祥驻守",
                                                       snippet: "1926年冯玉祥国民军驻守天津。",
                                                       locatorKind: "text", locatorStart: historyStart,
                                                       locatorEnd: historyStart + historyPhrase.count)!
        precondition((historyText as NSString).substring(with: historyRange) == historyPhrase)
        let imageID = "e67f7d78-54eb-427b-9e02-758312042024"
        let imageID2 = "dd6a3c28-7e46-4755-b907-e8f5250bef97"
        let firstRef = "![图片 1](know-image://" + imageID + ")"
        let secondRef = "![图片 2](know-image://" + imageID2 + ")"
        let imageText = "🙂前文\n" + firstRef + "\n中间正文\n" + secondRef + "\n目标文字"
        let labels = [imageID: "[图片 1 · 查看图片]", imageID2: "[图片 2 · 未保存，查看原因]"]
        let presentation = ReaderImagePresentation(text: imageText, labels: labels)
        precondition(!presentation.content.string.contains("know-image://"))
        for target in ["前文", "中间正文", "目标文字", firstRef, secondRef] {
            let range = (imageText as NSString).range(of: target)
            let displayed = presentation.displayRange(for: range)
            let actual = (presentation.content.string as NSString).substring(with: displayed)
            precondition(actual == (target == firstRef ? labels[imageID]! : target == secondRef ? labels[imageID2]! : target))
        }
        let fullRange = presentation.displayRange(for: NSRange(location: 0, length: imageText.utf16.count))
        precondition(fullRange.length == presentation.content.length)
        precondition(ReaderImagePresentation(text: imageText, labels: [:]).content.string == imageText)
        var clickedID: String?
        let imageReader = store.reader(id: "images", revision: 1, text: imageText, size: 17,
                                       imageLabels: labels, onImageClick: { clickedID = $0 })
        window.contentView = imageReader
        imageReader.locate(TextReaderRequest(sourceID: "images", revision: 1,
                                            range: (imageText as NSString).range(of: "目标文字")))
        precondition((imageReader.textView.string as NSString).substring(with: imageReader.textView.selectedRange()) == "目标文字")
        precondition(imageReader.textView(imageReader.textView, clickedOnLink: "know-image://" + imageID, at: 0))
        precondition(clickedID == imageID)
        imageReader.configure(revision: 1, text: imageText, size: 17, imageLabels: labels)
        precondition(imageReader.contentAssignments == 1)
        var savedLabels = labels
        savedLabels[imageID2] = "[图片 2 · 查看图片]"
        imageReader.configure(revision: 1, text: imageText, size: 17, imageLabels: savedLabels)
        precondition(imageReader.contentAssignments == 2 && imageReader.textView.string.contains(savedLabels[imageID2]!))
        print("PASS: image links, status updates, multiple replacements, UTF-16 search offsets and click routing")
        func measure(_ action: () -> Void) -> Double {
            let start = CFAbsoluteTimeGetCurrent()
            action()
            return (CFAbsoluteTimeGetCurrent() - start) * 1000
        }
        for count in [40_000, 200_000] {
            let text = String(String(repeating: paragraph, count: count / paragraph.count + 1).prefix(count))
            var native: NativeTextReader!
            print("native begin \(count)"); fflush(stdout)
            let first = measure {
                native = store.reader(id: "\(count)", revision: 1, text: text, size: 17)
                window.contentView = native
                native.frame = NSRect(x: 0, y: 0, width: 800, height: 700)
                native.layoutSubtreeIfNeeded()
                native.displayIfNeeded()
            }
            print("native first \(first)"); fflush(stdout)
            precondition(native.textView.textLayoutManager != nil, "TextKit 2 must stay active")
            precondition(native.textView.string == text, "No truncation")
            native.textView.setSelectedRange(NSRange(location: 20, length: 120))
            native.contentView.scroll(to: NSPoint(x: 0, y: 400))
            let position = native.contentView.bounds.origin
            let warm = measure {
                window.contentView = NSView()
                let reused = store.reader(id: "\(count)", revision: 1, text: text, size: 17)
                precondition(reused === native)
                window.contentView = reused
                reused.layoutSubtreeIfNeeded()
                reused.displayIfNeeded()
            }
            precondition(native.contentAssignments == 1, "Cache hit must not rewrite text")
            precondition(native.textView.selectedRange().length == 120)
            precondition(native.contentView.bounds.origin == position, "Reading position must survive")
            let scroll = measure {
                for step in 1...30 {
                    native.contentView.scroll(to: NSPoint(x: 0, y: CGFloat(step * 80)))
                    native.reflectScrolledClipView(native.contentView)
                    native.layoutSubtreeIfNeeded()
                    native.displayIfNeeded()
                }
            }
            let firstMatch = SearchLocation.localMatches(in: text, query: "跨段选择").last!
            let firstRequest = TextReaderRequest(sourceID: "\(count)", revision: 1, range: firstMatch.range)
            native.locate(firstRequest)
            precondition(native.textView.selectedRange() == firstMatch.range)
            let target = NSRange(location: (text as NSString).range(of: "数字", options: .backwards).location,
                                 length: 2)
            let nextRequest = TextReaderRequest(sourceID: "\(count)", revision: 1, range: target)
            native.locate(nextRequest)
            precondition(native.textView.selectedRange() == target, "Repeated search must locate the new result")
            precondition(native.contentView.bounds.origin.y > 0, "A late match must scroll into view")
            let legacy = count == 40_000 && !CommandLine.arguments.contains("--native-only") ? measure {
                let host = NSHostingView(rootView: ScrollView {
                    Text(text).font(.system(size: 17)).lineSpacing(9).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                })
                window.contentView = host
                host.layoutSubtreeIfNeeded()
                host.displayIfNeeded()
            }
            : -1
            print("chars=\(count) native_first_ms=\(Int(first)) cached_ms=\(Int(warm)) scroll_30_ms=\(Int(scroll)) legacy_first_ms=\(Int(legacy))")
            let hosted = measure {
                let host = NSHostingView(rootView: VStack {
                    Text("正文标题").font(.title)
                    TextReader(store: store, sourceID: "hosted-\(count)", revision: 1, text: text, size: 17,
                               request: nil)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    Text("来源与状态")
                }.frame(width: 800, height: 700))
                window.contentView = host
                host.layoutSubtreeIfNeeded()
                host.displayIfNeeded()
            }
            let hostedReader = store.reader(id: "hosted-\(count)", revision: 1, text: text, size: 17)
            precondition(hostedReader.bounds.height > 100 && hostedReader.bounds.height < 700)
            precondition(hostedReader.textView.bounds.height > hostedReader.contentView.bounds.height)
            print("hosted_\(count)_ms=\(Int(hosted)) viewport_height=\(Int(hostedReader.bounds.height))")
            fflush(stdout)
            hostedReader.textView.selectAll(nil)
            precondition(hostedReader.textView.selectedRange().length == text.utf16.count)
            hostedReader.configure(revision: 1, text: text, size: 21)
            precondition(hostedReader.contentAssignments == 1)
            _ = store.reader(id: "\(count)", revision: 2, text: "新版正文", size: 19)
            precondition(native.textView.string == "新版正文" && native.contentAssignments == 2)
        }
        if let path = CommandLine.arguments.dropFirst().first(where: { !$0.hasPrefix("--") }) {
            let text = try! String(contentsOfFile: path, encoding: .utf8)
            let elapsed = measure {
                let reader = store.reader(id: "real-article", revision: 1, text: text, size: 17)
                window.contentView = reader
                reader.layoutSubtreeIfNeeded()
                reader.displayIfNeeded()
                precondition(reader.textView.string == text)
            }
            print("real_article_chars=\(text.count) first_layout_ms=\(Int(elapsed))")
        }
        let detached = NativeTextReader()
        let delayedText = String(repeating: paragraph, count: 1_000)
        detached.configure(revision: 1, text: delayedText, size: 17)
        let delayedRange = NSRange(location: delayedText.utf16.count - 12, length: 4)
        detached.locate(TextReaderRequest(sourceID: "delayed", revision: 1, range: delayedRange))
        precondition(detached.textView.selectedRange().location == 0)
        window.contentView = detached
        detached.layoutSubtreeIfNeeded()
        precondition(detached.textView.selectedRange() == delayedRange,
                     "A result requested before window attachment must still be located")
        let oldest = store.reader(id: "eviction", revision: 1, text: "缓存", size: 17)
        for index in 0..<9 {
            _ = store.reader(id: "eviction-\(index)", revision: 1, text: "缓存", size: 17)
        }
        let recreated = store.reader(id: "eviction", revision: 1, text: "缓存", size: 17)
        precondition(recreated !== oldest)
        store.remove(id: "eviction")
        let afterDelete = store.reader(id: "eviction", revision: 1, text: "缓存", size: 17)
        precondition(afterDelete !== recreated,
                     "Deleting a source must discard its cached reader")
        store.removeAll()
        precondition(store.reader(id: "eviction", revision: 1, text: "缓存", size: 17) !== afterDelete)
        print("PASS: content, TextKit 2, search offsets and navigation, delayed navigation, scroll position, cache identity, revision invalidation, eviction, logout cleanup")
    }
}

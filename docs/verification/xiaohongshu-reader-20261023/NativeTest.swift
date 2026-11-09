import AppKit
import Foundation

@main struct XiaohongshuNativeTest {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        let window=NSWindow(contentRect:NSRect(x:0,y:0,width:760,height:520),styleMask:[.titled],backing:.buffered,defer:false)
        let reader=NativeWebTextReader(frame:window.contentView!.bounds);window.contentView=reader;window.orderFront(nil)
        let text="【标题】\n合成笔记标题\n\n【正文】\n原文中的同一句话。\n\n另一个同一句话。\n\n【评论区 · 已加载 1 条 / 页面显示共 4 条】\n仅含当前页面已加载的评论及回复，不代表完整评论区。\n\n访客：\n评论里的定位目标。"
        reader.configure(revision:1,text:text,size:17,presentationSourceURL:"https://www.xiaohongshu.com/explore/synthetic")
        var ready=false
        for _ in 0..<100 {try await Task.sleep(for:.milliseconds(70));if (try? await reader.evaluateForTesting("document.querySelector('.xhs-comments') !== null")) as? Bool == true {ready=true;break}}
        precondition(ready)
        let check1 = try await reader.evaluateForTesting("document.querySelector('details').open") as? Bool == false
        precondition(check1)
        let at=(text as NSString).range(of:"评论里的定位目标")
        let check2 = try await reader.evaluateForTesting("locateEvidence(\(at.location),\(at.length)) && document.querySelector('details').open") as? Bool == true
        precondition(check2)
        let check3 = try await reader.evaluateForTesting("document.querySelector('mark.evidence-highlight').textContent") as? String == "评论里的定位目标"
        precondition(check3)
        let check4 = try await reader.evaluateForTesting("window.pkaRawMarkdown") as? String == text
        precondition(check4)
        // Reusing the same cached view for a non-Xiaohongshu source must remove the layout.
        reader.configure(revision:1,text:text,size:17,presentationSourceURL:nil)
        var ordinary=false
        for _ in 0..<100 {try await Task.sleep(for:.milliseconds(70));if (try? await reader.evaluateForTesting("document.getElementById('content-area').innerText.includes('【标题】') && !document.querySelector('.xhs-reading')")) as? Bool == true {ordinary=true;break}}
        precondition(ordinary)
        window.orderOut(nil)
        print("PASS: real WKWebView: collapsed comments, original-offset AI citation, unchanged raw text, same-view site isolation")
    }
}

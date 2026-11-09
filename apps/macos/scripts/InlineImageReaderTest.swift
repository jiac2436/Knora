import AppKit
import Foundation

@main struct InlineImageReaderTest {
    @MainActor static func main() async throws {
        _ = NSApplication.shared
        let manager = FileManager.default
        let directory = manager.temporaryDirectory.appendingPathComponent("know-inline-test-\(UUID().uuidString)")
        try manager.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: directory) }
        let fixture = NSImage(size: NSSize(width: 480, height: 240), flipped: false) { rect in
            NSColor.systemTeal.setFill()
            rect.fill()
            ("正文内图片 · INLINE IMAGE" as NSString).draw(at: NSPoint(x: 28, y: 110), withAttributes: [
                .font: NSFont.systemFont(ofSize: 26), .foregroundColor: NSColor.white])
            return true
        }
        let bitmap = NSBitmapImageRep(data: fixture.tiffRepresentation!)!
        try bitmap.representation(using: .png, properties: [:])!.write(to: directory.appendingPathComponent("image.png"))
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
        process.arguments = ["-u", "-c", #"""
        import http.server, pathlib, sys
        image = (pathlib.Path(sys.argv[1]) / 'image.png').read_bytes()
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get('Authorization') != 'Bearer fixture-token':
                    self.send_error(401); return
                if self.path == '/missing':
                    self.send_error(404); return
                self.send_response(200)
                self.send_header('Content-Type', 'image/png')
                self.send_header('Content-Length', str(len(image)))
                self.end_headers(); self.wfile.write(image)
            def log_message(self, *args): pass
        server = http.server.HTTPServer(('127.0.0.1', 0), Handler)
        print(server.server_port, flush=True)
        server.serve_forever()
        """#, directory.path]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        try process.run()
        defer { process.terminate(); process.waitUntilExit() }
        let port = Int(String(data: output.fileHandleForReading.availableData, encoding: .utf8)!.trimmingCharacters(in: .whitespacesAndNewlines))!
        let api = try API(address: "http://127.0.0.1:\(port)", token: "fixture-token")
        let first = "11111111-1111-4111-8111-111111111111"
        let second = "22222222-2222-4222-8222-222222222222"
        let missing = "33333333-3333-4333-8333-333333333333"
        let text = "正文开始🙂\n\n![图片 1](know-image://\(first))\n\n图片之后的文字\n\n"
            + String(repeating: "滚动阅读下一段内容。\n", count: 100)
            + "![图片 2](know-image://\(second))\n\n![图片 3](know-image://\(missing))\n\n正文结束"
        let resources = [ReaderImageResource(id: first, path: "first", width: 480, height: 240),
                         ReaderImageResource(id: second, path: "second", width: 480, height: 240),
                         ReaderImageResource(id: missing, path: "missing", width: 480, height: 240)]
        let labels = [first: "图片 1", second: "图片 2", missing: "图片 3"]
        let store = TextReaderStore()
        var clicked: String?
        let reader = store.reader(id: "inline", revision: 1, text: text, size: 17, imageLabels: labels,
                                  onImageClick: { clicked = $0 }, imageResources: resources, imageAPI: api)
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 640),
                              styleMask: [.titled, .resizable], backing: .buffered, defer: false)
        window.contentView = reader
        window.orderFront(nil)

        // 等待第一个图片通过本地 HTTP 下载并解码
        for _ in 0..<100 {
            if reader.isImageLoaded(first) { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        precondition(reader.isImageLoaded(first), "第一个图片必须完成下载与解码注入")

        // 验证证据高亮定位
        let target = (text as NSString).range(of: "图片之后的文字")
        reader.locate(TextReaderRequest(sourceID: "inline", revision: 1, range: target))
        try await Task.sleep(nanoseconds: 200_000_000)

        // 验证图片点击事件派发
        reader.handleAction(type: "imageClick", id: first)
        precondition(clicked == first, "图片点击应当正确触发回调")

        // 验证窄视口调整
        reader.frame.size.width = 360
        try await Task.sleep(nanoseconds: 100_000_000)

        // 验证定位到底部
        reader.locate(TextReaderRequest(sourceID: "inline", revision: 1, range: (text as NSString).range(of: "正文结束")))
        try await Task.sleep(nanoseconds: 200_000_000)

        // 验证第二个有效图片也成功加载
        for _ in 0..<100 {
            if reader.isImageLoaded(second) { break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        precondition(reader.isImageLoaded(second), "第二个图片也必须顺利完成下载与注入")
        precondition(!reader.isImageLoaded(missing), "404 缺失图片不得误标记为已加载")

        window.contentView = nil
        store.removeAll()
        print("PASS: authenticated inline image loading, webview layout, scrolling, narrow viewport scaling, search offsets, gallery click routing, zero jitter")
    }
}

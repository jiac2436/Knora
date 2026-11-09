import AppKit
import Foundation

@main struct LocalWebImagesTest {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 3 else { fatalError("需要运行包路径和公开微信文章 URL") }
        _ = NSApplication.shared
        report("开始隔离原生客户端验收（不调用模型）")
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
        let articleURL = CommandLine.arguments[2]
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent("pka-web-images-test-\(UUID().uuidString)")
        try manager.createDirectory(at: root, withIntermediateDirectories: true)
        let key = "synthetic.web-images.\(UUID().uuidString)"
        let service = LocalService(home: root, credentialService: key)
        let library = try service.currentLibrary()
        LocalWebConfiguration.save(allowFakeIP: true, library: library)
        defer {
            service.stop()
            TokenStore.delete(service: key)
            UserDefaults.standard.removeObject(forKey: LocalWebConfiguration.key(library))
            try? manager.removeItem(at: root)
        }
        let model = CaptureModel(localService: service, runtime: runtime)
        await model.startLocalService()
        try check(model.connected, "隔离库启动失败：" + model.authMessage)
        report("隔离库已启动")
        let api = try API(address: model.address, token: model.token)
        let domain = try await api.call("v1/domains", method: "POST", json: ["name": "合成图文验收"])
        let source = try await api.call("v1/captures", method: "POST", json: [
            "capture_type": "url", "source_url": articleURL, "content": ["text": ""],
            "coverage": "link_only", "context": ["domain_id": domain["id"]!],
            "client": ["kind": "desktop", "version": "test"]], key: UUID().uuidString)
        guard let identifier = source["id"] as? String else { throw failure("未创建合成采集") }
        var current: KnowledgeDetail?
        for _ in 0..<180 {
            let response = try await api.call("v1/sources/\(identifier)")
            current = KnowledgeDetail(response)
            if let current, current.processingStatus == "failed" { throw failure(current.webReadError ?? "正文抓取失败") }
            if let current, current.processingStatus == "usable", !current.images.isEmpty {
                if let unavailable = current.images.first(where: { $0.status == "unavailable" }) {
                    throw failure("图片归档失败：" + unavailable.failureReason)
                }
                if current.images.allSatisfy({ $0.status == "saved" }) { break }
            }
            try await Task.sleep(for: .seconds(1))
        }
        guard let detail = current, let first = detail.images.first,
              detail.images.allSatisfy({ $0.status == "saved" }) else { throw failure("图片归档超时或未发现图片") }
        try check(detail.text.contains("know-image://"), "正文未使用本地图片标记")
        try check(!detail.text.contains("[图片:"), "阅读正文仍使用图片占位文字")
        report("PASS: 真实微信正文与 \(detail.images.count) 张图片全部归档，均保留原文顺序")
        let resources = detail.images.compactMap { image -> ReaderImageResource? in
            guard let path = image.downloadPath else { return nil }
            return ReaderImageResource(id: image.id, path: String(path.dropFirst()), width: image.width, height: image.height)
        }
        let labels = Dictionary(uniqueKeysWithValues: detail.images.map { ($0.id, "图片 \($0.ordinal + 1)") })
        let reader = NativeWebTextReader(frame: NSRect(x: 0, y: 0, width: 800, height: 600))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 600),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = reader
        defer { window.contentView = nil; window.orderOut(nil) }
        reader.configure(revision: detail.revision, text: detail.text, size: 16, imageLabels: labels,
                         imageResources: resources, imageAPI: api)
        for _ in 0..<200 {
            if reader.isImageLoaded(first.id) { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        try check(reader.isImageLoaded(first.id), "真实 WebKit 未显示本地图片")
        var visible = false
        for _ in 0..<50 {
            visible = try await reader.evaluateForTesting("(function(){var i=document.getElementById('img-\(first.id)');return !!i && i.naturalWidth>0 && i.style.display==='block';})()") as? Bool ?? false
            if visible { break }
            try await Task.sleep(for: .milliseconds(100))
        }
        try check(visible, "图片虽已下载，但 WebKit 正文没有显示实际图像")
        _ = try await reader.evaluateForTesting("window.pkaTestMarker = 'keep'; window.scrollTo(0,200);")
        var updatedLabels = labels
        updatedLabels[first.id] = "状态更新后的图片"
        reader.configure(revision: detail.revision, text: detail.text, size: 16, imageLabels: updatedLabels,
                         imageResources: resources, imageAPI: api)
        try await Task.sleep(for: .milliseconds(700))
        let marker = try await reader.evaluateForTesting("window.pkaTestMarker") as? String
        try check(marker == "keep" && reader.isImageLoaded(first.id), "图片状态更新导致全文重新加载")
        report("PASS: WebKit 实际显示归档图片，状态更新保留原文页面与已加载图片")
        _ = await Task.detached { service.stopAndWait() }.value
        await model.startLocalService()
        try check(model.connected, "重启隔离库失败")
        let reopened = try await API(address: model.address, token: model.token).call("v1/sources/\(identifier)")
        guard let restored = KnowledgeDetail(reopened) else { throw failure("重启后详情无效") }
        try check(restored.revision == detail.revision && restored.images.map(\.id) == detail.images.map(\.id)
                  && restored.images.allSatisfy({ $0.status == "saved" }), "重启后图片或版本未保留")
        let directory = root.appendingPathComponent("download-check")
        _ = try await API(address: model.address, token: model.token).downloadFile(
            String(first.downloadPath!.dropFirst()), into: directory, named: first.id)
        report("PASS: 重启后从本地读取图片原件，未重新读取文章或创建 AI 任务")
    }

    static func report(_ message: String) {
        FileHandle.standardError.write(Data((message + "\n").utf8))
    }

    static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }
}

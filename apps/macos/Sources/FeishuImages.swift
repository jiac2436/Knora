import Foundation

struct FeishuImage: Identifiable, Codable {
    var id = UUID().uuidString.lowercased()
    let ordinal: Int
    let token: String
    let blockID: String
    let caption: String
    var file: URL?
    var failureReason = ""
    var ocrText = ""
    var retrievalMethod = "download"

    var metadata: [String: Any] {
        ["id": id, "ordinal": ordinal, "block_id": blockID, "caption": caption,
         "status": file == nil ? "unavailable" : "pending", "failure_reason": failureReason,
         "ocr_text": ocrText, "retrieval_method": retrievalMethod]
    }
}

enum FeishuImages {
    static let maximumImageBytes = 20 * 1024 * 1024
    static let maximumDocumentBytes = 200 * 1024 * 1024

    /// v1 external-development compatibility; the bundled capture path uses the kernel.
    static func references(in xml: String) throws -> [FeishuImage] {
        let tags = try NSRegularExpression(pattern: "<img\\b[^>]*>", options: [.caseInsensitive])
        let attributes = try NSRegularExpression(pattern: "([A-Za-z_][\\w:-]*)\\s*=\\s*([\"'])(.*?)\\2", options: [.dotMatchesLineSeparators])
        let source = xml as NSString
        let matches = tags.matches(in: xml, range: NSRange(location: 0, length: source.length))
        guard matches.count <= 200 else { throw failure("文档超过 200 张图片，当前无法完整导入。") }
        return matches.enumerated().map { index, match in
            let tag = source.substring(with: match.range) as NSString
            var values: [String: String] = [:]
            for attribute in attributes.matches(in: tag as String, range: NSRange(location: 0, length: tag.length)) {
                values[tag.substring(with: attribute.range(at: 1)).lowercased()] = decode(tag.substring(with: attribute.range(at: 3)))
            }
            return FeishuImage(ordinal: index, token: values["token"] ?? values["file_token"] ?? values["src"] ?? "",
                               blockID: String((values["id"] ?? "").prefix(200)),
                               caption: String((values["alt"] ?? values["name"] ?? "图片 \(index + 1)").prefix(500)))
        }
    }

    private static func decode(_ value: String) -> String {
        value.replacingOccurrences(of: "&quot;", with: "\"").replacingOccurrences(of: "&apos;", with: "'")
            .replacingOccurrences(of: "&lt;", with: "<").replacingOccurrences(of: "&gt;", with: ">")
            .replacingOccurrences(of: "&amp;", with: "&")
    }

    static func archive(_ document: FeishuDocument, cli: FeishuCLI,
                        progress: @escaping @MainActor (Int, Int) -> Void) async throws -> FeishuDocument {
        guard !document.images.isEmpty else { return document }
        var result = document
        let manager = FileManager.default
        let directory = manager.temporaryDirectory.appendingPathComponent("know-feishu-images-\(UUID().uuidString)")
        try manager.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        var retained = false
        defer { if !retained { try? manager.removeItem(at: directory) } }
        result.imageDirectory = directory
        var totalBytes = 0
        for index in result.images.indices {
            try Task.checkCancellation()
            await progress(index + 1, result.images.count)
            let imageDirectory = directory.appendingPathComponent(result.images[index].id)
            do {
                let token = result.images[index].token
                guard token.range(of: "^[A-Za-z0-9_-]{1,200}$", options: .regularExpression) != nil else {
                    throw failure("图片未提供可下载的资源标识。")
                }
                try manager.createDirectory(at: imageDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
                let response: [String: Any]
                do {
                    response = try await retrieve(token: token, command: "+media-download", cli: cli, directory: imageDirectory)
                } catch {
                    guard FeishuCLI.isMediaExportDenied(error) else { throw error }
                    try Task.checkCancellation()
                    // The official preview endpoint applies its own permissions.
                    // Remove any incomplete download before requesting a preview.
                    try manager.removeItem(at: imageDirectory)
                    try manager.createDirectory(at: imageDirectory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
                    do {
                        response = try await retrieve(token: token, command: "+media-preview", cli: cli, directory: imageDirectory)
                        result.images[index].retrievalMethod = "preview"
                    } catch is CancellationError { throw CancellationError() }
                    catch { throw failure("图片原件无导出权限，官方预览也未能获取：" + error.localizedDescription) }
                }
                let data = response["data"] as? [String: Any] ?? response
                guard let path = data["saved_path"] as? String else { throw failure("CLI 未返回图片文件。") }
                let file = path.hasPrefix("/") ? URL(fileURLWithPath: path) : imageDirectory.appendingPathComponent(path)
                let resolved = file.resolvingSymlinksInPath()
                guard resolved.deletingLastPathComponent().path == imageDirectory.resolvingSymlinksInPath().path else {
                    throw failure("CLI 图片保存路径无效。")
                }
                let properties = try resolved.resourceValues(forKeys: [.isRegularFileKey, .fileSizeKey])
                let bytes = properties.fileSize ?? 0
                guard properties.isRegularFile == true, bytes > 0, bytes <= maximumImageBytes,
                      totalBytes + bytes <= maximumDocumentBytes else {
                    throw failure("图片超过单张 20 MB 或整篇 200 MB 的限制。")
                }
                try manager.setAttributes([.posixPermissions: 0o600], ofItemAtPath: resolved.path)
                let ocr = try await Task.detached(priority: .utility) { try recognize(file: resolved) }.value
                result.images[index].file = resolved
                result.images[index].ocrText = ocr
                totalBytes += bytes
            } catch is CancellationError { throw CancellationError() }
            catch {
                result.images[index].failureReason = String(error.localizedDescription.prefix(500))
                try? manager.removeItem(at: imageDirectory)
            }
        }
        retained = true
        return result
    }

    private static func retrieve(token: String, command: String, cli: FeishuCLI, directory: URL) async throws -> [String: Any] {
        try await cli.json(["docs", command, "--token", token, "--output", "image", "--as", "user", "--format", "json"],
                           timeout: 60, workingDirectory: directory)
    }

    static func recognize(file: URL, using recognizer: any ImageTextRecognizing = MacImageTextRecognizer()) throws -> String {
        try recognizer.recognize(file: file)
    }

    static func markdown(_ document: FeishuDocument) -> String {
        var text = document.markdown
        for image in document.images {
            guard !image.token.isEmpty else { continue }
            let escaped = NSRegularExpression.escapedPattern(for: image.token)
            let prefix = "(?:https://(?:[A-Za-z0-9-]+\\.)*(?:feishu\\.cn|larksuite\\.com|larkoffice\\.com)/file/)?"
            let destination = prefix + escaped + "(?:\\?[^)\\s]*)?"
            let pattern = "<img\\b[^>]*\\b(?:src|token|file_token)\\s*=\\s*[\"']\(destination)[\"'][^>]*>|!\\[[^\\]]*\\]\\(\(destination)(?:\\s+\"[^\"]*\")?\\)"
            if let expression = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
               let match = expression.firstMatch(in: text, range: NSRange(location: 0, length: (text as NSString).length)),
               let range = Range(match.range, in: text) {
                text.replaceSubrange(range, with: "![图片 \(image.ordinal + 1)](know-image://\(image.id))")
            }
        }
        return text
    }
}

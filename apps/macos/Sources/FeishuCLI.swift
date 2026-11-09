import Combine
import Foundation

struct FeishuDocument: Codable {
    let markdown: String
    let title: String
    let documentID: String
    let revisionID: Int
    let cliVersion: String
    let partial: Bool
    var images: [FeishuImage] = []
    var imageDirectory: URL?

    var metadata: [String: Any] {
        ["document_id": documentID, "revision_id": revisionID, "cli_version": cliVersion]
    }
}

struct FeishuCLI {
    let executable: URL
    let profile: String
    static let maximumCharacters = 1_000_000

    static func isFeishuLink(_ url: URL) -> Bool {
        guard let host = url.host?.lowercased() else { return false }
        return ["feishu.cn", "larksuite.com", "larkoffice.com"].contains {
            host == $0 || host.hasSuffix("." + $0)
        }
    }

    static func resolve(path: String = "") throws -> URL {
        try MacCLIResolver.resolveFeishu(path: path)
    }

    var commands: any RestrictedCommandRunning = MacRestrictedCommands()

    func run(_ arguments: [String], timeout: TimeInterval = 120, workingDirectory: URL? = nil) async throws -> Data {
        let selected = profile.trimmingCharacters(in: .whitespacesAndNewlines)
        let args = (selected.isEmpty ? [] : ["--profile", selected]) + arguments
        let result: RestrictedCommandResult
        do {
            result = try await commands.run(executable: executable, arguments: args, timeout: timeout,
                workingDirectory: workingDirectory, maximumDirectoryBytes: FeishuImages.maximumImageBytes)
        } catch RestrictedCommandFailure.start {
            throw failure("无法启动飞书 CLI，请检查所选可执行文件。")
        } catch RestrictedCommandFailure.timeout {
            throw failure("飞书 CLI 操作超时，请重试。")
        } catch RestrictedCommandFailure.outputLimit {
            throw failure("飞书 CLI 返回内容过大，当前无法处理。")
        }
        if result.exitCode != 0 {
            if let json = try? JSONSerialization.jsonObject(with: result.errors) as? [String: Any] {
                throw Self.commandError(json)
            }
            throw failure("飞书 CLI 操作失败，请检查登录和权限；未保存任何内容。")
        }
        return result.output
    }

    func json(_ arguments: [String], timeout: TimeInterval = 120, workingDirectory: URL? = nil) async throws -> [String: Any] {
        let data = try await run(arguments, timeout: timeout, workingDirectory: workingDirectory)
        guard let result = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            throw failure("飞书 CLI 返回格式无效，请检查 CLI 版本（已验证 1.0.97）。")
        }
        if result["ok"] as? Bool == false { throw Self.commandError(result) }
        return result
    }

    static func commandError(_ result: [String: Any]) -> NSError {
        let error = result["error"] as? [String: Any] ?? [:]
        switch error["type"] as? String {
        case "authorization":
            if error["subtype"] as? String == "permission_denied",
               (error["message"] as? String ?? "").contains("export permission") {
                return NSError(domain: "FeishuCLI.ExportPermission", code: 1, userInfo: [NSLocalizedDescriptionKey: "当前飞书账号没有这篇文档的图片导出权限，无法保存图片原件。请联系文档所有者开放下载/导出权限后重新导入；重新登录不能解除文档的导出限制。"])
            }
            return failure("飞书授权不足。请检查文档访问权限，并在设置中连接飞书以补充图片下载授权后重试。")
        case "authentication", "auth": return failure("飞书登录已失效，请在设置中重新连接飞书。")
        case "config": return failure("飞书 CLI 尚未完成应用配置，或配置不可用。请先运行 lark-cli config init。")
        case "permission": return failure("缺少文档读取权限，请检查文档共享设置及飞书授权。")
        case "network": return failure("连接飞书失败，请检查网络后重试。")
        default: return failure("飞书 CLI 操作失败，请检查登录、文档权限和链接类型后重试。")
        }
    }

    static func isMediaExportDenied(_ error: Error) -> Bool {
        let value = error as NSError
        return value.domain == "FeishuCLI.ExportPermission" && value.code == 1
    }

    /// Frozen compatibility path for external development API clients only.
    /// The bundled app submits kernel jobs; its rules live in app/feishu_capture.py.
    func fetch(_ url: URL) async throws -> FeishuDocument {
        guard Self.isFeishuLink(url), url.scheme == "https", url.user == nil, url.password == nil,
              url.path.range(of: "^/(wiki|docx|doc)/[A-Za-z0-9]+/?$", options: .regularExpression) != nil else {
            throw failure("当前支持飞书 /wiki/、/docx/ 和 /doc/ 文档链接；表格、多维表格等暂未接入。")
        }
        var components = URLComponents(url: url, resolvingAgainstBaseURL: false)!
        components.fragment = nil // Selection anchors must not silently restrict a full-document import.
        let versionData = try await run(["--version"], timeout: 15)
        let version = String(data: versionData, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "unknown"
        let result = try await json(["docs", "+fetch", "--doc", components.url!.absoluteString,
                                     "--scope", "full", "--doc-format", "markdown", "--as", "user", "--format", "json"])
        var document = try Self.document(result, version: String(version.prefix(80)))
        let xml = try await json(["docs", "+fetch", "--doc", components.url!.absoluteString,
                                  "--scope", "full", "--doc-format", "xml", "--detail", "full",
                                  "--revision-id", String(document.revisionID), "--as", "user", "--format", "json"])
        guard let data = xml["data"] as? [String: Any], let original = data["document"] as? [String: Any],
              original["document_id"] as? String == document.documentID,
              original["revision_id"] as? Int == document.revisionID, let content = original["content"] as? String else {
            throw failure("图片结构与正文版本不一致，请重试导入。")
        }
        document.images = try FeishuImages.references(in: content)
        return document
    }

    static func document(_ result: [String: Any], version: String) throws -> FeishuDocument {
        guard result["ok"] as? Bool == true, result["identity"] as? String == "user",
              let data = result["data"] as? [String: Any], let document = data["document"] as? [String: Any],
              let markdown = document["content"] as? String, !markdown.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              let id = document["document_id"] as? String, !id.isEmpty, id.count <= 200,
              let revision = document["revision_id"] as? Int, revision >= 0 else {
            throw failure("飞书未返回有效的文档正文及版本信息，无法保存。")
        }
        guard markdown.unicodeScalars.count <= maximumCharacters else {
            throw failure("飞书文档超过 100 万字符，当前无法导入；正文未被截断。")
        }
        var title = "飞书文档"
        if let range = markdown.range(of: "(?s)<title>(.*?)</title>", options: .regularExpression) {
            title = String(markdown[range]).replacingOccurrences(of: "<title>", with: "")
                .replacingOccurrences(of: "</title>", with: "")
        } else if let line = markdown.split(separator: "\n").first(where: { $0.hasPrefix("# ") }) {
            title = String(line.dropFirst(2))
        }
        let tips = (document["tips"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        let warnings = data["warnings"] as? [Any] ?? []
        return FeishuDocument(markdown: markdown, title: String(title.trimmingCharacters(in: .whitespacesAndNewlines).prefix(500)),
                              documentID: id, revisionID: revision, cliVersion: version,
                              partial: !tips.isEmpty || !warnings.isEmpty)
    }
}

@MainActor final class FeishuConnection: ObservableObject {
    @Published var executablePath = UserDefaults.standard.string(forKey: "feishuCLIPath") ?? ""
    @Published var profile = UserDefaults.standard.string(forKey: "feishuCLIProfile") ?? ""
    @Published var message = "复用本机飞书 CLI 授权。飞书文档由本机读取，正文保存到当前 \(AppBrand.name) 知识库。"
    @Published var busy = false
    @Published var authorizationURL: URL?
    private var authorizationTask: Task<Void, Never>?

    func client() throws -> FeishuCLI {
        FeishuCLI(executable: try FeishuCLI.resolve(path: executablePath), profile: profile)
    }

    func save() {
        UserDefaults.standard.set(executablePath, forKey: "feishuCLIPath")
        UserDefaults.standard.set(profile, forKey: "feishuCLIProfile")
    }

    func check() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        save()
        do {
            let result = try await client().json(["auth", "status", "--json"])
            let identities = result["identities"] as? [String: Any]
            let user = identities?["user"] as? [String: Any]
            let status = user?["status"] as? String ?? ""
            let tokenStatus = user?["tokenStatus"] as? String ?? ""
            message = user?["available"] as? Bool == true || tokenStatus == "valid"
                || status == "authorized" || status == "authenticated"
                ? "已检测到本机飞书登录。可以粘贴文档链接导入；具体读取权限在导入时检查。"
                : "CLI 可用。请连接飞书，或直接导入以检查已有授权。"
        } catch { message = error.localizedDescription }
    }

    func connect() {
        guard !busy else { return }
        save()
        busy = true
        authorizationTask = Task {
            defer { busy = false; authorizationURL = nil; authorizationTask = nil }
            do {
                let cli = try client()
                let result = try await cli.json(["auth", "login", "--scope", "docx:document:readonly docs:document.media:download", "--no-wait", "--json"])
                try Task.checkCancellation()
                let data = result["data"] as? [String: Any] ?? result
                let link = data["verification_url"] as? String ?? data["verification_uri_complete"] as? String
                guard let link, let url = URL(string: link), url.scheme == "https", FeishuCLI.isFeishuLink(url),
                      let code = data["device_code"] as? String, !code.isEmpty else {
                    throw failure("CLI 未返回有效的飞书授权链接，请检查 CLI 版本及应用配置。")
                }
                authorizationURL = url
                message = "请在浏览器中完成文档只读授权，完成后会自动更新连接状态。"
                DesktopIntegration.shared.open(url)
                _ = try await cli.json(["auth", "login", "--device-code", code, "--json"], timeout: 300)
                try Task.checkCancellation()
                message = "飞书连接成功，可以粘贴文档链接导入。"
            } catch is CancellationError { message = "已停止等待授权。若已在浏览器完成授权，可点击检查连接。" }
            catch { message = error.localizedDescription }
        }
    }

    func cancelAuthorization() { authorizationTask?.cancel() }

    func disconnect() async {
        guard !busy else { return }
        busy = true
        defer { busy = false }
        do {
            _ = try await client().json(["auth", "logout", "--json"])
            message = "已断开此 CLI 配置的飞书连接，可重新授权；已保存的资料仍保留。"
        } catch { message = error.localizedDescription }
    }
}

import Foundation

enum MacCLIResolver {
    static func resolveFeishu(path: String = "") throws -> URL {
        let fileManager = FileManager.default
        var candidates: [String] = []
        if !path.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            candidates = [(path as NSString).expandingTildeInPath]
        } else {
            candidates = ["/opt/homebrew/bin/lark-cli", "/usr/local/bin/lark-cli",
                          fileManager.homeDirectoryForCurrentUser.appendingPathComponent(".local/bin/lark-cli").path]
            candidates += (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":")
                .map { String($0) + "/lark-cli" }
            let nodeVersions = fileManager.homeDirectoryForCurrentUser.appendingPathComponent(".nvm/versions/node")
            let versions = (try? fileManager.contentsOfDirectory(atPath: nodeVersions.path)) ?? []
            candidates += versions.sorted { $0.compare($1, options: .numeric) == .orderedDescending }.map {
                nodeVersions.appendingPathComponent($0 + "/lib/node_modules/@larksuite/cli/bin/lark-cli").path
            }
        }
        for candidate in candidates {
            var url = URL(fileURLWithPath: candidate).resolvingSymlinksInPath()
            // npm's launcher needs Node on PATH. Invoke its native binary directly.
            if url.lastPathComponent == "run.js", url.deletingLastPathComponent().lastPathComponent == "scripts" {
                url = url.deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("bin/lark-cli")
            }
            var directory: ObjCBool = false
            if fileManager.fileExists(atPath: url.path, isDirectory: &directory), !directory.boolValue,
               fileManager.isExecutableFile(atPath: url.path) { return url }
        }
        throw failure("未找到飞书 CLI。请先安装 lark-cli，或在飞书设置中选择其可执行文件。")
    }

    static func detected(executable: String, appName: String, home: URL) -> Bool {
        let manager = FileManager.default
        var directories = (ProcessInfo.processInfo.environment["PATH"] ?? "").split(separator: ":").map(String.init)
        directories += ["/opt/homebrew/bin", "/usr/local/bin", home.appendingPathComponent(".local/bin").path,
                        home.appendingPathComponent(".volta/bin").path, home.appendingPathComponent(".bun/bin").path]
        let node = home.appendingPathComponent(".nvm/versions/node")
        directories += ((try? manager.contentsOfDirectory(atPath: node.path)) ?? []).map { node.appendingPathComponent($0 + "/bin").path }
        if directories.contains(where: { manager.isExecutableFile(atPath: URL(fileURLWithPath: $0).appendingPathComponent(executable).path) }) { return true }
        return [URL(fileURLWithPath: "/Applications"), home.appendingPathComponent("Applications")]
            .contains { manager.fileExists(atPath: $0.appendingPathComponent(appName).path) }
    }
}

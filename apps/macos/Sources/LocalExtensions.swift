import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct LocalExtensionPackage: Decodable, Identifiable {
    let id: String
    let title: String
    let summary: String
    let version: String
    let bytes: Int64
    let installed_bytes: Int64
    let installed: Bool
    let enabled: Bool
    let removal_pending: Bool
    let online_available: Bool?
    let download_bytes: Int64?
    let online_reason: String?
    let components: [LocalExtensionComponent]?
}

struct LocalExtensionComponent: Decodable, Identifiable {
    let id: String
    let title: String
    let detail: String
    let bytes: Int64
}

struct LocalExtensionComponentProgress: Decodable {
    let id: String
    let state: String
    let fraction: Double
}

struct LocalExtensionSnapshot: Decodable {
    let bundled: Bool
    let packages: [LocalExtensionPackage]
    let source: String
}

private struct ExtensionProgress: Decodable {
    let phase: String
    let fraction: Double
    let components: [LocalExtensionComponentProgress]?
}

@MainActor final class LocalExtensionManager: ObservableObject {
    @Published var snapshot: LocalExtensionSnapshot?
    @Published var source = ""
    @Published var busy = false
    @Published var message = ""
    @Published var fraction: Double?
    @Published var phase = ""
    @Published var needsRestart = false
    @Published var running: [String: String] = [:]
    @Published var activePackageID: String?
    @Published var components: [String: LocalExtensionComponentProgress] = [:]
    private var cancelFile: URL?
    private let runtime: URL?
    private let root: URL

    init(runtime: URL? = nil, home: URL? = nil) {
        self.runtime = runtime ?? MacKernelHost.runtime
        self.root = MacPaths().configuration(home: home ?? FileManager.default.homeDirectoryForCurrentUser)
            .appendingPathComponent("extensions")
    }

    func loadCurrent(model: CaptureModel) async {
        guard model.connected else { return }
        do {
            let result = try await API(address: model.address, token: model.token).call("/v1/runtime-extensions")
            let values = result["extensions"] as? [String: [String: Any]] ?? [:]
            running = values.mapValues { $0["status"] as? String ?? "unknown" }
        } catch { message = "无法读取当前生效状态，请刷新重试。" }
    }

    func label(_ package: LocalExtensionPackage) -> String {
        if package.removal_pending { return "等待重启卸载" }
        let loaded = ["ready", "bundled"].contains(running[package.id] ?? "")
        if loaded { return package.enabled ? "已启用" : "等待重启停用" }
        if running[package.id] == "failed" { return package.enabled ? "校验失败 · 请重新安装" : "扩展不可用" }
        return package.enabled ? "等待重启启用" : package.installed ? "已停用" : "未安装"
    }

    func cancel() {
        guard let cancelFile else { return }
        try? Data().write(to: cancelFile, options: .atomic)
        message = "正在取消，请稍候…"
    }

    func run(_ operation: String, value: String? = nil) async {
        guard !busy else { return }
        guard let runtime else {
            message = "当前安装包缺少本地运行环境。"; return
        }
        busy = true
        activePackageID = operation == "download" ? value : nil
        components = [:]
        fraction = nil
        phase = operation == "list" ? "正在读取扩展…" : "正在准备…"
        defer { busy = false; fraction = nil; phase = ""; cancelFile = nil }
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("pka-extension-\(UUID().uuidString)")
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
            defer { try? FileManager.default.removeItem(at: directory) }
            let progress = directory.appendingPathComponent("progress.json")
            cancelFile = directory.appendingPathComponent("cancel")
            let monitor = Task { [weak self] in
                while !Task.isCancelled {
                    if let data = try? Data(contentsOf: progress),
                       let update = try? JSONDecoder().decode(ExtensionProgress.self, from: data) {
                        self?.fraction = min(1, max(0, update.fraction))
                        self?.components = Dictionary(uniqueKeysWithValues: (update.components ?? []).map { ($0.id, $0) })
                        self?.phase = switch update.phase {
                        case "downloading": "正在下载…"
                        case "checking": "正在检查扩展可用性…"
                        case "installing": "正在安装…"
                        case "completed": "安装完成"
                        default: "正在处理…"
                        }
                    }
                    try? await Task.sleep(for: .milliseconds(200))
                }
            }
            defer { monitor.cancel() }
            let layout = MacPaths().runtime(runtime)
            var arguments = ["-I", "-B", layout.backend.appendingPathComponent("app/extension_install.py").path,
                             "--root", root.path, "--progress", progress.path, operation]
            if let value { arguments.append(value) }
            let result = try await MacRestrictedCommands().run(executable: layout.python,
                arguments: arguments, timeout: 3700, workingDirectory: nil, maximumDirectoryBytes: nil)
            if result.exitCode != 0 {
                let error = (try? JSONSerialization.jsonObject(with: result.output)) as? [String: Any]
                throw failure(error?["error"] as? String ?? "扩展操作未完成，请重试；已有资料和扩展保持不变。")
            }
            let state = try JSONDecoder().decode(LocalExtensionSnapshot.self, from: result.output)
            snapshot = state
            source = state.source
            if ["install", "download", "enable", "disable", "remove"].contains(operation) {
                needsRestart = true
                message = "设置已保存。请等待当前任务结束，用 ⌘Q 退出并重新打开客户端后生效。"
            } else if operation == "source" {
                message = "下载源已保存，下载的安装包仍须通过客户端内置清单校验。"
            } else if !needsRestart { message = "" }
        } catch { message = error.localizedDescription }
    }

    func importPackage() {
        let panel = NSOpenPanel()
        panel.title = "选择官方离线扩展包"
        panel.message = "请选择与当前客户端配套的 .pkaext 文件。"
        panel.allowedContentTypes = [UTType(filenameExtension: "pkaext") ?? .data]
        panel.allowsMultipleSelection = false
        panel.canChooseDirectories = false
        guard panel.runModal() == .OK, let url = panel.url else { return }
        Task { await run("install", value: url.path) }
    }
}

@MainActor struct LocalExtensionsView: View {
    @ObservedObject var model: CaptureModel
    @StateObject private var manager: LocalExtensionManager
    @State private var expandedComponents: Set<String> = []

    init(model: CaptureModel, extensionManager: LocalExtensionManager? = nil) {
        self.model = model
        _manager = StateObject(wrappedValue: extensionManager ?? LocalExtensionManager())
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Surface {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "sparkles").foregroundStyle(InboxStyle.accent)
                    VStack(alignment: .leading, spacing: 6) {
                        Text("基础功能与云端 AI").font(.headline)
                        Text("阅读、精确搜索、资料管理及云端 AI 分析随 App 提供，无需安装扩展。AI 调用仍需配置模型并确认。")
                            .font(.callout).foregroundStyle(InboxStyle.muted)
                    }
                    Spacer(minLength: 0)
                    Text("已内置").font(.caption).foregroundStyle(InboxStyle.accent)
                }
            }
            if let state = manager.snapshot {
                if state.bundled {
                    Text("当前为完整运行包，已包含文字识别、文档解析和语义搜索。")
                        .foregroundStyle(InboxStyle.muted)
                } else {
                    HStack {
                        Text("按需安装").font(.headline)
                        Spacer()
                        Button("导入离线扩展包") { manager.importPackage() }
                            .disabled(manager.busy)
                            .accessibilityIdentifier("extensions-import")
                    }
                    ForEach(state.packages) { package in
                        packageRow(package)
                    }
                    Text("点击安装即可自动下载已验证的依赖，无需配置下载地址。下载和校验期间可继续使用客户端；安装完成后，等待任务结束再退出重开生效。")
                        .font(.callout).foregroundStyle(InboxStyle.muted)
                }
            }
            if manager.busy {
                HStack(spacing: 12) {
                    if let fraction = manager.fraction { ProgressView(value: fraction).frame(width: 140) }
                    else { ProgressView().controlSize(.small) }
                    Text(manager.phase).font(.callout)
                    Spacer()
                    Button("取消") { manager.cancel() }
                }
            }
            if !manager.message.isEmpty {
                Text(manager.message).font(.callout).textSelection(.enabled)
                    .foregroundStyle(manager.needsRestart ? Color.orange : InboxStyle.muted)
            }
            Text("停用只影响下次启动后的处理能力，已保存的正文、图片、识别文字和 AI 结果仍可阅读。停用会保留扩展文件；卸载会在相关任务与进程退出后移除扩展程序及模型。")
                .font(.caption).foregroundStyle(InboxStyle.muted)
            Button("刷新状态") { Task { await manager.run("list"); await manager.loadCurrent(model: model) } }.disabled(manager.busy)
        }
        .task { await manager.run("list"); await manager.loadCurrent(model: model) }
        .onChange(of: manager.activePackageID) { _, value in
            if let value { expandedComponents = [value] }
        }
    }

    private func packageRow(_ package: LocalExtensionPackage) -> some View {
        Surface {
            VStack(alignment: .leading, spacing: 12) {
                HStack {
                    Text(package.title).font(.headline)
                    Spacer()
                    Text(manager.label(package))
                        .font(.caption).foregroundStyle(package.enabled ? InboxStyle.accent : InboxStyle.muted)
                }
                Text(package.summary).font(.callout).foregroundStyle(InboxStyle.muted)
                HStack {
                    Text("版本 \(package.version) · \(package.online_available == true ? "下载" : "离线包") \(size(package.download_bytes ?? package.bytes)) · 安装后 \(size(package.installed_bytes))")
                        .font(.caption).foregroundStyle(InboxStyle.muted)
                    Spacer()
                    if package.installed {
                        Button(package.enabled ? "停用" : "启用") {
                            Task { await manager.run(package.enabled ? "disable" : "enable", value: package.id) }
                        }.disabled(manager.busy)
                        Button("卸载") { Task { await manager.run("remove", value: package.id) } }
                            .disabled(manager.busy || package.removal_pending)
                    } else if package.online_available == true {
                        Button("安装") { Task { await manager.run("download", value: package.id) } }
                            .disabled(manager.busy)
                            .help("自动下载并校验扩展所需依赖")
                    } else {
                        Text("仅支持离线安装").font(.caption).foregroundStyle(InboxStyle.muted)
                    }
                }
                if package.online_available != true, let reason = package.online_reason, !reason.isEmpty {
                    Text(reason).font(.caption).foregroundStyle(InboxStyle.muted)
                }
                if let components = package.components, !components.isEmpty {
                    DisclosureGroup(isExpanded: Binding(get: { expandedComponents.contains(package.id) },
                        set: { if $0 { expandedComponents.insert(package.id) } else { expandedComponents.remove(package.id) } })) {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 10) {
                                ForEach(components) { component in
                                    componentRow(component, package: package)
                                }
                            }.padding(.vertical, 8).padding(.trailing, 8)
                        }.frame(height: min(CGFloat(components.count) * 54, 240))
                            .scrollIndicators(.visible)
                    } label: {
                        Text("下载清单 · \(components.count) 项").font(.callout)
                            .foregroundStyle(InboxStyle.muted)
                    }
                }
            }
        }
    }

    private func componentRow(_ component: LocalExtensionComponent, package: LocalExtensionPackage) -> some View {
        let update = manager.activePackageID == package.id ? manager.components[component.id] : nil
        let state = package.installed ? "installed" : update?.state ?? "pending"
        let status: String = switch state {
        case "installed": "已安装"
        case "downloading": "\(Int(min(1, max(0, update?.fraction ?? 0)) * 100))%"
        case "verified": "已校验"
        case "completed": "已就绪"
        case "failed": "下载失败"
        case "cancelled": "已取消"
        default: "待下载"
        }
        return HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 3) {
                Text(component.title).font(.callout).lineLimit(1)
                if !component.detail.isEmpty {
                    Text(component.detail).font(.caption).foregroundStyle(InboxStyle.muted).lineLimit(1)
                }
            }
            Spacer(minLength: 8)
            Text(size(component.bytes)).font(.caption).foregroundStyle(InboxStyle.muted)
            Text(status).font(.caption).monospacedDigit().frame(width: 64, alignment: .trailing)
                .foregroundStyle(state == "failed" ? Color.orange : InboxStyle.muted)
        }.frame(minHeight: 40)
    }

    private func size(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}

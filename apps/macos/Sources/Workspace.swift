import AppKit
import SwiftUI
import UniformTypeIdentifiers

private enum WorkspacePage {
    case reader
    case capture
}

struct WorkspaceView: View {
    @ObservedObject var model: CaptureModel
    @State private var page: WorkspacePage = .reader
    @State private var showSettings = false
    @State private var openBackupSettings = false
    @State private var openedLibrary = false
    private static let brandIcon: NSImage? = {
        guard let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns") else { return nil }
        return NSImage(contentsOf: url)
    }()

    var body: some View {
        Group {
            if !openedLibrary && !model.connected {
                VStack(spacing: 16) {
                    ProgressView("正在打开本地知识库…")
                    if model.usesLocalService {
                        Button("取消启动") { model.cancelLocalServiceStart() }
                    }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.backupBusy {
                VStack(spacing: 16) {
                    ProgressView("正在处理本地备份…")
                    Text("完成后会自动重新打开本地服务。请保持客户端打开。")
                        .foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if model.connected {
                HStack(spacing: 0) {
                    rail
                    Rectangle().fill(InboxStyle.separator).frame(width: 1)
                    switch page {
                    case .reader:
                        KnowledgeView(model: model, showSettings: $showSettings)
                    case .capture:
                        CaptureView(model: model, showSettings: $showSettings, onOpenSource: { page = .reader })
                    }
                }
            } else if model.usesLocalService {
                VStack(spacing: 16) {
                    Text("本地知识库暂时无法打开").font(.title2)
                    Text(model.authMessage).foregroundStyle(.secondary).textSelection(.enabled)
                    Button("重试") { Task { await model.startLocalService() } }.disabled(model.busy || model.recoveringService)
                    Button("备份与恢复…") {
                        openBackupSettings = true
                        showSettings = true
                    }.disabled(model.busy || model.recoveringService || model.backupBusy)
                    if model.busy || model.recoveringService {
                        ProgressView()
                        Button("取消恢复") { model.cancelLocalServiceStart() }
                    }
                }.padding(32).frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                VStack(spacing: 16) {
                    Text("客户端运行包不完整").font(.title2)
                    Text(model.authMessage).foregroundStyle(.secondary).textSelection(.enabled)
                }.padding(32).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .background(InboxStyle.canvas)
        .preferredColorScheme(.dark)
        .tint(InboxStyle.accent)
        // Ordinary actions share the secondary style; sheets set it at their own boundary.
        // Primary actions and compact icon/list controls override it explicitly.
        .buttonStyle(InboxButtonStyle(.secondary))
        .frame(minWidth: 950, minHeight: 680)
        .sheet(isPresented: $showSettings, onDismiss: { openBackupSettings = false }) {
            SettingsView(model: model, openBackup: openBackupSettings)
                .buttonStyle(InboxButtonStyle(.secondary))
        }
        .task {
            await model.openLocalLibrary()
            openedLibrary = true
            await model.openPendingArticleLink()
        }
        .onOpenURL { url in
            page = .reader
            showSettings = false
            model.receiveArticleLink(url)
            NSApp.activate(ignoringOtherApps: true)
            NSApp.windows.first(where: { $0.canBecomeMain })?.makeKeyAndOrderFront(nil)
            Task { await model.openPendingArticleLink() }
        }
        .onChange(of: model.connected) { _, connected in
            if connected { Task { await model.openPendingArticleLink() } }
        }
        .onChange(of: model.backupBusy) { _, busy in
            if !busy { Task { await model.openPendingArticleLink() } }
        }
        .onChange(of: model.trashBusy) { _, busy in
            if !busy { Task { await model.openPendingArticleLink() } }
        }
        .alert("无法打开文章", isPresented: Binding(
            get: { model.articleLinkError != nil },
            set: { if !$0 { model.articleLinkError = nil } })) {
            Button("好") { model.articleLinkError = nil }
        } message: { Text(model.articleLinkError ?? "") }
        .onReceive(NotificationCenter.default.publisher(for: .openSettings)) { _ in
            showSettings = true
        }
        .onReceive(NotificationCenter.default.publisher(for: .localServiceStopped)) { notification in
            model.localServiceStopped(service: notification.object as? LocalService)
        }
        .onReceive(DesktopIntegration.shared.wakeNotifications) { _ in
            model.scheduleLocalServiceWakeCheck()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.didBecomeActiveNotification)) { _ in
            model.scheduleLocalServiceWakeCheck()
        }
        .onReceive(NotificationCenter.default.publisher(for: NSApplication.willTerminateNotification)) { _ in
            model.cleanup()
            model.cancelLocalServiceStart()
        }
    }

    private var rail: some View {
        VStack(spacing: 0) {
            Group {
                if let icon = Self.brandIcon {
                    Image(nsImage: icon).resizable().interpolation(.high).scaledToFit()
                } else {
                    Image(systemName: "square.stack.3d.up").font(.system(size: 20))
                }
            }
                .frame(width: 30, height: 30)
                .frame(height: 56)
                .accessibilityLabel(AppBrand.displayName)
            VStack(spacing: 8) {
                railButton("books.vertical", page: .reader, help: "阅读与知识树")
                railButton("square.and.arrow.down", page: .capture, help: "采集内容")
            }
            .padding(.top, 12)
            Spacer()
            Button { showSettings = true } label: {
                Image(systemName: "gearshape")
                    .font(.system(size: 16)).frame(width: 32, height: 32)
            }
            .buttonStyle(WorkspaceRailButtonStyle())
            .foregroundStyle(InboxStyle.muted)
            .help("设置 (⌘,)")
            .accessibilityLabel("设置")
            .padding(.bottom, 12)
        }
        .frame(width: 52)
        .frame(maxHeight: .infinity)
        .background(InboxStyle.surface)
    }

    private func railButton(_ symbol: String, page target: WorkspacePage, help: String) -> some View {
        Button { page = target } label: {
            Image(systemName: symbol)
                .font(.system(size: 16, weight: page == target ? .medium : .regular))
                .frame(width: 32, height: 32)
                .foregroundStyle(page == target ? InboxStyle.text : InboxStyle.muted)
        }
        .buttonStyle(WorkspaceRailButtonStyle(selected: page == target))
        .help(help)
        .accessibilityLabel(help)
    }
}

private struct WorkspaceRailButtonStyle: ButtonStyle {
    var selected = false

    func makeBody(configuration: Configuration) -> some View {
        RailLabel(configuration: configuration, selected: selected)
    }

    private struct RailLabel: View {
        let configuration: ButtonStyle.Configuration
        let selected: Bool
        @State private var hovering = false
        @Environment(\.isEnabled) private var enabled

        var body: some View {
            configuration.label
                .background(selected || hovering || configuration.isPressed ? InboxStyle.hover : .clear,
                            in: RoundedRectangle(cornerRadius: 8))
                .contentShape(RoundedRectangle(cornerRadius: 8))
                .opacity(!enabled ? 0.4 : configuration.isPressed ? 0.75 : 1)
                .onHover { hovering = $0 }
        }
    }
}

extension Notification.Name {
    static let openSettings = Notification.Name("pka.openSettings")
}

private enum WebReadTab: String, CaseIterable, Identifiable {
    case general = "普通网页"
    case feishu = "飞书"
    case wechat = "微信"

    var id: Self { self }
}

private enum SettingsSection: String, CaseIterable, Identifiable {
    case backup, trash, storage, extensions, ai, agents, tags, web, pdf, about

    var id: Self { self }
    var title: String {
        switch self {
        case .extensions: "扩展功能"
        case .storage: "存储"
        case .tags: "标签管理"
        case .ai: "模型与 AI"
        case .agents: "Agent 接入"
        case .backup: "备份与恢复"
        case .trash: "回收站"
        case .web: "网页读取"
        case .pdf: "PDF 文字识别"
        case .about: "关于"
        }
    }
    var symbol: String {
        switch self {
        case .extensions: "puzzlepiece.extension"
        case .storage: "internaldrive"
        case .tags: "tag"
        case .ai: "sparkles"
        case .agents: "point.3.connected.trianglepath.dotted"
        case .backup: "externaldrive"
        case .trash: "trash"
        case .web: "globe"
        case .pdf: "doc.text.viewfinder"
        case .about: "info.circle"
        }
    }
    var summary: String {
        switch self {
        case .extensions: "按需安装文字识别、文档解析和语义搜索能力"
        case .storage: "查看应用与本地资料占用的存储空间"
        case .tags: "管理标准标签、别名和主题层级"
        case .ai: "配置模型或分析网关，产物保存在本机"
        case .agents: "授权本地 Agent 检索知识并复制连接配置"
        case .backup: "导出资料，或从备份恢复到新知识库"
        case .trash: "恢复删除的资料，或永久清理"
        case .web: "配置普通网页、飞书与微信内容读取"
        case .pdf: "设置 PDF 解析引擎和识别模型"
        case .about: "认识知径 · Knora"
        }
    }
}

struct SettingsView: View {
    @ObservedObject var model: CaptureModel
    @Environment(\.dismiss) private var dismiss
    @State private var section: SettingsSection = .web
    @State private var webReadTab: WebReadTab = .general
    @State private var pendingRestorePackage: URL?
    @State private var confirmRestore = false

    init(model: CaptureModel, openBackup: Bool = false, openAI: Bool = false) {
        self.model = model
        _section = State(initialValue: openBackup ? .backup : openAI ? .ai : .web)
    }

    var body: some View {
        HStack(spacing: 0) {
            sidebar
                .frame(width: 196)
            Rectangle().fill(InboxStyle.separator).frame(width: 1)
            VStack(spacing: 0) {
                HStack {
                    Text(section.title).font(.system(size: 14, weight: .semibold))
                    Spacer()
                    Button("完成") { dismiss() }
                        .buttonStyle(InboxButtonStyle(.secondary))
                        .disabled(model.backupBusy || model.trashBusy || model.tagsBusy || model.agentConnectionsBusy)
                }
                .padding(.horizontal, 24).frame(height: 56)
                Rectangle().fill(InboxStyle.separator).frame(height: 1)
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        Text(section.summary)
                            .font(.system(size: 13))
                            .foregroundStyle(InboxStyle.muted)
                            .fixedSize(horizontal: false, vertical: true)
                        Group {
                            switch section {
                            case .extensions: LocalExtensionsView(model: model)
                            case .storage: LocalStorageView(locations: model.localService.storageLocations())
                            case .tags: LocalTagCatalogView(model: model)
                            case .ai: LocalAISettingsView(model: model)
                            case .agents: LocalAgentConnectionsView(model: model)
                            case .backup: backupSettings
                            case .trash: LocalTrashView(model: model)
                            case .web: webSettings
                            case .pdf: pdfSettings
                            case .about: AboutView()
                            }
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(24)
                }
                .scrollIndicators(.automatic)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .frame(width: settingsSize.width, height: settingsSize.height)
        .font(.system(size: 13))
        .foregroundStyle(InboxStyle.text)
        .buttonStyle(InboxButtonStyle(.secondary))
        .textFieldStyle(InboxFieldStyle())
        .toggleStyle(.switch)
        .controlSize(.small)
        .background(InboxStyle.canvas)
        .interactiveDismissDisabled(model.backupBusy || model.trashBusy || model.tagsBusy || model.agentConnectionsBusy)
        .alert("从备份恢复并打开新知识库？", isPresented: $confirmRestore) {
            Button("取消", role: .cancel) { pendingRestorePackage = nil }
            Button("恢复并打开") {
                guard let package = pendingRestorePackage else { return }
                pendingRestorePackage = nil
                Task { await model.restoreLocalBackup(from: package) }
            }
        } message: {
            Text("备份将恢复到新目录，完成后打开恢复的知识库。当前知识库与未入库草稿会保留，你可以在此页面回到恢复前的知识库。")
        }
    }

    private var settingsSize: CGSize {
        let available = NSScreen.main?.visibleFrame.size ?? CGSize(width: 1000, height: 750)
        return CGSize(width: min(900, max(720, available.width - 80)),
                      height: min(650, max(460, available.height - 80)))
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("设置").font(.system(size: 14, weight: .semibold))
                .padding(.horizontal, 18)
                .frame(height: 56)
            Rectangle().fill(InboxStyle.separator).frame(height: 1)
            ScrollView {
                VStack(spacing: 4) {
                    ForEach(SettingsSection.allCases.filter {
                        model.usesLocalService || ($0 != .backup && $0 != .trash && $0 != .ai && $0 != .agents && $0 != .tags)
                    }) { item in
                        Button { section = item } label: {
                            HStack(spacing: 10) {
                                Image(systemName: item.symbol)
                                    .font(.system(size: 14))
                                    .foregroundStyle(section == item ? InboxStyle.text : InboxStyle.muted)
                                    .frame(width: 18)
                                Text(item.title)
                                Spacer(minLength: 0)
                            }
                            .font(.system(size: 13, weight: section == item ? .medium : .regular))
                            .foregroundStyle(InboxStyle.text)
                            .padding(.horizontal, 10).frame(height: 36)
                        }
                        .buttonStyle(WorkspaceRailButtonStyle(selected: section == item))
                        .disabled(model.backupBusy || model.trashBusy || model.tagsBusy || model.agentConnectionsBusy)
                    }
                }
                .padding(10)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(InboxStyle.surface)
    }

    private var backupSettings: some View {
        VStack(alignment: .leading, spacing: 16) {
            settingsCard {
                VStack(alignment: .leading, spacing: 12) {
                    Text("完整备份").font(.system(size: 14, weight: .semibold))
                    Text("包含已入库资料、补充、版本及文件原件。草稿与采集队列、访问凭证和缓存不包含在备份中。")
                        .font(.callout).foregroundStyle(.secondary)
                    Button("导出本地备份…") {
                        DesktopIntegration.shared.saveFile(title: "导出本地知识库备份",
                            name: "知识库备份-\(Date().formatted(.iso8601.year().month().day().dateSeparator(.dash))).pka-backup",
                            extensions: []) { output in
                            Task { await model.exportLocalBackup(to: output) }
                        }
                    }
                    .disabled(model.backupBusy || model.trashBusy || model.tagsBusy || model.busy || model.browseBusy || model.statusBusy ||
                              model.recoveringService || !model.connected)
                }
            }
            settingsCard {
                VStack(alignment: .leading, spacing: 12) {
                    Text("可读导出").font(.system(size: 14, weight: .semibold))
                    Text("导出 Markdown 与文件原件，便于查看和使用。生成的 ZIP 包含私人知识，不能用于知识库恢复。")
                        .font(.callout).foregroundStyle(.secondary)
                    Button("导出 Markdown 与原件…") {
                        DesktopIntegration.shared.saveFile(title: "导出 Markdown 与文件原件",
                            name: "知识库可读导出-\(Date().formatted(.iso8601.year().month().day().dateSeparator(.dash))).zip",
                            extensions: ["zip"]) { output in
                            Task { await model.exportReadableKnowledge(to: output) }
                        }
                    }
                    .disabled(model.backupBusy || model.trashBusy || model.tagsBusy || model.busy || model.browseBusy || model.statusBusy ||
                              model.recoveringService || !model.connected)
                }
            }
            settingsCard {
                VStack(alignment: .leading, spacing: 12) {
                    Text("恢复知识库").font(.system(size: 14, weight: .semibold))
                    Text("从完整备份恢复到新目录，保留当前知识库。")
                        .font(.callout).foregroundStyle(.secondary)
                    HStack(spacing: 8) {
                        Button("从备份恢复…") {
                            DesktopIntegration.shared.chooseFile(title: "选择本地知识库备份") { package in
                                pendingRestorePackage = package
                                confirmRestore = true
                            }
                        }
                        .disabled(model.backupBusy || model.trashBusy || model.tagsBusy || model.busy || model.browseBusy || model.statusBusy ||
                                  model.recoveringService)
                        if model.canReturnToPreviousLocalLibrary {
                            Button("回到恢复前知识库") {
                                Task { await model.returnToPreviousLocalLibrary() }
                            }
                            .disabled(model.backupBusy || model.trashBusy || model.tagsBusy || model.busy || model.browseBusy || model.statusBusy ||
                                      model.recoveringService)
                        }
                    }
                }
            }
            Text("处理备份期间会暂时停止本地服务，完成后自动重新打开。备份未加密，请保存在可信的位置。")
                .font(.caption).foregroundStyle(.secondary)
            if model.backupBusy { ProgressView("正在处理本地备份…").controlSize(.small) }
            if !model.backupMessage.isEmpty {
                Text(model.backupMessage).font(.callout).textSelection(.enabled)
            }
        }
    }

    private var webSettings: some View {
        VStack(alignment: .leading, spacing: 18) {
            if model.usesLocalService {
                settingsCard { LocalWebNetworkSettingsView(model: model) }
            }
            SettingsTabs(title: "网页来源", selection: $webReadTab, options:
                WebReadTab.allCases.map { InboxSelectOption($0, $0.rawValue) })

            switch webReadTab {
            case .general:
                generalWebSettingsCard
            case .feishu:
                FeishuSettingsView(connection: model.feishu).disabled(model.busy)
            case .wechat:
                wechatSettingsCard
            }
        }
    }

    private var generalWebSettingsCard: some View {
        settingsCard {
            VStack(alignment: .leading, spacing: 18) {
                InboxSelect("读取方式", selection: $model.webReadEngine, options: [
                    InboxSelectOption("legacy", "原有方式 · 轻量读取", symbol: "doc.text"),
                    InboxSelectOption("crawl4ai", "Crawl4AI · 浏览器读取", symbol: "globe")
                ])
                Divider()
                Toggle("通用清洗", isOn: $model.webCleaningEnabled)
                    .disabled(model.webReadEngine != "crawl4ai")
                Text("按页面结构和文字密度减少菜单、广告及页脚。仅影响新保存或重新读取的网页；原始 Markdown 仍可查看。")
                    .font(.callout).foregroundStyle(.secondary)
                HStack {
                    Spacer()
                    Button("保存网页设置") { Task { await model.saveWebReadPreference() } }
                        .buttonStyle(InboxButtonStyle(.primary))
                        .disabled(model.busy)
                }
                if !model.webReadMessage.isEmpty {
                    Text(model.webReadMessage).font(.caption).foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
        }
    }

    private var wechatSettingsCard: some View {
        settingsCard {
            VStack(alignment: .leading, spacing: 18) {
                HStack(alignment: .top, spacing: 12) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text("微信公众号文章读取")
                            .font(.system(size: 14, weight: .semibold))
                        Text("已启用 UA 伪装抓取方案 · 自动识别 mp.weixin.qq.com 链接")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                    HStack(spacing: 5) {
                        Circle().fill(Color.green).frame(width: 5, height: 5)
                        Text("已启用").font(.caption)
                    }
                    .foregroundStyle(InboxStyle.muted)
                    .padding(.top, 3)
                }

                Divider()

                VStack(alignment: .leading, spacing: 8) {
                    Text("自动检测与路由").font(.system(size: 13, weight: .medium))
                    Text("添加微信公众号文章链接时，系统自动采用桌面浏览器 User-Agent 伪装通道，无需额外手动配置。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }

                VStack(alignment: .leading, spacing: 8) {
                    Text("完整正文与元数据提取").font(.system(size: 13, weight: .medium))
                    Text("自动提取文章标题、作者、公众号来源及发布时间；自动转换微信懒加载图片（data-src）并排版为 Markdown。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }

                VStack(alignment: .leading, spacing: 8) {
                    Text("阅读与知识库检索").font(.system(size: 13, weight: .medium))
                    Text("无需配置 Cookie 或微信账号，抓取后自动进入阅读模式与分块检索，并支持查看原始 Markdown。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var pdfSettings: some View {
        settingsCard {
            VStack(alignment: .leading, spacing: 18) {
                InboxSelect("解析引擎", selection: $model.ocrEngine, options: [
                    InboxSelectOption("pdfium", "现有方案 · PDFium + PaddleOCR", symbol: "doc.text.viewfinder"),
                    InboxSelectOption("pdf_inspector", "pdf-inspector + PaddleOCR", symbol: "doc.text.viewfinder")
                ])
                InboxSelect("识别模型", selection: $model.ocrModel, options:
                    (model.usesLocalService ? [] : [InboxSelectOption("tiny", "Tiny · 最省资源")]) +
                    [InboxSelectOption("small", "Small · 均衡")] +
                    (model.usesLocalService ? [] : [InboxSelectOption("medium", "Medium · 较高精度")])
                )
                HStack {
                    Spacer()
                    Button("保存 PDF 设置") { Task { await model.saveOCRPreference() } }
                        .buttonStyle(InboxButtonStyle(.primary))
                        .disabled(model.busy)
                }
                if !model.ocrMessage.isEmpty {
                    Text(model.ocrMessage).font(.caption).foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }
            }
        }
    }

    private func settingsCard<Content: View>(@ViewBuilder content: () -> Content) -> some View {
        content()
            .padding(18)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).stroke(InboxStyle.separator, lineWidth: 1))
    }
}

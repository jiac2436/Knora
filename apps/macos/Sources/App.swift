import SwiftUI
import AppKit

final class PasteTextView: NSTextView {
    var onFiles: (([URL]) -> Void)?
    var onFocusChanged: ((Bool) -> Void)?
    override func becomeFirstResponder() -> Bool {
        let accepted = super.becomeFirstResponder()
        if accepted { onFocusChanged?(true) }
        return accepted
    }
    override func resignFirstResponder() -> Bool {
        let accepted = super.resignFirstResponder()
        if accepted { onFocusChanged?(false) }
        return accepted
    }
    private func files(from info: NSDraggingInfo) -> [URL] {
        info.draggingPasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
    }
    override func draggingEntered(_ sender: NSDraggingInfo) -> NSDragOperation {
        files(from: sender).isEmpty ? super.draggingEntered(sender) : .copy
    }
    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        let urls = files(from: sender)
        guard !urls.isEmpty else { return super.performDragOperation(sender) }
        onFiles?(urls)
        return true
    }
    override func paste(_ sender: Any?) {
        let urls = DesktopIntegration.shared.clipboardFiles()
        if !urls.isEmpty {
            onFiles?(urls)
        } else { super.pasteAsPlainText(sender) }
    }
}

struct PasteEditor: NSViewRepresentable {
    @Binding var text: String
    let onFiles: ([URL]) -> Void
    var onFocusChanged: (Bool) -> Void = { _ in }
    func makeCoordinator() -> Coordinator { Coordinator(self) }
    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        let editor = PasteTextView()
        editor.registerForDraggedTypes([.fileURL])
        editor.isRichText = false
        editor.font = .systemFont(ofSize: 15)
        editor.textContainerInset = NSSize(width: 18, height: 18)
        editor.textColor = NSColor(InboxStyle.text)
        editor.insertionPointColor = NSColor(InboxStyle.accent)
        let paragraphStyle = NSMutableParagraphStyle()
        paragraphStyle.lineSpacing = 5
        editor.defaultParagraphStyle = paragraphStyle
        editor.typingAttributes = [
            .font: NSFont.systemFont(ofSize: 15),
            .foregroundColor: NSColor(InboxStyle.text),
            .paragraphStyle: paragraphStyle
        ]
        editor.isVerticallyResizable = true
        editor.autoresizingMask = [.width]
        editor.textContainer?.widthTracksTextView = true
        editor.delegate = context.coordinator
        editor.onFiles = onFiles
        editor.onFocusChanged = onFocusChanged
        scroll.documentView = editor
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        editor.drawsBackground = false
        return scroll
    }
    func updateNSView(_ view: NSScrollView, context: Context) {
        context.coordinator.parent = self
        guard let editor = view.documentView as? PasteTextView else { return }
        editor.onFiles = onFiles
        editor.onFocusChanged = onFocusChanged
        if editor.string != text { editor.string = text }
    }
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: PasteEditor
        init(_ parent: PasteEditor) { self.parent = parent }
        func textDidChange(_ notification: Notification) {
            if let editor = notification.object as? NSTextView { parent.text = editor.string }
        }
    }
}

struct CaptureView: View {
    @ObservedObject var model: CaptureModel
    @Binding var showSettings: Bool
    var onOpenSource: () -> Void = {}
    @State private var showCreation = false
    @State private var composerFocused = false
    @State private var queueFilter: CaptureQueueFilter = .pending
    @AppStorage("captureWorkbenchCollapsed") private var workbenchCollapsed = false

    private var canSend: Bool {
        model.connected && (!model.domainID.isEmpty || model.domains.isEmpty) &&
        (!model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.items.contains { !$0.sent })
    }

    var body: some View {
        VStack(spacing: 0) {
            toolbar
            GeometryReader { geometry in
                ScrollView {
                    VStack(alignment: .leading, spacing: 18) {
                        HStack(alignment: .center, spacing: 24) {
                            introduction.frame(maxWidth: .infinity, alignment: .leading)
                            if !workbenchCollapsed {
                                CaptureWorkbench(items: model.items,
                                    domains: model.domains, selectedDomainID: model.domainID,
                                    connected: model.connected, submitting: model.busy)
                                    .frame(width: min(580, geometry.size.width * 0.49))
                            }
                        }
                        .padding(.bottom, 8)
                        Rectangle().fill(InboxStyle.separator).frame(height: 1)
                        if !model.connected { connectionNotice }
                        HStack(alignment: .top, spacing: 24) {
                            VStack(alignment: .leading, spacing: 18) {
                                sectionHeading("1", title: "添加资料", detail: "粘贴文字或链接，也可以拖入文件")
                                composer
                                sectionHeading("2", title: "选择保存位置", detail: "资料将归入所选知识库，项目为可选项")
                                destination
                            }
                            .frame(maxWidth: .infinity)
                            captureQueue
                                .frame(width: min(440, max(340, (geometry.size.width - 64) * 0.38)))
                        }
                    }
                    .frame(maxWidth: 1240, alignment: .leading)
                    .padding(.horizontal, 32)
                    .padding(.vertical, 20)
                    .frame(maxWidth: .infinity)
                }
            }
            saveBar
        }
        .background(InboxStyle.canvas)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .task {
            await model.refreshFileStatuses(quiet: true, includeFinished: true)
            while !Task.isCancelled {
                do { try await Task.sleep(for: .seconds(4)) } catch { return }
                await model.refreshFileStatuses(quiet: true)
            }
        }
    }

    private var toolbar: some View {
        HStack(spacing: 10) {
            Image(systemName: "tray.and.arrow.down")
                .foregroundStyle(InboxStyle.muted)
            Text("采集").font(.system(size: 13, weight: .medium))
            Spacer()
            if model.busy { ProgressView().controlSize(.small) }
            Button { workbenchCollapsed.toggle() } label: {
                Image(systemName: workbenchCollapsed ? "chevron.down" : "chevron.up")
            }
            .buttonStyle(InboxButtonStyle(.quiet))
            .help(workbenchCollapsed ? "展开熊猫整理台" : "收起熊猫整理台")
            .accessibilityLabel(workbenchCollapsed ? "展开熊猫整理台" : "收起熊猫整理台")
            Text("笔记 · 链接 · 文件")
                .font(.system(size: 12))
                .foregroundStyle(InboxStyle.muted)
        }
        .padding(.horizontal, 24)
        .frame(height: 56)
        .overlay(alignment: .bottom) { Rectangle().fill(InboxStyle.separator).frame(height: 1) }
    }

    private var introduction: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("把资料交给熊猫")
                .font(.system(size: 24, weight: .semibold))
                .foregroundStyle(InboxStyle.text)
            Text("添加资料，选择知识库，点击「开始整理」。\n保存和读取进度都可以在右侧查看。")
                .font(.system(size: 13))
                .foregroundStyle(InboxStyle.muted)
        }
    }

    private func sectionHeading(_ number: String, title: String, detail: String) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text(number).font(.system(size: 12, weight: .semibold))
                .foregroundStyle(InboxStyle.accent)
                .frame(width: 24, height: 24)
                .background(InboxStyle.accent.opacity(0.10), in: Circle())
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 14, weight: .semibold))
                Text(detail).font(.system(size: 11)).foregroundStyle(InboxStyle.muted)
            }
        }
    }

    private var connectionNotice: some View {
        HStack(spacing: 10) {
            Image(systemName: "wifi.exclamationmark").foregroundStyle(.orange)
            Text("连接后台后即可保存内容。")
                .font(.system(size: 13))
            Spacer()
            Button("设置连接") { showSettings = true }
                .buttonStyle(InboxButtonStyle(.secondary))
        }
        .padding(12)
        .background(.orange.opacity(0.08), in: RoundedRectangle(cornerRadius: 8))
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 0) {
            PasteEditor(text: $model.draft, onFiles: { if !model.busy { model.addFiles($0) } },
                        onFocusChanged: { composerFocused = $0 })
                .frame(height: 200)
                .overlay(alignment: .topLeading) {
                    if model.draft.isEmpty && !composerFocused {
                        Text("粘贴文字、链接，或拖入文件…")
                            .font(.system(size: 15))
                            .foregroundStyle(InboxStyle.muted)
                            .padding(.leading, 20)
                            .padding(.top, 20)
                            .allowsHitTesting(false)
                    }
                }
                .disabled(model.busy)
            HStack(spacing: 8) {
                Button("粘贴", systemImage: "clipboard") { model.paste() }
                    .buttonStyle(InboxButtonStyle(.quiet))
                Button("添加文件", systemImage: "paperclip") { model.chooseFiles() }
                    .buttonStyle(InboxButtonStyle(.quiet))
                Spacer(minLength: 4)
                if let kind = model.draftKind {
                    Label(kind.label, systemImage: kind.symbol)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(InboxStyle.accent)
                }
                Button("加入列表", systemImage: "plus") { model.addDraft() }
                    .buttonStyle(InboxButtonStyle(.secondary))
                    .disabled(model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .disabled(model.busy)
            .padding(10)
        }
        .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(InboxStyle.separator))
        .overlay(alignment: .bottomTrailing) {
            Text("支持粘贴 Finder 文件 · 单文件 ≤ 50 MB")
                .font(.system(size: 11))
                .foregroundStyle(InboxStyle.muted)
                .offset(y: 21)
        }
        .padding(.bottom, 12)
    }

    private var destination: some View {
        HStack(alignment: .bottom, spacing: 12) {
            InboxSelect("保存到知识库", selection: $model.domainID, options:
                [InboxSelectOption("", model.domains.isEmpty ? "首次整理时创建默认知识库" : "选择知识库", symbol: "folder")] +
                model.domains.map { InboxSelectOption($0.id, $0.name, symbol: "folder") }
            )
            .frame(maxWidth: .infinity)
            InboxSelect("关联项目", selection: $model.projectID, options:
                [InboxSelectOption("", "不关联项目", symbol: "square.stack")] +
                model.projects.map { InboxSelectOption($0.id, $0.name, symbol: "square.stack") }
            )
            .frame(maxWidth: .infinity)
            Button { showCreation.toggle() } label: { Image(systemName: "plus") }
                .buttonStyle(InboxButtonStyle(.quiet))
                .frame(height: 38)
                .help("新建知识库或项目")
                .accessibilityLabel("新建知识库或项目")
                .popover(isPresented: $showCreation) {
                    VStack(alignment: .leading, spacing: 16) {
                        Text("新建知识库或项目").font(.headline)
                        createDomain
                        createProject
                    }.padding(20).frame(width: 380)
                }
        }
        .font(.system(size: 12))
        .padding(12)
        .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 10))
        .disabled(model.busy)
    }

    private var createDomain: some View {
        HStack(spacing: 8) {
            TextField("知识库名称", text: $model.domainName)
                .textFieldStyle(InboxFieldStyle())
                .frame(minWidth: 100)
            Button("创建知识库") { Task { await model.createDomain() } }
                .buttonStyle(InboxButtonStyle(.secondary))
                .disabled(!model.connected || model.busy || model.domainName.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private var createProject: some View {
        HStack(spacing: 8) {
            TextField("项目名称", text: $model.projectName)
                .textFieldStyle(InboxFieldStyle())
                .frame(minWidth: 100)
            Button("创建项目") { Task { await model.createProject() } }
                .buttonStyle(InboxButtonStyle(.secondary))
                .disabled(!model.connected || model.busy || model.projectName.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    private var filteredItems: [CaptureItem] {
        model.items.filter { $0.queueFilter == queueFilter }
    }

    private var captureQueue: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Text("整理队列").font(.system(size: 13, weight: .semibold))
                Text("\(model.items.count) 项")
                    .font(.system(size: 12).monospacedDigit())
                    .foregroundStyle(InboxStyle.muted)
                Spacer()
                if queueFilter != .processing {
                    Button("清空", systemImage: "trash") {
                        model.clearQueue(queueFilter)
                    }
                    .buttonStyle(InboxButtonStyle(.quiet))
                    .help("仅清空当前列表，保留知识库中的资料")
                    .disabled(model.busy || filteredItems.isEmpty)
                }
                if model.statusBusy { ProgressView().controlSize(.small) }
                Button {
                    Task { await model.refreshFileStatuses() }
                } label: {
                    Image(systemName: "arrow.clockwise")
                }
                .buttonStyle(InboxButtonStyle(.quiet))
                .help("刷新资料状态")
                .accessibilityLabel("刷新资料状态")
                .disabled(model.busy || model.statusBusy || !model.connected || !model.items.contains {
                    $0.sent && $0.sourceID != nil && $0.kind != .note
                })
            }
            HStack(spacing: 4) {
                ForEach(CaptureQueueFilter.allCases) { filter in
                    Button {
                        queueFilter = filter
                    } label: {
                        HStack(spacing: 4) {
                            Text(filter.rawValue)
                            Text("\(model.items.filter { $0.queueFilter == filter }.count)")
                                .monospacedDigit().foregroundStyle(InboxStyle.muted)
                        }
                        .font(.system(size: 11, weight: queueFilter == filter ? .semibold : .regular))
                        .frame(maxWidth: .infinity).frame(height: 36)
                        .contentShape(Rectangle())
                        .background(queueFilter == filter ? InboxStyle.hover : .clear,
                                    in: RoundedRectangle(cornerRadius: 7))
                    }
                    .buttonStyle(.plain)
                    .foregroundStyle(queueFilter == filter ? InboxStyle.text : InboxStyle.muted)
                    .accessibilityLabel("\(filter.rawValue)，\(model.items.filter { $0.queueFilter == filter }.count) 项")
                    .accessibilityAddTraits(queueFilter == filter ? .isSelected : [])
                }
            }
            .padding(3)
            .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 9))
            ScrollView(.vertical) {
                LazyVStack(spacing: 0) {
                    ForEach(filteredItems) { item in captureRow(item) }
                }
                .frame(maxWidth: .infinity)
            }
            .scrollIndicators(.visible)
            .id(queueFilter)
            .frame(height: 360)
            .overlay {
                if filteredItems.isEmpty {
                    VStack(spacing: 10) {
                        Image(systemName: queueFilter.symbol).font(.system(size: 23, weight: .light))
                        Text(queueFilter.emptyMessage).font(.system(size: 12))
                    }
                    .foregroundStyle(InboxStyle.muted).allowsHitTesting(false)
                }
            }
            .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 10))
            .clipShape(RoundedRectangle(cornerRadius: 10))
        }
    }

    private var saveBar: some View {
        HStack(alignment: .center, spacing: 24) {
            VStack(alignment: .leading, spacing: 5) {
                if !model.message.isEmpty {
                    Text(model.message)
                        .font(.system(size: 12, weight: .medium))
                        .foregroundStyle(InboxStyle.text)
                        .lineLimit(2).textSelection(.enabled)
                }
                Text(model.usesLocalService ? "未发送内容已保存在本机，可稍后继续。" : "未发送的内容仅在本次运行中保留。")
                    .font(.system(size: 11))
                    .foregroundStyle(InboxStyle.muted)
                if !model.queueMessage.isEmpty {
                    Text(model.queueMessage)
                        .font(.system(size: 11))
                        .foregroundStyle(InboxStyle.muted)
                }
            }
            Spacer(minLength: 4)
            Button {
                Task { await model.send() }
            } label: {
                HStack(spacing: 12) {
                    Text("开始整理")
                    Text("⌘↩").font(.system(size: 12)).opacity(0.6)
                }
            }
            .buttonStyle(InboxButtonStyle(.primary))
            .keyboardShortcut(.return, modifiers: .command)
            .disabled(model.busy || !canSend)
        }
        .frame(maxWidth: 1240, alignment: .leading)
        .padding(.horizontal, 32)
        .padding(.vertical, 16)
        .frame(maxWidth: .infinity)
        .background(InboxStyle.canvas)
        .overlay(alignment: .top) { Rectangle().fill(InboxStyle.separator).frame(height: 1) }
    }

    private func captureRow(_ item: CaptureItem) -> some View {
        HStack(alignment: .center, spacing: 12) {
            Image(systemName: item.kind.symbol)
                .font(.system(size: 16))
                .foregroundStyle(InboxStyle.muted)
                .frame(width: 28)
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 8) {
                    Text(item.title).font(.system(size: 13, weight: .medium)).lineLimit(1)
                    Text(item.kind.label).font(.system(size: 10)).foregroundStyle(InboxStyle.muted)
                }
                Text(item.status).font(.system(size: 12)).foregroundStyle(InboxStyle.muted)
                    .lineLimit(2).textSelection(.enabled)
            }
            Spacer(minLength: 8)
            if item.stage.active {
                ProgressView().controlSize(.small)
            } else if item.stage == .ready {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                    .accessibilityLabel("内容已就绪")
            } else if item.stage == .attention {
                Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
                if let sourceID = item.sourceID {
                    Button { Task { await model.openSource(sourceID, force: true); onOpenSource() } }
                    label: { Image(systemName: "arrow.up.right") }
                        .help("查看原因或重试").accessibilityLabel("查看原因或重试")
                        .buttonStyle(InboxButtonStyle(.quiet))
                }
            } else if item.sent {
                Image(systemName: "tray.full").foregroundStyle(InboxStyle.muted)
                    .accessibilityLabel("原件已保存")
            }
            if item.sent && item.stage != .attention, let sourceID = item.sourceID {
                Button {
                    Task { await model.openSource(sourceID); onOpenSource() }
                } label: { Image(systemName: "arrow.up.right") }
                .buttonStyle(InboxButtonStyle(.quiet))
                .help("打开资料").accessibilityLabel("打开\(item.title)")
            }
            Button {
                model.remove(item)
            } label: {
                Image(systemName: "xmark").font(.system(size: 11))
            }
            .buttonStyle(InboxButtonStyle(.quiet))
            .help("从采集列表移除")
            .accessibilityLabel("移除\(item.title)")
            .disabled(model.busy)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 13)
        .overlay(alignment: .bottom) {
            Rectangle().fill(InboxStyle.separator).frame(height: 1).padding(.leading, 54)
        }
    }
}

#if !CAPTURE_QUEUE_TESTS
@main struct KnowledgeInboxApp: App {
    @StateObject private var model = CaptureModel()
    var body: some Scene {
        Window(AppBrand.displayName, id: "main") { WorkspaceView(model: model) }
            .commands {
                CommandGroup(replacing: .newItem) {}
                CommandGroup(replacing: .appSettings) {
                    Button("设置…") {
                        NotificationCenter.default.post(name: .openSettings, object: nil)
                    }
                    .keyboardShortcut(",", modifiers: .command)
                }
            }
    }
}

#endif

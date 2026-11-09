import SwiftUI

/// Presentation only: saving a source is not the same as finishing extraction.
enum CaptureStage: Equatable {
    case waiting, receiving, reading, ready, saved, attention

    static func resolve(status: String, sent: Bool) -> Self {
        if ["失败", "取消", "缺失", "已变更", "未获取", "状态暂不可用"].contains(where: status.contains) { return .attention }
        if status.contains("可搜索") || status == "笔记已保存" { return .ready }
        if ["正在发送", "上传中", "正在保存"].contains(where: status.contains) { return .receiving }
        if ["等待", "读取", "识别中", "解析中", "正在获取", "仍在处理"].contains(where: status.contains) {
            return sent || status.contains("正在") ? .reading : .waiting
        }
        return sent ? .saved : .waiting
    }

    var active: Bool { self == .receiving || self == .reading }
    var label: String {
        switch self {
        case .waiting: "等待整理"
        case .receiving: "接收资料"
        case .reading: "读取内容"
        case .ready: "整理完成"
        case .saved: "原件已保存"
        case .attention: "需要处理"
        }
    }
}

enum CaptureQueueFilter: String, CaseIterable, Identifiable {
    case pending = "待处理"
    case processing = "处理中"
    case completed = "已完成"
    case failed = "失败"

    var id: Self { self }
    var symbol: String {
        switch self {
        case .pending: "tray"
        case .processing: "hourglass"
        case .completed: "checkmark.circle"
        case .failed: "exclamationmark.circle"
        }
    }
    var emptyMessage: String {
        switch self {
        case .pending: "暂无待处理资料"
        case .processing: "暂无正在处理的资料"
        case .completed: "暂无已完成资料"
        case .failed: "暂无失败资料"
        }
    }
}

extension CaptureItem {
    var stage: CaptureStage { .resolve(status: status, sent: sent) }
    var queueFilter: CaptureQueueFilter {
        switch stage {
        case .waiting: .pending
        case .receiving, .reading: .processing
        case .ready, .saved: .completed
        case .attention: .failed
        }
    }
}

/// Only a successful, settled batch starts the five-second return timer.
/// Its identity changes when work is added/retried, cancelling an older timer.
struct CaptureWorkbenchActivity: Equatable {
    struct TaskState: Equatable {
        let id: String
        let stage: CaptureStage
        let sent: Bool
    }
    let tasks: [TaskState]
    let submitting: Bool
    init(items: [CaptureItem], submitting: Bool) {
        tasks = items.map { TaskState(id: $0.id, stage: $0.stage, sent: $0.sent) }
        self.submitting = submitting
    }
    var canReturnHome: Bool {
        !submitting && !tasks.isEmpty && tasks.allSatisfy {
            $0.sent && ($0.stage == .ready || $0.stage == .saved)
        }
    }
}

struct CaptureWorkbench: View {
    let items: [CaptureItem]
    let domains: [DomainItem]
    let selectedDomainID: String
    let connected: Bool
    var submitting = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.scenePhase) private var scenePhase
    @State private var visible = false
    @State private var filing = false
    @State private var resting = false
    @State private var previousActivity: CaptureWorkbenchActivity?

    private var activity: CaptureWorkbenchActivity {
        CaptureWorkbenchActivity(items: items, submitting: submitting)
    }
    private var focus: CaptureItem? {
        guard !resting, !(previousActivity == nil && activity.canReturnHome) else { return nil }
        return items.first { $0.stage.active } ?? items.first { $0.stage == .attention }
            ?? items.first { !$0.sent } ?? items.last
    }
    private var library: String {
        domains.first { $0.id == (focus?.domainID ?? selectedDomainID) }?.name ?? "选择知识库"
    }
    private var stage: CaptureStage { focus?.stage ?? .waiting }
    private var running: Bool { connected && stage.active && visible && scenePhase == .active && !reduceMotion }
    private static let panda: NSImage? = {
        guard let url = Bundle.main.url(forResource: "panda", withExtension: "png", subdirectory: "Capture") else { return nil }
        return NSImage(contentsOf: url)
    }()

    var body: some View {
        VStack(spacing: 8) {
            TimelineView(.animation(minimumInterval: 1.0 / 24, paused: !running)) { context in
                scene(time: running ? context.date.timeIntervalSinceReferenceDate : 0)
            }
            .frame(height: 160)
            HStack(spacing: 18) {
                step("接收", symbol: "tray.and.arrow.down", selected: stage == .receiving)
                Image(systemName: "chevron.right").font(.system(size: 9))
                step("读取", symbol: "doc.text.viewfinder", selected: stage == .reading)
                Image(systemName: "chevron.right").font(.system(size: 9))
                step("归档", symbol: "folder", selected: stage == .ready || stage == .saved)
            }
            .foregroundStyle(InboxStyle.muted)
            Text(!connected ? "连接后继续整理，待发送资料会留在本机" : focus.map { "\($0.title) · \($0.status)" } ?? (items.isEmpty ? "熊猫准备好了，添加第一份资料吧" : "整理完成，熊猫准备好接收新的资料了"))
                .font(.system(size: 12)).foregroundStyle(stage == .attention ? .orange : InboxStyle.muted)
                .lineLimit(1).help(focus?.status ?? "添加文件、文字或链接")
        }
        .frame(maxWidth: .infinity)
        .task(id: activity) {
            let entering = previousActivity == nil
            previousActivity = activity
            resting = entering && activity.canReturnHome
            guard activity.canReturnHome, !entering else { return }
            do { try await Task.sleep(for: .seconds(5)) } catch { return }
            guard !Task.isCancelled else { return }
            withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.65)) { resting = true }
        }
        .task(id: stage) {
            guard stage == .ready, !reduceMotion else { filing = false; return }
            filing = true
            do { try await Task.sleep(for: .milliseconds(700)) } catch { return }
            withAnimation(.easeOut(duration: 0.25)) { filing = false }
        }
        .onAppear { visible = true }
        .onDisappear { visible = false }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("熊猫整理台，\(stage.label)。\(focus?.status ?? "等待添加资料")")
    }

    private func step(_ title: String, symbol: String, selected: Bool) -> some View {
        Label(title, systemImage: symbol)
            .font(.system(size: 12, weight: selected ? .semibold : .regular))
            .foregroundStyle(selected ? InboxStyle.accent : InboxStyle.muted)
    }

    private func scene(time: Double) -> some View {
        GeometryReader { geometry in
            let width = geometry.size.width
            let phase = (sin(time * 2) + 1) / 2
            let position: CGFloat = stage == .ready || stage == .saved ? 0.73 : stage == .waiting ? 0.25 : 0.43
            ZStack {
                Capsule().fill(InboxStyle.separator).frame(width: width * 0.72, height: 1).offset(y: 45)
                tray.position(x: width * 0.14, y: 102)
                scanner(scan: phase).position(x: width * 0.56, y: 105)
                folder.position(x: width * 0.87, y: 98)
                ZStack {
                    if let panda = Self.panda {
                        Image(nsImage: panda).resizable().interpolation(.high).scaledToFit()
                    } else {
                        Text("🐼").font(.system(size: 95))
                    }
                    if stage == .receiving || stage == .reading || filing {
                        paper.frame(width: 31, height: 42).rotationEffect(.degrees(12))
                            .offset(x: 35, y: 15)
                    }
                    if (stage == .ready && !filing) || stage == .attention {
                        Image(systemName: stage == .ready ? "checkmark.circle.fill" : "exclamationmark.circle.fill")
                            .font(.system(size: 22)).foregroundStyle(stage == .ready ? .green : .orange)
                            .background(InboxStyle.canvas, in: Circle()).offset(x: 43, y: -48)
                    }
                }
                .frame(width: 132, height: 150)
                .rotationEffect(.degrees(running && stage == .receiving ? sin(time * 4) * 2 : 0))
                .position(x: width * position, y: 78 + (running ? sin(time * 3) * 2 : 0))
                .animation(reduceMotion ? nil : .easeInOut(duration: 0.65), value: stage)
            }
        }
    }

    private var paper: some View {
        RoundedRectangle(cornerRadius: 3).fill(Color(white: 0.88).gradient)
            .overlay {
                VStack(spacing: 4) {
                    ForEach(0..<3) { _ in Capsule().fill(Color.black.opacity(0.2)).frame(height: 2) }
                }.padding(6)
            }
            .shadow(color: .black.opacity(0.15), radius: 3, y: 2)
    }

    private var tray: some View {
        ZStack(alignment: .bottom) {
            ForEach(0..<3) { index in
                paper.frame(width: 43, height: 53).rotationEffect(.degrees(-7))
                    .offset(x: CGFloat(index * 10 - 10), y: CGFloat(-index * 7 - 18))
            }
            RoundedRectangle(cornerRadius: 7).fill(InboxStyle.hover.gradient)
                .frame(width: 100, height: 42)
                .overlay(Text("待整理").font(.system(size: 11)).foregroundStyle(InboxStyle.muted))
        }.frame(width: 105, height: 85)
    }

    private func scanner(scan: Double) -> some View {
        ZStack {
            RoundedRectangle(cornerRadius: 10).fill(InboxStyle.hover.gradient)
                .frame(width: 112, height: 58)
                .overlay(RoundedRectangle(cornerRadius: 10).stroke(InboxStyle.separator))
            RoundedRectangle(cornerRadius: 4).fill(InboxStyle.accent.opacity(0.12))
                .frame(width: 84, height: 27).offset(y: -6)
            if stage == .reading {
                Capsule().fill(InboxStyle.accent).frame(width: 76, height: 2)
                    .shadow(color: InboxStyle.accent.opacity(0.6), radius: 6)
                    .offset(y: -16 + scan * 20)
            }
            Capsule().fill(connected && stage == .reading ? InboxStyle.accent : InboxStyle.muted)
                .frame(width: 18, height: 3).offset(y: 19)
        }.frame(width: 120, height: 70)
    }

    private var folder: some View {
        VStack(spacing: 4) {
            Image(systemName: "folder.fill")
                .font(.system(size: 75, weight: .light))
                .foregroundStyle(InboxStyle.accent.gradient)
                .shadow(color: .black.opacity(0.2), radius: 8, y: 5)
            Text(library).font(.system(size: 11, weight: .medium))
                .foregroundStyle(InboxStyle.muted).lineLimit(1)
        }.frame(width: 110)
    }
}

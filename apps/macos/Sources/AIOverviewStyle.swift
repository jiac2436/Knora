import SwiftUI

enum AIReadPhase { case loading, ready, failed }
struct AIReadStatus {
    let sourceID: String
    let sourceGeneration: UUID
    let phase: AIReadPhase
}

@MainActor extension CaptureModel {
    var aiSourceReadPhase: AIReadPhase {
        guard let status = aiReadStatus, status.sourceID == selectedSource?.id,
              status.sourceGeneration == sourceLoadGeneration else { return .loading }
        return status.phase
    }
    var aiObservationID: String {
        "\(connected):\(aiGeneration):\(sourceLoadGeneration):\(selectedSource?.id ?? "")"
    }
}

struct AIOverviewPresentation {
    var isVisible = false
    var isExpanded = false
    mutating func toggle() {
        isVisible.toggle()
        isExpanded = false
    }
}

/// The current task takes precedence over an earlier saved result.
struct AIOverviewState {
    let job: LocalAIJob?
    let artifact: LocalAIArtifact?

    init(sourceID: String?, jobs: [LocalAIJob], artifacts: [LocalAIArtifact]) {
        let currentJobs = jobs.filter { $0.source_id == sourceID }
        job = currentJobs.first(where: \.isActive) ?? currentJobs.first
        artifact = artifacts.first { $0.source_id == sourceID && !$0.stale && $0.phase != "segment" }
    }

    enum Action { case progress, overview, confirm }
    func action(forceReanalysis: Bool = false) -> Action {
        if isActive { return .progress }
        return artifact != nil && !forceReanalysis ? .overview : .confirm
    }

    var isActive: Bool { job?.isActive == true }
    var canExpand: Bool { artifact != nil && !isActive }
    var progressText: String {
        guard let job else { return "" }
        if job.pipeline_status == "indexing" { return "正在更新知识检索…" }
        if job.status == "pending" { return "已加入分析队列，等待开始…" }
        guard isActive else { return job.statusText }
        if let total = job.total_steps, total > 1 {
            let completed = min(total, max(0, job.completed_steps ?? 0))
            return "正在分析资料 · 已完成 \(completed) / \(total) 步"
        }
        return "正在分析正文与图片…"
    }
}

/// A small native arc spinner, inspired by LottieFiles' minimal loading loops.
struct AIOverviewLoading: View {
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        TimelineView(.animation(minimumInterval: 1.0 / 30, paused: reduceMotion)) { context in
            let turn = reduceMotion ? 0 : context.date.timeIntervalSinceReferenceDate.truncatingRemainder(dividingBy: 1.1) / 1.1
            ZStack {
                Circle().stroke(InboxStyle.accent.opacity(0.15), lineWidth: 1.7)
                Circle().trim(from: 0.04, to: 0.76)
                    .stroke(InboxStyle.accent, style: StrokeStyle(lineWidth: 1.7, lineCap: .round))
                    .rotationEffect(.degrees(turn * 360))
            }.padding(2)
        }
        .frame(width: 18, height: 18)
        .accessibilityLabel("AI 分析处理中")
    }
}

/// One continuous outline avoids a seam between the pointer and the card.
struct AIOverviewBubble: Shape {
    var pointerX: CGFloat?
    static let pointerHeight: CGFloat = 14
    static let pointerHalfWidth: CGFloat = 18

    static func pointer(_ proposed: CGFloat, width: CGFloat) -> CGFloat {
        min(max(24, proposed), max(24, width - 24))
    }

    func path(in rect: CGRect) -> Path {
        let top: CGFloat = pointerX == nil ? 0 : Self.pointerHeight
        let radius: CGFloat = 10
        var p = Path()
        p.move(to: CGPoint(x: radius, y: top))
        if let pointerX {
            let x = Self.pointer(pointerX, width: rect.width)
            p.addLine(to: CGPoint(x: x - Self.pointerHalfWidth, y: top))
            p.addLine(to: CGPoint(x: x, y: 0))
            p.addLine(to: CGPoint(x: x + Self.pointerHalfWidth, y: top))
        }
        p.addLine(to: CGPoint(x: rect.width - radius, y: top))
        p.addQuadCurve(to: CGPoint(x: rect.width, y: top + radius), control: CGPoint(x: rect.width, y: top))
        p.addLine(to: CGPoint(x: rect.width, y: rect.height - radius))
        p.addQuadCurve(to: CGPoint(x: rect.width - radius, y: rect.height), control: CGPoint(x: rect.width, y: rect.height))
        p.addLine(to: CGPoint(x: radius, y: rect.height))
        p.addQuadCurve(to: CGPoint(x: 0, y: rect.height - radius), control: CGPoint(x: 0, y: rect.height))
        p.addLine(to: CGPoint(x: 0, y: top + radius))
        p.addQuadCurve(to: CGPoint(x: radius, y: top), control: CGPoint(x: 0, y: top))
        p.closeSubpath()
        return p
    }
}

/// Wraps tag chips without forcing the overview to scroll horizontally.
struct AIOverviewTagLayout: Layout {
    var spacing: CGFloat = 8
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrangement(width: proposal.width ?? 600, subviews: subviews).size
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let layout = arrangement(width: bounds.width, subviews: subviews)
        for (index, frame) in layout.frames.enumerated() {
            subviews[index].place(at: CGPoint(x: bounds.minX + frame.minX, y: bounds.minY + frame.minY),
                proposal: ProposedViewSize(frame.size))
        }
    }
    private func arrangement(width: CGFloat, subviews: Subviews) -> (size: CGSize, frames: [CGRect]) {
        let width = max(1, width)
        var frames: [CGRect] = [], x: CGFloat = 0, y: CGFloat = 0, rowHeight: CGFloat = 0
        for view in subviews {
            let size = view.sizeThatFits(ProposedViewSize(width: width, height: nil))
            if x > 0 && x + size.width > width { x = 0; y += rowHeight + spacing; rowHeight = 0 }
            frames.append(CGRect(x: x, y: y, width: min(size.width, width), height: size.height))
            x += size.width + spacing; rowHeight = max(rowHeight, size.height)
        }
        return (CGSize(width: width, height: frames.isEmpty ? 0 : y + rowHeight), frames)
    }
}

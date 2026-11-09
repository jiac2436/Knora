import Foundation
import SwiftUI

struct LocalAIAnalyzeButton: View {
    @ObservedObject var model: CaptureModel
    var compact: Bool = false
    var forceReanalysis: Bool = false
    var showOverview: (() -> Void)? = nil
    let showDetails: () -> Void
    @State private var pendingPlan: LocalAIPlan?
    @State private var pendingSourceID: String?
    @State private var pendingLibrary: LocalLibraryDescriptor?
    @State private var pendingGeneration: UUID?
    @State private var pendingSourceGeneration: UUID?
    @State private var preparing = false
    @State private var confirmAnalysis = false
    @State private var preparationNotice: PreparationNotice?
    @State private var showConfiguration = false
    private enum PreparationNotice {
        case configuration, unavailable
        var title: String { self == .configuration ? "请先配置 AI" : "无法检查 AI 配置" }
        var message: String {
            self == .configuration ? "请先在“设置 → 模型与 AI”配置模型名称、API 地址和 API Key，再开始分析。" :
                "暂时无法读取本地 AI 配置，请检查客户端连接后重试。"
        }
    }
    private var currentJobs: [LocalAIJob] { model.aiJobs.filter { $0.source_id == model.selectedSource?.id } }
    private var active: Bool { currentJobs.contains(where: \.isActive) }
    private var analyzed: Bool {
        model.aiArtifacts.contains { $0.source_id == model.selectedSource?.id && !$0.stale && $0.phase != "segment" }
    }
    private var opensOverview: Bool {
        guard !forceReanalysis else { return false }
        if model.aiSourceReadPhase != .ready { return showOverview != nil }
        return active || analyzed
    }
    private var buttonTitle: String {
        if opensOverview { return active ? "显示或隐藏分析进度" : "显示或隐藏 AI 知识概览" }
        if !forceReanalysis && model.aiSourceReadPhase == .loading { return "正在读取分析状态" }
        if !forceReanalysis && model.aiSourceReadPhase == .failed { return "重新读取分析状态" }
        return active ? "显示或隐藏分析进度" : analyzed ? (forceReanalysis ? "重新分析" : "显示或隐藏 AI 知识概览") : "AI 分析"
    }
    var body: some View {
        Button { prepare() } label: {
            if compact {
                Image(systemName: model.aiSourceReadPhase == .failed ? "exclamationmark.circle" : active ? "clock" : "sparkles")
                    .foregroundStyle(analyzed && !active ? InboxStyle.accent : InboxStyle.muted)
            } else {
                Text(buttonTitle)
            }
        }
            .help(buttonTitle)
            .accessibilityLabel(buttonTitle)
            .accessibilityIdentifier(forceReanalysis ? "ai-overview-reanalyze" : "ai-overview-toggle")
            .disabled(preparing || (!opensOverview && (!model.connected || model.aiBusy || model.tagsBusy || model.backupBusy || model.trashBusy || model.busy || model.recoveringService || model.summaryIndexBusy || model.ragBusy || model.agentConnectionsBusy)))
            .alert("开始 AI 分析？", isPresented: $confirmAnalysis) {
                Button("取消", role: .cancel) { clear() }
                Button("同意并开始") {
                    guard let plan = pendingPlan, let sourceID = pendingSourceID, let library = pendingLibrary,
                          model.aiGeneration == pendingGeneration, model.sourceLoadGeneration == pendingSourceGeneration, model.selectedSource?.id == sourceID,
                          (try? model.localService.currentLibrary()) == library else { clear(); return }
                    clear()
                    Task { await model.createLocalAI(kind: "analyze", planHash: plan.plan_hash, expectedSourceID: sourceID) }
                }
            } message: {
                if let plan = pendingPlan {
                    Text("所选资料正文、已归档图片、个人补充与上下文、已有领域、项目和标签名称将发送到配置的 AI 网关，可能消耗模型额度。结果和检索索引保存在本机。\n\(plan.scopeText)\n预计 \(plan.calls) 次模型调用。确认后自动生成总结、保存 AI 标签并更新检索；原文和人工标签保留。")
                }
            }
            .alert(preparationNotice?.title ?? "提示", isPresented: Binding(
                get: { preparationNotice != nil }, set: { if !$0 { preparationNotice = nil } })) {
                if preparationNotice == .configuration {
                    Button("前往配置") { showConfiguration = true }
                }
                Button(preparationNotice == .configuration ? "取消" : "好", role: .cancel) { preparationNotice = nil }
            } message: { Text(preparationNotice?.message ?? "") }
            .sheet(isPresented: $showConfiguration) {
                SettingsView(model: model, openAI: true).buttonStyle(InboxButtonStyle(.secondary))
            }
    }
    private func clear() {
        pendingPlan = nil; pendingSourceID = nil; pendingLibrary = nil; pendingGeneration = nil; pendingSourceGeneration = nil
    }
    private func prepare() {
        guard !preparing else { return }
        if opensOverview && (model.aiSourceReadPhase == .ready || active || analyzed) { showOverview?(); return }
        guard let sourceID = model.selectedSource?.id,
              let library = try? model.localService.currentLibrary() else { return }
        preparing = true
        let generation = model.aiGeneration, sourceGeneration = model.sourceLoadGeneration
        Task {
            defer { preparing = false }
            let refreshed = await model.refreshLocalAI(sourceID: sourceID)
            guard model.selectedSource?.id == sourceID, model.aiGeneration == generation, model.sourceLoadGeneration == sourceGeneration,
                  (try? model.localService.currentLibrary()) == library else { return }
            guard refreshed else { showOverview?(); return }
            let state = AIOverviewState(sourceID: sourceID, jobs: model.aiJobs, artifacts: model.aiArtifacts)
            switch state.action(forceReanalysis: forceReanalysis) {
            case .progress: showOverview?(); return
            case .overview: showOverview?(); return
            case .confirm: break
            }
            let endpoint = model.address, credential = model.token
            do {
                let configuration = try await API(address: endpoint, token: credential).call("v1/ai/config")
                guard model.selectedSource?.id == sourceID, model.aiGeneration == generation,
                      model.sourceLoadGeneration == sourceGeneration, model.address == endpoint, model.token == credential,
                      (try? model.localService.currentLibrary()) == library else { return }
                guard let configured = configuration["configured"] as? Bool else { throw failure("AI 配置响应无效。") }
                if !configured { clear(); preparationNotice = .configuration; return }
            } catch {
                guard model.selectedSource?.id == sourceID, model.aiGeneration == generation,
                      model.sourceLoadGeneration == sourceGeneration, model.address == endpoint, model.token == credential,
                      (try? model.localService.currentLibrary()) == library else { return }
                preparationNotice = .unavailable
                return
            }
            guard let plan = await model.previewLocalAI(kind: "analyze"), model.selectedSource?.id == sourceID,
                  model.aiGeneration == generation, model.sourceLoadGeneration == sourceGeneration, (try? model.localService.currentLibrary()) == library else { return }
            // Each new analysis needs consent, including single-call plans and reanalysis.
            pendingPlan = plan; pendingSourceID = sourceID; pendingLibrary = library
            pendingGeneration = generation; pendingSourceGeneration = sourceGeneration; confirmAnalysis = true
        }
    }
}

struct LocalAIResultCard: View {
    @ObservedObject var model: CaptureModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Binding var isExpanded: Bool
    var analysisButtonMidX: CGFloat? = nil
    var showDetails: () -> Void = {}
    @State private var contentHeight: CGFloat = 1
    private var state: AIOverviewState {
        AIOverviewState(sourceID: model.selectedSource?.id, jobs: model.aiJobs, artifacts: model.aiArtifacts)
    }
    private var detailsExpanded: Bool { isExpanded && state.canExpand }
    var body: some View {
        Group {
            if state.artifact != nil || state.job != nil || model.aiSourceReadPhase != .ready {
                card
                    .animation(reduceMotion ? nil : .smooth(duration: 0.22), value: detailsExpanded)
                    .animation(reduceMotion ? nil : .smooth(duration: 0.22), value: contentHeight)
                    .zIndex(10)
            }
        }
        .onChange(of: model.selectedSource?.id) { _, _ in isExpanded = false; contentHeight = 1 }
        .onChange(of: state.isActive) { _, active in if active { isExpanded = false } }
        .onExitCommand { isExpanded = false }
    }

    private var card: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if detailsExpanded, let artifact = state.artifact {
                VStack(alignment: .leading, spacing: 0) {
                    Rectangle().fill(InboxStyle.separator).frame(height: 1)
                    ScrollView {
                        expandedContent(artifact).padding(16)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .fixedSize(horizontal: false, vertical: true)
                            .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { contentHeight = $0 }
                    }.frame(height: min(240, max(1, contentHeight)))
                    HStack {
                        Text(state.job?.statusText ?? completionText(artifact))
                            .font(.caption).foregroundStyle(.secondary)
                        Spacer(minLength: 12)
                        LocalAIAnalyzeButton(model: model, forceReanalysis: true, showDetails: showDetails)
                            .buttonStyle(InboxButtonStyle(.secondary))
                    }.padding(.horizontal, 16).padding(.vertical, 12)
                }
                .transition(.opacity)
            }
        }
        .padding(.top, analysisButtonMidX == nil ? 0 : AIOverviewBubble.pointerHeight)
        .frame(maxWidth: .infinity, alignment: .leading)
        .clipped()
        .background {
            GeometryReader { geometry in
                let pointer = analysisButtonMidX.map { $0 - geometry.frame(in: .named("knowledgeReader")).minX }
                AIOverviewBubble(pointerX: pointer).fill(InboxStyle.surface)
                    .overlay { AIOverviewBubble(pointerX: pointer).stroke(InboxStyle.separator, lineWidth: 1) }
                    .shadow(color: .black.opacity(detailsExpanded ? 0.28 : 0), radius: 16, y: 8)
            }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Image(systemName: "sparkles").foregroundStyle(InboxStyle.accent)
            Text("AI 知识概览")
                .font(.system(size: 12, weight: .semibold)).fixedSize()
            Text(headerText)
                .font(.system(size: 12)).foregroundStyle(InboxStyle.muted)
                .lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
            if !state.isActive, let artifact = state.artifact,
               let warning = coverageWarning(artifact) {
                Image(systemName: "exclamationmark.circle.fill")
                    .foregroundStyle(.orange).help(warning).accessibilityLabel(warning)
            }
            if state.isActive || model.aiSourceReadPhase == .loading {
                AIOverviewLoading()
            } else if state.canExpand {
                Button { isExpanded.toggle() } label: {
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .medium)).foregroundStyle(InboxStyle.muted)
                        .rotationEffect(.degrees(detailsExpanded ? 180 : 0))
                        .frame(width: 28, height: 28)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(detailsExpanded ? "收起详情" : "展开详情")
                .accessibilityLabel(detailsExpanded ? "收起 AI 知识概览详情" : "展开 AI 知识概览详情")
                .accessibilityIdentifier("ai-overview-details-toggle")
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 12)
        .contentShape(Rectangle())
    }

    private var headerText: String {
        if model.aiSourceReadPhase == .loading { return "正在读取已保存的分析状态…" }
        if model.aiSourceReadPhase == .failed { return "分析状态读取失败，稍后自动重试" }
        if state.isActive { return state.progressText }
        if let artifact = state.artifact {
            return overviewText(artifact).split(whereSeparator: \.isWhitespace).joined(separator: " ")
        }
        return state.progressText
    }

    private func coverageWarning(_ artifact: LocalAIArtifact) -> String? {
        var warnings: [String] = []
        if artifact.coverage == "partial" || artifact.analysis_plan?.coverage == "partial" || artifact.analysis_plan?.source_coverage == "partial_text" {
            warnings.append("仅分析部分正文，不能作为全文总结")
        }
        if artifact.analysis_plan?.synthesis_evidence_coverage == "partial" {
            warnings.append("最终归纳依据为原文节选，可能遗漏细节；检索仍可读取完整原文")
        }
        return warnings.isEmpty ? nil : warnings.joined(separator: "；")
    }

    private func expandedContent(_ artifact: LocalAIArtifact) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(overviewText(artifact))
                .font(.callout).textSelection(.enabled)
            if let count = artifact.analysis_plan?.image_count, count > 0 {
                Text("已结合 \(count) 张原图分析").font(.caption).foregroundStyle(.secondary)
            } else if model.selectedSource?.images.isEmpty == false {
                Text("历史结果未包含原图；重新分析可联合处理正文和图片").font(.caption).foregroundStyle(.secondary)
            }
            ForEach(Array(artifact.result.insights.prefix(3).enumerated()), id: \.offset) { _, item in
                Text("• \(artifact.result.contract_version == 3 ? item.text : String(item.text.prefix(40)))").font(.callout).textSelection(.enabled)
            }
            if artifact.result.contract_version != 3 {
                Text("历史分析结果简览").font(.caption2).foregroundStyle(.secondary)
            }
            if artifact.coverage == "partial" || artifact.analysis_plan?.coverage == "partial" || artifact.analysis_plan?.source_coverage == "partial_text" {
                Label("仅分析部分正文，不能作为全文总结", systemImage: "exclamationmark.circle")
                    .font(.caption).foregroundStyle(.orange)
            }
            if artifact.analysis_plan?.synthesis_evidence_coverage == "partial" {
                Label("最终归纳依据为原文节选，可能遗漏细节；检索仍可读取完整原文", systemImage: "exclamationmark.circle")
                    .font(.caption).foregroundStyle(.orange)
            }
            let tags = model.sourceTags.filter { !$0.stale && $0.origins.contains("ai") }
            if !tags.isEmpty {
                AIOverviewTagLayout {
                    ForEach(tags) { tag in
                        Label(tag.name, systemImage: "number")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(InboxStyle.accent)
                            .lineLimit(1).truncationMode(.tail)
                            .padding(.horizontal, 10).padding(.vertical, 6)
                            .background(InboxStyle.accent.opacity(0.10), in: RoundedRectangle(cornerRadius: 7))
                            .overlay { RoundedRectangle(cornerRadius: 7).stroke(InboxStyle.accent.opacity(0.20)) }
                            .accessibilityLabel("标签：" + tag.name)
                    }
                }
            }
        }
    }

    private func overviewText(_ artifact: LocalAIArtifact) -> String {
        let text = artifact.result.overview?.text ?? artifact.result.summary
        return artifact.result.contract_version == 3 ? text : String(text.prefix(120))
    }
    private func completionText(_ artifact: LocalAIArtifact) -> String {
        switch artifact.pipeline_status {
        case "completed": return "分析完成，已更新知识检索"
        case "index_failed": return "分析已保存，检索更新失败"
        case "indexing": return "正在更新知识检索"
        default: return artifact.result.contract_version == 3 ? "分析已保存，等待确认检索状态" : "历史分析 · 标签需在详情中确认"
        }
    }
}

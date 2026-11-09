import SwiftUI

/// Three stable reading surfaces share scoped caches; switching tabs never clears their content.
@MainActor struct LocalDomainOverviewView: View {
    @ObservedObject var model: CaptureModel
    let domainID: String
    @StateObject private var store: LocalDomainOverviewStore
    @StateObject private var fullStore = LocalDomainFullAnalysisStore()
    @Environment(\.dismiss) private var dismiss
    @State private var reviewing = false
    @State private var showingFullAnalysis = false
    @State private var managingTopics = false
    @State private var showingMaintenance = false
    @State private var showingProcessing = false
    @State private var processingPath: [String] = []
    @State private var evidence: LocalDomainEvidenceSelection?
    @State private var reader: LocalDomainReaderSelection?
    @State private var topic: LocalDomainDirectoryTopic?
    @State private var showingQuestion = false
    @State private var confirmUnknown = false
    @State private var confirmPolicy = false
    @State private var automaticUpdates = false
    @State private var maxCalls = 20
    @State private var maxInputTokens = 200_000
    @State private var maxSources = 200
    @State private var maxSteps = 100
    @State private var loadedPolicy = false
    @State private var loadedScope: String?

    init(model: CaptureModel, domainID: String, store: LocalDomainOverviewStore? = nil) {
        self.model = model; self.domainID = domainID
        _store = StateObject(wrappedValue: store ?? LocalDomainOverviewStore())
    }

    private var busy: Bool { !model.connected || store.isMutating || store.requiresReconnect }
    private var domainName: String { model.domains.first { $0.id == domainID }?.name ?? "知识域" }
    private var activeBuild: Bool { ["pending", "running", "waiting"].contains(store.build?.status ?? "") }
    private var scopeIdentity: String { "\(model.address)|\(model.token)|\(domainID)" }

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            sectionBar
            Divider()
            // Keep all three surfaces mounted. Opacity avoids destroying scroll and tree state.
            ZStack(alignment: .topLeading) {
                overviewSection
                    .opacity(store.selectedSection == .overview ? 1 : 0)
                    .disabled(store.selectedSection != .overview || store.requiresReconnect)
                    .allowsHitTesting(store.selectedSection == .overview && !store.requiresReconnect)
                    .accessibilityHidden(store.selectedSection != .overview || store.requiresReconnect)
                LocalDomainTreeView(store: store, domainName: domainName,
                    manage: { managingTopics = true },
                    openSource: { reader = LocalDomainReaderSelection(id: $0.id, title: $0.title) },
                    openTopic: { topic = $0 })
                    .id(scopeIdentity)
                    .opacity(store.selectedSection == .topics ? 1 : 0)
                    .disabled(store.selectedSection != .topics || store.requiresReconnect)
                    .allowsHitTesting(store.selectedSection == .topics && !store.requiresReconnect)
                    .accessibilityHidden(store.selectedSection != .topics || store.requiresReconnect)
                LocalDomainInformationView(store: store)
                    .id(scopeIdentity)
                    .opacity(store.selectedSection == .information ? 1 : 0)
                    .disabled(store.selectedSection != .information || store.requiresReconnect)
                    .allowsHitTesting(store.selectedSection == .information && !store.requiresReconnect)
                    .accessibilityHidden(store.selectedSection != .information || store.requiresReconnect)
                if store.requiresReconnect {
                    VStack(spacing: 18) {
                        EmptyMessage(symbol: "externaldrive.badge.exclamationmark", title: "需要重新连接本地服务",
                            detail: store.error ?? "连接恢复后即可重新读取当前知识域。")
                        Button("重新连接") {
                            Task { await store.configure(address: model.address, token: model.token, domainID: domainID) }
                        }.disabled(!model.connected)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .background(InboxStyle.canvas)
                }
            }
            .opacity(loadedScope == scopeIdentity ? 1 : 0)
            .allowsHitTesting(loadedScope == scopeIdentity)
            .accessibilityHidden(loadedScope != scopeIdentity)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            Divider()
            HStack(spacing: 9) {
                if store.isLoading || store.directoryLoading || store.informationLoading {
                    ProgressView().controlSize(.small)
                }
                Text(statusMessage).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                Spacer(minLength: 0)
            }.padding(.horizontal, 24).frame(height: 38)
        }
        .frame(minWidth: 720, idealWidth: 920, minHeight: 590, idealHeight: 740)
        .background(InboxStyle.canvas)
        .transaction { $0.animation = nil }
        .task(id: "\(model.address)|\(model.token)|\(domainID)|\(model.connected)") {
            if loadedScope != scopeIdentity {
                loadedPolicy = false; reader = nil; evidence = nil; topic = nil
                showingFullAnalysis = false; fullStore.reset()
                managingTopics = false; reviewing = false; showingMaintenance = false; showingProcessing = false
                automaticUpdates = false; maxCalls = 20; maxInputTokens = 200_000; maxSources = 200; maxSteps = 100
            }
            loadedScope = scopeIdentity
            guard model.connected else { store.reset(); fullStore.reset(); return }
            async let overviewRead: Void = store.configure(address: model.address, token: model.token, domainID: domainID)
            async let fullRead: Void = fullStore.configure(address: model.address, token: model.token, domainID: domainID)
            _ = await (overviewRead, fullRead)
            loadPolicyDraft()
        }
        .task { await pollActiveBuild() }
        .sheet(isPresented: $managingTopics, onDismiss: {
            store.invalidateLibrary()
            Task { await store.refreshSelected() }
        }) { LocalDomainTopicsView(model: model, domainID: domainID).buttonStyle(InboxButtonStyle(.secondary)) }
        .sheet(isPresented: $showingFullAnalysis) {
            LocalDomainFullAnalysisView(model: model, domainID: domainID, domainName: domainName, store: fullStore)
                .buttonStyle(InboxButtonStyle(.secondary))
        }
        .onChange(of: fullStore.job?.resultsKey) {
            guard let job = fullStore.job, !job.isActive else { return }
            store.invalidateLibrary()
            Task { await store.refreshSelected() }
        }
        .onChange(of: fullStore.requiresReconnect) {
            if fullStore.requiresReconnect { store.expireAccess() }
        }
        .sheet(isPresented: $reviewing) { reviewSheet.buttonStyle(InboxButtonStyle(.secondary)) }
        .sheet(isPresented: $showingMaintenance) { maintenanceSheet.buttonStyle(InboxButtonStyle(.secondary)) }
        .sheet(item: $evidence) { LocalDomainEvidenceView(model: model, domainID: domainID, selection: $0).buttonStyle(InboxButtonStyle(.secondary)) }
        .sheet(item: $reader) { LocalDomainSourceReaderView(model: model, domainID: domainID, selection: $0).buttonStyle(InboxButtonStyle(.secondary)) }
        .sheet(item: $topic) { LocalDomainTopicOverviewSheet(model: model, domainID: domainID, topic: $0).buttonStyle(InboxButtonStyle(.secondary)) }
        .sheet(isPresented: $showingQuestion) { LocalRAGView(model: model, domainID: domainID, scopeTitle: domainName).buttonStyle(InboxButtonStyle(.secondary)) }
        .alert("新建任务可能重复消耗额度", isPresented: $confirmUnknown) {
            Button("取消", role: .cancel) { }
            Button("已核对，继续新建") { reviewing = false; Task { await store.createBuild(confirmedUnknown: true) } }
        } message: { Text("先检查网关和任务结果。已有任务结果未知时，不会自动重试模型调用。确认后将创建新任务。") }
        .alert("授权知识域分析", isPresented: $confirmPolicy) {
            Button("取消", role: .cancel) { }
            Button("同意并保存") {
                Task { await store.saveAuthorization(confirmed: true, automaticUpdates: automaticUpdates,
                    maxCalls: maxCalls, maxInputTokens: maxInputTokens, maxSources: maxSources, maxSteps: maxSteps) }
            }
        } message: { Text("保存会开启新的授权周期，累计用量重新计数。资料摘要与原文节选会发送到配置的 AI 网关。累计预算为 \(maxCalls) 次调用、\(maxInputTokens) 输入 token。\(automaticUpdates ? "首次手动构建成功后，后续内容变化可在预算内自动更新。" : "自动更新关闭，只有手动确认才构建。")") }
    }

    private var header: some View {
        HStack(alignment: .center, spacing: 16) {
            Image(systemName: "rectangle.3.group").font(.title2).foregroundStyle(InboxStyle.accent)
            VStack(alignment: .leading, spacing: 4) {
                Text(domainName).font(.title2.weight(.semibold)).lineLimit(1).truncationMode(.tail)
                Text("知识域概览").font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("更新知识库") {
                if activeBuild { showingMaintenance = true }
                else { showingFullAnalysis = true }
            }.buttonStyle(InboxButtonStyle(.primary)).disabled(busy || store.pendingRequestID != nil)
                .help("检查资料范围，更新单篇分析、知识库概览和检索索引")
            Menu {
                Button("刷新当前页面") { Task { await store.refreshSelected() } }
                Button("更新记录与设置…") { loadPolicyDraft(); showingMaintenance = true }
                Divider()
                Button("重建本地概览索引") { Task { await store.rebuildIndex() } }
                    .disabled(busy || store.navigation?.overview.available != true)
            } label: { Image(systemName: "ellipsis").frame(width: 22) }
                .menuStyle(.borderlessButton).fixedSize().help("更多").disabled(store.requiresReconnect)
            Button("完成") { dismiss() }.keyboardShortcut(.cancelAction)
        }.padding(.horizontal, 26).padding(.vertical, 20)
    }

    private var sectionBar: some View {
        HStack(spacing: 28) {
            ForEach(LocalDomainSection.allCases) { section in
                Button { Task { await store.selectSection(section) } } label: {
                    VStack(spacing: 12) {
                        Text(section.title).font(.callout.weight(store.selectedSection == section ? .semibold : .regular))
                            .foregroundStyle(store.selectedSection == section ? Color.primary : Color.secondary)
                        Rectangle().fill(store.selectedSection == section ? InboxStyle.accent : .clear).frame(height: 2)
                    }.padding(.top, 16)
                }.buttonStyle(.plain).disabled(store.requiresReconnect)
                    .accessibilityAddTraits(store.selectedSection == section ? .isSelected : [])
            }
            Spacer()
        }.padding(.horizontal, 28)
    }

    private var statusMessage: String {
        if !model.connected { return "本地服务尚未连接；恢复连接后可读取当前知识域。" }
        if let error = store.error { return error }
        if let message = store.message { return message }
        if activeBuild { return "概览正在更新，你可以继续浏览；完成后会自动显示结果。" }
        return "查看概览、展开分类和读取原文不调用模型。"
    }

    private var overviewSection: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 24) {
                if let navigation = store.navigation, navigation.domain_id == domainID {
                    HStack {
                        Text("整体理解").font(.headline)
                        Spacer()
                        if let date = localDomainDate(navigation.overview.created_at) {
                            Text("更新于 \(date)").font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    if navigation.overview.available, let result = navigation.overview.result {
                        LocalDomainResultContent(result: result, showEvidence: { point in
                            guard let id = navigation.overview.id else { return }
                            evidence = LocalDomainEvidenceSelection(artifactID: id, point: point)
                        })
                        if let coverage = navigation.overview.coverage {
                            Divider()
                            coverageContent(coverage)
                            if let excluded = coverage.excluded, !excluded.isEmpty {
                                DisclosureGroup("查看未纳入的资料（\(excluded.count)）") {
                                    VStack(alignment: .leading, spacing: 10) {
                                        ForEach(excluded.keys.sorted(), id: \.self) { id in
                                            Button { reader = LocalDomainReaderSelection(id: id, title: "资料") } label: {
                                                Text(exclusionText(excluded[id] ?? ""))
                                            }.buttonStyle(.link).font(.caption)
                                        }
                                    }.padding(.top, 10)
                                }
                            }
                        }
                        Text("概览根据有效单篇分析汇总，用于了解知识范围；具体判断请核对原文。")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        VStack(alignment: .leading, spacing: 14) {
                            Text(unavailableText(navigation.overview.reason)).font(.body)
                            Text("生成后可阅读整个知识域的概览与核心要点；主题分类和原文始终可以直接浏览。")
                                .font(.callout).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, minHeight: 180, alignment: .topLeading)
                    }
                    Divider()
                    HStack(alignment: .center, spacing: 20) {
                        VStack(alignment: .leading, spacing: 5) {
                            Text("从这里继续探索").font(.headline)
                            Text("围绕当前知识域提问，回答将引用资料原文。")
                                .font(.callout).foregroundStyle(.secondary)
                        }
                        Spacer()
                        Button("向这个知识域提问…") { showingQuestion = true }.disabled(!model.connected)
                    }
                } else if !model.connected {
                    EmptyMessage(symbol: "externaldrive.badge.exclamationmark", title: "等待本地服务", detail: "连接恢复后即可读取这个知识域的已有结果。")
                } else if store.isLoading {
                    VStack(alignment: .leading, spacing: 22) {
                        RoundedRectangle(cornerRadius: 5).fill(InboxStyle.elevated).frame(width: 150, height: 24)
                        ForEach(0..<6) { _ in RoundedRectangle(cornerRadius: 5).fill(InboxStyle.elevated).frame(height: 22) }
                    }.frame(minHeight: 300, alignment: .top).accessibilityLabel("正在读取知识域概览")
                } else {
                    EmptyMessage(symbol: "rectangle.3.group", title: "暂未读取概览", detail: store.error ?? "请从更多菜单刷新当前页面。")
                }
            }.padding(28).frame(maxWidth: 1000, alignment: .leading).frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var maintenanceSheet: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack { Text("更新记录与设置").font(.title2.weight(.semibold)); Spacer(); Button("完成") { showingMaintenance = false } }.padding(22)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 24) {
                    buildContent
                    if store.pendingRequestID != nil {
                        Text("上次操作的响应尚未确认。恢复会沿用原请求 ID 确认结果；另建任务可能重复消耗额度。")
                            .font(.callout).foregroundStyle(.orange)
                        Button("恢复原请求确认") { Task { await store.retryPendingMutation() } }.disabled(busy)
                    }
                    policyContent
                    Divider()
                    Text("本地检索维护").font(.headline)
                    Text("重建概览索引不调用模型，也不会重新分析原文。")
                        .font(.caption).foregroundStyle(.secondary)
                    Button("重建本地概览索引") { Task { await store.rebuildIndex() } }
                        .disabled(busy || store.navigation?.overview.available != true)
                    if let error = store.error { Text(error).font(.caption).foregroundStyle(.orange) }
                }.padding(24)
            }
        }.frame(width: 650, height: 580).background(InboxStyle.canvas)
            .sheet(isPresented: $showingProcessing) { processingSheet.buttonStyle(InboxButtonStyle(.secondary)) }
    }

    private func coverageContent(_ coverage: LocalDomainCoverage, planned: Bool = false) -> some View {
        let processed = coverage.processed_source_count
        let partial = processed.map { $0 < coverage.source_count } ?? true
        let count = processed.map { "\(planned ? "计划纳入" : "已纳入") \($0) / \(coverage.source_count) 篇" }
            ?? "范围共 \(coverage.source_count) 篇，纳入数量未记录"
        return Text("\(count) · 依据为文档分析汇总，具体事实需查原文\(partial ? "，仍有资料未覆盖" : "")")
            .font(.caption).foregroundStyle(partial ? Color.orange : Color.secondary)
    }

    private func exclusionText(_ reason: String) -> String {
        switch reason {
        case "no_valid_summary": "需要完成或更新这篇资料的 AI 分析"
        case "summary_too_long": "旧版总结过长，需要更新 AI 分析"
        case "evidence_not_bounded": "原文引用无法验证，请检查资料"
        case "single_document_exceeds_batch", "batch_budget": "原文引用超过本次更新范围"
        default: "未纳入当前概览，请检查资料分析状态"
        }
    }

    @ViewBuilder private var buildContent: some View {
        if let build = store.build {
            VStack(alignment: .leading, spacing: 12) {
                Text("最近更新：\(buildStatus(build.status))").font(.headline)
                coverageContent(build.coverage, planned: build.status != "completed")
                if let error = build.last_error { Text(error).font(.caption).foregroundStyle(.orange) }
                if activeBuild { ProgressView("正在更新已有资料的概览…").font(.caption) }
                DisclosureGroup("技术明细") {
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(Array(build.steps.enumerated()), id: \.element.key) { index, step in
                            HStack {
                                Text("步骤 \(index + 1)：\(buildStatus(step.status))").font(.caption).foregroundStyle(.secondary)
                                Spacer()
                                if let id = step.artifact_id {
                                    Button("查看中间结果") { Task { await store.readProcessing(id); processingPath = [id]; showingProcessing = store.processing != nil } }
                                        .font(.caption).disabled(busy)
                                }
                            }
                        }
                    }.padding(.top, 12)
                }
            }
        } else { Text("暂无更新记录。浏览现有资料不需要先生成概览。").foregroundStyle(.secondary) }
    }

    private func pollActiveBuild() async {
        while !Task.isCancelled {
            do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { return }
            if model.connected, !showingFullAnalysis, fullStore.job?.isActive == true, !fullStore.isMutating, !fullStore.requiresReconnect {
                await fullStore.refreshJob()
            }
            guard model.connected, activeBuild, !store.isLoading, !store.isMutating else { continue }
            await store.refreshProgress()
        }
    }

    private var policyContent: some View {
        DisclosureGroup("自动更新与预算") {
            VStack(alignment: .leading, spacing: 12) {
                if let schedule = store.schedule {
                    Text("\(scheduleText(schedule.automatic_status)) · 剩余 \(schedule.remaining_calls) 次调用 / \(schedule.remaining_input_tokens) 输入 token")
                        .font(.caption).foregroundStyle(.secondary)
                    if schedule.policy.enabled {
                        Button(schedule.policy.paused ? "恢复自动调度" : "暂停自动调度") { Task { await store.setPaused(!schedule.policy.paused) } }.disabled(busy)
                    }
                }
                Toggle("内容变化后自动更新概览", isOn: $automaticUpdates)
                Text("默认关闭。首次构建仍需手动确认；后续变化合并处理，范围扩大或预算不足时等待确认。暂停不终止已发出的模型请求。")
                    .font(.caption).foregroundStyle(.secondary)
                Stepper("累计调用上限：\(maxCalls)", value: $maxCalls, in: 1...1000)
                Stepper("累计输入 token 上限：\(maxInputTokens)", value: $maxInputTokens, in: 1000...10_000_000, step: 1000)
                Stepper("自动更新资料上限：\(maxSources)", value: $maxSources, in: 1...2000)
                Stepper("自动更新步骤上限：\(maxSteps)", value: $maxSteps, in: 1...256)
                Text("保存将开启新的授权周期，累计用量重新计数；输入 token 为保守授权上限，不代表实际计费。")
                    .font(.caption).foregroundStyle(.secondary)
                Button("审阅并保存授权…") { confirmPolicy = true }.disabled(busy || store.pendingRequestID != nil)
            }.padding(.top, 12)
        }
    }

    private var reviewSheet: some View {
        VStack(alignment: .leading, spacing: 16) {
            Text("更新知识域概览").font(.title2.weight(.semibold))
            if let preview = store.preview {
                Text("\(store.navigation?.node.name ?? domainName)").font(.headline)
                coverageContent(preview.coverage, planned: true)
                Text("所选范围内的资料摘要和原文节选将发送到配置的 AI 网关，可能消耗额度。产物保存在本机，原文保留。")
                    .font(.callout).foregroundStyle(.secondary)
                DisclosureGroup("AI 调用与预算明细") {
                    VStack(alignment: .leading, spacing: 8) {
                        Text("预计 \(preview.calls) 次模型调用 · 预留 \(preview.input_token_reservation) 输入 token · 共 \(preview.total_steps) 个步骤")
                        Text("输入预留为保守授权上限，不代表实际用量或费用。")
                            .foregroundStyle(.secondary)
                    }.font(.caption).padding(.top, 8)
                }
                if store.needsBuildAuthorization {
                    Text("当前可用预算不足或尚未授权。本次确认会按预览设置新的授权预算并开始，自动更新关闭。")
                        .font(.callout).foregroundStyle(.secondary)
                }
            }
            HStack {
                Spacer()
                Button("取消") { reviewing = false }
                Button("开始更新") {
                    if store.build?.status == "unknown" || store.schedule?.phase == "unknown" { confirmUnknown = true; return }
                    reviewing = false
                    Task {
                        if store.needsBuildAuthorization { await store.authorizeAndBuild(confirmed: true) }
                        else { await store.createBuild() }
                    }
                }.disabled(busy || store.preview == nil)
            }
        }.padding(24).frame(width: 560)
    }

    private var processingSheet: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack { Text("加工节点").font(.title2.weight(.semibold)); Spacer(); Button("完成") { showingProcessing = false } }
            if processingPath.count > 1 {
                Button("返回上一级加工节点") {
                    processingPath.removeLast()
                    if let id = processingPath.last { Task { await store.readProcessing(id) } }
                }.disabled(busy)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    if let processing = store.processing {
                        if processing.available, let result = processing.result {
                            LocalDomainResultContent(result: result)
                            if let coverage = processing.coverage { coverageContent(coverage) }
                            ForEach(Array((processing.children ?? []).enumerated()), id: \.element.artifact_id) { index, child in
                                Button("查看批次 \(index + 1)") {
                                    Task { await store.readProcessing(child.artifact_id); processingPath.append(child.artifact_id) }
                                }.disabled(busy)
                            }
                        } else { Text("该加工节点已失效或不可读取，请返回概览刷新。").foregroundStyle(.secondary) }
                    }
                    if let error = store.error { Text(error).foregroundStyle(.orange) }
                    Text("批次是内部加工层，并非独立主题，也不能代替原文证据。").font(.caption).foregroundStyle(.secondary)
                }
            }
        }.padding(24).frame(width: 600, height: 500)
    }

    private func loadPolicyDraft() {
        guard !loadedPolicy, let policy = store.schedule?.policy else { return }
        automaticUpdates = policy.automatic_updates
        if policy.enabled {
            maxCalls = max(1, policy.max_calls); maxInputTokens = max(1000, policy.max_input_tokens)
            maxSources = max(1, policy.automatic_max_sources); maxSteps = max(1, min(256, policy.automatic_max_steps))
        }
        loadedPolicy = true
    }

    private func unavailableText(_ reason: String?) -> String {
        switch reason {
        case "stale", "stale_inputs": return "内容已变化，现有概览已过期。可预览预算后更新。"
        case "not_built", nil: return "尚未生成概览。先查看加工范围和预算，再确认分析。"
        default: return "当前没有有效概览，可刷新或重新预览加工计划。"
        }
    }
    private func indexText(_ value: String) -> String {
        switch value {
        case "completed", "ready": return "可用于检索"
        case "pending": return "等待建立"
        case "running": return "正在建立"
        case "disabled": return "语义索引未启用，仍可按关键词检索"
        case "failed": return "建立失败，可手动重建"
        default: return "尚未建立"
        }
    }
    private func buildStatus(_ value: String) -> String {
        switch value {
        case "pending", "waiting": return "等待加工"
        case "running": return "正在加工"
        case "completed", "reused": return value == "reused" ? "复用已有结果" : "已完成"
        case "unknown": return "结果未知，请先核对，可能已消耗额度"
        case "failed": return "加工失败，等待手动处理"
        case "cancelled": return "已取消"
        case "stale", "stale_inputs": return "内容已变化"
        default: return value
        }
    }
    private func scheduleText(_ value: String) -> String {
        switch value {
        case "disabled", "automatic_updates_disabled": return "自动更新关闭"
        case "paused": return "自动更新暂停"
        case "ready": return "等待自动更新"
        case "clean", "no_changes": return "概览与资料一致"
        case "running": return "正在加工"
        case "debouncing", "waiting": return "正在合并内容变化"
        case "budget_exhausted": return "累计预算不足"
        case "first_build_requires_confirmation": return "首次构建需手动确认"
        case "configuration_requires_confirmation", "structure_requires_confirmation", "scope_requires_confirmation": return "范围或配置变化，需重新确认"
        case "previous_plan_requires_manual_retry": return "上次任务需要手动处理"
        case "gateway_unavailable": return "AI 网关未配置"
        default: return "等待处理"
        }
    }
}

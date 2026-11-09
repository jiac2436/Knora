import SwiftUI

@MainActor struct LocalDomainFullAnalysisView: View {
    @ObservedObject var model: CaptureModel
    let domainID: String
    let domainName: String
    @ObservedObject var store: LocalDomainFullAnalysisStore
    @Environment(\.dismiss) private var dismiss
    @State private var reviewing = false
    @State private var confirmStop = false
    @State private var confirmUnknown = false
    @State private var showingModeHelp = false
    @State private var loadedScope: String?
    private var scopeIdentity: String { "\(model.address)|\(model.token)|\(domainID)" }
    private var unavailable: Bool { !model.connected || store.requiresReconnect || store.isMutating }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 16) {
                VStack(alignment: .leading, spacing: 6) {
                    Text("更新知识库").font(.title2.weight(.semibold))
                    Text(domainName).font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Button("完成") { dismiss() }.keyboardShortcut(.cancelAction)
            }.padding(24)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    if store.requiresReconnect {
                        EmptyMessage(symbol: "externaldrive.badge.exclamationmark", title: "需要重新连接本地服务",
                            detail: store.readError ?? "连接恢复后可继续查看已经创建的任务。")
                        Button("重新连接") { Task { await connect() } }.disabled(!model.connected)
                    } else if !model.connected {
                        Text("本地服务尚未连接，连接恢复后即可查看原任务。不会自动创建新的分析任务。")
                            .foregroundStyle(.secondary)
                    } else if store.pendingRequestID != nil {
                        pendingContent
                    } else if reviewing || (store.job == nil && !store.isReading) {
                        previewContent
                    } else if let job = store.job {
                        jobContent(job)
                    } else {
                        VStack(alignment: .leading, spacing: 18) {
                            ProgressView("正在读取分析记录…")
                            Text("读取不会调用模型。").font(.caption).foregroundStyle(.secondary)
                        }.frame(maxWidth: .infinity, minHeight: 180, alignment: .topLeading)
                    }
                    if let error = store.readError, !store.requiresReconnect {
                        HStack(alignment: .top) {
                            Text(error).font(.callout).foregroundStyle(.orange)
                            Spacer()
                            Button("重试读取") { Task { await store.refreshLatest(force: true) } }.disabled(unavailable || store.isReading)
                        }
                    }
                    if let error = store.mutationError { Text(error).font(.callout).foregroundStyle(.orange) }
                }.padding(24).frame(maxWidth: .infinity, alignment: .leading)
            }
            .opacity(loadedScope == scopeIdentity ? 1 : 0)
            .allowsHitTesting(loadedScope == scopeIdentity)
            .accessibilityHidden(loadedScope != scopeIdentity)
            Divider()
            HStack(spacing: 8) {
                if store.isReading || store.isPreviewing || store.isMutating { ProgressView().controlSize(.small) }
                Text(store.job?.isActive == true ? "任务在后台继续执行，关闭此窗口不会停止分析。" : "查看进度、刷新记录和预览不会调用模型。")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer()
            }.padding(.horizontal, 24).frame(height: 42)
        }
        .frame(minWidth: 620, idealWidth: 700, minHeight: 520, idealHeight: 650)
        .background(InboxStyle.canvas)
        .transaction { $0.animation = nil }
        .task(id: "\(scopeIdentity)|\(model.connected)") { await connect() }
        .task { await poll() }
        .onChange(of: store.job?.id) { _, _ in
            if store.job?.isActive == true { reviewing = false }
        }
        .alert("停止后续分析？", isPresented: $confirmStop) {
            Button("继续执行", role: .cancel) { }
            Button("停止后续步骤", role: .destructive) { Task { await store.cancel() } }
        } message: { Text("已完成的结果会保留。已发送的模型调用可能继续完成并产生用量，停止不代表撤销这些调用。") }
        .alert("确认重新发起模型调用", isPresented: $confirmUnknown) {
            Button("取消", role: .cancel) { }
            Button("已核对，开始新任务") { Task { await store.create(confirmed: true, confirmedUnknown: true) } }
        } message: { Text("上次任务结果未知，可能已经产生用量。请先核对网关和现有结果；新任务将按本次预览执行，不会恢复原调用。") }
    }

    private var pendingContent: some View {
        VStack(alignment: .leading, spacing: 16) {
            Label("上次创建请求尚待确认", systemImage: "exclamationmark.circle").font(.headline)
            Text("请求可能已被后台接受。先刷新查看最新任务；手动恢复会沿用原请求 ID 确认，不会另建任务。")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                Button("刷新最新任务") { Task { await store.refreshLatest(force: true) } }.disabled(unavailable || store.isReading)
                Button("恢复原请求确认") { Task { await store.restorePendingRequest() } }.disabled(unavailable)
            }
            if let job = store.job { Text("最近任务：\(statusText(job.status))").font(.caption).foregroundStyle(.secondary) }
        }.frame(maxWidth: .infinity, minHeight: 180, alignment: .topLeading)
    }

    @ViewBuilder private var previewContent: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("更新分析、概览与检索").font(.headline)
            Text("默认复用有效分析，仅分析从未分析的文章；已过期的分析会跳过，不纳入概览。人工标签和原文保留。")
                .font(.callout).foregroundStyle(.secondary)
            HStack(spacing: 8) {
                Toggle("重新分析", isOn: Binding(get: { store.reanalyze }, set: { value in
                    Task { await store.setReanalyze(value) }
                })).toggleStyle(.checkbox).fixedSize()
                    .disabled(unavailable || store.pendingRequestID != nil)
                Button { showingModeHelp.toggle() } label: {
                    Image(systemName: "exclamationmark.circle").foregroundStyle(.secondary)
                }.buttonStyle(.plain).accessibilityLabel("重新分析说明").help("说明重新分析的范围")
                    .popover(isPresented: $showingModeHelp, arrowEdge: .bottom) {
                        Text("未勾选：只分析从未分析过的文章，复用有效结果，跳过已过期结果。\n勾选：重新分析全部文章，可能产生更多模型用量。")
                            .font(.callout).padding(16).frame(width: 310)
                    }
                Spacer()
            }
            if let preview = store.preview {
                HStack(spacing: 28) {
                    metric("资料总数", preview.total_sources)
                    metric("复用有效分析", preview.reused_sources)
                    metric("需要分析", preview.analyze_sources)
                    metric("分析已过期", preview.stale_sources ?? 0)
                }.frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 6)
                if (preview.skipped_sources ?? 0) > 0 {
                    Text("本次跳过 \(preview.skipped_sources ?? 0) 篇已过期文章，概览不会引用这些旧结果。")
                        .font(.callout).foregroundStyle(.orange)
                }
                Text("需要分析的资料正文、已归档图片、备注及相关分类信息，以及汇总所需的摘要与原文节选，将发送到已配置的 AI 网关。可能消耗模型额度，生成结果保存在本机。")
                    .font(.callout).foregroundStyle(.secondary)
                if preview.requires_unknown_confirmation == true {
                    Text("部分资料存在结果未知的历史调用，可能已产生用量。开始前需要先核对，并再次明确确认。")
                        .font(.callout).foregroundStyle(.orange)
                }
                if preview.configured == false && preview.calls > 0 {
                    Text("尚未配置 AI 网关，请先完成 AI 服务配置，再重新预览。")
                        .font(.callout).foregroundStyle(.orange)
                }
                if !preview.blocked_sources.isEmpty {
                    Label("先处理以下资料，再重新预览", systemImage: "exclamationmark.triangle").font(.headline).foregroundStyle(.orange)
                    ForEach(preview.blocked_sources) { source in
                        VStack(alignment: .leading, spacing: 5) {
                            Text(source.title).font(.callout.weight(.medium))
                            Text(source.reason).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                if let documents = preview.documents, !documents.isEmpty {
                    DisclosureGroup("查看本次资料范围（\(documents.count) 篇）") {
                        LazyVStack(alignment: .leading, spacing: 10) {
                            ForEach(documents) { source in
                                HStack(alignment: .top) {
                                    Text(source.title).font(.callout)
                                    Spacer()
                                    Text(source.status == "reused" ? "复用分析" : source.status == "skipped" ? "已过期 · 跳过" : source.status == "blocked" ? "待处理" : "发送分析")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                            }
                        }.padding(.top, 10)
                    }
                }
                if preview.total_sources == 0 { Text("这个知识域还没有资料。添加资料后再更新知识库。").foregroundStyle(.secondary) }
                HStack {
                    if store.job != nil { Button("返回最近任务") { reviewing = false } }
                    Spacer()
                    Button("开始更新") {
                        if store.job?.status == "unknown" || preview.requires_unknown_confirmation == true { confirmUnknown = true }
                        else { Task { await store.create(confirmed: true) } }
                    }.buttonStyle(InboxButtonStyle(.primary))
                        .disabled(unavailable || store.isPreviewing || !preview.blocked_sources.isEmpty || preview.total_sources == 0 ||
                            (preview.configured == false && preview.calls > 0) || store.readError != nil || store.previewError != nil)
                }
            } else if store.isPreviewing {
                ProgressView("正在检查资料范围…").frame(maxWidth: .infinity, minHeight: 150, alignment: .topLeading)
            } else {
                Text(store.previewError ?? "先检查知识库的资料范围，再确认开始。")
                    .font(.callout).foregroundStyle(store.previewError == nil ? Color.secondary : Color.orange)
                Button("检查资料范围") { Task { await store.loadPreview() } }.disabled(unavailable)
            }
            if let error = store.previewError, store.preview != nil { Text(error).font(.caption).foregroundStyle(.orange) }
        }
    }

    private func jobContent(_ job: LocalDomainFullJob) -> some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack {
                Text(statusText(job.status)).font(.headline)
                Spacer()
                if let date = localDomainDate(job.completed_at ?? job.created_at) { Text(date).font(.caption).foregroundStyle(.secondary) }
            }
            if job.isActive {
                ProgressView(value: progressValue(job), total: 3)
                Text(phaseText(job.phase)).font(.callout.weight(.medium))
                Text("单篇资料已处理 \(job.completed_sources) / \(job.total_sources) 篇").font(.caption).foregroundStyle(.secondary)
                if job.phase == "overview", let progress = job.overview_progress {
                    Text("概览汇总已完成 \(progress.completed_steps) / \(progress.total_steps) 步")
                        .font(.callout).monospacedDigit()
                    if let title = progress.current_step_title {
                        Text("正在汇总：\(title)").font(.caption).foregroundStyle(.secondary)
                    }
                }
                if let title = job.current_source_title { Text(title).font(.callout).foregroundStyle(.secondary) }
            }
            HStack(spacing: 28) {
                metric("资料总数", job.total_sources)
                metric("复用分析", job.reused_sources)
                metric("本次已分析", job.analyzed_sources)
                metric("更新前已过期", job.stale_sources ?? 0)
            }
            if job.status == "completed" {
                Text((job.skipped_sources ?? 0) > 0
                    ? "知识库已更新；跳过 \(job.skipped_sources ?? 0) 篇已过期文章，概览仅引用有效分析。"
                    : "知识库分析、概览与检索已更新。可返回概览或主题分类查看结果。")
                    .font(.callout).foregroundStyle(.secondary)
            } else if job.status == "unknown" {
                Text("模型调用结果未知，可能已经产生用量。已停止后续步骤；先核对现有结果和网关，再决定是否重新预览新任务。")
                    .font(.callout).foregroundStyle(.orange)
            } else if job.status == "cancelled" || job.status == "cancelling" {
                Text("保留已完成结果。已经发送的模型调用无法撤销，可能仍会产生用量。")
                    .font(.callout).foregroundStyle(.secondary)
            } else if ["stale", "failed", "partial"].contains(job.status) {
                Text("后续步骤已停止。已完成结果保留；处理问题后可重新预览，复用仍有效的结果。")
                    .font(.callout).foregroundStyle(.secondary)
            }
            if let error = job.last_error { Text(error).font(.callout).foregroundStyle(.orange) }
            HStack {
                Button("刷新进度") { Task { await store.refreshJob() } }.disabled(unavailable || store.isReading)
                Spacer()
                if job.isActive {
                    Button("停止后续步骤") { confirmStop = true }.disabled(unavailable || job.status == "cancelling")
                } else {
                    Button("重新检查资料") { reviewing = true; Task { await store.loadPreview() } }.disabled(unavailable)
                }
            }
            Divider()
            Text("资料处理明细").font(.headline)
            LazyVStack(alignment: .leading, spacing: 14) {
                ForEach(job.documents) { document in
                    HStack(alignment: .top, spacing: 12) {
                        Image(systemName: document.status == "completed" || document.status == "reused" ? "checkmark.circle" : document.status == "running" ? "arrow.triangle.2.circlepath" : "doc.text")
                            .foregroundStyle(.secondary).frame(width: 18)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(document.title).font(.callout)
                            if let error = document.last_error { Text(error).font(.caption).foregroundStyle(.orange) }
                        }
                        Spacer()
                        Text(statusText(document.status)).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
            if job.status == "completed", let usage = job.token_usage {
                Text("本次用量：\(usage.total_tokens.formatted()) token（输入 \(usage.input_tokens.formatted()) / 输出 \(usage.output_tokens.formatted())）")
                    .font(.callout).foregroundStyle(.secondary)
                Text("根据本次模型回执汇总；复用的历史结果不计入。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }
    }
    private func metric(_ title: String, _ value: Int) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("\(value)").font(.title2.weight(.semibold)).monospacedDigit()
            Text(title).font(.caption).foregroundStyle(.secondary)
        }
    }
    private func connect() async {
        loadedScope = scopeIdentity
        guard model.connected else { store.reset(); return }
        await store.configure(address: model.address, token: model.token, domainID: domainID)
        reviewing = false
        if store.pendingRequestID == nil && store.job?.isActive != true { await store.setReanalyze(false) }
        if store.job == nil && store.pendingRequestID == nil && !store.requiresReconnect && store.readError == nil { await store.loadPreview() }
    }
    private func poll() async {
        while !Task.isCancelled {
            do { try await Task.sleep(nanoseconds: 2_000_000_000) } catch { return }
            guard model.connected, store.job?.isActive == true, !store.isMutating, !store.requiresReconnect else { continue }
            await store.refreshJob()
        }
    }
    private func progressValue(_ job: LocalDomainFullJob) -> Double {
        switch job.phase {
        case "overview":
            guard let progress = job.overview_progress, progress.total_steps > 0 else { return 1 }
            return 1 + min(1, Double(progress.completed_steps) / Double(progress.total_steps))
        case "indexing": return 2
        case "completed": return 3
        default: return min(1, Double(job.completed_sources) / Double(max(1, job.total_sources)))
        }
    }
    private func phaseText(_ phase: String) -> String {
        switch phase {
        case "documents": return "第一步：补齐单篇分析与标签"
        case "overview": return "第二步：汇总知识域概览"
        case "indexing": return "第三步：更新检索索引"
        case "completed": return "全部步骤已完成"
        default: return "正在准备分析"
        }
    }
    private func statusText(_ status: String) -> String {
        switch status {
        case "pending", "waiting": return "等待处理"
        case "running": return "正在处理"
        case "completed": return "已完成"
        case "reused": return "复用有效结果"
        case "skipped": return "已过期 · 跳过"
        case "failed": return "处理失败"
        case "unknown": return "结果未知，已停止"
        case "stale": return "内容已变化，已停止"
        case "partial": return "部分完成"
        case "cancelled": return "已停止"
        case "cancelling": return "正在停止后续步骤"
        default: return status
        }
    }
}

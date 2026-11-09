import SwiftUI

struct LocalDomainEvidenceSelection: Identifiable {
    let artifactID: String
    let point: LocalDomainPoint
    var id: String { artifactID + "|" + point.citations.joined(separator: "|") }
}

struct LocalDomainReaderSelection: Identifiable { let id: String; let title: String }

/// Reading stays local to this presentation, without replacing the main reader's selection.
@MainActor struct LocalDomainSourceReaderView: View {
    @ObservedObject var model: CaptureModel
    let domainID: String
    let selection: LocalDomainReaderSelection
    @Environment(\.dismiss) private var dismiss
    @State private var detail: KnowledgeDetail?
    @State private var error: String?
    @State private var loading = true
    @State private var generation = UUID()
    @State private var readers = TextReaderStore()

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                VStack(alignment: .leading, spacing: 5) {
                    Text(detail?.title ?? selection.title).font(.title3.weight(.semibold)).lineLimit(2)
                    if let detail { Text("原文 · 版本 \(detail.revision.map(String.init) ?? "未记录")").font(.caption).foregroundStyle(.secondary) }
                }
                Spacer()
                Button("返回") { dismiss() }.keyboardShortcut(.cancelAction)
            }.padding(22)
            Divider()
            if let detail {
                TextReader(store: readers, sourceID: "domain-reader:\(detail.id)", revision: detail.revision,
                           text: detail.text, size: 16, request: nil)
                    .padding(.horizontal, 16)
            } else if loading {
                ProgressView("读取原文…").frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                EmptyMessage(symbol: "doc.text.magnifyingglass", title: "暂时无法读取原文", detail: error ?? "请返回后重试。")
            }
        }
        .frame(minWidth: 620, idealWidth: 840, minHeight: 520, idealHeight: 690)
        .background(InboxStyle.canvas)
        .task(id: "\(model.address)|\(model.token)|\(model.connected)|\(domainID)|\(selection.id)") { await load() }
        .onDisappear { generation = UUID() }
    }

    private func load() async {
        let read = UUID(); generation = read; detail = nil; error = nil; loading = true
        defer { if generation == read { loading = false } }
        guard model.connected else { error = "本地服务尚未连接。"; return }
        do {
            let value = try await API(address: model.address, token: model.token).call("v1/sources/\(selection.id)")
            guard generation == read, !Task.isCancelled else { return }
            guard value["domain_id"] as? String == domainID else { throw failure("这篇资料已不在当前知识域，请返回刷新分类。") }
            guard let source = KnowledgeDetail(value) else { throw failure("原文数据不完整，请刷新后重试。") }
            detail = source
        } catch { if generation == read, !Task.isCancelled { self.error = error.localizedDescription } }
    }
}

private struct LocalDomainResolvedCitation: Decodable, Identifiable {
    let id: String
    let source_id: String
    let title: String
    let revision_id: String
    let evidence_id: String
    let text: String
    let locator_kind: String
    let locator_start: Int
    let locator_end: Int
    let text_start: Int
    let text_end: Int

    var location: String {
        if locator_kind == "pdf_page" { return locator_start == locator_end ? "第 \(locator_start) 页" : "第 \(locator_start)–\(locator_end) 页" }
        return "原文位置 \(locator_start)–\(locator_end)"
    }
}

@MainActor struct LocalDomainEvidenceView: View {
    @ObservedObject var model: CaptureModel
    let domainID: String
    let selection: LocalDomainEvidenceSelection
    @Environment(\.dismiss) private var dismiss
    @State private var citations: [LocalDomainResolvedCitation] = []
    @State private var loading = true
    @State private var error: String?
    @State private var generation = UUID()
    @State private var reader: LocalDomainReaderSelection?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack { Text("原文依据").font(.title2.weight(.semibold)); Spacer(); Button("返回") { dismiss() } }.padding(22)
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 22) {
                    Text(selection.point.text).font(.headline).textSelection(.enabled)
                    Text("以下为概览关联资料的原文节选。概览可能根据单篇分析汇总，引用用于定位资料；具体结论请结合全文核对。")
                        .font(.callout).foregroundStyle(.secondary)
                    if loading { ProgressView("核验并读取原文依据…").frame(maxWidth: .infinity, minHeight: 120) }
                    if let error { Text(error).foregroundStyle(.orange).textSelection(.enabled) }
                    ForEach(citations) { citation in
                        VStack(alignment: .leading, spacing: 10) {
                            HStack(alignment: .top) {
                                VStack(alignment: .leading, spacing: 5) {
                                    Text(citation.title).font(.headline)
                                    Text(citation.location).font(.caption).foregroundStyle(.secondary)
                                }
                                Spacer()
                                Button("查看全文") { reader = LocalDomainReaderSelection(id: citation.source_id, title: citation.title) }
                            }
                            Text(citation.text).textSelection(.enabled).lineSpacing(5)
                            Divider()
                        }
                    }
                }.padding(24)
            }
        }
        .frame(minWidth: 600, idealWidth: 760, minHeight: 480, idealHeight: 660)
        .background(InboxStyle.canvas)
        .task(id: "\(model.address)|\(model.token)|\(model.connected)|\(domainID)|\(selection.id)") { await load() }
        .onDisappear { generation = UUID() }
        .sheet(item: $reader) { LocalDomainSourceReaderView(model: model, domainID: domainID, selection: $0).buttonStyle(InboxButtonStyle(.secondary)) }
    }

    private func load() async {
        let read = UUID(); generation = read; citations = []; loading = true; error = nil
        defer { if generation == read { loading = false } }
        guard model.connected else { error = "本地服务尚未连接。"; return }
        do {
            let value = try await API(address: model.address, token: model.token).call(
                "v1/domains/\(domainID)/analysis/citations/\(selection.artifactID)", method: "POST",
                json: ["scope": "app", "citation_ids": selection.point.citations])
            guard generation == read, !Task.isCancelled else { return }
            let data = try JSONSerialization.data(withJSONObject: value["items"] as? [[String: Any]] ?? [])
            citations = try JSONDecoder().decode([LocalDomainResolvedCitation].self, from: data)
            if citations.isEmpty { error = "这条结论暂无可读取的原文依据。请刷新概览后检查。" }
        } catch {
            guard generation == read, !Task.isCancelled else { return }
            if (error as NSError).userInfo["HTTPStatus"] as? Int == 409 {
                self.error = "资料或概览已经变化，这组原文依据已失效。请返回刷新概览后重新查看。"
            } else { self.error = error.localizedDescription }
        }
    }
}

struct LocalDomainResultContent: View {
    let result: LocalDomainResult
    var showEvidence: ((LocalDomainPoint) -> Void)?
    @State private var showingAll = false
    @State private var showingLimitations = false

    var body: some View {
        VStack(alignment: .leading, spacing: 24) {
            point(result.overview, prominent: true)
            if !result.insights.isEmpty {
                VStack(alignment: .leading, spacing: 15) {
                    Text("核心要点").font(.headline)
                    ForEach(Array(result.insights.prefix(showingAll ? result.insights.count : 3).enumerated()), id: \.offset) { _, insight in point(insight) }
                    if result.insights.count > 3 {
                        Button(showingAll ? "收起更多要点" : "查看其余 \(result.insights.count - 3) 条要点") { showingAll.toggle() }.buttonStyle(.link)
                    }
                }
            }
            if !result.limitations.isEmpty {
                DisclosureGroup("适用条件与限制", isExpanded: $showingLimitations) {
                    VStack(alignment: .leading, spacing: 14) {
                        ForEach(Array(result.limitations.enumerated()), id: \.offset) { _, limitation in point(limitation) }
                    }.padding(.top, 12)
                }
            }
        }
    }

    private func point(_ point: LocalDomainPoint, prominent: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(point.text).font(prominent ? .title3 : .body).lineSpacing(5).textSelection(.enabled)
            if !point.citations.isEmpty, let showEvidence {
                Button("查看关联原文") { showEvidence(point) }.buttonStyle(.link).font(.caption)
            }
        }.frame(maxWidth: .infinity, alignment: .leading)
    }
}

func localDomainDate(_ value: String?) -> String? {
    guard let value else { return nil }
    let zoned = value.hasSuffix("Z") || value.suffix(6).contains("+") || value.suffix(6).contains("-") ? value : value + "Z"
    let fractional = ISO8601DateFormatter(); fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
    guard let date = fractional.date(from: zoned) ?? ISO8601DateFormatter().date(from: zoned) else { return nil }
    return date.formatted(date: .abbreviated, time: .shortened)
}

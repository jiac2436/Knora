import SwiftUI

/// Expansion, selection and scroll state belong to this tab, not to a request's lifetime.
@MainActor struct LocalDomainTreeView: View {
    @ObservedObject var store: LocalDomainOverviewStore
    let domainName: String
    var manage: () -> Void
    var openSource: (LocalDomainSource) -> Void
    var openTopic: (LocalDomainDirectoryTopic) -> Void
    @State private var expanded: Set<String> = []
    @State private var selectedSourceID: String?

    private struct Row: Identifiable {
        enum Content { case topic(LocalDomainDirectoryTopic), source(LocalDomainSource), more, placeholder(String), failure(String), unclassified }
        let id: String
        let depth: Int
        let branch: String
        let content: Content
        var topicID: String? { branch == "__unclassified" ? nil : branch }
        var unclassified: Bool { branch == "__unclassified" }
    }

    private var rows: [Row] {
        guard let directory = store.directory else { return [] }
        let children = Dictionary(grouping: directory.items.filter { $0.parent_id != nil }, by: { $0.parent_id! })
        var result: [Row] = []
        for topic in directory.items.filter({ $0.parent_id == nil }) {
            append(topic, depth: 0, children: children, into: &result)
        }
        if directory.unclassified_count > 0 {
            let key = LocalDomainOverviewStore.treeKey(topicID: nil, unclassified: true)
            result.append(Row(id: key, depth: 0, branch: key, content: .unclassified))
            if expanded.contains(key) { appendFiles(branch: key, depth: 1, into: &result) }
        }
        return result
    }

    private func append(_ topic: LocalDomainDirectoryTopic, depth: Int,
                        children: [String: [LocalDomainDirectoryTopic]], into result: inout [Row]) {
        let key = LocalDomainOverviewStore.treeKey(topicID: topic.id, unclassified: false)
        result.append(Row(id: "topic:\(topic.id)", depth: depth, branch: key, content: .topic(topic)))
        guard expanded.contains(key) else { return }
        // The product currently supports two category levels. Files can belong directly to either.
        for child in children[topic.id] ?? [] {
            result.append(Row(id: "topic:\(child.id)", depth: depth + 1, branch: child.id, content: .topic(child)))
            if expanded.contains(child.id) { appendFiles(branch: child.id, depth: depth + 2, into: &result) }
        }
        appendFiles(branch: key, depth: depth + 1, into: &result)
    }

    private func appendFiles(branch: String, depth: Int, into result: inout [Row]) {
        guard let page = store.treeSources[branch] else {
            result.append(Row(id: "placeholder:\(branch)", depth: depth, branch: branch,
                              content: .placeholder(store.treeLoading.contains(branch) ? "正在读取文件…" : store.treeErrors[branch] ?? "文件尚未读取，点击重试")))
            return
        }
        for source in page.items {
            result.append(Row(id: "file:\(branch):\(source.id)", depth: depth, branch: branch, content: .source(source)))
        }
        if page.items.isEmpty {
            result.append(Row(id: "empty:\(branch)", depth: depth, branch: branch, content: .placeholder("本分类没有直属文件")))
        }
        if let error = store.treeErrors[branch] {
            result.append(Row(id: "error:\(branch)", depth: depth, branch: branch, content: .failure(error)))
        }
        if page.next_offset != nil { result.append(Row(id: "more:\(branch)", depth: depth, branch: branch, content: .more)) }
    }

    var body: some View {
        ScrollViewReader { _ in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    HStack(alignment: .top) {
                        VStack(alignment: .leading, spacing: 7) {
                            Text("主题分类").font(.title2.weight(.semibold))
                            if let directory = store.directory {
                                Text("\(directory.topic_count) 个分类 · \(directory.source_count) 篇资料").font(.callout).foregroundStyle(.secondary)
                            } else { Text("按分类浏览当前知识域的文件").font(.callout).foregroundStyle(.secondary) }
                        }
                        Spacer()
                        if store.directoryLoading { ProgressView().controlSize(.small).accessibilityLabel("正在刷新分类") }
                        Button("管理分类…", action: manage)
                    }.padding(.bottom, 22)
                    if let error = store.directoryError { Text(error).font(.callout).foregroundStyle(.orange).padding(.bottom, 14) }
                    if store.directory == nil, let error = store.directoryError, !store.directoryLoading {
                        VStack(alignment: .leading, spacing: 14) {
                            EmptyMessage(symbol: "folder.badge.questionmark", title: "暂时无法读取分类", detail: error)
                            Button("重新读取分类") { Task { await store.refreshSelected() } }
                        }.frame(minHeight: 260)
                    } else if store.directory == nil {
                        VStack(alignment: .leading, spacing: 16) {
                            ForEach(0..<5) { _ in RoundedRectangle(cornerRadius: 5).fill(InboxStyle.elevated).frame(height: 26) }
                        }.frame(minHeight: 240, alignment: .top).accessibilityLabel("正在读取分类目录")
                    } else if rows.isEmpty {
                        EmptyMessage(symbol: "folder", title: "这里还没有资料", detail: "收集资料后，可以在这里按主题分类浏览。")
                            .frame(minHeight: 260)
                    } else {
                        ForEach(rows) { row in rowContent(row).id(row.id) }
                        Text("一份文件可以属于多个分类，知识域文档总数按文件去重。分类旁的数量包含下级分类。")
                            .font(.caption).foregroundStyle(.secondary).padding(.top, 22)
                    }
                }.padding(28)
            }
        }
    }

    @ViewBuilder private func rowContent(_ row: Row) -> some View {
        HStack(spacing: 9) {
            switch row.content {
            case .topic(let topic):
                Button { toggle(row) } label: {
                    HStack(spacing: 10) {
                        Image(systemName: expanded.contains(row.branch) ? "chevron.down" : "chevron.right").font(.caption).frame(width: 12)
                        Image(systemName: expanded.contains(row.branch) ? "folder.fill" : "folder").foregroundStyle(InboxStyle.accent)
                        Text(topic.name).fontWeight(.medium)
                        Spacer()
                        Text("\(topic.source_count) 篇").font(.caption).foregroundStyle(.secondary)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel("\(expanded.contains(row.branch) ? "折叠" : "展开")分类 \(topic.name)，\(topic.source_count) 篇")
                Button("概览") { openTopic(topic) }.buttonStyle(.link).font(.caption).help("查看分类概览")
            case .unclassified:
                Button { toggle(row) } label: {
                    HStack(spacing: 10) {
                        Image(systemName: expanded.contains(row.branch) ? "chevron.down" : "chevron.right").font(.caption).frame(width: 12)
                        Image(systemName: "tray").foregroundStyle(.secondary)
                        Text("未分类").fontWeight(.medium)
                        Spacer()
                        Text("\(store.directory?.unclassified_count ?? 0) 篇").font(.caption).foregroundStyle(.secondary)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain)
            case .source(let source):
                Button {
                    selectedSourceID = source.id
                    openSource(source)
                } label: {
                    HStack(spacing: 10) {
                        Image(systemName: "doc.text").foregroundStyle(.secondary).frame(width: 16)
                        Text(source.title).lineLimit(2).multilineTextAlignment(.leading)
                        Spacer(minLength: 8)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).foregroundStyle(selectedSourceID == source.id ? InboxStyle.accent : Color.primary)
            case .more:
                Button(store.treeLoading.contains(row.branch) ? "正在读取…" : "加载更多文件") {
                    Task { await store.loadTreeSources(topicID: row.topicID, unclassified: row.unclassified, more: true) }
                }.buttonStyle(.link).font(.callout).disabled(store.treeLoading.contains(row.branch))
                Spacer()
            case .placeholder(let title):
                if store.treeSources[row.branch] == nil, !store.treeLoading.contains(row.branch) {
                    Button(title) { Task { await store.loadTreeSources(topicID: row.topicID, unclassified: row.unclassified, force: true) } }
                        .buttonStyle(.link).font(.caption)
                } else { Text(title).font(.caption).foregroundStyle(.secondary) }
                Spacer()
            case .failure(let error):
                Text(error).font(.caption).foregroundStyle(.orange).lineLimit(2)
                Spacer()
                Button("重试") { Task { await store.loadTreeSources(topicID: row.topicID, unclassified: row.unclassified, force: true) } }
                    .buttonStyle(.link).font(.caption).disabled(store.treeLoading.contains(row.branch))
            }
        }
        .padding(.leading, CGFloat(row.depth) * 24 + 9).padding(.trailing, 10).padding(.vertical, 11)
        .background(rowBackground(row), in: RoundedRectangle(cornerRadius: 7))
    }

    private func rowBackground(_ row: Row) -> Color {
        if case .source(let source) = row.content, selectedSourceID == source.id { return InboxStyle.accent.opacity(0.09) }
        return .clear
    }

    private func toggle(_ row: Row) {
        if expanded.contains(row.branch) { expanded.remove(row.branch) }
        else {
            expanded.insert(row.branch)
            Task { await store.loadTreeSources(topicID: row.topicID, unclassified: row.unclassified) }
        }
    }
}

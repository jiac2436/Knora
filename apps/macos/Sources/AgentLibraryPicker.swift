import SwiftUI

struct AgentLibraryPicker: View {
    let domains: [DomainItem]
    @Binding var selection: Set<String>
    @State private var query = ""
    @State private var isExpanded = true
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    private var ids: Set<String> { Set(domains.map(\.id)) }
    private var filtered: [DomainItem] {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return domains.filter { text.isEmpty || $0.name.localizedStandardContains(text) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                Text("授权知识库").font(.headline)
                Text("已选 \(selection.intersection(ids).count) / \(domains.count)")
                    .font(.caption).foregroundStyle(InboxStyle.muted)
                Spacer()
                Button(selection.intersection(ids) == ids && !ids.isEmpty ? "取消全选" : "选择全部") {
                    selection = selection.intersection(ids) == ids ? [] : ids
                }.disabled(ids.isEmpty)
                Button {
                    withAnimation(reduceMotion ? nil : .smooth(duration: 0.18)) { isExpanded.toggle() }
                } label: {
                    Label(isExpanded ? "收起" : "展开", systemImage: isExpanded ? "chevron.up" : "chevron.down")
                }
                .buttonStyle(InboxButtonStyle(.quiet))
                .accessibilityLabel(isExpanded ? "收起授权知识库列表" : "展开授权知识库列表")
                .accessibilityIdentifier("agent-library-toggle")
            }
            if isExpanded, domains.isEmpty {
                Text("还没有知识库，请先创建知识库再接入。")
                    .font(.callout).foregroundStyle(InboxStyle.muted)
            } else if isExpanded {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass").foregroundStyle(InboxStyle.muted)
                    TextField("查找知识库", text: $query).textFieldStyle(.plain)
                        .accessibilityLabel("查找授权知识库")
                    if !query.isEmpty {
                        Button { query = "" } label: { Image(systemName: "xmark.circle.fill") }
                            .buttonStyle(.plain).accessibilityLabel("清除知识库筛选")
                    }
                }.padding(10).background(InboxStyle.canvas, in: RoundedRectangle(cornerRadius: 8))
                ScrollView {
                    LazyVStack(spacing: 3) {
                        ForEach(filtered) { domain in
                            Button {
                                if selection.contains(domain.id) { selection.remove(domain.id) }
                                else { selection.insert(domain.id) }
                            } label: {
                                HStack(spacing: 10) {
                                    Image(systemName: selection.contains(domain.id) ? "checkmark.square.fill" : "square")
                                        .foregroundStyle(selection.contains(domain.id) ? InboxStyle.accent : InboxStyle.muted)
                                    Image(systemName: "folder").foregroundStyle(InboxStyle.muted)
                                    Text(domain.name).lineLimit(1).truncationMode(.middle)
                                    Spacer(minLength: 0)
                                }
                                .padding(.horizontal, 10).frame(height: 36)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(selection.contains(domain.id) ? InboxStyle.accent.opacity(0.08) : .clear,
                                            in: RoundedRectangle(cornerRadius: 6))
                                .contentShape(Rectangle())
                            }.buttonStyle(.plain)
                                .accessibilityLabel(domain.name)
                                .accessibilityValue(selection.contains(domain.id) ? "已选择" : "未选择")
                        }
                        if filtered.isEmpty { Text("没有找到知识库").foregroundStyle(InboxStyle.muted).padding(12) }
                    }.padding(.trailing, 8)
                }.frame(height: min(210, CGFloat(max(1, filtered.count)) * 39))
                    .scrollIndicators(.automatic)
            }
            Text("仅授权已选知识库中的可共享资料；以后新建的知识库需另外选择并保存。")
                .font(.caption).foregroundStyle(InboxStyle.muted)
        }
    }
}

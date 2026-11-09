import AppKit
import SwiftUI

enum KnowledgeSearchMode: String, CaseIterable {
    case exact = "精确搜索"
    case semantic = "语义搜索"
    var apiValue: String { self == .exact ? "exact" : "semantic" }
}

enum KnowledgeSearchScope: String {
    case library = "当前知识库"
    case current = "当前内容"
}

/// This is a session preference: CaptureModel owns it; it is never written to defaults.
struct KnowledgeSearchPanel: View {
    @Binding var text: String
    @Binding var mode: KnowledgeSearchMode
    let scope: KnowledgeSearchScope
    let results: [KnowledgeRow]
    let matches: [LocalSearchMatch]
    @Binding var selection: Int
    let loading: Bool
    let message: String?
    let activate: (Int) -> Void
    let close: () -> Void
    @FocusState private var focused: Bool

    private var local: Bool { scope == .current && mode == .exact }
    private var count: Int { local ? matches.count : results.count }
    private var identities: [String] {
        local ? matches.map { "\($0.range.location):\($0.range.length)" } : results.map { $0.id + String($0.locatorStart ?? -1) + $0.snippet }
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 12) {
                Menu {
                    ForEach(KnowledgeSearchMode.allCases, id: \.self) { choice in
                        Button {
                            mode = choice; focused = true
                        } label: {
                            if mode == choice { Label(choice.rawValue, systemImage: "checkmark") }
                            else { Text(choice.rawValue) }
                        }
                    }
                } label: {
                    HStack(spacing: 6) {
                        Text(mode.rawValue)
                        Image(systemName: "chevron.down").font(.caption2)
                    }
                    .font(.callout).padding(.horizontal, 10).frame(height: 36)
                    .background(InboxStyle.elevated, in: RoundedRectangle(cornerRadius: 7))
                }
                .menuStyle(.borderlessButton).menuIndicator(.hidden).frame(width: 116)
                .accessibilityLabel("搜索方式：\(mode.rawValue)")
                TextField("搜索\(scope.rawValue)", text: $text)
                    .textFieldStyle(.plain).font(.system(size: 22)).focused($focused)
                    .accessibilityIdentifier("knowledge-search-query")
                    .onSubmit { activate(selection) }
                    .onKeyPress(.downArrow) {
                        if count > 0 { selection = min(selection + 1, count - 1) }
                        return .handled
                    }
                    .onKeyPress(.tab) {
                        mode = mode == .exact ? .semantic : .exact
                        return .handled
                    }
                    .onKeyPress(.upArrow) { selection = max(0, selection - 1); return .handled }
                    .onKeyPress(.escape) { close(); return .handled }
                Text(scope.rawValue).font(.caption).foregroundStyle(InboxStyle.muted).fixedSize()
                Button(action: close) { Image(systemName: "xmark") }
                    .buttonStyle(InboxIconButtonStyle()).help("关闭搜索")
                    .accessibilityLabel("关闭搜索")
            }.padding(15)
            if let message, count > 0 {
                Text(message).font(.caption).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(.horizontal, 15).padding(.bottom, 8)
            }
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                Divider()
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 4) {
                            if local {
                                ForEach(Array(matches.enumerated()), id: \.offset) { index, match in
                                    result(index) {
                                        Text(match.preview).lineLimit(3)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                }
                            } else {
                                ForEach(Array(results.enumerated()), id: \.offset) { index, row in
                                    result(index) {
                                        HStack(spacing: 12) {
                                            Image(systemName: row.kind.symbol).frame(width: 20)
                                            VStack(alignment: .leading, spacing: 4) {
                                                Text(row.title).lineLimit(1)
                                                Text(row.snippet).lineLimit(2).font(.caption).foregroundStyle(.secondary)
                                                SummarySearchMatchView(row: row)
                                            }
                                            Spacer(minLength: 0)
                                        }
                                    }
                                }
                            }
                            if count == 0 {
                                HStack(spacing: 9) {
                                    if loading { ProgressView().controlSize(.small) }
                                    Text(loading ? "正在搜索…" : message ?? "没有找到匹配内容")
                                        .font(.callout).foregroundStyle(.secondary)
                                }.frame(maxWidth: .infinity).padding(24)
                            }
                        }
                        .padding(8)
                        .background(SearchScrollIndicators().allowsHitTesting(false).accessibilityHidden(true))
                    }
                    .scrollIndicators(.visible)
                    .frame(height: min(340, max(84, CGFloat(count) * 96 + 16)))
                    .onChange(of: selection) { _, index in proxy.scrollTo(index) }
                    .onChange(of: identities) { _, _ in proxy.scrollTo(selection, anchor: .top) }
                }
            }
        }
        .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 14))
        .overlay(RoundedRectangle(cornerRadius: 14).stroke(InboxStyle.separator))
        .shadow(color: .black.opacity(0.3), radius: 22, y: 8)
        .onExitCommand(perform: close)
        .task { focused = true }
    }

    private func result<Content: View>(_ index: Int, @ViewBuilder content: () -> Content) -> some View {
        Button { activate(index) } label: {
            content().padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .foregroundStyle(InboxStyle.text)
                .background(selection == index ? InboxStyle.accent.opacity(0.22) : .clear,
                            in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .stroke(selection == index ? InboxStyle.accent.opacity(0.6) : .clear))
                .contentShape(RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain).id(index)
        .accessibilityIdentifier("knowledge-search-result-\(index)")
        .accessibilityAddTraits(selection == index ? .isSelected : [])
    }
}

/// A dedicated results scroller shows its track only when content overflows,
/// including when macOS normally hides overlay scrollbars until scrolling.
private struct SearchScrollIndicators: NSViewRepresentable {
    final class Anchor: NSView {
        override func viewDidMoveToWindow() { super.viewDidMoveToWindow(); configure() }
        override func viewDidMoveToSuperview() { super.viewDidMoveToSuperview(); configure() }
        func configure() {
            DispatchQueue.main.async { [weak self] in
                guard let scroll = self?.enclosingScrollView else { return }
                if scroll.scrollerStyle != .legacy { scroll.scrollerStyle = .legacy }
                scroll.hasVerticalScroller = true
                scroll.autohidesScrollers = true
            }
        }
    }
    func makeNSView(context: Context) -> Anchor { Anchor(frame: .zero) }
    func updateNSView(_ view: Anchor, context: Context) { view.configure() }
}

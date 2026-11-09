import SwiftUI

struct InboxSelectOption<Value: Hashable>: Identifiable {
    let value: Value
    let title: String
    var subtitle: String?
    var symbol: String?
    var id: Value { value }

    init(_ value: Value, _ title: String, subtitle: String? = nil, symbol: String? = nil) {
        self.value = value; self.title = title; self.subtitle = subtitle; self.symbol = symbol
    }
}

/// A single-choice field with a themed, keyboard-accessible native popover.
struct InboxSelect<Value: Hashable>: View {
    let title: String
    @Binding var selection: Value
    let options: [InboxSelectOption<Value>]
    var showsLabel: Bool
    var placeholder: String
    @Environment(\.isEnabled) private var isEnabled
    @State private var isPresented = false
    @State private var hovering = false
    @State private var fieldWidth: CGFloat = 260
    @FocusState private var focused: Bool

    init(_ title: String, selection: Binding<Value>, options: [InboxSelectOption<Value>],
         showsLabel: Bool = true, placeholder: String = "请选择") {
        self.title = title; _selection = selection; self.options = options
        self.showsLabel = showsLabel; self.placeholder = placeholder
    }

    private var selected: InboxSelectOption<Value>? { options.first { $0.value == selection } }
    private var available: Bool { isEnabled && !options.isEmpty }

    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            if showsLabel {
                Text(title).font(.system(size: 12, weight: .medium)).foregroundStyle(InboxStyle.muted)
            }
            Button { isPresented.toggle() } label: {
                HStack(spacing: 9) {
                    if let symbol = selected?.symbol {
                        Image(systemName: symbol).foregroundStyle(InboxStyle.muted).frame(width: 16)
                    }
                    Text(selected?.title ?? placeholder)
                        .font(.system(size: 13, weight: .medium))
                        .foregroundStyle(selected == nil ? InboxStyle.muted : InboxStyle.text)
                        .lineLimit(1).truncationMode(.tail)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 10, weight: .semibold)).foregroundStyle(InboxStyle.muted)
                }
                .padding(.horizontal, 12).frame(height: 38)
                .background(hovering && available ? InboxStyle.elevated : InboxStyle.canvas,
                            in: RoundedRectangle(cornerRadius: 9))
                .overlay {
                    RoundedRectangle(cornerRadius: 9)
                        .stroke(isPresented || focused ? InboxStyle.accent.opacity(0.65) : InboxStyle.separator, lineWidth: 1)
                }
                .contentShape(RoundedRectangle(cornerRadius: 9))
            }
            .buttonStyle(.plain)
            .focusable().focused($focused).focusEffectDisabled()
            .disabled(!available)
            .opacity(available ? 1 : 0.45)
            .onHover { hovering = $0 }
            .help(selected?.title ?? placeholder)
            .accessibilityLabel(title)
            .accessibilityValue(selected?.title ?? placeholder)
            .accessibilityHint("展开选项")
            .accessibilityIdentifier("inbox-select-" + title)
            .onGeometryChange(for: CGFloat.self) { $0.size.width } action: { fieldWidth = $0 }
            .onKeyPress(keys: [.return, .space, .upArrow, .downArrow]) { _ in
                guard available else { return .ignored }
                isPresented = true
                return .handled
            }
            .popover(isPresented: $isPresented, arrowEdge: .bottom) {
                InboxSelectPanel(title: title, selection: selection, options: options,
                    choose: { value in
                        guard available, options.contains(where: { $0.value == value }) else { return }
                        selection = value; isPresented = false
                    }, close: { isPresented = false })
                    .frame(width: min(440, max(260, fieldWidth)))
                    .preferredColorScheme(.dark)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .onChange(of: available) { _, enabled in if !enabled { isPresented = false } }
    }
}

private struct InboxSelectPanel<Value: Hashable>: View {
    let title: String
    let selection: Value
    let options: [InboxSelectOption<Value>]
    let choose: (Value) -> Void
    let close: () -> Void
    @State private var query = ""
    @State private var highlighted = 0
    @State private var committed = false
    @FocusState private var searchFocused: Bool
    @FocusState private var listFocused: Bool

    private var searchable: Bool { options.count >= 12 }
    private var indices: [Int] {
        let text = query.trimmingCharacters(in: .whitespacesAndNewlines)
        return options.indices.filter {
            text.isEmpty || options[$0].title.localizedCaseInsensitiveContains(text) ||
                (options[$0].subtitle?.localizedCaseInsensitiveContains(text) == true)
        }
    }
    private func rowHeight(_ index: Int) -> CGFloat { options[index].subtitle == nil ? 44 : 56 }
    private var listHeight: CGFloat {
        min(288, indices.isEmpty ? 44 : indices.reduce(CGFloat(0)) { $0 + rowHeight($1) } + CGFloat(max(0, indices.count - 1) * 4))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title).font(.system(size: 12, weight: .semibold)).foregroundStyle(InboxStyle.muted)
                .padding(.horizontal, 8).padding(.top, 5).padding(.bottom, 3)
            if searchable {
                HStack(spacing: 8) {
                    Image(systemName: "magnifyingglass").foregroundStyle(InboxStyle.muted)
                    TextField("筛选选项", text: $query)
                        .textFieldStyle(.plain).focused($searchFocused)
                        .accessibilityLabel("筛选" + title)
                        .accessibilityIdentifier("inbox-select-search-" + title)
                        .onSubmit { commit(highlighted) }
                }
                .font(.system(size: 13)).padding(.horizontal, 10).frame(height: 34)
                .background(InboxStyle.canvas, in: RoundedRectangle(cornerRadius: 7))
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: 4) {
                        if indices.isEmpty {
                            Text("没有匹配选项").font(.system(size: 13)).foregroundStyle(InboxStyle.muted)
                                .frame(maxWidth: .infinity).frame(height: 44)
                        }
                        ForEach(indices, id: \.self) { index in
                            optionRow(index).id(index)
                        }
                    }
                    .padding(.horizontal, 1)
                }
                .frame(height: listHeight)
                .scrollIndicators(.visible)
                .onChange(of: highlighted) { _, index in proxy.scrollTo(index) }
                .onAppear { proxy.scrollTo(highlighted) }
            }
        }
        .padding(8)
        .background(InboxStyle.surface)
        .focusable().focused($listFocused).focusEffectDisabled()
        .onAppear {
            resetHighlight()
            if searchable { searchFocused = true } else { listFocused = true }
        }
        .onChange(of: query) { _, _ in highlighted = indices.first ?? 0 }
        .onChange(of: options.map(\.value)) { _, _ in resetHighlight() }
        .onChange(of: selection) { _, _ in resetHighlight() }
        .onKeyPress(keys: [.upArrow, .downArrow, .return, .escape]) { press in
            switch press.key {
            case .upArrow: move(-1)
            case .downArrow: move(1)
            case .return: commit(highlighted)
            case .escape: close()
            default: return .ignored
            }
            return .handled
        }
        .onKeyPress(.space) {
            guard !searchFocused else { return .ignored }
            commit(highlighted)
            return .handled
        }
        .onExitCommand(perform: close)
    }

    private func optionRow(_ index: Int) -> some View {
        let option = options[index]
        let selected = option.value == selection
        return Button { commit(index) } label: {
            HStack(spacing: 10) {
                if let symbol = option.symbol {
                    Image(systemName: symbol).foregroundStyle(selected ? InboxStyle.accent : InboxStyle.muted)
                        .frame(width: 18)
                }
                VStack(alignment: .leading, spacing: 3) {
                    Text(option.title).font(.system(size: 13, weight: selected ? .semibold : .regular))
                        .foregroundStyle(InboxStyle.text).lineLimit(option.subtitle == nil ? 2 : 1)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    if let subtitle = option.subtitle {
                        Text(subtitle).font(.system(size: 11)).foregroundStyle(InboxStyle.muted).lineLimit(1)
                    }
                }
                Image(systemName: "checkmark").font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(InboxStyle.accent).opacity(selected ? 1 : 0).frame(width: 16)
            }
            .padding(.horizontal, 10).frame(height: rowHeight(index))
            .background(selected ? InboxStyle.accent.opacity(0.12) : highlighted == index ? InboxStyle.hover : .clear,
                        in: RoundedRectangle(cornerRadius: 7))
            .overlay {
                RoundedRectangle(cornerRadius: 7)
                    .stroke(highlighted == index ? InboxStyle.accent.opacity(0.25) : .clear)
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { inside in if inside { highlighted = index } }
        .help(option.title)
        .accessibilityLabel(option.title)
        .accessibilityAddTraits(selected ? .isSelected : [])
        .accessibilityIdentifier("inbox-select-option-\(title)-\(index)")
    }

    private func resetHighlight() {
        highlighted = indices.first(where: { options[$0].value == selection }) ?? indices.first ?? 0
    }
    private func move(_ offset: Int) {
        let visible = indices
        guard !visible.isEmpty else { return }
        let current = visible.firstIndex(of: highlighted) ?? (offset > 0 ? -1 : visible.count)
        highlighted = visible[min(max(0, current + offset), visible.count - 1)]
    }
    private func commit(_ index: Int) {
        guard !committed, indices.contains(index) else { return }
        committed = true
        choose(options[index].value)
    }
}

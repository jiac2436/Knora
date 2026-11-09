import SwiftUI

enum InboxStyle {
    static let accent = Color(red: 0.66, green: 0.71, blue: 0.95)
    static let canvas = Color(white: 0.094)
    static let surface = Color(white: 0.125)
    static let elevated = Color(white: 0.161)
    static let hover = Color(white: 0.188)
    static let text = Color(white: 0.925)
    static let muted = Color(white: 0.59)
    static let separator = Color.white.opacity(0.07)
    static let quiet = muted
}

struct InboxButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary, quiet }
    let kind: Kind
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    init(_ kind: Kind = .secondary) { self.kind = kind }

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .medium))
            .padding(.horizontal, 12).padding(.vertical, 8)
            .foregroundStyle(configuration.role == .destructive ? Color.red :
                kind == .primary ? InboxStyle.canvas : InboxStyle.text)
            .background(background(pressed: configuration.isPressed), in: RoundedRectangle(cornerRadius: 8))
            .contentShape(RoundedRectangle(cornerRadius: 8))
            .opacity(isEnabled ? 1 : 0.4)
            .onHover { hovering = $0 }
    }

    private func background(pressed: Bool) -> Color {
        if kind == .primary { return InboxStyle.accent.opacity(pressed ? 0.75 : 1) }
        if pressed || hovering { return InboxStyle.hover }
        return kind == .secondary ? InboxStyle.elevated : .clear
    }
}

struct InboxFieldStyle: TextFieldStyle {
    func _body(configuration: TextField<Self._Label>) -> some View {
        configuration
            .textFieldStyle(.plain)
            .font(.system(size: 13))
            .padding(10)
            .background(InboxStyle.canvas, in: RoundedRectangle(cornerRadius: 8))
            .overlay(RoundedRectangle(cornerRadius: 8).stroke(InboxStyle.separator))
    }
}

struct InboxIconButtonStyle: ButtonStyle {
    var selected = false
    @Environment(\.isEnabled) private var isEnabled
    @State private var hovering = false

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 15, weight: .regular))
            .foregroundStyle(selected ? InboxStyle.text : InboxStyle.muted)
            .frame(width: 32, height: 32)
            .background(selected || hovering || configuration.isPressed ? InboxStyle.hover : .clear,
                        in: RoundedRectangle(cornerRadius: 7))
            .contentShape(RoundedRectangle(cornerRadius: 7))
            .opacity(isEnabled ? 1 : 0.35)
            .onHover { hovering = $0 }
    }
}

struct Surface<Content: View>: View {
    let content: Content

    init(@ViewBuilder content: () -> Content) { self.content = content() }

    var body: some View {
        content
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 12))
            .overlay(RoundedRectangle(cornerRadius: 12).stroke(InboxStyle.separator))
    }
}

struct Eyebrow: View {
    let title: String
    var body: some View {
        Text(title)
            .font(.system(size: 11, weight: .medium))
            .foregroundStyle(InboxStyle.muted)
    }
}

struct EmptyMessage: View {
    let symbol: String
    let title: String
    let detail: String

    var body: some View {
        VStack(spacing: 9) {
            Image(systemName: symbol)
                .font(.system(size: 28, weight: .light))
                .foregroundStyle(InboxStyle.accent)
                .frame(width: 58, height: 58)
                .background(InboxStyle.accent.opacity(0.10), in: RoundedRectangle(cornerRadius: 15))
            Text(title).font(.system(size: 16, weight: .semibold))
            Text(detail).font(.callout).foregroundStyle(.secondary).multilineTextAlignment(.center)
        }
        .padding(26)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

import SwiftUI

/// Shared by the settings pages; the entire segment is a keyboard-accessible button.
struct SettingsTabs<Value: Hashable>: View {
    let title: String
    @Binding var selection: Value
    let options: [InboxSelectOption<Value>]
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        HStack(spacing: 0) {
            ForEach(Array(options.enumerated()), id: \.element.id) { index, option in
                Button {
                    withAnimation(reduceMotion ? nil : .easeInOut(duration: 0.15)) { selection = option.value }
                } label: {
                    Text(option.title)
                        .font(.system(size: 13, weight: selection == option.value ? .semibold : .medium))
                        .foregroundStyle(selection == option.value ? InboxStyle.text : InboxStyle.muted)
                        .frame(maxWidth: .infinity).frame(height: 30)
                        .background {
                            if selection == option.value { RoundedRectangle(cornerRadius: 6).fill(InboxStyle.hover) }
                        }
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .accessibilityLabel(option.title)
                .accessibilityAddTraits(selection == option.value ? .isSelected : [])
                if index < options.count - 1 && selection != option.value && selection != options[index + 1].value {
                    Rectangle().fill(InboxStyle.separator).frame(width: 1, height: 14)
                }
            }
        }
        .padding(3)
        .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(InboxStyle.separator, lineWidth: 1))
        .accessibilityElement(children: .contain).accessibilityLabel(title)
    }
}

import SwiftUI

enum ReaderSidebarLayout {
    static let minimum = 200.0
    static let maximum = 360.0

    static func width(_ proposed: Double) -> Double {
        guard proposed.isFinite else { return 248 }
        return min(maximum, max(minimum, proposed))
    }
}

/// Canvas-colored stops fade viewport edges without tinting an empty background.
struct ReaderEdgeVeil: View {
    let top: Bool

    var body: some View {
        LinearGradient(stops: [
            .init(color: InboxStyle.canvas, location: 0),
            .init(color: InboxStyle.canvas.opacity(0.72), location: 0.25),
            .init(color: InboxStyle.canvas.opacity(0.20), location: 0.65),
            .init(color: InboxStyle.canvas.opacity(0), location: 1),
        ], startPoint: top ? .top : .bottom, endPoint: top ? .bottom : .top)
            .frame(height: 24)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
    }
}

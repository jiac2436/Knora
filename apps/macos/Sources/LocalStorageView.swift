import SwiftUI

struct LocalStorageView: View {
    let locations: StorageScanLocations
    @State private var snapshot: StorageSnapshot?
    @State private var scanning = true
    @State private var failed = false
    @State private var refreshID = UUID()
    @State private var hovered: StorageGroup?

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Surface {
                VStack(alignment: .leading, spacing: 18) {
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text(AppBrand.displayName).font(.system(size: 14, weight: .semibold))
                            Text("当前 App 与所有本地资料库").font(.system(size: 12)).foregroundStyle(InboxStyle.muted)
                        }
                        Spacer(minLength: 12)
                        if let snapshot {
                            VStack(alignment: .trailing, spacing: 4) {
                                Text(size(snapshot.totalBytes)).font(.system(size: 24, weight: .medium)).monospacedDigit()
                                Text(snapshot.unreadableItems == 0 ? "总文件大小" : "已统计文件大小")
                                    .font(.system(size: 11)).foregroundStyle(InboxStyle.muted)
                            }
                        } else {
                            ProgressView().controlSize(.small).accessibilityLabel("正在统计存储")
                        }
                    }
                    if let snapshot {
                        StorageUsageBar(snapshot: snapshot, hovered: $hovered).zIndex(1)
                        // Adaptive columns keep all labels legible in the settings panel.
                        LazyVGrid(columns: [GridItem(.adaptive(minimum: 104), alignment: .leading)], alignment: .leading, spacing: 10) {
                            ForEach(StorageGroup.allCases.filter { $0.bytes(in: snapshot) > 0 }) { category in
                                HStack(spacing: 6) {
                                    Circle().fill(category.color).frame(width: 7, height: 7)
                                    Text(category.title).font(.system(size: 11)).foregroundStyle(InboxStyle.muted)
                                }
                                .contentShape(Rectangle())
                                .onHover { inside in
                                    if inside { hovered = category }
                                    else if hovered == category { hovered = nil }
                                }
                            }
                        }
                    } else {
                        RoundedRectangle(cornerRadius: 6).fill(InboxStyle.elevated).frame(height: 26)
                        Text(failed ? "暂时无法完成统计，请刷新重试。" : "正在统计存储空间…")
                            .font(.callout).foregroundStyle(InboxStyle.muted)
                    }
                }
            }
            HStack {
                Text("存储明细").font(.system(size: 13, weight: .semibold))
                Spacer()
                if scanning, snapshot != nil { ProgressView().controlSize(.mini) }
                Button {
                    refreshID = UUID()
                } label: {
                    Label("刷新", systemImage: "arrow.clockwise")
                }
                .disabled(scanning)
                .accessibilityIdentifier("storage-refresh")
            }
            if let snapshot {
                VStack(spacing: 0) {
                    ForEach(StorageGroup.allCases) { category in
                        row(category, snapshot: snapshot)
                        if category != StorageGroup.allCases.last {
                            Rectangle().fill(InboxStyle.separator).frame(height: 1).padding(.leading, 54)
                        }
                    }
                }
                .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 12))
                .overlay(RoundedRectangle(cornerRadius: 12).stroke(InboxStyle.separator))
                if snapshot.unreadableItems > 0 {
                    notice("有 \(snapshot.unreadableItems) 个目录或文件暂时无法读取，统计可能不完整。请刷新重试。")
                }
                if snapshot.unclassifiedDatabases > 0 {
                    notice("部分数据暂时无法细分，其文件大小已计入“数据索引”。可稍后刷新。")
                }
                VStack(alignment: .leading, spacing: 7) {
                    Text("按文件大小统计，包含历史版本、回收站中仍保存的数据及本地模型。")
                    Text("不包含其他版本的 App、导出到外部的备份和系统临时文件。文件大小与 APFS 实际分配的磁盘空间可能不同。")
                    Text("更新于 \(snapshot.scannedAt.formatted(date: .omitted, time: .standard))")
                        .monospacedDigit()
                }
                .font(.system(size: 11)).foregroundStyle(InboxStyle.muted)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
        .task(id: refreshID) { await refresh() }
        .accessibilityElement(children: .contain)
    }

    private func row(_ category: StorageGroup, snapshot: StorageSnapshot) -> some View {
        HStack(spacing: 12) {
            Image(systemName: category.symbol).font(.system(size: 15)).foregroundStyle(category.color)
                .frame(width: 28, height: 30)
            VStack(alignment: .leading, spacing: 5) {
                Text(category.title).font(.system(size: 13, weight: .medium))
                Text(category.detail).font(.system(size: 11)).foregroundStyle(InboxStyle.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 12)
            Text(size(category.bytes(in: snapshot))).font(.system(size: 13)).monospacedDigit().fixedSize()
        }
        .padding(.horizontal, 16).padding(.vertical, 13)
        .accessibilityElement(children: .combine)
        .accessibilityIdentifier("storage-category-\(category.rawValue)")
    }

    private func notice(_ text: String) -> some View {
        Label(text, systemImage: "info.circle").font(.system(size: 12)).foregroundStyle(InboxStyle.muted)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func size(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }

    @MainActor private func refresh() async {
        scanning = true; failed = false
        defer { scanning = false }
        do {
            let result = try await MacStorageScanner.snapshot(locations)
            guard !Task.isCancelled else { return }
            snapshot = result
        } catch is CancellationError { }
        catch { if !Task.isCancelled { failed = true } }
    }
}

private extension StorageGroup {
    var color: Color {
        switch self {
        case .application: Color(red: 0.94, green: 0.35, blue: 0.36)
        case .documents: Color(red: 0.96, green: 0.62, blue: 0.28)
        case .analysis: InboxStyle.accent
        case .data: Color(red: 0.36, green: 0.72, blue: 0.80)
        case .cache: Color(white: 0.57)
        }
    }
}

struct StorageUsageBar: View {
    let snapshot: StorageSnapshot
    @Binding var hovered: StorageGroup?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        GeometryReader { geometry in
            let segments = StorageBarLayout.segments(snapshot, width: geometry.size.width)
            ZStack(alignment: .topLeading) {
                HStack(spacing: 0) {
                    ForEach(segments) { segment in
                        Rectangle().fill(segment.group.color)
                            .opacity(hovered == nil || hovered == segment.group ? 1 : 0.65)
                            .frame(width: segment.width)
                            .contentShape(Rectangle())
                            .onHover { inside in
                                if inside { hovered = segment.group }
                                else if hovered == segment.group { hovered = nil }
                            }
                            .accessibilityLabel("\(segment.group.title)，\(size(segment.group.bytes(in: snapshot)))")
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(InboxStyle.elevated)
                .clipShape(RoundedRectangle(cornerRadius: 6))
                if let segment = segments.first(where: { $0.group == hovered }) {
                    let placement = StorageBubblePlacement(segmentCenter: segment.center, barWidth: geometry.size.width)
                    VStack(spacing: 3) {
                        Text(segment.group.title).font(.system(size: 12, weight: .medium))
                        Text(size(segment.group.bytes(in: snapshot)))
                            .font(.system(size: 13, weight: .semibold)).monospacedDigit()
                    }
                    .foregroundStyle(InboxStyle.text)
                    .padding(.bottom, StorageHoverBubble.pointerHeight)
                    .frame(width: placement.width, height: 64)
                    .background {
                        StorageHoverBubble(pointer: placement.pointer)
                            .fill(InboxStyle.elevated)
                            .shadow(color: .black.opacity(0.28), radius: 7, y: 4)
                    }
                    .overlay(StorageHoverBubble(pointer: placement.pointer).stroke(Color.white.opacity(0.18)))
                    .position(x: placement.center, y: -38)
                    .transition(.opacity.combined(with: .offset(y: reduceMotion ? 0 : 4)))
                    .allowsHitTesting(false)
                    .accessibilityElement(children: .combine)
                    .accessibilityIdentifier("storage-hover-bubble")
                }
            }
            .animation(reduceMotion ? nil : .smooth(duration: 0.16), value: hovered)
        }
        .frame(height: 26)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("storage-usage-bar")
        .onDisappear { hovered = nil }
    }

    private func size(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
    }
}

/// A single path keeps the bubble and its downward pointer seamless.
struct StorageHoverBubble: Shape {
    var pointer: CGFloat
    static let pointerHeight: CGFloat = 9
    var animatableData: CGFloat {
        get { pointer }
        set { pointer = newValue }
    }
    func path(in rect: CGRect) -> Path {
        let bottom = rect.height - Self.pointerHeight
        let radius: CGFloat = 12
        let half: CGFloat = 7
        var path = Path()
        path.move(to: CGPoint(x: radius, y: 0))
        path.addLine(to: CGPoint(x: rect.width - radius, y: 0))
        path.addQuadCurve(to: CGPoint(x: rect.width, y: radius), control: CGPoint(x: rect.width, y: 0))
        path.addLine(to: CGPoint(x: rect.width, y: bottom - radius))
        path.addQuadCurve(to: CGPoint(x: rect.width - radius, y: bottom), control: CGPoint(x: rect.width, y: bottom))
        path.addLine(to: CGPoint(x: pointer + half, y: bottom))
        path.addLine(to: CGPoint(x: pointer, y: rect.height))
        path.addLine(to: CGPoint(x: pointer - half, y: bottom))
        path.addLine(to: CGPoint(x: radius, y: bottom))
        path.addQuadCurve(to: CGPoint(x: 0, y: bottom - radius), control: CGPoint(x: 0, y: bottom))
        path.addLine(to: CGPoint(x: 0, y: radius))
        path.addQuadCurve(to: CGPoint(x: radius, y: 0), control: .zero)
        path.closeSubpath()
        return path
    }
}

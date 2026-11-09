import Foundation

/// Presentation groups do not change the scanner's precise, non-overlapping accounting.
enum StorageGroup: String, CaseIterable, Identifiable, Sendable {
    case application, documents, analysis, data, cache
    var id: Self { self }
    var categories: [StorageCategory] {
        switch self {
        case .application: [.application]
        case .documents: [.documents, .images]
        case .analysis: [.analysis]
        case .data: [.indexes, .models, .other]
        case .cache: [.cache, .backups, .drafts]
        }
    }
    func bytes(in snapshot: StorageSnapshot) -> Int64 {
        categories.reduce(0) { $0 + snapshot[$1] }
    }
    var title: String {
        switch self {
        case .application: "应用程序"
        case .documents: "文稿文档"
        case .analysis: "AI 分析"
        case .data: "数据索引"
        case .cache: "缓存"
        }
    }
    var detail: String {
        switch self {
        case .application: "App 本身、内置运行环境、扩展与资源"
        case .documents: "保存的文档、网页、图片与历史版本"
        case .analysis: "文章分析、知识库概览、主题与问答记录"
        case .data: "用于搜索和阅读的本地数据、模型与设置"
        case .cache: "缓存、日志、库内备份与待整理资料"
        }
    }
    var symbol: String {
        switch self {
        case .application: "app"
        case .documents: "doc.text"
        case .analysis: "sparkles"
        case .data: "externaldrive"
        case .cache: "clock.arrow.circlepath"
        }
    }
}

struct StorageBarSegment: Identifiable {
    let group: StorageGroup
    let start: CGFloat
    let width: CGFloat
    var center: CGFloat { start + width / 2 }
    var id: StorageGroup { group }
}

struct StorageBubblePlacement {
    let center: CGFloat
    let pointer: CGFloat
    let width: CGFloat

    init(segmentCenter: CGFloat, barWidth: CGFloat) {
        width = min(132, max(1, barWidth))
        // Keep the pointer exactly on the segment's center. Only the bubble body
        // avoids the edges, using the card's 20-point inset as breathing room.
        let left = min(max(segmentCenter - width / 2, -20), barWidth - width + 20)
        center = left + width / 2
        pointer = segmentCenter - left
    }
}

enum StorageBarLayout {
    static func segments(_ snapshot: StorageSnapshot, width: CGFloat) -> [StorageBarSegment] {
        guard width > 0, width.isFinite, snapshot.totalBytes > 0 else { return [] }
        var cursor: CGFloat = 0
        return StorageGroup.allCases.compactMap { group in
            let bytes = group.bytes(in: snapshot)
            guard bytes > 0 else { return nil }
            let size = width * CGFloat(bytes) / CGFloat(snapshot.totalBytes)
            defer { cursor += size }
            return StorageBarSegment(group: group, start: cursor, width: size)
        }
    }
}

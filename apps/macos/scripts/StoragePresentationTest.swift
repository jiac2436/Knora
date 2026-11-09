import Foundation

@main struct StoragePresentationTest {
    static func main() {
        let members = StorageGroup.allCases.flatMap(\.categories)
        precondition(Set(members) == Set(StorageCategory.allCases) && members.count == StorageCategory.allCases.count)
        let bytes = Dictionary(uniqueKeysWithValues: StorageCategory.allCases.enumerated().map { ($1, Int64($0 + 1) * 1234) })
        let snapshot = StorageSnapshot(bytes: bytes, unreadableItems: 0, unclassifiedDatabases: 0, scannedAt: Date())
        precondition(StorageGroup.allCases.reduce(0) { $0 + $1.bytes(in: snapshot) } == snapshot.totalBytes)
        precondition(StorageGroup.documents.bytes(in: snapshot) == snapshot[.documents] + snapshot[.images])
        precondition(StorageGroup.data.bytes(in: snapshot) == snapshot[.indexes] + snapshot[.models] + snapshot[.other])
        precondition(StorageGroup.cache.bytes(in: snapshot) == snapshot[.cache] + snapshot[.backups] + snapshot[.drafts])
        precondition(StorageGroup.allCases.map(\.title) == ["应用程序", "文稿文档", "AI 分析", "数据索引", "缓存"])
        for width: CGFloat in [300, 520, 614, 1000] {
            for input in [snapshot, StorageSnapshot(bytes: [.application: 1_000_000_000, .cache: 1], unreadableItems: 0, unclassifiedDatabases: 0, scannedAt: Date())] {
                let segments = StorageBarLayout.segments(input, width: width)
                precondition(abs(segments.map(\.width).reduce(0, +) - width) < 0.000001)
                for segment in segments {
                    let bubble = StorageBubblePlacement(segmentCenter: segment.center, barWidth: width)
                    let left = bubble.center - bubble.width / 2
                    precondition(abs(left + bubble.pointer - segment.center) < 0.000001, "尖角必须严格对齐色块中心")
                    precondition(left >= -20 && left + bubble.width <= width + 20.000001)
                    precondition(bubble.pointer >= 19 && bubble.pointer <= bubble.width - 19)
                    if segment.center >= bubble.width / 2 && segment.center <= width - bubble.width / 2 {
                        precondition(bubble.center == segment.center, "空间足够时，气泡整体也必须居中")
                    }
                }
            }
        }
        let empty = StorageSnapshot(bytes: [:], unreadableItems: 0, unclassifiedDatabases: 0, scannedAt: Date())
        precondition(StorageBarLayout.segments(empty, width: 600).isEmpty)
        precondition(StorageBarLayout.segments(snapshot, width: 0).isEmpty)
        print("PASS: 五类准确合并、总量不变、空状态、占比宽度、常规/极小色块与宽窄窗口的气泡中心和边界避让")
    }
}

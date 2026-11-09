import Foundation

/// File lengths and SQLite page bytes use the same additive, logical-size unit.
/// These are not estimates of reclaimable space or APFS physical allocation.
enum StorageCategory: String, CaseIterable, Identifiable, Sendable {
    case application, documents, images, analysis, indexes, models, drafts, backups, cache, other
    var id: Self { self }
    var title: String {
        switch self {
        case .application: "应用程序"
        case .documents: "文档与正文"
        case .images: "图片"
        case .analysis: "AI 分析"
        case .indexes: "索引"
        case .models: "本地模型"
        case .drafts: "草稿与待整理"
        case .backups: "库内备份"
        case .cache: "缓存与日志"
        case .other: "数据库与设置"
        }
    }
    var detail: String {
        switch self {
        case .application: "当前 App、内置运行环境、已安装扩展与资源，不含下列模型文件"
        case .documents: "文件原件、网页正文、历史版本与证据文本"
        case .images: "已归档图片、图片信息与识别文字"
        case .analysis: "文章分析、知识库概览、主题与问答记录"
        case .indexes: "关键词、向量、总结索引与数据库查询索引"
        case .models: "内置及已下载的检索、文字识别模型"
        case .drafts: "采集草稿、队列记录与尚在暂存的原件"
        case .backups: "保存在客户端管理目录内的备份文件"
        case .cache: "持久缓存、网页缓存与诊断日志"
        case .other: "数据库空闲页、日志及未分类数据、客户端设置"
        }
    }
    var symbol: String {
        switch self {
        case .application: "app.dashed"
        case .documents: "doc.text"
        case .images: "photo"
        case .analysis: "sparkles"
        case .indexes: "text.magnifyingglass"
        case .models: "cpu"
        case .drafts: "tray.and.arrow.down"
        case .backups: "externaldrive"
        case .cache: "clock.arrow.circlepath"
        case .other: "internaldrive"
        }
    }
}

struct StorageScanRoot: Sendable {
    enum Kind: Sendable {
        case application, data(multiple: Bool), cache, settings
    }
    let url: URL
    let kind: Kind
}

struct StorageScanLocations: Sendable {
    let roots: [StorageScanRoot]

    init(home: URL, application: URL) {
        let paths = MacPaths()
        let legacy = paths.library(home: home, credentialService: "", id: nil)
        let library = home.appendingPathComponent("Library")
        func siblings(_ url: URL) -> URL {
            url.deletingLastPathComponent().appendingPathComponent(url.lastPathComponent + "-libraries")
        }
        roots = [StorageScanRoot(url: application, kind: .application),
                 StorageScanRoot(url: legacy.data, kind: .data(multiple: false)),
                 StorageScanRoot(url: paths.librariesRoot(home: home), kind: .data(multiple: true)),
                 StorageScanRoot(url: legacy.cache, kind: .cache),
                 StorageScanRoot(url: siblings(legacy.cache), kind: .cache),
                 StorageScanRoot(url: legacy.logs, kind: .cache),
                 StorageScanRoot(url: siblings(legacy.logs), kind: .cache),
                 StorageScanRoot(url: paths.configuration(home: home), kind: .settings),
                 StorageScanRoot(url: library.appendingPathComponent("Preferences/local.pka.knowledge-inbox.plist"), kind: .settings),
                 StorageScanRoot(url: library.appendingPathComponent("WebKit/local.pka.knowledge-inbox"), kind: .cache),
                 StorageScanRoot(url: library.appendingPathComponent("HTTPStorages/local.pka.knowledge-inbox"), kind: .cache),
                 StorageScanRoot(url: library.appendingPathComponent("Saved Application State/local.pka.knowledge-inbox.savedState"), kind: .cache)]
    }

    init(roots: [StorageScanRoot]) { self.roots = roots }
}

struct StorageSnapshot: Sendable {
    let bytes: [StorageCategory: Int64]
    let unreadableItems: Int
    let unclassifiedDatabases: Int
    let scannedAt: Date
    var totalBytes: Int64 { bytes.values.reduce(0, +) }
    subscript(_ category: StorageCategory) -> Int64 { bytes[category, default: 0] }
}

enum StorageRules {
    static func category(path: [String], kind: StorageScanRoot.Kind) -> StorageCategory {
        switch kind {
        case .application:
            if path.starts(with: ["Contents", "Resources", "runtime", "model"]) ||
                path.starts(with: ["Contents", "Resources", "runtime", "ocr-models"]) { return .models }
            return .application
        case .cache:
            return path.contains("models") || path.contains("ocr-models") ? .models : .cache
        case .settings:
            if path.starts(with: ["extensions", "staging"]) ||
                (path.first == "extensions" && path.dropFirst().first?.hasPrefix("download-") == true) { return .cache }
            if path.starts(with: ["extensions", "packages"]) {
                return path.contains("model") || path.contains("ocr-models") ? .models : .application
            }
            return .other
        case .data(let multiple):
            var path = path
            if multiple {
                guard let first = path.first, UUID(uuidString: first) != nil else { return .other }
                path.removeFirst()
            }
            if path.starts(with: ["assets", "images"]) { return .images }
            if path.first == "assets" { return .documents }
            if path.first == "backups" { return .backups }
            if path.first == "capture-queue" || path.first == "staging" { return .drafts }
            return .other
        }
    }

    static func isDatabase(path: [String], kind: StorageScanRoot.Kind) -> Bool {
        guard case .data(let multiple) = kind else { return false }
        let path = multiple && path.first.flatMap(UUID.init(uuidString:)) != nil ? Array(path.dropFirst()) : path
        return path == ["knowledge.db"]
    }

    static func databaseCategory(name: String, type: String) -> StorageCategory {
        if type == "index" { return .indexes }
        switch name {
        case "source_revisions", "evidence_chunks", "web_read_markdown", "feishu_document_artifacts", "file_assets": return .documents
        case "source_images": return .images
        case "ai_artifacts", "ai_jobs", "ai_job_steps", "domain_summary_artifacts", "domain_summary_jobs",
             "domain_full_analysis_jobs", "domain_builds", "domain_topics", "domain_topic_members",
             "rag_sessions", "rag_turns", "rag_jobs", "tag_suggestions", "tag_rejections": return .analysis
        case "source_search_documents", "source_search_terms", "evidence_embeddings", "semantic_index_jobs",
             "summary_search_terms", "summary_embeddings", "summary_index_jobs", "domain_node_embeddings",
             "domain_node_index_jobs", "domain_topic_index_states": return .indexes
        default: return .other
        }
    }
}

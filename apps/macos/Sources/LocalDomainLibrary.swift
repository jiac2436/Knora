import Foundation

enum LocalDomainSection: String, CaseIterable, Identifiable {
    case overview, topics, information
    var id: String { rawValue }
    var title: String {
        switch self {
        case .overview: "概览"
        case .topics: "主题分类"
        case .information: "知识域信息"
        }
    }
}

struct LocalDomainDirectoryTopic: Codable, Identifiable {
    let id: String
    let name: String
    let parent_id: String?
    let source_count: Int
    let direct_source_count: Int
}

struct LocalDomainDirectory: Codable {
    let domain_id: String
    let scope: String
    let source_count: Int
    let topic_count: Int
    let unclassified_count: Int
    var items: [LocalDomainDirectoryTopic]
    var next_offset: Int?
}

struct LocalDomainSourcePage: Codable {
    let domain_id: String
    let scope: String
    let topic_id: String?
    let unclassified: Bool
    var items: [LocalDomainSource]
    let total_count: Int
    var next_offset: Int?
}

struct LocalDomainIndexLayer: Codable {
    let total: Int
    let ready: Int
    let pending: Int
    let failed: Int
    let disabled: Int
    let unavailable: Int
    let chunk_count: Int
}

struct LocalDomainInformation: Codable {
    struct Documents: Codable {
        let total: Int
        let analyzed: Int
        let pending: Int
        let outdated: Int
        let running: Int
        let failed: Int
    }
    struct Topics: Codable {
        let total: Int
        let roots: Int
        let children: Int
    }
    struct Tag: Codable, Identifiable {
        let id: String
        let name: String
        let origins: [String]
    }
    struct Tags: Codable {
        let total: Int
        let manual: Int
        let ai: Int
        let both: Int
        let items: [Tag]
        let next_offset: Int?
    }
    struct Storage: Codable {
        let total_bytes: Int64?
        let physical_bytes: Int64?
        let recorded_asset_bytes: Int64
        let text_utf8_bytes: Int64
        let ai_payload_bytes: Int64
        let vector_payload_bytes: Int64
        let known_payload_bytes: Int64
        let measurement: String
        let note: String
    }
    struct Layers: Codable {
        let original: LocalDomainIndexLayer
        let summary: LocalDomainIndexLayer
        let topics: LocalDomainIndexLayer
        let domain: LocalDomainIndexLayer
    }
    struct RAG: Codable {
        let semantic_enabled: Bool
        let keyword_available: Bool?
        let layers: Layers
    }
    let domain_id: String
    let scope: String
    let computed_at: String
    let documents: Documents
    let topics: Topics
    let tags: Tags
    let storage: Storage
    let rag: RAG
    let overview_coverage: LocalDomainCoverage?
}

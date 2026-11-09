import Foundation
import SwiftUI
import PDFKit

private enum SourceDateParser {
    static let fractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()
    static let plain = ISO8601DateFormatter()

    static func parse(_ value: String?) -> Date? {
        guard let value else { return nil }
        let hasZone = value.hasSuffix("Z") || value.suffix(6).contains("+") || value.suffix(6).contains("-")
        let timestamp = hasZone ? value : value + "Z"
        return fractional.date(from: timestamp) ?? plain.date(from: timestamp)
    }
}

struct KnowledgeRow: Identifiable {
    let id: String
    let domainID: String?
    let projectIDs: [String]
    let kind: CaptureKind
    let title: String
    let snippet: String
    let revision: Int?
    let contextVersion: Int?
    let note: String?
    let evidenceID: String?
    let matchType: String?
    let matchText: String?
    let matchSources: [String]
    let summarySnippet: String?
    let summaryArtifactID: String?
    let summaryCoverage: String?
    let page: Int?
    let locatorKind: String?
    let locatorStart: Int?
    let locatorEnd: Int?
    let isSearchResult: Bool
    let createdAt: Date?
    var tags: [String]
    let processingStatus: String

    init?(_ value: [String: Any], searchResult: Bool) {
        guard let id = value[searchResult ? "source_id" : "id"] as? String,
              let title = value["title"] as? String else { return nil }
        self.id = id
        self.domainID = value["domain_id"] as? String
        self.projectIDs = value["project_ids"] as? [String] ?? []
        self.kind = CaptureKind(serverValue: value["content_type"] as? String ??
            value["capture_type"] as? String ?? ((value["source_url"] as? String) == nil ? "note" : "link"))
        self.title = kind == .note ? "笔记" : title
        self.processingStatus = value["processing_status"] as? String ?? "usable"
        self.snippet = value["snippet"] as? String ?? value["preview"] as? String ?? ""
        self.revision = value["revision"] as? Int
        self.contextVersion = (value["context"] as? [String: Any])?["version"] as? Int
        self.note = (value[searchResult ? "personal_context" : "context"] as? [String: Any])?["note"] as? String
        self.evidenceID = searchResult ? value["evidence_id"] as? String : nil
        self.matchType = searchResult ? value["match_type"] as? String : nil
        self.matchText = searchResult ? value["match_text"] as? String : nil
        self.matchSources = searchResult ? value["match_sources"] as? [String] ?? [] : []
        self.summarySnippet = searchResult ? value["summary_snippet"] as? String : nil
        self.summaryArtifactID = searchResult ? value["summary_artifact_id"] as? String : nil
        self.summaryCoverage = searchResult ? value["summary_coverage"] as? String : nil
        let locator = value["locator"] as? [String: Any]
        let textLocator = value["text_locator"] as? [String: Any] ?? locator
        self.locatorKind = searchResult ? textLocator?["kind"] as? String : nil
        self.locatorStart = searchResult ? textLocator?["start"] as? Int : nil
        self.locatorEnd = searchResult ? textLocator?["end"] as? Int : nil
        self.page = locator?["kind"] as? String == "pdf_page" ? locator?["start"] as? Int : nil
        self.isSearchResult = searchResult
        self.createdAt = SourceDateParser.parse(value["created_at"] as? String)
        self.tags = value["tags"] as? [String] ?? []
    }
}

struct EvidenceLink: Identifiable {
    let id: String
    let preview: String
    let page: Int?
    init?(_ value: [String: Any]) {
        guard let id = value["evidence_id"] as? String else { return nil }
        self.id = id
        preview = value["preview"] as? String ?? ""
        let locator = value["locator"] as? [String: Any]
        page = locator?["kind"] as? String == "pdf_page" ? locator?["start"] as? Int : nil
    }
}

struct RevisionItem: Identifiable {
    let version: Int
    let isCurrent: Bool
    let evidence: [EvidenceLink]
    let parserVersion: String
    var id: Int { version }
    init?(_ value: [String: Any]) {
        guard let version = value["version"] as? Int else { return nil }
        self.version = version
        isCurrent = value["is_current"] as? Bool ?? false
        parserVersion = value["parser_version"] as? String ?? ""
        evidence = (value["evidence"] as? [[String: Any]] ?? []).compactMap(EvidenceLink.init)
    }
}

struct KnowledgeDetail {
    let id: String
    let domainID: String?
    let kind: CaptureKind
    let title: String
    let revision: Int?
    let contextVersion: Int
    let coverage: String
    let text: String
    let readingTextDiffers: Bool
    let note: String?
    let sourceURL: String?
    let ocrModel: String?
    let ocrStatus: String?
    let ocrError: String?
    let documentParseStatus: String?
    let documentParseError: String?
    let pageCount: Int?
    let isPDF: Bool
    let tags: [String]
    let processingStatus: String
    let webReadError: String?
    let webReadCreatedAt: Date?
    let webReadEngine: String?
    let crawl4aiMarkdownAvailable: Bool
    let crawl4aiMarkdownOrigin: String?
    let webImagesCanArchive: Bool
    let isFeishuDocument: Bool
    let isXiaohongshuPost: Bool
    let images: [SourceImageItem]
    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, let title = value["title"] as? String else { return nil }
        self.id = id
        self.domainID = value["domain_id"] as? String
        self.kind = CaptureKind(serverValue: value["content_type"] as? String ??
            value["capture_type"] as? String ?? ((value["source_url"] as? String) == nil ? "note" : "link"))
        self.title = kind == .note ? "笔记" : title
        processingStatus = value["processing_status"] as? String ?? "usable"
        let webRead = value["web_read"] as? [String: Any]
        webReadError = webRead?["last_error"] as? String
        webReadCreatedAt = SourceDateParser.parse(webRead?["created_at"] as? String)
        webReadEngine = webRead?["engine"] as? String
        crawl4aiMarkdownAvailable = value["crawl4ai_markdown_available"] as? Bool ?? false
        crawl4aiMarkdownOrigin = value["crawl4ai_markdown_origin"] as? String
        webImagesCanArchive = value["web_images_can_archive"] as? Bool ?? false
        isFeishuDocument = value["capture_type"] as? String == "feishu_document"
        isXiaohongshuPost = value["capture_type"] as? String == "browser_page"
            && XiaohongshuReader.isPostURL(value["source_url"] as? String)
        images = (value["images"] as? [[String: Any]] ?? []).compactMap(SourceImageItem.init)
        revision = value["revision"] as? Int
        contextVersion = (value["context"] as? [String: Any])?["version"] as? Int ?? 0
        coverage = value["coverage"] as? String ?? ""
        let indexedText = value["extracted_text"] as? String ?? ""
        text = value["reading_text"] as? String ?? indexedText
        readingTextDiffers = text != indexedText
        note = (value["context"] as? [String: Any])?["note"] as? String
        sourceURL = value["source_url"] as? String
        let ocr = value["ocr"] as? [String: Any]
        ocrModel = ocr?["model"] as? String
        ocrStatus = ocr?["status"] as? String
        ocrError = (ocr?["last_error"] as? String).map { $0.hasPrefix("extension_required:") ? $0.split(separator: ":", maxSplits: 2).last.map(String.init) ?? $0 : $0 }
        let documentParse = value["document_parse"] as? [String: Any]
        documentParseStatus = documentParse?["status"] as? String
        documentParseError = (documentParse?["last_error"] as? String).map { $0.hasPrefix("extension_required:") ? $0.split(separator: ":", maxSplits: 2).last.map(String.init) ?? $0 : $0 }
        pageCount = ocr?["page_count"] as? Int
        let file = value["file"] as? [String: Any]
        isPDF = ocr != nil || ((file?["filename"] as? String)?.lowercased().hasSuffix(".pdf") ?? false)
        tags = value["tags"] as? [String] ?? []
    }
}

struct EvidenceDetail {
    let id: String
    let version: Int
    let text: String
    let hasNewer: Bool
    init?(_ value: [String: Any]) {
        guard let id = value["evidence_id"] as? String,
              let version = value["revision"] as? Int,
              let text = value["text"] as? String else { return nil }
        self.id = id
        self.version = version
        self.text = text
        hasNewer = value["has_newer_revision"] as? Bool ?? false
    }
}

struct CachedKnowledge {
    let detail: KnowledgeDetail
    let revisions: [RevisionItem]
    var evidence: [String: EvidenceDetail] = [:]
    var selectedEvidenceID: String?
}

private enum KnowledgeBrowseMode: CaseIterable {
    case title, date, tag

    var title: String {
        switch self {
        case .title: "按标题浏览"
        case .date: "按录入日期浏览"
        case .tag: "按内容标签浏览"
        }
    }

    var symbol: String {
        switch self {
        case .title: "text.alignleft"
        case .date: "calendar"
        case .tag: "tag"
        }
    }
}

extension CaptureModel {
    func clearKnowledgeCache() {
        textReaderStore.removeAll()
        cachedKnowledge.removeAll()
        knowledgeCacheOrder.removeAll()
        outdatedSourceIDs.removeAll()
        delayedWebReadIDs.removeAll()
    }

    func deleteSource(_ id: String) async -> Bool {
        guard !trashBusy, !busy, !backupBusy else { return false }
        guard connected else { browseMessage = "请先连接后台"; return false }
        let deletingAddress = address
        let deletingToken = token
        do {
            _ = try await API(address: deletingAddress, token: deletingToken)
                .call("v1/sources/\(id)", method: "DELETE")
            guard connected, address == deletingAddress, token == deletingToken else { return false }
            sourceListGeneration = UUID()
            knowledgeRows.removeAll { $0.id == id }
            allKnowledgeRows.removeAll { $0.id == id }
            nextSourceOffset = nil
            if let cached = cachedKnowledge[id] {
                for evidenceID in cached.evidence.keys {
                    textReaderStore.remove(id: "evidence:\(evidenceID)")
                }
            }
            if loadingSourceID == id {
                sourceLoadGeneration = UUID()
                loadingSourceID = ""
            }
            if selectedSource?.id == id {
                for revision in sourceRevisions {
                    for evidence in revision.evidence {
                        textReaderStore.remove(id: "evidence:\(evidence.id)")
                    }
                }
                selectedSource = nil
                sourceRevisions = []
                selectedEvidence = nil
                supplementDraft = ""
            }
            readingSessionStore?.forget(sourceID: id)
            cachedKnowledge.removeValue(forKey: id)
            knowledgeCacheOrder.removeAll { $0 == id }
            outdatedSourceIDs.remove(id)
            delayedWebReadIDs.remove(id)
            textReaderStore.remove(id: id)
            textReaderStore.remove(id: "note:\(id)")
            forgetPDF(id)
            browseBusy = false
            browseMessage = usesLocalService ? "资料已移到回收站，可在设置中恢复。" : "资料已删除"
            return true
        } catch {
            browseMessage = "删除失败：\(error.localizedDescription)"
            return false
        }
    }

    func rememberKnowledge(_ detail: KnowledgeDetail, revisions: [RevisionItem]) {
        cachedKnowledge[detail.id] = CachedKnowledge(detail: detail, revisions: revisions)
        touchKnowledge(detail.id)
        outdatedSourceIDs.remove(detail.id)
    }

    private func touchKnowledge(_ id: String) {
        knowledgeCacheOrder.removeAll { $0 == id }
        knowledgeCacheOrder.append(id)
        while knowledgeCacheOrder.count > 30 {
            let removed = knowledgeCacheOrder.removeFirst()
            cachedKnowledge.removeValue(forKey: removed)
            outdatedSourceIDs.remove(removed)
        }
    }

    private func observeRows(_ rows: [KnowledgeRow]) {
        for row in rows {
            guard let cached = cachedKnowledge[row.id] else { continue }
            if (row.revision ?? -1) > (cached.detail.revision ?? -1)
                || (row.contextVersion ?? -1) > cached.detail.contextVersion {
                outdatedSourceIDs.insert(row.id)
            }
        }
    }

    func checkCachedSourceUpdates() async {
        guard !trashBusy, !backupBusy else { return }
        guard connected, !cachedKnowledge.isEmpty else { return }
        let checkedAddress = address
        let checkedToken = token
        let ids = Array(cachedKnowledge.keys)
        do {
            let result = try await API(address: checkedAddress, token: checkedToken).call(
                "v1/sources/versions", method: "POST", json: ["source_ids": ids])
            guard connected, address == checkedAddress, token == checkedToken else { return }
            for item in result["items"] as? [[String: Any]] ?? [] {
                guard let id = item["source_id"] as? String,
                      let cached = cachedKnowledge[id] else { continue }
                let revision = item["revision"] as? Int
                let contextVersion = item["context_version"] as? Int ?? 0
                if (revision ?? -1) > (cached.detail.revision ?? -1)
                    || contextVersion > cached.detail.contextVersion {
                    outdatedSourceIDs.insert(id)
                }
            }
        } catch {
            // A failed metadata check must not discard readable cached content.
        }
    }

    func loadRecent() async {
        guard !trashBusy, !backupBusy else { return }
        guard connected else { browseMessage = "请先连接后台"; return }
        let generation = UUID()
        sourceListGeneration = generation
        browseBusy = true
        defer { if sourceListGeneration == generation { browseBusy = false } }
        do {
            var query = [URLQueryItem(name: "limit", value: "50")]
            if !searchDomainID.isEmpty { query.append(URLQueryItem(name: "domain_id", value: searchDomainID)) }
            if !searchProjectID.isEmpty { query.append(URLQueryItem(name: "project_id", value: searchProjectID)) }
            let result = try await API(address: address, token: token).call("v1/sources", query: query)
            guard sourceListGeneration == generation else { return }
            let rows = (result["items"] as? [[String: Any]] ?? []).compactMap { KnowledgeRow($0, searchResult: false) }
            observeRows(rows)
            allKnowledgeRows = rows
            knowledgeRows = rows
            nextSourceOffset = result["next_offset"] as? Int
            browseMessage = knowledgeRows.isEmpty ? "还没有资料；可先到“采集”保存一条笔记。" : "最近资料：\(knowledgeRows.count) 条"
        } catch {
            if sourceListGeneration == generation { browseMessage = error.localizedDescription }
        }
    }

    func loadAllSources() async {
        guard !trashBusy, !backupBusy else { return }
        guard connected else { browseMessage = "请先连接后台"; return }
        let generation = UUID()
        sourceListGeneration = generation
        browseBusy = true
        defer { if sourceListGeneration == generation { browseBusy = false } }
        do {
            var offset = 0
            var rows: [KnowledgeRow] = []
            repeat {
                var query = [
                    URLQueryItem(name: "limit", value: "200"),
                    URLQueryItem(name: "offset", value: String(offset)),
                ]
                if !searchDomainID.isEmpty { query.append(URLQueryItem(name: "domain_id", value: searchDomainID)) }
                if !searchProjectID.isEmpty { query.append(URLQueryItem(name: "project_id", value: searchProjectID)) }
                let result = try await API(address: address, token: token).call("v1/sources", query: query)
                guard sourceListGeneration == generation else { return }
                rows.append(contentsOf: (result["items"] as? [[String: Any]] ?? [])
                    .compactMap { KnowledgeRow($0, searchResult: false) })
                observeRows(rows)
                allKnowledgeRows = rows
                knowledgeRows = rows
                guard let next = result["next_offset"] as? Int else { break }
                offset = next
            } while true
            nextSourceOffset = nil
            browseMessage = "全部资料：\(rows.count) 条"
        } catch {
            if sourceListGeneration == generation { browseMessage = error.localizedDescription }
        }
    }

    func loadMoreSources() async {
        guard !trashBusy, !backupBusy else { return }
        guard connected, let offset = nextSourceOffset, !browseBusy else { return }
        let generation = sourceListGeneration
        browseBusy = true
        defer { if sourceListGeneration == generation { browseBusy = false } }
        do {
            var query = [URLQueryItem(name: "limit", value: "50"),
                         URLQueryItem(name: "offset", value: String(offset))]
            if !searchDomainID.isEmpty { query.append(URLQueryItem(name: "domain_id", value: searchDomainID)) }
            if !searchProjectID.isEmpty { query.append(URLQueryItem(name: "project_id", value: searchProjectID)) }
            let result = try await API(address: address, token: token).call("v1/sources", query: query)
            guard sourceListGeneration == generation else { return }
            let rows = (result["items"] as? [[String: Any]] ?? []).compactMap { KnowledgeRow($0, searchResult: false) }
            observeRows(rows)
            knowledgeRows.append(contentsOf: rows)
            nextSourceOffset = result["next_offset"] as? Int
            browseMessage = "已加载 \(knowledgeRows.count) 条资料"
        } catch {
            if sourceListGeneration == generation { browseMessage = error.localizedDescription }
        }
    }

    func searchKnowledge() async {
        guard !trashBusy, !backupBusy else { return }
        let query = searchQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else { await loadRecent(); return }
        guard connected else { browseMessage = "请先连接后台"; return }
        let generation = UUID()
        sourceListGeneration = generation
        browseBusy = true
        defer { if sourceListGeneration == generation { browseBusy = false } }
        do {
            var request: [String: Any] = ["query": query, "limit": 20]
            if !searchDomainID.isEmpty { request["domain_ids"] = [searchDomainID] }
            if !searchProjectID.isEmpty { request["project_id"] = searchProjectID }
            let tagQuery = searchTagQuery.trimmingCharacters(in: .whitespacesAndNewlines)
            if !tagQuery.isEmpty { request["tag_query"] = tagQuery }
            let result = try await API(address: address, token: token).call("v1/search", method: "POST", json: request)
            guard sourceListGeneration == generation else { return }
            knowledgeRows = (result["items"] as? [[String: Any]] ?? []).compactMap { KnowledgeRow($0, searchResult: true) }
            observeRows(knowledgeRows)
            nextSourceOffset = nil
            let indexState = result["semantic_index"] as? String
            let baseMessage = knowledgeRows.isEmpty ? "没有找到匹配的资料" : "找到 \(knowledgeRows.count) 条；最多显示 20 条"
            let summaryState = result["summary_index"] as? String
            let summaryNote = summaryState == "pending" ? "；总结索引仍在建立" : summaryState == "failed" ? "；总结索引失败，可重建" : ""
            browseMessage = baseMessage + summaryNote + (indexState == "pending" ? "；语义索引仍在建立" :
                                            indexState == "failed" ? "；语义索引失败，请检查 Worker" : "")
        } catch {
            if sourceListGeneration == generation { browseMessage = error.localizedDescription }
        }
    }

    func openSource(_ id: String, force: Bool = false) async {
        guard !trashBusy, !backupBusy else { return }
        guard connected else { browseMessage = "请先连接后台"; return }
        guard force || selectedSource?.id != id else { return }
        guard loadingSourceID != id else { return }
        if !force, let cached = cachedKnowledge[id] {
            sourceLoadGeneration = UUID()
            loadingSourceID = ""
            browseBusy = false
            touchKnowledge(id)
            selectedSource = cached.detail
            sourceRevisions = cached.revisions
            selectedEvidence = cached.selectedEvidenceID.flatMap { cached.evidence[$0] }
            browseMessage = "已打开：\(cached.detail.title)"
            return
        }
        let generation = UUID()
        sourceLoadGeneration = generation
        loadingSourceID = id
        browseBusy = true
        defer {
            if sourceLoadGeneration == generation {
                browseBusy = false
                loadingSourceID = ""
            }
        }
        do {
            if !force || cachedKnowledge[id] == nil {
                selectedSource = nil
                sourceRevisions = []
                selectedEvidence = nil
            }
            try await fetchSource(id, generation: generation)
        } catch {
            if sourceLoadGeneration == generation { browseMessage = error.localizedDescription }
        }
    }

    func fetchSource(_ id: String, generation: UUID? = nil,
                             expectedConnection: (address: String, token: String)? = nil) async throws {
        let api = try API(address: address, token: token)
        let detailResponse = try await api.call("v1/sources/\(id)")
        let revisionsResponse = try await api.call("v1/sources/\(id)/revisions")
        if let generation, generation != sourceLoadGeneration { return }
        if let expectedConnection {
            guard connected, address == expectedConnection.address, token == expectedConnection.token else { return }
        }
        guard let detail = KnowledgeDetail(detailResponse) else { throw failure("资料详情响应无效") }
        let revisions = (revisionsResponse["items"] as? [[String: Any]] ?? []).compactMap(RevisionItem.init)
        rememberKnowledge(detail, revisions: revisions)
        selectedSource = detail
        sourceRevisions = revisions
        selectedEvidence = nil
        browseMessage = "已打开：\(detail.title)"
    }

    func pollSelectedWebRead(_ id: String) async {
        guard !trashBusy, !backupBusy else { return }
        guard selectedSource?.id == id, selectedSource?.kind == .link,
              ["pending", "processing"].contains(selectedSource?.processingStatus ?? "") else { return }
        let started = selectedSource?.webReadCreatedAt ?? Date()
        while !Task.isCancelled && selectedSource?.id == id {
            if Date().timeIntervalSince(started) > 120 { delayedWebReadIDs.insert(id) }
            do { try await Task.sleep(for: .seconds(2)) } catch { return }
            guard selectedSource?.id == id else { return }
            do {
                let status = try await API(address: address, token: token).call("v1/sources/\(id)")
                let state = status["processing_status"] as? String ?? ""
                if !["pending", "processing"].contains(state) {
                    guard selectedSource?.id == id else { return }
                    let generation = sourceLoadGeneration
                    await loadRecent()
                    guard selectedSource?.id == id else { return }
                    delayedWebReadIDs.remove(id)
                    try await fetchSource(id, generation: generation)
                    return
                }
            } catch {
                // Network recovery should not discard the URL or stop the reading indicator.
            }
        }
    }

    func retryWebRead(_ id: String) async {
        guard !trashBusy, !backupBusy else { return }
        do {
            _ = try await API(address: address, token: token).call(
                "v1/sources/\(id)/web-read", method: "POST")
            guard selectedSource?.id == id else { return }
            await openSource(id, force: true)
            await loadRecent()
        } catch { browseMessage = error.localizedDescription }
    }

    func openEvidence(_ id: String) async {
        guard !trashBusy, !backupBusy else { return }
        guard connected else { browseMessage = "请先连接后台"; return }
        guard let sourceID = selectedSource?.id else { return }
        if var cached = cachedKnowledge[sourceID], let evidence = cached.evidence[id] {
            cached.selectedEvidenceID = id
            cachedKnowledge[sourceID] = cached
            selectedEvidence = evidence
            return
        }
        browseBusy = true
        defer { browseBusy = false }
        do {
            let result = try await API(address: address, token: token).call("v1/evidence/\(id)")
            guard let evidence = EvidenceDetail(result) else { throw failure("证据响应无效") }
            guard selectedSource?.id == sourceID else { return }
            if var cached = cachedKnowledge[sourceID] {
                cached.evidence[id] = evidence
                cached.selectedEvidenceID = id
                cachedKnowledge[sourceID] = cached
            }
            selectedEvidence = evidence
        } catch { browseMessage = error.localizedDescription }
    }

    func addSupplement() async {
        guard !trashBusy, !backupBusy else { return }
        guard connected, let id = selectedSource?.id else { browseMessage = "请先打开资料"; return }
        let text = supplementDraft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { browseMessage = "请填写补充内容"; return }
        guard text.count <= 102400 else { browseMessage = "补充内容不能超过 102400 字符"; return }
        browseBusy = true
        defer { browseBusy = false }
        do {
            _ = try await API(address: address, token: token).call(
                "v1/sources/\(id)/supplements", method: "POST", json: ["text": text])
            supplementDraft = ""
            try await fetchSource(id)
            if searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                await loadRecent()
            } else {
                await searchKnowledge()
            }
            browseMessage = "补充已保存为新版本；旧证据仍可打开。"
        } catch { browseMessage = error.localizedDescription }
    }

    @discardableResult
    func saveSourceRevision(text: String) async -> Bool {
        guard !trashBusy, !backupBusy else { return false }
        guard connected, let id = selectedSource?.id else { browseMessage = "请先打开资料"; return false }
        browseBusy = true
        defer { browseBusy = false }
        do {
            _ = try await API(address: address, token: token).call(
                "v1/sources/\(id)/revisions", method: "POST", json: ["text": text])
            try await fetchSource(id)
            if searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                await loadRecent()
            } else {
                await searchKnowledge()
            }
            browseMessage = "内容已保存为新版本；旧证据与历史版本仍可随时查看。"
            return true
        } catch {
            browseMessage = "保存失败: \(error.localizedDescription)"
            return false
        }
    }

    func reprocessPDF() async {
        guard !trashBusy, !backupBusy else { return }
        guard connected, let id = selectedSource?.id else { browseMessage = "请先打开 PDF"; return }
        browseBusy = true
        defer { browseBusy = false }
        do {
            _ = try await API(address: address, token: token).call(
                "v1/sources/\(id)/ocr", method: "POST")
            try await fetchSource(id)
            browseMessage = "已提交重新扫描，稍后点击刷新查看结果。"
        } catch { browseMessage = error.localizedDescription }
    }

    func retryDocumentParse() async {
        guard !trashBusy, !backupBusy else { return }
        guard connected, !browseBusy, let detail = selectedSource,
              !detail.isPDF, detail.documentParseStatus == "failed" else { return }
        let generation = UUID()
        sourceLoadGeneration = generation
        let listGeneration = sourceListGeneration
        let connection = (address: address, token: token)
        browseBusy = true
        defer {
            if sourceLoadGeneration == generation && sourceListGeneration == listGeneration { browseBusy = false }
        }
        do {
            _ = try await API(address: address, token: token).call(
                "v1/sources/\(detail.id)/document-parse", method: "POST")
            guard sourceLoadGeneration == generation, sourceListGeneration == listGeneration,
                  connected, address == connection.address, token == connection.token else { return }
            try await fetchSource(detail.id, generation: generation, expectedConnection: connection)
            guard sourceLoadGeneration == generation, sourceListGeneration == listGeneration,
                  connected, address == connection.address, token == connection.token else { return }
            browseMessage = "已提交重新解析，已有版本和用户补充保留；稍后点击刷新查看结果。"
        } catch {
            if sourceLoadGeneration == generation && sourceListGeneration == listGeneration &&
                connected && address == connection.address && token == connection.token {
                browseMessage = error.localizedDescription
            }
        }
    }
}

struct KnowledgeView: View {
    @ObservedObject var model: CaptureModel
    @Binding var showSettings: Bool
    enum ReaderDisplayMode: String, CaseIterable {
        case reading = "阅读"
        case editing = "编辑"
    }
    @State private var readerMode: ReaderDisplayMode = .reading
    @State private var editingDraft = ""
    @State private var editingSourceID: String?
    @State private var isSavingRevision = false
    @State private var readingSize: CGFloat = 17
    @State private var showEvidenceSheet = false
    @State private var crawl4aiMarkdown: String?
    @State private var showImages = false
    @State private var selectedImageID: String?
    @State private var crawl4aiMarkdownRevision: Int?
    @State private var crawl4aiMarkdownSourceID = ""
    @State private var crawl4aiMarkdownError: String?
    @State private var crawl4aiMarkdownOrigin: String?
    @State private var showCrawl4AIMarkdown = false
    @State private var showSupplement = false
    @State private var selectedVersion: Int?
    @State private var pdfDocument: PDFDocument?
    @State private var pdfDocumentSourceID: String?
    @State private var pdfLoadError: String?
    @State private var pdfPageRequest: PDFPageRequest?
    @State private var currentPDFPage = 1
    @State private var searchPDFTarget: (sourceID: String, page: Int)?
    @State private var textReaderRequest: TextReaderRequest?
    @State private var showOriginal = false
    @State private var showDomainCreator = false
    @State private var showProjectCreator = false
    @State private var showVaultSwitcher = false
    @State private var showDomainManager = false
    @State private var browseMode: KnowledgeBrowseMode = .title
    @State private var selectedTag: String?
    @State private var aiAnalysisButtonFrame: CGRect = .zero
    @State private var aiOverview = AIOverviewPresentation()
    @Environment(\.accessibilityReduceMotion) private var reduceOverviewMotion
    @State private var showAIAnalysis = false
    @State private var showDomainOverview = false
    @State private var showRAG = false
    @State private var showTags = false
    @AppStorage("readerSidebarCollapsed") private var sidebarCollapsed = false
    @AppStorage("readerSidebarWidth") private var sidebarWidth = 248.0
    @State private var sidebarDragStart: Double?
    @State private var showEvidencePanel = false
    @State private var pendingDeleteRow: KnowledgeRow?
    @State private var showDeleteConfirmation = false
    @State private var deletingSourceID: String?
    @State private var hoveredSourceID: String?
    @State private var showSearch = false
    @State private var searchText = ""
    @State private var searchScope: KnowledgeSearchScope = .library
    @State private var searchResults: [KnowledgeRow] = []
    @State private var summaryIndexState: String?
    @State private var searchLoading = false
    @State private var currentMatches: [LocalSearchMatch] = []
    @State private var searchError: String?
    @State private var searchSelection = 0

    var body: some View {
        readerContent.onChange(of: model.articleNavigationID) { _, _ in
            showAIAnalysis = false; showDomainOverview = false; showDomainManager = false
            showRAG = false; showTags = false; showEvidenceSheet = false
            showImages = false; showCrawl4AIMarkdown = false; showSearch = false
        }
        .task(id: model.selectedSource.map { "\($0.id):\($0.images.contains(where: \.isDownloading))" } ?? "") {
            if let id = model.selectedSource?.id { await model.pollSelectedWebImages(id) }
        }
    }

    @ViewBuilder private var imageGallery: some View {
        if let detail = model.selectedSource {
            FeishuImageGallery(sourceID: detail.id, images: detail.images,
                address: model.address, token: model.token, initialImageID: selectedImageID,
                onRetry: { id in try await model.retryWebImages(sourceID: detail.id, imageID: id) }).id(detail.id)
        }
    }

    private var readerContent: some View {
        GeometryReader { geometry in
            HStack(spacing: 0) {
                if !sidebarCollapsed {
                    navigation.frame(width: ReaderSidebarLayout.width(sidebarWidth))
                    sidebarResizeHandle
                }
                reader(compact: geometry.size.width < 1120)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                if showEvidencePanel && geometry.size.width >= 1120 {
                    Rectangle().fill(InboxStyle.separator).frame(width: 1)
                    evidencePanel(isSheet: false).frame(width: 290)
                }
            }
            .background(InboxStyle.canvas)
            .sheet(isPresented: $showAIAnalysis) { LocalAIView(model: model).buttonStyle(InboxButtonStyle(.secondary)) }
            .sheet(isPresented: $showDomainOverview) {
                LocalDomainOverviewView(model: model, domainID: model.domainID)
                    .buttonStyle(InboxButtonStyle(.secondary))
            }
            .sheet(isPresented: $showDomainManager) {
                VStack {
                    HStack { Spacer(); Button("完成") { showDomainManager = false }.disabled(model.busy) }
                    ScrollView { DomainManagerView(model: model) }
                }.padding(24).frame(width: 540, height: 520)
                    .buttonStyle(InboxButtonStyle(.secondary))
                    .interactiveDismissDisabled(model.busy)
            }
            .sheet(isPresented: $showRAG) { LocalRAGView(model: model).buttonStyle(InboxButtonStyle(.secondary)) }
            .sheet(isPresented: $showTags) { LocalSourceTagsView(model: model).buttonStyle(InboxButtonStyle(.secondary)) }
            .sheet(isPresented: $showEvidenceSheet) {
                evidencePanel(isSheet: true).frame(width: 340, height: 610)
                    .buttonStyle(InboxButtonStyle(.secondary))
            }
            .sheet(isPresented: $showImages) { imageGallery.buttonStyle(InboxButtonStyle(.secondary)) }
            .sheet(isPresented: $showCrawl4AIMarkdown) {
                VStack(alignment: .leading, spacing: 14) {
                    HStack {
                        Text(crawl4aiMarkdownOrigin == "html_fallback"
                             ? "从网页正文生成的 Markdown" : crawl4aiMarkdownOrigin == "feishu_cli"
                             ? "飞书文档 Markdown" : crawl4aiMarkdownOrigin == "wechat_ua"
                             ? "微信公众号 Markdown" : "Crawl4AI Markdown").font(.headline)
                        Spacer()
                        Button("完成") { showCrawl4AIMarkdown = false }
                    }
                    if let error = crawl4aiMarkdownError {
                        Text(error).foregroundStyle(.orange)
                    } else if let markdown = crawl4aiMarkdown {
                        TextReader(store: model.textReaderStore,
                                   sourceID: "crawl4ai:\(crawl4aiMarkdownSourceID)",
                                   revision: crawl4aiMarkdownRevision, text: markdown, size: 15, request: nil)
                    } else {
                        ProgressView("正在读取 Markdown…")
                    }
                }
                .padding(24).frame(width: 760, height: 620)
                .buttonStyle(InboxButtonStyle(.secondary))
            }
            .overlay(alignment: .top) {
                ZStack(alignment: .top) {
                    if showSearch {
                        Color.black.opacity(0.001).contentShape(Rectangle())
                            .onTapGesture { closeSearch() }.transition(.opacity)
                        searchPanel.frame(width: min(660, geometry.size.width - 40))
                            .padding(.top, 64).transition(.opacity)
                    }
                }
                .animation(reduceOverviewMotion ? nil : .smooth(duration: 0.18), value: showSearch)
            }
        }
        .background {
            HStack {
                Button("搜索当前内容") { openSearch(scope: .current) }
                    .keyboardShortcut("f", modifiers: .command)
                Button("搜索当前知识库") { openSearch(scope: .library) }
                    .keyboardShortcut("f", modifiers: [.command, .shift])
            }.frame(width: 0, height: 0).clipped().accessibilityHidden(true)
        }
        .onChange(of: showSettings) { _, visible in if visible { closeSearch() } }
        .task {
            if model.connected {
                await model.loadRecent()
                await model.restoreLastReadingSession()
                if !Task.isCancelled, model.selectedSource == nil, model.loadingSourceID.isEmpty,
                   model.pendingArticleLink == nil, model.articleLinkInFlight == nil,
                   let first = model.knowledgeRows.first {
                    await model.openSource(first.id)
                }
            }
        }
        .onChange(of: model.connected) { _, connected in
            if !connected { showDomainOverview = false }
            summaryIndexState = nil
            if connected { Task { await model.loadRecent() } }
        }
        .onChange(of: model.address) { _, _ in summaryIndexState = nil }
        .onChange(of: model.token) { _, _ in summaryIndexState = nil }
        .task(id: model.connected) {
            guard model.connected else { return }
            while !Task.isCancelled && model.connected {
                await model.checkCachedSourceUpdates()
                try? await Task.sleep(nanoseconds: 20_000_000_000)
            }
        }
        .onChange(of: model.sourceRevisions.map(\.version)) { _, _ in
            selectedVersion = model.sourceRevisions.first?.version
        }
        .onChange(of: model.selectedSource?.id) { _, _ in
            if let detail = model.selectedSource {
                editingSourceID = detail.id
                editingDraft = detail.text
                readerMode = .reading
            }
        }
        .task(id: searchRequestID) { await performSearch() }
        .onChange(of: showOriginal) { _, isOriginal in
            if let detail = model.selectedSource, detail.isPDF {
                model.readingSessionStore?.setPDFOriginal(isOriginal, sourceID: detail.id)
            }
            if isOriginal, pdfDocument != nil { navigatePDF(to: currentPDFPage) }
        }
        .task(id: model.selectedSource?.id) { await prepareSelectedPDF() }
        .task(id: model.selectedSource.map { "\($0.id):\($0.processingStatus)" } ?? "") {
            if let id = model.selectedSource?.id { await model.pollSelectedWebRead(id) }
        }
        .confirmationDialog("删除这条资料？", isPresented: $showDeleteConfirmation,
                            titleVisibility: .visible) {
            Button("删除", role: .destructive) {
                guard let row = pendingDeleteRow else { return }
                pendingDeleteRow = nil
                Task { await deleteSource(row) }
            }
            Button("取消", role: .cancel) { pendingDeleteRow = nil }
        } message: {
            Text(model.usesLocalService ? "资料将移到本地回收站，可在设置的回收站中恢复。只有永久删除才会清理原件和关联内容。" : "删除后将从知识库移除，后台会清理关联内容。")
        }
    }

    private func openCrawl4AIMarkdown(_ sourceID: String) async {
        crawl4aiMarkdownSourceID = sourceID
        crawl4aiMarkdown = nil
        crawl4aiMarkdownRevision = nil
        crawl4aiMarkdownError = nil
        crawl4aiMarkdownOrigin = nil
        showCrawl4AIMarkdown = true
        do {
            let result = try await API(address: model.address, token: model.token)
                .call("v1/sources/\(sourceID)/crawl4ai-markdown")
            guard showCrawl4AIMarkdown, model.selectedSource?.id == sourceID else { return }
            crawl4aiMarkdown = result["markdown"] as? String ?? ""
            crawl4aiMarkdownRevision = result["revision"] as? Int
            crawl4aiMarkdownOrigin = result["origin"] as? String
        } catch {
            guard showCrawl4AIMarkdown else { return }
            crawl4aiMarkdownError = error.localizedDescription
        }
    }

    private var navigation: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                ForEach(KnowledgeBrowseMode.allCases, id: \.self) { mode in
                    Button { switchMode(mode) } label: {
                        Image(systemName: mode.symbol)
                    }
                    .buttonStyle(InboxIconButtonStyle(selected: browseMode == mode))
                    .help(mode.title)
                    .accessibilityLabel(mode.title)
                }
                Spacer(minLength: 0)
                Button { openSearch(scope: .library) } label: {
                    Image(systemName: "magnifyingglass")
                }
                .buttonStyle(InboxIconButtonStyle())
                .help("搜索当前知识库（⌘⇧F）")
                .accessibilityLabel("搜索")
            }
            .padding(.horizontal, 14).frame(height: 56)
            if model.usesLocalService {
                Button { showDomainOverview = true } label: {
                    Label("知识库概览", systemImage: "square.grid.2x2")
                        .font(.system(size: 13, weight: .medium))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 18).padding(.vertical, 9)
                }
                .buttonStyle(.plain)
                .disabled(!model.connected || model.domainID.isEmpty)
                .help("查看当前知识库“\(activeVaultName)”的概览、主题和加工状态")
            }
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 7) {
                    Eyebrow(title: browseMode.title)
                        .padding(.horizontal, 18).padding(.top, 17).padding(.bottom, 5)
                    if browseMode == .title {
                        ForEach(model.knowledgeRows) { sourceButton($0, indented: false) }
                    } else if browseMode == .date {
                        ForEach(dateGroups, id: \.date) { group in
                            Text(DateFormatter.localizedString(from: group.date, dateStyle: .medium, timeStyle: .none))
                                .font(.caption).foregroundStyle(.secondary)
                                .padding(.horizontal, 18).padding(.top, 12)
                            ForEach(group.rows) { sourceButton($0, indented: false) }
                        }
                    } else {
                        ForEach(tagGroups, id: \.name) { group in
                            Button {
                                selectedTag = selectedTag == group.name ? nil : group.name
                            } label: {
                                HStack {
                                    Image(systemName: "tag").frame(width: 16)
                                    Text(group.name).lineLimit(1)
                                    Spacer()
                                    Text("\(group.rows.count)")
                                        .font(.caption).foregroundStyle(.secondary)
                                }
                                .font(.system(size: 13))
                                .padding(.horizontal, 12).frame(height: 34)
                                .background(selectedTag == group.name ? InboxStyle.elevated : .clear,
                                            in: RoundedRectangle(cornerRadius: 8))
                            }.buttonStyle(.plain).padding(.horizontal, 9)
                            if selectedTag == group.name {
                                ForEach(group.rows) {
                                    sourceButton($0, indented: true)
                                }
                            }
                        }
                        if tagGroups.isEmpty {
                            Text("暂无标签；新资料保存后会自动提取关键词。")
                                .font(.caption).foregroundStyle(.secondary).padding(.horizontal, 18)
                        }
                    }
                    if model.knowledgeRows.isEmpty && browseMode != .tag {
                        Text("这里还没有资料。")
                            .font(.caption).foregroundStyle(.secondary).padding(.horizontal, 18)
                    }
                    if browseMode == .title && model.nextSourceOffset != nil {
                        Button("加载更多") { Task { await model.loadMoreSources() } }
                            .buttonStyle(.plain).foregroundStyle(InboxStyle.accent)
                            .padding(.horizontal, 18).padding(.top, 6)
                            .disabled(model.browseBusy)
                    }
                }.padding(.bottom, 20)
            }
            Divider()
            knowledgeVault
        }
        .background(InboxStyle.surface)
        .popover(isPresented: $showDomainCreator) {
            VStack(alignment: .leading, spacing: 10) {
                Text("新知识域").font(.headline)
                TextField("名称", text: $model.domainName)
                Button("创建知识域") {
                    Task {
                        await model.createDomain()
                        if model.domainName.isEmpty {
                            showDomainCreator = false
                            browseMode = .title
                            model.searchDomainID = model.domainID
                            model.searchProjectID = ""
                            await model.loadRecent()
                        }
                    }
                }
                .disabled(model.domainName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
                Text(model.message).font(.caption).foregroundStyle(.secondary)
            }.padding(16).frame(width: 260)
                .buttonStyle(InboxButtonStyle(.secondary))
        }
        .popover(isPresented: $showProjectCreator) {
            VStack(alignment: .leading, spacing: 10) {
                Text("新项目").font(.headline)
                TextField("名称", text: $model.projectName)
                Button("创建项目") {
                    Task {
                        await model.createProject()
                        if model.projectName.isEmpty { showProjectCreator = false }
                    }
                }
                .disabled(model.projectName.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.busy)
                Text(model.message).font(.caption).foregroundStyle(.secondary)
            }.padding(16).frame(width: 260)
                .buttonStyle(InboxButtonStyle(.secondary))
        }
    }

    private var dateGroups: [(date: Date, rows: [KnowledgeRow])] {
        let groups = Dictionary(grouping: model.knowledgeRows.compactMap { row -> (Date, KnowledgeRow)? in
            guard let date = row.createdAt else { return nil }
            return (Calendar.current.startOfDay(for: date), row)
        }, by: { $0.0 })
        return groups.keys.sorted(by: >).map { date in
            (date, groups[date, default: []].map(\.1))
        }
    }

    private var tagGroups: [(name: String, rows: [KnowledgeRow])] {
        var groups: [String: [KnowledgeRow]] = [:]
        for row in model.knowledgeRows {
            for tag in row.tags { groups[tag, default: []].append(row) }
        }
        return groups.keys.sorted().map { ($0, groups[$0, default: []]) }
    }

    private func switchMode(_ mode: KnowledgeBrowseMode) {
        guard browseMode != mode else { return }
        browseMode = mode
        selectedTag = nil
        model.searchQuery = ""
        Task { if mode == .title { await model.loadRecent() } else { await model.loadAllSources() } }
    }

    private var activeVaultName: String {
        model.domains.first(where: { $0.id == model.domainID })?.name ?? "未选择知识库"
    }

    private var knowledgeVault: some View {
        Button { showVaultSwitcher.toggle() } label: {
            HStack(spacing: 10) {
                Image(systemName: "chevron.up.chevron.down")
                    .font(.system(size: 13, weight: .semibold)).foregroundStyle(.secondary)
                Text(activeVaultName).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                Spacer(minLength: 4)
                if model.browseBusy { ProgressView().controlSize(.mini) }
                else { Circle().fill(.green).frame(width: 7, height: 7) }
            }
            .padding(.horizontal, 15)
            .frame(maxWidth: .infinity, alignment: .leading)
            .frame(height: 36)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("当前知识库：\(activeVaultName)。点击切换")
        .accessibilityLabel("当前知识库：\(activeVaultName)")
        .popover(isPresented: $showVaultSwitcher, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: 0) {
                Text("切换知识库")
                    .font(.caption).foregroundStyle(.secondary)
                    .padding(.horizontal, 13).padding(.top, 12).padding(.bottom, 6)
                ScrollView {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("当前知识库").font(.caption2).foregroundStyle(.secondary)
                            .padding(.horizontal, 9).padding(.top, 8)
                        ForEach(model.domains) { domain in
                            vaultOption(domain.name, symbol: "folder",
                                        selected: model.searchDomainID == domain.id && model.searchProjectID.isEmpty) {
                                browseMode = .title
                                model.domainID = domain.id
                                model.projectID = ""
                                model.searchDomainID = domain.id
                                model.searchProjectID = ""
                                Task { await model.loadRecent() }
                            }
                        }
                        if !model.projects.isEmpty {
                            Divider().padding(.vertical, 5)
                            Text("项目视图").font(.caption2).foregroundStyle(.secondary)
                                .padding(.horizontal, 13)
                            ForEach(model.projects) { project in
                                vaultOption(project.name, symbol: "square.stack",
                                            selected: model.searchProjectID == project.id) {
                                    browseMode = .title
                                    model.searchDomainID = ""
                                    model.searchProjectID = project.id
                                    Task { await model.loadRecent() }
                                }
                            }
                        }
                    }.padding(.horizontal, 7)
                }.frame(maxHeight: 290)
                Divider().padding(.top, 8)
                Button { showVaultSwitcher = false; showDomainCreator = true } label: {
                    Label("创建知识域", systemImage: "folder.badge.plus")
                }.buttonStyle(.plain).padding(.horizontal, 14).padding(.top, 12)
                Button { showVaultSwitcher = false; showProjectCreator = true } label: {
                    Label("创建项目", systemImage: "plus.square.on.square")
                }.buttonStyle(.plain).padding(.horizontal, 14).padding(.top, 10)
                Button { showVaultSwitcher = false; showDomainManager = true } label: {
                    Label("管理知识库…", systemImage: "gearshape")
                }
                .buttonStyle(.plain)
                .padding(.horizontal, 14).padding(.top, 10).padding(.bottom, 14)
            }
            .frame(width: 255)
        }
    }

    private func vaultOption(_ title: String, symbol: String, selected: Bool,
                             action: @escaping () -> Void) -> some View {
        Button {
            showVaultSwitcher = false
            action()
        } label: {
            HStack(spacing: 9) {
                Image(systemName: symbol).frame(width: 16)
                Text(title).lineLimit(1)
                Spacer()
                if selected { Image(systemName: "checkmark").foregroundStyle(InboxStyle.accent) }
            }
            .font(.system(size: 13))
            .padding(.horizontal, 8).frame(height: 30)
            .background(selected ? InboxStyle.elevated : .clear,
                        in: RoundedRectangle(cornerRadius: 6))
            .contentShape(Rectangle())
        }.buttonStyle(.plain)
    }


    private func prepareSelectedPDF() async {
        pdfLoadError = nil
        guard let detail = model.selectedSource, detail.isPDF else {
            showOriginal = false
            return
        }
        let saved = model.readingSessionStore?.current
        let savedPDF = saved?.sourceID == detail.id ? saved?.pdf : nil
        let explicitSearch = searchPDFTarget?.sourceID == detail.id || textReaderRequest?.sourceID == detail.id
        showOriginal = textReaderRequest?.sourceID != detail.id && (explicitSearch || saved?.pdfOriginal != false)
        let requestedPage = searchPDFTarget?.sourceID == detail.id ? searchPDFTarget?.page ?? 1 :
            model.cachedPDFPage(detail.id) ?? savedPDF?.page ?? 1
        do {
            if let cached = model.cachedPDF(detail.id) {
                pdfDocument = cached
                pdfDocumentSourceID = detail.id
                navigatePDF(to: max(1, min(cached.pageCount, requestedPage)))
                return
            }
            pdfDocument = nil
            pdfDocumentSourceID = nil
            pdfPageRequest = nil
            currentPDFPage = 1
            let file = model.pdfCacheDirectory.appendingPathComponent("\(detail.id).pdf")
            let localFile: URL
            if FileManager.default.fileExists(atPath: file.path) {
                localFile = file
            } else {
                localFile = try await API(address: model.address, token: model.token)
                    .downloadFile("v1/sources/\(detail.id)/file", into: model.pdfCacheDirectory,
                                  named: "\(detail.id).pdf")
            }
            let document = await Task.detached(priority: .userInitiated) {
                PDFDocument(url: localFile)
            }.value
            guard !Task.isCancelled, model.selectedSource?.id == detail.id else { return }
            pdfDocument = document
            pdfDocumentSourceID = document == nil ? nil : detail.id
            if let document {
                model.rememberPDF(document, id: detail.id)
                let page = max(1, min(document.pageCount, requestedPage))
                currentPDFPage = page
                pdfPageRequest = PDFPageRequest(page: page, position: explicitSearch ? nil : savedPDF)
            }
            if pdfDocument == nil { pdfLoadError = "原件无法解析，请切换文字视图。" }
        } catch {
            if !Task.isCancelled { pdfLoadError = error.localizedDescription }
        }
    }

    private var searchRequestID: [String] {
        [searchText, searchScope.rawValue, model.knowledgeSearchMode.rawValue,
         String(showSearch), model.domainID, model.selectedSource?.id ?? "",
         String(model.selectedSource?.revision ?? 0), model.address, model.token]
    }

    @MainActor private func performSearch() async {
            guard showSearch else { searchLoading = false; return }
            searchResults = []; currentMatches = []; searchError = nil
            summaryIndexState = nil; searchSelection = 0; searchLoading = false
            let query = searchText.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !query.isEmpty else { return }
            searchLoading = true
            defer { if !Task.isCancelled { searchLoading = false } }
            do { try await Task.sleep(for: .milliseconds(200)) } catch { return }
            guard !Task.isCancelled else { return }
            if usesLocalSearchMatches {
                guard let detail = model.selectedSource else { searchError = "请先打开一条资料"; return }
                currentMatches = SearchLocation.localMatches(in: detail.text, query: query)
                if !currentMatches.isEmpty { locateCurrentMatch(0) }
            } else {
                if searchScope == .current && model.selectedSource == nil { searchError = "请先打开一条资料"; return }
                guard !model.domainID.isEmpty else { searchError = "请先选择知识库"; return }
                do {
                    let address = model.address, token = model.token
                    let library = try? model.localService.currentLibrary()
                    var request: [String: Any] = ["query": query, "limit": 20,
                        "mode": model.knowledgeSearchMode.apiValue, "result_unit": "passage", "domain_ids": [model.domainID]]
                    if searchScope == .current { request["source_id"] = model.selectedSource?.id }
                    let result = try await API(address: address, token: token)
                        .call("v1/search", method: "POST", json: request)
                    guard !Task.isCancelled, showSearch, model.address == address, model.token == token,
                          !model.usesLocalService || (try? model.localService.currentLibrary()) == library else { return }
                    summaryIndexState = result["summary_index"] as? String
                    searchResults = (result["items"] as? [[String: Any]] ?? [])
                        .compactMap { KnowledgeRow($0, searchResult: true) }
                } catch { if !Task.isCancelled { searchError = error.localizedDescription } }
            }
    }

    private func openSearch(scope: KnowledgeSearchScope) {
        searchText = ""
        searchResults = []
        currentMatches = []
        searchError = nil
        searchSelection = 0
        searchScope = scope
        showSearch = true
    }

    private func closeSearch() {
        showSearch = false
    }

    private var usesLocalSearchMatches: Bool {
        searchScope == .current && model.knowledgeSearchMode == .exact
    }

    private var searchCount: Int {
        usesLocalSearchMatches ? currentMatches.count : searchResults.count
    }

    private func locateCurrentMatch(_ index: Int) {
        guard currentMatches.indices.contains(index), let detail = model.selectedSource else { return }
        let match = currentMatches[index]
        textReaderRequest = TextReaderRequest(sourceID: detail.id, revision: detail.revision, range: match.range)
        if detail.isPDF { showOriginal = false }
    }

    private func activateSearchResult(_ index: Int) {
        guard index >= 0 && index < searchCount else { return }
        if usesLocalSearchMatches {
            locateCurrentMatch(index)
        } else {
            let row = searchResults[index]
            let query = searchText
            searchPDFTarget = row.page.map { (sourceID: row.id, page: $0) }
            Task {
                await model.openSource(row.id)
                guard let detail = model.selectedSource, detail.id == row.id else { return }
                let useLocator = (row.revision == nil || row.revision == detail.revision) && (!detail.readingTextDiffers || row.matchText != nil)
                if let range = SearchLocation.resultRange(in: detail.text, query: query, snippet: row.snippet,
                                                          locatorKind: useLocator ? row.locatorKind : nil,
                                                          locatorStart: useLocator ? row.locatorStart : nil,
                                                          locatorEnd: useLocator ? row.locatorEnd : nil,
                                                          matchText: row.matchText) {
                    textReaderRequest = TextReaderRequest(sourceID: detail.id, revision: detail.revision,
                                                          range: range)
                    if detail.isPDF { showOriginal = false }
                } else if let page = row.page, detail.isPDF {
                    textReaderRequest = nil
                    showOriginal = true
                    navigatePDF(to: page)
                }
                if let evidenceID = row.evidenceID { await model.openEvidence(evidenceID) }
            }
        }
        closeSearch()
    }

    private var searchPanel: some View {
        KnowledgeSearchPanel(text: $searchText, mode: $model.knowledgeSearchMode, scope: searchScope,
            results: searchResults, matches: currentMatches, selection: $searchSelection,
            loading: searchLoading, message: searchError ?? (model.knowledgeSearchMode == .semantic ?
                (summaryIndexState == "pending" ? "总结索引仍在建立，结果可能不完整。" :
                 summaryIndexState == "failed" ? "总结索引暂不可用，当前仅显示其他检索结果。" : nil) : nil),
            activate: activateSearchResult, close: closeSearch)
    }

    private func sourceButton(_ row: KnowledgeRow, indented: Bool) -> some View {
        HStack(spacing: 0) {
            Button {
                if !row.isSearchResult { textReaderRequest = nil }
                searchPDFTarget = model.outdatedSourceIDs.contains(row.id) ? nil :
                    row.page.map { (sourceID: row.id, page: $0) }
                if model.selectedSource?.id != row.id, let cached = model.cachedPDF(row.id) {
                    pdfDocument = cached
                    pdfDocumentSourceID = row.id
                    showOriginal = true
                    navigatePDF(to: max(1, min(cached.pageCount,
                        row.page ?? model.cachedPDFPage(row.id) ?? 1)))
                }
                Task {
                    await model.openSource(row.id)
                    if row.isSearchResult, let detail = model.selectedSource, detail.id == row.id {
                        let useLocator = (row.revision == nil || row.revision == detail.revision) && (!detail.readingTextDiffers || row.matchText != nil)
                        if let range = SearchLocation.resultRange(in: detail.text, query: model.searchQuery,
                                                                  snippet: row.snippet,
                                                                  locatorKind: useLocator ? row.locatorKind : nil,
                                                                  locatorStart: useLocator ? row.locatorStart : nil,
                                                                  locatorEnd: useLocator ? row.locatorEnd : nil,
                                                          matchText: row.matchText) {
                            textReaderRequest = TextReaderRequest(sourceID: detail.id, revision: detail.revision,
                                                                  range: range)
                            if detail.isPDF { showOriginal = false }
                        } else if let page = row.page, detail.isPDF {
                            textReaderRequest = nil
                            showOriginal = true
                            navigatePDF(to: page)
                        }
                    }
                    if row.isSearchResult, row.revision == model.selectedSource?.revision,
                       !model.outdatedSourceIDs.contains(row.id),
                       let evidenceID = row.evidenceID {
                        await model.openEvidence(evidenceID)
                    }
                }
            } label: {
                HStack(spacing: 8) {
                    Image(systemName: row.kind.symbol).font(.caption).foregroundStyle(.secondary)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(row.title).lineLimit(1).font(.system(size: 13))
                        if row.isSearchResult || row.kind == .note && !row.snippet.isEmpty {
                            Text(row.snippet.replacingOccurrences(of: "\n", with: " "))
                                .lineLimit(row.isSearchResult ? 2 : 1)
                                .font(.caption2).foregroundStyle(.secondary)
                        }
                        if row.isSearchResult { SummarySearchMatchView(row: row) }
                    }
                    Spacer(minLength: 0)
                    if row.kind == .link && ["pending", "processing"].contains(row.processingStatus) {
                        if model.delayedWebReadIDs.contains(row.id) {
                            Image(systemName: "exclamationmark.circle")
                                .font(.caption).foregroundStyle(.orange)
                        } else {
                            ProgressView().controlSize(.mini)
                        }
                    }
                    if row.isSearchResult {
                        Text(row.matchType == "semantic" ? "语义" : row.matchType == "hybrid" ? "综合" : "关键词")
                            .font(.caption2).foregroundStyle(.secondary)
                    }
                }
                .frame(maxWidth: .infinity, minHeight: row.isSearchResult ? 54 :
                    row.kind == .note && !row.snippet.isEmpty ? 48 : 36)
                .contentShape(Rectangle())
            }.buttonStyle(.plain)
            if model.outdatedSourceIDs.contains(row.id) {
                Button {
                    Task { await model.openSource(row.id, force: true) }
                } label: {
                    Circle().stroke(.green, lineWidth: 2)
                        .frame(width: 10, height: 10)
                        .frame(width: 24, height: row.isSearchResult ? 54 :
                            row.kind == .note && !row.snippet.isEmpty ? 48 : 36)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("读取最新资料")
            }
        }
        .padding(.leading, indented ? 28 : 11).padding(.trailing, 8)
        .background(model.selectedSource?.id == row.id ? InboxStyle.hover : hoveredSourceID == row.id ? InboxStyle.elevated : .clear,
                    in: RoundedRectangle(cornerRadius: 7))
        .padding(.horizontal, 9)
        .onHover { hoveredSourceID = $0 ? row.id : nil }
        .contextMenu {
            Button(role: .destructive) {
                pendingDeleteRow = row
                showDeleteConfirmation = true
            } label: {
                Label(row.kind == .link ? "删除文章" : "删除资料", systemImage: "trash")
            }
            .disabled(deletingSourceID == row.id)
        }
    }

    private func deleteSource(_ row: KnowledgeRow) async {
        guard deletingSourceID == nil else { return }
        deletingSourceID = row.id
        defer { deletingSourceID = nil }
        guard await model.deleteSource(row.id) else { return }
        if pdfDocumentSourceID == row.id {
            pdfDocument = nil
            pdfDocumentSourceID = nil
            pdfPageRequest = nil
            currentPDFPage = 1
            showOriginal = false
        }
        if searchPDFTarget?.sourceID == row.id { searchPDFTarget = nil }
        if textReaderRequest?.sourceID == row.id { textReaderRequest = nil }
        selectedVersion = nil
        if browseMode == .title { await model.loadRecent() }
    }

    private var sidebarResizeHandle: some View {
        Rectangle().fill(.clear).frame(width: 8)
            .overlay { Rectangle().fill(InboxStyle.separator).frame(width: 1) }
            .contentShape(Rectangle())
            .onHover { NSCursor.resizeLeftRight.set(); if !$0 { NSCursor.arrow.set() } }
            .gesture(DragGesture(minimumDistance: 0, coordinateSpace: .global)
                .onChanged { value in
                    if sidebarDragStart == nil { sidebarDragStart = ReaderSidebarLayout.width(sidebarWidth) }
                    sidebarWidth = ReaderSidebarLayout.width((sidebarDragStart ?? sidebarWidth) + value.translation.width)
                }
                .onEnded { _ in sidebarDragStart = nil; NSCursor.arrow.set() })
            .onDisappear { NSCursor.arrow.set() }
            .accessibilityLabel("调整资料列表宽度")
            .accessibilityValue("\(Int(ReaderSidebarLayout.width(sidebarWidth))) 点")
            .accessibilityAdjustableAction { direction in
                switch direction {
                case .increment: sidebarWidth = ReaderSidebarLayout.width(sidebarWidth + 20)
                case .decrement: sidebarWidth = ReaderSidebarLayout.width(sidebarWidth - 20)
                @unknown default: break
                }
            }
    }

    private var aiOverviewIsPresented: Bool {
        guard aiOverview.isVisible, let sourceID = model.selectedSource?.id else { return false }
        let state = AIOverviewState(sourceID: sourceID, jobs: model.aiJobs, artifacts: model.aiArtifacts)
        return state.artifact != nil || state.job != nil || model.aiSourceReadPhase != .ready
    }

    private func readerToolbar(compact: Bool) -> some View {
        HStack(spacing: 10) {
            Button { sidebarCollapsed.toggle() } label: { Image(systemName: "sidebar.left") }
                .buttonStyle(InboxIconButtonStyle(selected: !sidebarCollapsed))
                .help(sidebarCollapsed ? "展开资料列表" : "收起资料列表")
                .accessibilityLabel(sidebarCollapsed ? "展开资料列表" : "收起资料列表")
            Text(model.selectedSource?.kind.label ?? "阅读器")
                .font(.system(size: 11, weight: .medium)).foregroundStyle(InboxStyle.accent)
                .padding(.horizontal, 7).padding(.vertical, 4)
                .background(InboxStyle.accent.opacity(0.08), in: RoundedRectangle(cornerRadius: 5))
                .fixedSize()
            Text(model.selectedSource?.title ?? "选择资料开始阅读")
                .font(.system(size: 18, weight: .semibold))
                .lineLimit(1).truncationMode(.middle)
                .help(model.selectedSource?.title ?? "")
                .frame(maxWidth: .infinity, alignment: .leading)
            if model.usesLocalService {
                Button { showRAG = true } label: { Image(systemName: "bubble.left.and.bubble.right") }
                    .buttonStyle(InboxIconButtonStyle())
                    .help("知识库问答").accessibilityLabel("知识库问答")
                    .disabled(model.backupBusy || model.trashBusy || !model.connected)
                if model.selectedSource != nil {
                    LocalAIAnalyzeButton(model: model, compact: true, showOverview: { aiOverview.toggle() }, showDetails: { showAIAnalysis = true })
                        .buttonStyle(InboxIconButtonStyle(selected: aiOverviewIsPresented))
                        .accessibilityAddTraits(aiOverviewIsPresented ? .isSelected : [])
                        .onGeometryChange(for: CGRect.self) { geometry in
                            geometry.frame(in: .named("knowledgeReader"))
                        } action: { aiAnalysisButtonFrame = $0 }
                        .disabled(model.backupBusy || model.trashBusy || !model.connected)
                }
            }
            if model.selectedSource?.isPDF == true {
                Picker("阅读方式", selection: $showOriginal) {
                    Text("原件").tag(true)
                    Text("文字").tag(false)
                }.pickerStyle(.segmented).labelsHidden().frame(width: 100)
            }
            if model.selectedSource != nil && (!showOriginal || model.selectedSource?.isPDF != true) {
                Picker("视图模式", selection: $readerMode) {
                    Text("阅读").tag(ReaderDisplayMode.reading)
                    Text("编辑").tag(ReaderDisplayMode.editing)
                }.pickerStyle(.segmented).labelsHidden().frame(width: 100)
                    .help("切换阅读模式与编辑模式")
            }
            Menu {
                if model.usesLocalService && model.selectedSource != nil {
                    Button("管理标签") { showTags = true }
                        .disabled(model.backupBusy || model.trashBusy || !model.connected)
                }
                if let detail = model.selectedSource {
                    Button("复制原文链接") {
                        guard let url = detail.sourceURL, !url.isEmpty else { return }
                        DesktopIntegration.shared.copyText(url)
                    }
                    .disabled(detail.sourceURL?.isEmpty != false)
                    Divider()

                    if !detail.images.isEmpty {
                        Button("查看图片（\(detail.images.count)）") { selectedImageID = nil; showImages = true }
                    }
                    if detail.webImagesCanArchive && detail.images.isEmpty {
                        Button("补齐文章图片") {
                            Task {
                                do { try await model.retryWebImages(sourceID: detail.id) }
                                catch { model.browseMessage = error.localizedDescription }
                            }
                        }.disabled(model.backupBusy || model.trashBusy || !model.connected)
                    }
                    if detail.kind == .link, detail.processingStatus == "usable",
                       detail.sourceURL != nil, !detail.isFeishuDocument {
                        Button("重新读取网页") { Task { await model.retryWebRead(detail.id) } }
                            .disabled(model.browseBusy)
                    }
                    if detail.crawl4aiMarkdownAvailable || detail.webReadEngine == "crawl4ai" {
                        Button("查看 Markdown") { Task { await openCrawl4AIMarkdown(detail.id) } }
                            .disabled(!detail.crawl4aiMarkdownAvailable)
                    }
                    if detail.isPDF {
                        Button("重新扫描 PDF") { Task { await model.reprocessPDF() } }
                            .disabled(model.browseBusy || detail.ocrStatus == "running" || detail.ocrStatus == "pending")
                    }
                    if detail.documentParseStatus == "cancelled",
                       detail.documentParseError == "已保留手动编辑，自动解析已停止；可手动重试解析" {
                        Button("重新解析文档") { Task { await model.retryDocumentParse() } }
                            .disabled(model.browseBusy)
                    }
                }
                if !showOriginal || model.selectedSource?.isPDF != true {
                    Divider()
                    Button("放大字号") { readingSize = min(24, readingSize + 1) }
                        .disabled(readingSize >= 24)
                    Button("缩小字号") { readingSize = max(14, readingSize - 1) }
                        .disabled(readingSize <= 14)
                }
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 16))
                    .foregroundStyle(InboxStyle.muted).frame(width: 28, height: 32)
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .help("更多阅读操作").accessibilityLabel("更多阅读操作")
            Button {
                if compact { showEvidenceSheet = true }
                else { showEvidencePanel.toggle() }
            } label: { Image(systemName: "sidebar.right") }
                .buttonStyle(InboxIconButtonStyle(selected: showEvidencePanel && !compact))
                .help(showEvidencePanel && !compact ? "收起证据与笔记" : "展开证据与笔记")
                .accessibilityLabel("证据与笔记")
        }.padding(.horizontal, 20).frame(height: 56)
    }

    private func reader(compact: Bool) -> some View {
        VStack(spacing: 0) {
            readerToolbar(compact: compact)
            Divider()
            if model.selectedSource?.isPDF == true, showOriginal,
               pdfDocumentSourceID == model.selectedSource?.id, let pdfDocument {
                HStack(spacing: 8) {
                    Text("PDF 原件").font(.caption).foregroundStyle(InboxStyle.muted)
                    Spacer()
                    Button { navigatePDF(to: max(1, currentPDFPage - 1)) } label: {
                        Image(systemName: "chevron.left")
                    }.disabled(currentPDFPage <= 1).help("上一页").accessibilityLabel("上一页")
                    Text("\(currentPDFPage) / \(pdfDocument.pageCount)")
                        .font(.caption.monospacedDigit()).foregroundStyle(InboxStyle.muted)
                    Button { navigatePDF(to: min(pdfDocument.pageCount, currentPDFPage + 1)) } label: {
                        Image(systemName: "chevron.right")
                    }.disabled(currentPDFPage >= pdfDocument.pageCount).help("下一页").accessibilityLabel("下一页")
                }.buttonStyle(InboxIconButtonStyle())
                    .padding(.horizontal, 24).frame(height: 36)
                Divider()
            }
            if let detail = model.selectedSource {
                if detail.isPDF && showOriginal, pdfDocumentSourceID == detail.id, let pdfDocument {
                    PDFReader(view: model.pdfView(for: detail.id), document: pdfDocument,
                              request: pdfPageRequest, currentPage: $currentPDFPage,
                              onPosition: { [store = model.readingSessionStore] position in
                                  store?.record(pdf: position, sourceID: detail.id)
                              })
                        .id(detail.id)
                        .overlay(alignment: .top) { ReaderEdgeVeil(top: true) }
                        .overlay(alignment: .bottom) { ReaderEdgeVeil(top: false) }
                } else if detail.isPDF && showOriginal {
                    EmptyMessage(symbol: "doc.text.viewfinder",
                                 title: pdfLoadError == nil ? "正在打开 PDF 原件" : "无法打开 PDF 原件",
                                 detail: pdfLoadError ?? "正在下载并准备页面，请稍候。")
                } else {
                    VStack(alignment: .leading, spacing: 0) {
                    VStack(alignment: .leading, spacing: 12) {
                        if detail.isPDF {
                            HStack(spacing: 12) {
                                Label(ocrStatusLabel(detail.ocrStatus ?? "not_started"), systemImage: "text.viewfinder")
                                Button("刷新") { Task { await model.openSource(detail.id, force: true) } }
                            }.font(.callout).foregroundStyle(.secondary)
                            if let error = detail.ocrError, !error.isEmpty {
                                Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                            }
                        } else if let status = detail.documentParseStatus {
                            HStack(spacing: 12) {
                                Label(documentStatusLabel(status), systemImage: "doc.text.magnifyingglass")
                                Button("刷新") { Task { await model.openSource(detail.id, force: true) } }
                                if status == "failed" {
                                    Button("重试解析") { Task { await model.retryDocumentParse() } }
                                        .disabled(model.browseBusy)
                                }
                            }.font(.callout).foregroundStyle(.secondary)
                            if let error = detail.documentParseError, !error.isEmpty {
                                Text(error).font(.caption).foregroundStyle(.orange).textSelection(.enabled)
                            }
                        }
                        if detail.kind == .link && ["pending", "processing"].contains(detail.processingStatus) {
                            HStack(spacing: 10) {
                                if model.delayedWebReadIDs.contains(detail.id) {
                                    Image(systemName: "exclamationmark.circle").foregroundStyle(.orange)
                                    Text(detail.processingStatus == "pending"
                                         ? "等待超过 2 分钟：请确认 Worker 已重启并运行。"
                                         : "读取超过 2 分钟：请检查 Worker 和浏览器服务。")
                                        .foregroundStyle(.orange)
                                } else {
                                    ProgressView().controlSize(.small)
                                    Text(detail.processingStatus == "pending" ? "等待后台读取网页…" : "正在读取网页正文…")
                                        .foregroundStyle(.secondary)
                                }
                            }.font(.callout)
                        } else if detail.kind == .link && detail.processingStatus == "failed" {
                            HStack(spacing: 10) {
                                Text(detail.webReadError ?? "网页读取失败，原始链接已保留。")
                                    .font(.callout).foregroundStyle(.orange)
                                Button("重试读取") { Task { await model.retryWebRead(detail.id) } }
                            }
                        } else if detail.kind == .link && detail.processingStatus == "needs_input" {
                            Button("读取网页正文") { Task { await model.retryWebRead(detail.id) } }
                        }
                        if detail.kind == .link && detail.coverage == "link_only" {
                            Text("原始链接：\(detail.sourceURL ?? detail.text)")
                                .font(.callout).foregroundStyle(.secondary).textSelection(.enabled)
                        } else if detail.text.isEmpty {
                            Text(detail.isPDF ? "尚无可读文字。识别完成后刷新，或检查 PDF 页面。" : "此资料尚无可读正文。")
                                .font(.callout).foregroundStyle(.secondary)
                        } else if readerMode == .reading {
                            TextReader(store: model.textReaderStore, sourceID: detail.id,
                                       revision: detail.revision,
                                       text: currentDisplayReadingText(detail),
                                       size: readingSize,
                                       request: textReaderRequest,
                                       imageLabels: Dictionary(uniqueKeysWithValues: detail.images.map {
                                           ($0.id.lowercased(), "[图片 \($0.ordinal + 1) · \($0.status == "saved" ? "查看图片" : "未保存，查看原因")]")
                                       }), onImageClick: { [selectedImageID = $selectedImageID, showImages = $showImages] id in
                                           selectedImageID.wrappedValue = id
                                           showImages.wrappedValue = true
                                       }, imageResources: detail.images.compactMap {
                                           guard $0.status == "saved", let path = $0.downloadPath,
                                                 path == "/v1/sources/\(detail.id)/images/\($0.id)" else { return nil }
                                           return ReaderImageResource(id: $0.id.lowercased(), path: String(path.dropFirst()),
                                                                      width: $0.width, height: $0.height)
                                       }, imageAPI: try? API(address: model.address, token: model.token),
                                       presentationSourceURL: detail.isXiaohongshuPost ? detail.sourceURL : nil,
                                       readingPosition: model.readingSessionStore?.current?.sourceID == detail.id ? model.readingSessionStore?.current?.text : nil,
                                       onReadingPosition: { [store = model.readingSessionStore] position in
                                           store?.record(text: position, sourceID: detail.id)
                                       })
                                .id(detail.id)
                                .frame(maxWidth: .infinity, maxHeight: .infinity)
                                .overlay(alignment: .top) { ReaderEdgeVeil(top: true) }
                                .overlay(alignment: .bottom) { ReaderEdgeVeil(top: false) }
                        } else {
                            markdownEditorView(detail: detail)
                        }
                    }
                    .frame(maxWidth: 800, maxHeight: .infinity, alignment: .topLeading)
                    .frame(maxWidth: .infinity)
                    .padding(.horizontal, 24).padding(.top, 4).padding(.bottom, 8)
                    }
                }
            } else {
                EmptyMessage(symbol: "book.closed", title: "打开一条资料开始阅读",
                             detail: "从左侧知识树或搜索结果中选择内容。")
            }
            Divider()
            HStack {
                Text(model.browseMessage).font(.caption).foregroundStyle(.secondary)
                    .lineLimit(1).textSelection(.enabled)
                Spacer()
                if let detail = model.selectedSource {
                    Text(detail.isPDF ? (showOriginal ? "PDF 原件" : "PDF 文字视图") : (readerMode == .reading ? "阅读" : "Markdown 编辑"))
                        .font(.caption).foregroundStyle(.secondary)
                }
            }.padding(.horizontal, 20).frame(height: 36)
        }
        .overlay(alignment: .top) {
            ZStack(alignment: .top) {
                if model.usesLocalService, model.selectedSource != nil, aiOverview.isVisible {
                    LocalAIResultCard(model: model, isExpanded: $aiOverview.isExpanded,
                        analysisButtonMidX: aiAnalysisButtonFrame.isEmpty ? nil : aiAnalysisButtonFrame.midX,
                        showDetails: { showAIAnalysis = true })
                        .fixedSize(horizontal: false, vertical: true)
                        .padding(.horizontal, 24)
                        .padding(.top, 59 + (model.selectedSource?.isPDF == true && showOriginal && pdfDocumentSourceID == model.selectedSource?.id && pdfDocument != nil ? 37 : 0))
                        .transition(.opacity)
                }
            }
            .animation(reduceOverviewMotion ? nil : .smooth(duration: 0.18), value: aiOverview.isVisible)
        }
        .coordinateSpace(name: "knowledgeReader")
        .onChange(of: model.selectedSource?.id) { _, _ in
            var transaction = Transaction()
            transaction.disablesAnimations = true
            withTransaction(transaction) { aiOverview = AIOverviewPresentation() }
        }
        .task(id: model.aiObservationID) {
            guard model.usesLocalService, model.connected, let id = model.selectedSource?.id else { return }
            model.sourceTags = []
            await model.refreshLocalAI(sourceID: id)
            await model.refreshLocalTags(source: true)
            while !Task.isCancelled, model.connected, model.selectedSource?.id == id {
                do { try await Task.sleep(for: .seconds(3)) } catch { return }
                await model.refreshLocalAI(sourceID: id)
            }
        }
    }

    private func evidencePanel(isSheet: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text("证据与笔记").font(.system(size: 13, weight: .semibold))
                Spacer()
                Text("\(model.sourceRevisions.first?.evidence.count ?? 0) 条")
                    .font(.caption).foregroundStyle(.secondary)
                if isSheet {
                    Button("完成") { showEvidenceSheet = false }.font(.caption)
                }
            }.padding(.horizontal, 18).frame(height: 56)
            Divider()
            if model.selectedSource == nil {
                EmptyMessage(symbol: "text.quote", title: "证据在这里", detail: "打开资料后可查看页码、原文和历史版本。")
            } else {
                ScrollView {
                    VStack(alignment: .leading, spacing: 19) {
                        if let detail = model.selectedSource, let note = detail.note, !note.isEmpty {
                            VStack(alignment: .leading, spacing: 7) {
                                Eyebrow(title: "我的理解")
                                if note.utf16.count > 1000 {
                                    TextReader(store: model.textReaderStore, sourceID: "note:\(detail.id)",
                                               revision: detail.contextVersion, text: note, size: 14, request: nil)
                                        .id("note:\(detail.id)")
                                        .frame(height: 180)
                                } else {
                                    Text(note).font(.callout).textSelection(.enabled)
                                }
                            }
                        }
                        Divider()
                        Eyebrow(title: "版本记录")
                        InboxSelect("查看版本", selection: Binding<Int?>(
                            get: { selectedVersion ?? model.sourceRevisions.first?.version },
                            set: { selectedVersion = $0 }
                        ), options: model.sourceRevisions.map { revision in
                            InboxSelectOption<Int?>(revision.version,
                                "版本 \(revision.version)\(revision.isCurrent ? " · 当前" : " · 历史")")
                        }, showsLabel: false).frame(maxWidth: .infinity)

                        if let revision = model.sourceRevisions.first(where: { $0.version == selectedVersion })
                            ?? model.sourceRevisions.first {
                            ForEach(revision.evidence) { evidence in
                                Button {
                                    if let page = evidence.page {
                                        navigatePDF(to: page)
                                        showOriginal = true
                                    }
                                    Task { await model.openEvidence(evidence.id) }
                                } label: {
                                    VStack(alignment: .leading, spacing: 6) {
                                        Text(evidence.page.map { "第 \($0) 页" } ?? "证据片段")
                                            .font(.caption.bold()).foregroundStyle(InboxStyle.accent)
                                        Text(evidence.preview).font(.callout).lineLimit(4)
                                            .frame(maxWidth: .infinity, alignment: .leading)
                                    }
                                    .padding(11)
                                    .background(InboxStyle.elevated, in: RoundedRectangle(cornerRadius: 9))
                                }.buttonStyle(.plain)
                            }
                            if revision.evidence.isEmpty {
                                Text("此版本没有文本证据。")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        if let evidence = model.selectedEvidence {
                            Divider()
                            Eyebrow(title: "证据原文 · 版本 \(evidence.version)")
                            if evidence.hasNewer {
                                Text("资料已有新版本，这条证据仍指向旧版。")
                                    .font(.caption).foregroundStyle(.orange)
                            }
                            TextReader(store: model.textReaderStore, sourceID: "evidence:\(evidence.id)",
                                       revision: evidence.version, text: evidence.text, size: 14, request: nil)
                                .id(evidence.id)
                                .frame(height: 300)
                        }
                        Divider()
                        DisclosureGroup("补充资料", isExpanded: $showSupplement) {
                            VStack(alignment: .leading, spacing: 9) {
                                Text("保存后生成新版本，旧证据仍可查看。")
                                    .font(.caption).foregroundStyle(.secondary)
                                TextEditor(text: $model.supplementDraft).frame(height: 100)
                                    .scrollContentBackground(.hidden)
                                    .padding(6)
                                    .background(InboxStyle.elevated, in: RoundedRectangle(cornerRadius: 8))
                                Button("保存为新版本") { Task { await model.addSupplement() } }
                                    .buttonStyle(InboxButtonStyle(.primary))
                                    .disabled(model.browseBusy || model.supplementDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                            }.padding(.top, 8)
                        }.font(.callout)
                    }.padding(17)
                }
            }
        }
        .background(InboxStyle.surface)
    }

    private func currentDisplayReadingText(_ detail: KnowledgeDetail) -> String {
        if editingSourceID == detail.id && !editingDraft.isEmpty && editingDraft != detail.text {
            return editingDraft
        }
        return detail.text
    }

    private func insertMarkdownSyntax(_ prefix: String, _ suffix: String) {
        if editingDraft.isEmpty {
            editingDraft = "\(prefix)\(suffix)"
        } else if editingDraft.hasSuffix("\n") {
            editingDraft += "\(prefix)\(suffix)"
        } else {
            editingDraft += "\n\(prefix)\(suffix)"
        }
    }

    @ViewBuilder
    private func markdownEditorView(detail: KnowledgeDetail) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Menu {
                    Button("粗体") { insertMarkdownSyntax("**", "**") }
                    Button("斜体") { insertMarkdownSyntax("*", "*") }
                    Button("三级标题") { insertMarkdownSyntax("### ", "") }
                    Button("引用") { insertMarkdownSyntax("> ", "") }
                    Button("提示框") { insertMarkdownSyntax("> [!NOTE]\n> ", "") }
                    Button("链接") { insertMarkdownSyntax("[链接文字](https://", ")") }
                    Button("表格") { insertMarkdownSyntax("\n| 标题 1 | 标题 2 |\n|---|---|\n| 内容 1 | 内容 2 |\n", "") }
                    Button("待办清单") { insertMarkdownSyntax("- [ ] ", "") }
                    Button("代码块") { insertMarkdownSyntax("```\n", "\n```") }
                } label: { Label("格式", systemImage: "textformat") }
                .menuStyle(.borderlessButton).fixedSize()

                Spacer()

                Text("\(editingDraft.count) 字符 · \(editingDraft.split(whereSeparator: { $0.isNewline }).count) 行")
                    .font(.caption).foregroundStyle(.secondary)

                if editingDraft != detail.text {
                    Button("放弃修改") {
                        editingDraft = detail.text
                    }
                    .buttonStyle(.plain)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .help("放弃未保存的修改，恢复到当前版本")
                }

                Button {
                    Task {
                        isSavingRevision = true
                        defer { isSavingRevision = false }
                        let success = await model.saveSourceRevision(text: editingDraft)
                        if success {
                            readerMode = .reading
                        }
                    }
                } label: {
                    HStack(spacing: 4) {
                        if isSavingRevision {
                            ProgressView().controlSize(.small)
                        }
                        Text("保存为新版本")
                    }
                }
                .buttonStyle(InboxButtonStyle(.primary))
                .controlSize(.small)
                .disabled(isSavingRevision || editingDraft == detail.text || model.browseBusy)
            }
            .padding(.horizontal, 4)
            .padding(.top, 4)

            TextEditor(text: $editingDraft)
                .font(.system(size: readingSize, design: .monospaced))
                .lineSpacing(5)
                .scrollContentBackground(.hidden)
                .padding(12)
                .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 8))
                .overlay(
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(InboxStyle.separator, lineWidth: 1)
                )
                .frame(minHeight: 140, maxHeight: .infinity)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear {
            if editingSourceID != detail.id {
                editingSourceID = detail.id
                editingDraft = detail.text
            }
        }
    }

    private func navigatePDF(to page: Int) {
        guard page > 0 else { return }
        currentPDFPage = page
        pdfPageRequest = PDFPageRequest(page: page)
    }

    private func ocrStatusLabel(_ value: String) -> String {
        switch value {
        case "pending": "排队识别中"
        case "running": "正在识别"
        case "completed": "已识别"
        case "waiting_extension": "等待安装扩展"
        case "failed": "识别失败"
        case "cancelled": "识别已取消"
        default: "尚未识别"
        }
    }

    private func documentStatusLabel(_ value: String) -> String {
        switch value {
        case "pending": "排队解析中"
        case "running": "正在解析"
        case "completed": "已解析"
        case "waiting_extension": "等待安装扩展"
        case "failed": "解析失败"
        case "cancelled": "解析已取消"
        default: "尚未解析"
        }
    }
}

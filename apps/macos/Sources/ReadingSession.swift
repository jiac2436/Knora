import Foundation

struct ReaderTextPosition: Codable, Equatable {
    var sourceOffset: Int?
    var imageID: String?
    var elementFraction: Double
    var scrollFraction: Double
    var contentSignature: String

    var isValid: Bool {
        (sourceOffset == nil || (0...20_000_000).contains(sourceOffset!)) &&
        (imageID == nil || UUID(uuidString: imageID!) != nil) &&
        elementFraction.isFinite && (-2...2).contains(elementFraction) &&
        scrollFraction.isFinite && (0...1).contains(scrollFraction) &&
        contentSignature.count == 64 && contentSignature.allSatisfy { $0.isHexDigit }
    }
}

struct ReaderPDFPosition: Codable, Equatable {
    let page: Int
    let x: Double
    let y: Double
    var isValid: Bool { (1...1_000_000).contains(page) && x.isFinite && y.isFinite && abs(x) < 1_000_000 && abs(y) < 1_000_000 }
}

struct ReadingSession: Codable, Equatable {
    var version = 1
    let sourceID: String
    var domainID: String?
    var text: ReaderTextPosition?
    var pdf: ReaderPDFPosition?
    var pdfOriginal: Bool?

    var isValid: Bool {
        version == 1 && UUID(uuidString: sourceID) != nil &&
        (domainID == nil || UUID(uuidString: domainID!) != nil) &&
        (text?.isValid ?? true) && (pdf?.isValid ?? true)
    }
}

/// One small private file per local library. Scroll events update memory; disk
/// commits are coalesced and explicitly flushed when leaving or quitting.
@MainActor final class ReadingSessionStore {
    let file: URL
    private(set) var current: ReadingSession?
    private(set) var error: String?
    private var readFailed = false
    private var dirty = false
    private var writeTask: Task<Void, Never>?

    init(directory: URL) {
        file = directory.appendingPathComponent("reading-session.json")
        do {
            guard FileManager.default.fileExists(atPath: file.path) else { return }
            try checkFile()
            let size = try file.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? Int.max
            guard size <= 16_384 else { throw failure("阅读记录过大，原文件已保留。") }
            let value = try JSONDecoder().decode(ReadingSession.self, from: Data(contentsOf: file))
            guard value.isValid else { throw failure("阅读记录格式无效，原文件已保留。") }
            current = value
        } catch {
            readFailed = true
            self.error = "阅读位置无法恢复：\(error.localizedDescription)"
        }
    }

    func select(sourceID: String, domainID: String?) {
        guard UUID(uuidString: sourceID) != nil else { return }
        if current?.sourceID == sourceID {
            current?.domainID = domainID
        } else {
            current = ReadingSession(sourceID: sourceID, domainID: domainID)
        }
        dirty = true
        flush()
    }

    func record(text: ReaderTextPosition, sourceID: String) {
        guard current?.sourceID == sourceID, text.isValid else { return }
        guard current?.text != text || current?.pdfOriginal != false else { return }
        current?.text = text; current?.pdfOriginal = false
        scheduleWrite()
    }

    func record(pdf: ReaderPDFPosition, sourceID: String) {
        guard current?.sourceID == sourceID, pdf.isValid else { return }
        guard current?.pdf != pdf || current?.pdfOriginal != true else { return }
        current?.pdf = pdf; current?.pdfOriginal = true
        scheduleWrite()
    }

    func setPDFOriginal(_ value: Bool, sourceID: String) {
        guard current?.sourceID == sourceID, current?.pdfOriginal != value else { return }
        current?.pdfOriginal = value
        scheduleWrite()
    }

    func forget(sourceID: String) {
        guard current?.sourceID == sourceID else { return }
        current = nil; dirty = true; flush()
    }

    func flush() {
        writeTask?.cancel(); writeTask = nil
        guard dirty, !readFailed else { return }
        do {
            try checkFile()
            if let current {
                guard current.isValid else { return }
                try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                        attributes: [.posixPermissions: 0o700])
                try JSONEncoder().encode(current).write(to: file, options: [.atomic, .completeFileProtectionUnlessOpen])
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: file.path)
            } else if FileManager.default.fileExists(atPath: file.path) {
                try FileManager.default.removeItem(at: file)
            }
            dirty = false; error = nil
        } catch {
            self.error = "阅读位置暂未保存：\(error.localizedDescription)"
        }
    }

    private func scheduleWrite() {
        dirty = true
        guard writeTask == nil else { return }
        writeTask = Task { [weak self] in
            do { try await Task.sleep(for: .milliseconds(300)) } catch { return }
            self?.flush()
        }
    }

    private func checkFile() throws {
        if FileManager.default.fileExists(atPath: file.path),
           try file.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink == true {
            throw failure("阅读记录不能是符号链接。")
        }
    }
}

extension CaptureModel {
    func restoreLastReadingSession() async {
        guard connected, selectedSource == nil, loadingSourceID.isEmpty,
              pendingArticleLink == nil, articleLinkInFlight == nil,
              let session = readingSessionStore?.current else { return }
        let scope = readingSessionStore
        let connection = (address: address, token: token)
        let generation = UUID()
        sourceLoadGeneration = generation; loadingSourceID = session.sourceID
        defer { if sourceLoadGeneration == generation { loadingSourceID = "" } }
        do {
            try await fetchSource(session.sourceID, generation: generation, expectedConnection: connection)
            guard sourceLoadGeneration == generation, let target = selectedSource?.domainID,
                  selectedSource?.id == session.sourceID, domains.contains(where: { $0.id == target }) else { return }
            if searchDomainID != target {
                domainID = target; searchDomainID = target; searchProjectID = ""
                await loadRecent()
            }
        } catch {
            guard sourceLoadGeneration == generation, readingSessionStore === scope else { return }
            if (error as NSError).userInfo["HTTPStatus"] as? Int == 404 { scope?.forget(sourceID: session.sourceID) }
        }
    }
}

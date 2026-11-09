import Foundation

struct ArticleLink: Equatable {
    let libraryID: String
    let sourceID: String

    init(_ url: URL) throws {
        guard let parts = URLComponents(url: url, resolvingAgainstBaseURL: false),
              parts.scheme == "knowledge-inbox", parts.host == "library",
              parts.user == nil, parts.password == nil, parts.port == nil,
              parts.query == nil, parts.fragment == nil else { throw Self.invalidLink() }
        let path = parts.percentEncodedPath.split(separator: "/", omittingEmptySubsequences: false)
        guard path.count == 4, path[0].isEmpty, path[2] == "source",
              let library = UUID(uuidString: String(path[1])),
              let source = UUID(uuidString: String(path[3])),
              library.uuidString.lowercased() == path[1].lowercased(),
              source.uuidString.lowercased() == path[3].lowercased() else { throw Self.invalidLink() }
        libraryID = library.uuidString.lowercased()
        sourceID = source.uuidString.lowercased()
    }

    private static func invalidLink() -> NSError {
        NSError(domain: "ArticleLink", code: 1,
                userInfo: [NSLocalizedDescriptionKey: "文章链接无效，请从知识库插件重新获取。"])
    }
}

extension CaptureModel {
    func receiveArticleLink(_ url: URL) {
        do {
            let link = try ArticleLink(url)
            articleLinkGeneration = UUID()
            sourceLoadGeneration = UUID()
            loadingSourceID = ""
            browseBusy = false
            pendingArticleLink = link
            articleLinkError = nil
        } catch {
            articleLinkError = error.localizedDescription
        }
    }

    func openPendingArticleLink() async {
        guard connected, !backupBusy, !trashBusy, let link = pendingArticleLink else { return }
        let attempt = articleLinkGeneration
        guard articleLinkInFlight != attempt else { return }
        pendingArticleLink = nil
        articleLinkInFlight = attempt
        let original = (address: address, token: token)
        let sourceAttempt = UUID()
        sourceLoadGeneration = sourceAttempt
        loadingSourceID = link.sourceID
        browseBusy = true
        defer {
            if articleLinkInFlight == attempt { articleLinkInFlight = nil }
            if sourceLoadGeneration == sourceAttempt { loadingSourceID = ""; browseBusy = false }
        }
        func current() -> Bool {
            !Task.isCancelled && articleLinkGeneration == attempt && sourceLoadGeneration == sourceAttempt &&
                connected && address == original.address && token == original.token
        }
        do {
            guard usesLocalService else { throw linkFailure("请使用完整本地客户端打开知识库文章链接。") }
            let identity = try await API(address: original.address, token: original.token).call("v1/me")
            guard current() else { return }
            guard (identity["library_id"] as? String)?.lowercased() == link.libraryID else {
                throw linkFailure("这篇文章属于另一知识库，请先打开对应知识库后再点击链接。")
            }
            // Reuse the existing loader and its cache, permission and late-response guards.
            // Keep the current article visible until the target has fully loaded.
            try await fetchSource(link.sourceID, generation: sourceAttempt, expectedConnection: original)
            guard current(), selectedSource?.id.lowercased() == link.sourceID else { return }
            searchQuery = ""; searchTagQuery = ""; searchProjectID = ""
            if let targetDomain = selectedSource?.domainID {
                domainID = targetDomain; searchDomainID = targetDomain; projectID = ""
            }
            articleNavigationID = UUID()
            await loadRecent()
        } catch {
            guard current() else { return }
            let status = (error as NSError).userInfo["HTTPStatus"] as? Int
            articleLinkError = status == 404 ? "文章不存在、已删除或当前不可访问。" : error.localizedDescription
        }
    }

    private func linkFailure(_ message: String) -> NSError {
        NSError(domain: "ArticleLink", code: 2, userInfo: [NSLocalizedDescriptionKey: message])
    }
}

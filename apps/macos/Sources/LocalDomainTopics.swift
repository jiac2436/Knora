import Foundation
import Combine

/// Owner-only manual taxonomy. Editing never authorizes or invokes a model.
@MainActor final class LocalDomainTopicsStore: ObservableObject {
    typealias Transport = (API, String, String, [String: Any]?) async throws -> [String: Any]
    @Published private(set) var topics: [LocalDomainTopic] = []
    @Published private(set) var sources: [LocalDomainSource] = []
    @Published private(set) var members: Set<String> = []
    @Published private(set) var selectedID: String?
    @Published private(set) var nextSourceOffset: Int?
    @Published private(set) var isLoading = false
    @Published private(set) var isMutating = false
    @Published private(set) var error: String?
    @Published private(set) var message: String?
    @Published private(set) var creationUnconfirmed = false
    @Published private(set) var unconfirmedName: String?
    private var api: API?
    private var root = ""
    private var epoch = UUID()
    private var read = UUID()
    private let transport: Transport

    init(transport: Transport? = nil) {
        self.transport = transport ?? { api, path, method, body in
            guard let components = URLComponents(string: path) else { throw failure("主题请求地址无效") }
            return try await api.call(components.path, method: method, json: body, query: components.queryItems ?? [])
        }
    }
    func reset() {
        epoch = UUID(); read = UUID(); api = nil; root = ""
        topics = []; sources = []; members = []; selectedID = nil; nextSourceOffset = nil
        isLoading = false; isMutating = false; error = nil; message = nil; creationUnconfirmed = false; unconfirmedName = nil
    }
    func configure(address: String, token: String, domainID: String) async {
        reset()
        do { api = try API(address: address, token: token); root = "/v1/domains/\(domainID)" }
        catch { self.error = error.localizedDescription; return }
        await refresh()
    }
    private func decode<T: Decodable>(_ type: T.Type, _ value: Any) throws -> T {
        try JSONDecoder().decode(type, from: JSONSerialization.data(withJSONObject: value))
    }
    private func directory(_ api: API, root: String, identity: UUID, request: UUID) async throws -> [LocalDomainTopic]? {
        var result: [LocalDomainTopic] = [], offset = 0
        while true {
            let value = try await transport(api, root + "/topics?scope=app&limit=100&offset=\(offset)", "GET", nil)
            guard epoch == identity, read == request else { return nil }
            let page = try decode(LocalDomainPage<LocalDomainTopic>.self, value)
            let existing = Set(result.map(\.id)); result += page.items.filter { !existing.contains($0.id) }
            guard let next = page.next_offset else { return result }
            guard next > offset else { throw failure("主题分页响应无效，请刷新") }; offset = next
        }
    }
    func refresh() async {
        guard !isMutating, let api else { return }
        let identity = epoch, request = UUID(), root = root, topicID = selectedID
        read = request; isLoading = true; error = nil
        defer { if epoch == identity && read == request { isLoading = false } }
        do {
            guard let catalog = try await directory(api, root: root, identity: identity, request: request) else { return }
            let value = try await transport(api, root + "/analysis/navigation", "POST", ["scope": "app"])
            guard epoch == identity, read == request else { return }
            let navigation = try decode(LocalDomainNavigation.self, value)
            topics = catalog; sources = navigation.sources.items; nextSourceOffset = navigation.sources.next_offset
            if let topicID, catalog.contains(where: { $0.id == topicID }) {
                try await readMembers(api, root: root, topicID: topicID, identity: identity, request: request)
            } else { selectedID = nil; members = [] }
        } catch { if epoch == identity && read == request { selectedID = nil; members = []; self.error = error.localizedDescription } }
    }
    func select(_ topicID: String) async {
        guard !isMutating, !isLoading, let api, topics.contains(where: { $0.id == topicID }) else { return }
        let identity = epoch, request = UUID(), root = root
        read = request; selectedID = topicID; members = []; isLoading = true; error = nil
        defer { if epoch == identity && read == request { isLoading = false } }
        do { try await readMembers(api, root: root, topicID: topicID, identity: identity, request: request) }
        catch { if epoch == identity && read == request { selectedID = nil; self.error = error.localizedDescription } }
    }
    private func readMembers(_ api: API, root: String, topicID: String, identity: UUID, request: UUID) async throws {
        var result: Set<String> = [], offset = 0
        while true {
            let value = try await transport(api, root + "/topics/\(topicID)/sources?limit=100&offset=\(offset)", "GET", nil)
            guard epoch == identity, read == request else { return }
            let page = try decode(LocalDomainPage<LocalDomainSource>.self, value)
            result.formUnion(page.items.map(\.id))
            guard let next = page.next_offset else { members = result; return }
            guard next > offset else { throw failure("成员分页响应无效，请刷新") }; offset = next
        }
    }
    func loadMoreSources() async {
        guard !isLoading, !isMutating, let api, let offset = nextSourceOffset else { return }
        let identity = epoch, request = read, root = root
        isLoading = true
        defer { if epoch == identity && read == request { isLoading = false } }
        do {
            let value = try await transport(api, root + "/analysis/navigation", "POST", ["scope": "app", "source_offset": offset])
            guard epoch == identity, read == request else { return }
            let page = try decode(LocalDomainNavigation.self, value).sources
            if let next = page.next_offset, next <= offset { throw failure("资料分页响应无效，请刷新") }
            let ids = Set(sources.map(\.id)); sources += page.items.filter { !ids.contains($0.id) }
            nextSourceOffset = page.next_offset
        } catch { if epoch == identity && read == request { self.error = error.localizedDescription } }
    }
    func save(topicID: String?, name: String, parentID: String?) async -> Bool {
        guard !isLoading, !isMutating, let api, !creationUnconfirmed || topicID != nil else { return false }
        let cleaned = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty, cleaned.count <= 120 else { error = "请输入 1～120 字的主题名称"; return false }
        let identity = epoch, root = root
        isMutating = true; error = nil
        defer { if epoch == identity { isMutating = false } }
        do {
            var body: [String: Any] = ["name": cleaned, "parent_id": parentID as Any? ?? NSNull()]
            if topicID == nil { body["scope"] = "app" }
            let value = try await transport(api, root + "/topics" + (topicID.map { "/" + $0 } ?? ""), topicID == nil ? "POST" : "PATCH", body)
            guard epoch == identity else { return false }
            selectedID = value["id"] as? String
            message = "主题已保存；人工名称与归属不会被本地自动归类覆盖。概览将按变更失效，更新仍受原有授权与预算控制。"
            isMutating = false; await refresh(); return epoch == identity
        } catch {
            guard epoch == identity else { return false }
            let status = (error as NSError).userInfo["HTTPStatus"] as? Int ?? 0
            if topicID == nil && !(400..<500).contains(status) {
                creationUnconfirmed = true; unconfirmedName = cleaned
                self.error = "创建结果尚未确认，请先刷新主题目录，确认是否已经创建，避免重复新增。"
            } else { self.error = error.localizedDescription }
            return false
        }
    }
    func acknowledgeUnconfirmedCreation(confirmed: Bool) {
        guard confirmed, !isLoading, !isMutating else { return }
        creationUnconfirmed = false; unconfirmedName = nil; error = nil
        message = "已确认核对原创建结果；如仍未确认服务端完成，请继续等待，避免重复创建。"
    }
    func setIncluded(sourceID: String, included: Bool) async {
        guard !isLoading, !isMutating, let api, let selectedID, sources.contains(where: { $0.id == sourceID }) else { return }
        let identity = epoch, root = root
        isMutating = true; error = nil
        defer { if epoch == identity { isMutating = false } }
        do {
            _ = try await transport(api, root + "/topics/\(selectedID)/members/\(sourceID)", "PUT", ["included": included])
            guard epoch == identity else { return }
            message = "资料归属已保存；移出主题不会删除原文。"
            isMutating = false; await refresh()
        } catch {
            guard epoch == identity else { return }
            self.error = "归属保存未确认，请刷新核对后再操作。\n" + error.localizedDescription
        }
    }
}

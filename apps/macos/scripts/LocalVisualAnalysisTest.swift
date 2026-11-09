import Foundation
import CryptoKit
import ImageIO

/// --real sends exactly one synthetic image; credentials stay in memory.
@main struct LocalVisualAnalysisTest {
    @MainActor static func main() async {
        do { try await run() }
        catch { report("FAIL: " + error.localizedDescription); exit(1) }
    }

    @MainActor static func run() async throws {
        let planBody: [String: Any] = ["plan_hash": String(repeating: "a", count: 64), "calls": 1,
            "sections": 0, "coverage": "full", "covered_chars": 20, "total_chars": 20,
            "source_coverage": "full_text", "image_count": 1]
        let plan = try decodeLocalAI(LocalAIPlan.self, body: planBody)
        precondition(plan.image_count == 1 && plan.scopeText.contains("1 张原图"))
        var legacy = planBody; legacy.removeValue(forKey: "image_count")
        let legacyPlan = try decodeLocalAI(LocalAIPlan.self, body: legacy)
        precondition(legacyPlan.image_count == nil)
        let evidence = try decodeLocalAI(LocalAIArtifact.Evidence.self, body: ["id": "virtual",
            "original_evidence_id": "original", "image_id": "image", "text": "相邻正文",
            "locator_kind": "paragraph", "locator_start": 0, "locator_end": 1])
        precondition(evidence.image_id == "image" && evidence.original_evidence_id == "original")
        report("PASS: 图文计划、历史 DTO、原图引用")
        guard CommandLine.arguments.dropFirst().first == "--real" else { return }
        guard CommandLine.arguments.count == 3 else { throw failure("需要显式指定合成图表 PNG") }

        // Resolve only the configured gateway address and its exact Keychain
        // item. Do not open, start, mutate or query the user's knowledge DB.
        let current = try LocalLibraryStore(home: FileManager.default.homeDirectoryForCurrentUser,
            credentialService: LocalService.credentialService).current()
        UserDefaults.standard.addSuite(named: "local.pka.knowledge-inbox")
        let address = LocalAIConfiguration.address(current)
        let health = try await API(address: address, token: "").call("health", timeout: 5)
        guard health["analysis_image_support"] as? Bool == true else {
            throw failure("网关尚未加载图文接口或未配置视觉模型；未发送模型请求")
        }
        guard let credential = TokenStore.load(service: LocalAIConfiguration.tokenService(current)), !credential.isEmpty else {
            throw failure("无法读取已配置的网关访问凭证；未发送请求，请勿把凭证发到聊天里")
        }
        let file = URL(fileURLWithPath: CommandLine.arguments[2])
        let data = try Data(contentsOf: file)
        guard data.count <= 20 * 1024 * 1024,
              let image = CGImageSourceCreateWithData(data as CFData, nil),
              let properties = CGImageSourceCopyPropertiesAtIndex(image, 0, nil) as? [CFString: Any],
              let width = properties[kCGImagePropertyPixelWidth] as? Int,
              let height = properties[kCGImagePropertyPixelHeight] as? Int else { throw failure("合成图片无效") }
        let imageID = UUID().uuidString.lowercased(), evidenceID = UUID().uuidString.lowercased()
        let body: [String: Any] = ["contract_version": 3, "request_id": UUID().uuidString.lowercased(),
            "kind": "analyze", "source_id": UUID().uuidString.lowercased(), "revision_id": UUID().uuidString.lowercased(),
            "evidence": [["id": evidenceID, "image_id": imageID,
                "text": "合成月报。请结合配图概括两个时间点的用户数及增长情况。",
                "locator_kind": "paragraph", "locator_start": 0, "locator_end": 1]],
            "images": [["id": imageID, "evidence_id": evidenceID, "ordinal": 0,
                "sha256": SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined(),
                "mime_type": "image/png", "size_bytes": data.count, "width": width, "height": height,
                "data_url": "data:image/png;base64," + data.base64EncodedString()]],
            "context": ["title": "合成图表质量验收"]]
        report("即将发送 1 次合成图表联合分析；失败或结果未知不重试")
        report("请求编号：\(body["request_id"] as? String ?? "")")
        var request = URLRequest(url: URL(string: address)!.appendingPathComponent("v1/analyze"))
        request.httpMethod = "POST"; request.timeoutInterval = 120
        request.setValue("Bearer " + credential, forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let (bytes, reply) = try await URLSession.shared.data(for: request)
        let response = (try? JSONSerialization.jsonObject(with: bytes)) as? [String: Any] ?? [:]
        guard let http = reply as? HTTPURLResponse, http.statusCode == 200 else {
            let detail = response["detail"] as? [String: Any] ?? [:]
            var safe = ["code", "stage", "category"].compactMap { key -> String? in
                guard let value = detail[key] as? String,
                      value.range(of: "^[a-z_]{1,80}$", options: .regularExpression) != nil else { return nil }
                return key + "=" + value
            }.joined(separator: ", ")
            if let status = detail["upstream_status"] as? Int { safe += ", upstream HTTP \(status)" }
            throw failure("网关 HTTP \((reply as? HTTPURLResponse)?.statusCode ?? 0)：\(safe)；停止调用，不自动重试")
        }
        guard let result = response["result"] as? [String: Any],
              let summary = result["summary"] as? String,
              let overview = result["overview"] as? [String: Any],
              let overviewText = overview["text"] as? String else { throw failure("模型响应缺少总结") }
        let answer = overviewText + " " + summary
        guard answer.contains("137"), answer.contains("37") else {
            report("FAIL: " + answer)
            throw failure("真实模型未正确提取仅存在于图中的两个关键数值，停止质量验收")
        }
        report("PASS: 真实模型读取图片中的 137 与 37%，正文没有提供这些数值")
        report("模型：\(response["model"] as? String ?? "未知")；概览：\(overviewText)")
    }

    static func report(_ message: String) { FileHandle.standardError.write(Data((message + "\n").utf8)) }
}

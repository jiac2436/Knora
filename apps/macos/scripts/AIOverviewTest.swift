import AppKit
import SwiftUI

@main struct AIOverviewTest {
    @MainActor static func main() {
        _ = NSApplication.shared
        Task { @MainActor in
            do { try await run(); NSApp.terminate(nil) }
            catch { print("FAIL: \(error)"); exit(1) }
        }
        NSApp.run()
    }

    @MainActor static func run() async throws {
        func job(_ status: String, pipeline: String? = nil, source: String = "overview-test", steps: Int = 0) throws -> LocalAIJob {
            var value: [String: Any] = ["id": "job", "source_id": source, "kind": "analyze", "status": status,
                "completed_steps": steps, "total_steps": 4, "contract_version": 3]
            if let pipeline { value["pipeline_status"] = pipeline }
            return try decodeLocalAI(LocalAIJob.self, body: value)
        }
        let artifact = try decodeLocalAI(LocalAIArtifact.self, body: ["id": "result", "source_id": "overview-test",
            "result": ["contract_version": 3, "summary": "通过反查关键词与多次搜索，逐步发现可验证的需求。",
                "summary_citations": [], "insights": [["text": "从真实流量入口反查关键词，再验证需求。", "citations": []]], "warnings": []],
            "evidence": [], "provider": "synthetic", "model": "synthetic", "stale": false, "pipeline_status": "completed"])
        let pending = try job("pending"), running = try job("running", steps: 2)
        let indexing = try job("completed", pipeline: "indexing", steps: 4)
        let completed = try job("completed", pipeline: "completed", steps: 4)
        for item in [pending, running, indexing] {
            let value = AIOverviewState(sourceID: "overview-test", jobs: [item], artifacts: [artifact])
            precondition(value.isActive && !value.canExpand && !value.progressText.isEmpty)
        }
        precondition(AIOverviewState(sourceID: "overview-test", jobs: [running], artifacts: []).isActive)
        precondition(AIOverviewState(sourceID: "overview-test", jobs: [completed], artifacts: [artifact]).canExpand)
        let failed = AIOverviewState(sourceID: "overview-test", jobs: [try job("failed")], artifacts: [])
        precondition(!failed.isActive && !failed.canExpand && failed.progressText == "分析失败")
        let other = AIOverviewState(sourceID: "another-source", jobs: [running], artifacts: [artifact])
        precondition(other.job == nil && other.artifact == nil && !other.canExpand)
        let overlapping = AIOverviewState(sourceID: "overview-test", jobs: [completed, running], artifacts: [artifact])
        precondition(overlapping.isActive && !overlapping.canExpand)
        precondition(AIOverviewBubble.pointer(-100, width: 600) == 24)
        precondition(AIOverviewBubble.pointer(900, width: 600) == 576)
        precondition(AIOverviewBubble.pointer(300, width: 600) == 300)
        print("PASS: first analysis/reanalysis/indexing cannot expand; completed expands; failed/other source isolated; active task prioritized; pointer clamped")
        guard let output = ProcessInfo.processInfo.environment["PKA_AI_OVERVIEW_PREVIEW"] else { return }
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-ai-overview-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let model = CaptureModel(queueStore: CaptureQueueStore(root: root.appendingPathComponent("queue")),
            localService: LocalService(home: root, credentialService: "synthetic.ai-overview"), runtime: root)
        model.address = "https://example.invalid"
        let detail: [String: Any] = ["id": "overview-test", "title": "保持好奇心，发现值得留下的知识", "content_type": "link",
            "revision": 2, "extracted_text": "# 保持好奇心\n\n阅读与搜索相互连接，让收集的资料变成随时可以使用的知识。\n\n从真实问题出发，记录依据，再检验自己的理解。"]
        model.selectedSource = KnowledgeDetail(detail)!
        model.knowledgeRows = [KnowledgeRow(detail, searchResult: false)!]
        model.domains = [DomainItem(id: "demo", name: "示例知识库")]
        model.domainID = "demo"; model.searchDomainID = "demo"
        let host = NSHostingView(rootView: KnowledgeView(model: model, showSettings: .constant(false)).preferredColorScheme(.dark))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1280, height: 820), styleMask: [.titled, .closable], backing: .buffered, defer: false)
        window.title = "AI 概览状态验收（合成数据）"
        window.contentView = host; window.center(); window.makeKeyAndOrderFront(nil)
        func capture(_ name: String) throws {
            host.layoutSubtreeIfNeeded()
            let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds)!
            host.cacheDisplay(in: host.bounds, to: bitmap)
            try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: output).appendingPathComponent(name + ".png"))
        }
        model.aiJobs = [pending]
        try await Task.sleep(for: .milliseconds(900)); try capture("pending")
        model.aiJobs = [running]; model.aiArtifacts = [artifact]
        try await Task.sleep(for: .milliseconds(500)); try capture("reanalyzing")
        model.aiJobs = [indexing]
        try await Task.sleep(for: .milliseconds(500)); try capture("indexing")
        model.aiJobs = [completed]
        try await Task.sleep(for: .milliseconds(500)); try capture("completed")
        window.setContentSize(NSSize(width: 900, height: 680))
        try await Task.sleep(for: .milliseconds(500)); try capture("narrow")
        window.setContentSize(NSSize(width: 1280, height: 820))
        if CommandLine.arguments.contains("--interactive") {
            // Isolated GUI interaction; no backend or model requests are made.
            print("READY: completed; touch running/completed/finish marker files in output to change fixture state")
            fflush(stdout)
            for _ in 0..<240 {
                if FileManager.default.fileExists(atPath: output + "/finish") { break }
                if FileManager.default.fileExists(atPath: output + "/running") { model.aiJobs = [running] }
                else { model.aiJobs = [completed] }
                try await Task.sleep(for: .milliseconds(500))
            }
            try capture("interactive-final")
        }
        window.orderOut(nil)
        print("PASS: native UI rendered at normal and narrow widths, pending/reanalysis/indexing/completed")
    }
}

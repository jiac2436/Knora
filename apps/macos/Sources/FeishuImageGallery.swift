import AppKit
import SwiftUI

struct SourceImageItem: Identifiable {
    let id: String
    let ordinal: Int
    let blockID: String
    let caption: String
    let status: String
    let failureReason: String
    let ocrText: String
    let downloadPath: String?
    let retrievalMethod: String
    let width: Int?
    let height: Int?

    init?(_ value: [String: Any]) {
        guard let id = value["id"] as? String, UUID(uuidString: id) != nil,
              let ordinal = value["ordinal"] as? Int, (0..<200).contains(ordinal) else { return nil }
        self.id = id
        self.ordinal = ordinal
        blockID = value["block_id"] as? String ?? ""
        caption = value["caption"] as? String ?? ""
        status = value["status"] as? String ?? "pending"
        failureReason = value["failure_reason"] as? String ?? ""
        ocrText = value["ocr_text"] as? String ?? ""
        downloadPath = value["download_path"] as? String
        retrievalMethod = value["retrieval_method"] as? String ?? "download"
        width = value["width"] as? Int
        height = value["height"] as? Int
    }
}

struct FeishuImageGallery: View {
    let sourceID: String
    let images: [SourceImageItem]
    let address: String
    let token: String
    var initialImageID: String? = nil
    var onRetry: ((String) async throws -> Void)? = nil
    @Environment(\.dismiss) private var dismiss
    @State private var selection = 0
    @State private var image: NSImage?
    @State private var error: String?
    @State private var loading = false
    @State private var retrying = false
    @State private var showOCR = false
    @State private var file: URL?
    @State private var directory = FileManager.default.temporaryDirectory.appendingPathComponent("know-image-gallery-\(UUID().uuidString)")

    private var selected: SourceImageItem? { images.indices.contains(selection) ? images[selection] : nil }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("文档图片").font(.headline)
                Spacer()
                Button("上一张") { selection -= 1 }.disabled(selection <= 0)
                Text("\(images.isEmpty ? 0 : selection + 1) / \(images.count)").monospacedDigit()
                Button("下一张") { selection += 1 }.disabled(selection + 1 >= images.count)
                Button("完成") { dismiss() }
            }
            if let selected {
                Text("图片 \(selected.ordinal + 1)").font(.callout)
                if selected.retrievalMethod == "preview" {
                    Text("已保存飞书预览图，清晰度可能与原件不同。")
                        .font(.caption).foregroundStyle(.secondary)
                }
                if !selected.caption.isEmpty {
                    DisclosureGroup("图片说明") {
                        ScrollView { Text(selected.caption).font(.callout).textSelection(.enabled) }
                            .frame(maxHeight: 110)
                    }
                }
                if !selected.blockID.isEmpty {
                    Text("原文段落：\(selected.blockID)").font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
                Divider()
                if loading {
                    ProgressView("正在打开图片…").frame(maxWidth: .infinity, maxHeight: .infinity)
                } else if let image {
                    ScrollView([.horizontal, .vertical]) {
                        Image(nsImage: image).resizable().scaledToFit().frame(width: 760)
                    }
                    HStack {
                        Button("用系统预览打开") { if let file { DesktopIntegration.shared.open(file) } }
                        Toggle("查看 OCR 文字", isOn: $showOCR).toggleStyle(.checkbox)
                    }
                    if showOCR {
                        ScrollView {
                            Text(selected.ocrText.isEmpty ? "未识别到文字或本机 OCR 未完成。图片已保存。" : selected.ocrText)
                                .font(.callout).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                        }.frame(maxHeight: 150)
                        Text(selected.isWebImage && selected.ocrText.isEmpty
                             ? "原图已保存；可在文章中点击“AI 分析”，结合正文理解图片。图片未单独生成 OCR 文字。"
                             : "OCR 可能识别错误，已作为图片识别文字加入检索。")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                } else {
                    Text(error ?? "图片未获取").foregroundStyle(.secondary)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                    if selected.isWebImage && selected.status == "unavailable", let onRetry {
                        Button(retrying ? "正在提交重试…" : "重试下载这张图片") {
                            retrying = true
                            Task {
                                defer { retrying = false }
                                do { try await onRetry(selected.id) }
                                catch { self.error = error.localizedDescription }
                            }
                        }.disabled(retrying)
                    }
                }
            }
        }
        .padding(22).frame(width: 820, height: 680)
        .background(InboxStyle.canvas)
        .onAppear {
            if let initialImageID, let index = images.firstIndex(where: { $0.id.lowercased() == initialImageID.lowercased() }) {
                selection = index
            }
        }
        .task(id: "\(selected?.id ?? "")|\(selected?.status ?? "")") { await load() }
        .onDisappear { try? FileManager.default.removeItem(at: directory) }
    }

    private func load() async {
        image = nil
        file = nil
        error = nil
        showOCR = false
        loading = false
        guard let selected else { return }
        guard selected.status == "saved", let path = selected.downloadPath,
              path == "/v1/sources/\(sourceID)/images/\(selected.id)" else {
            error = selected.isDownloading ? "图片正在后台下载，完成后会自动显示。"
                : selected.status == "pending" ? "图片尚未上传，请重试原采集记录。" : selected.failureReason
            return
        }
        loading = true
        defer { if self.selected?.id == selected.id { loading = false } }
        do {
            let api = try API(address: address, token: token)
            let local = directory.appendingPathComponent(selected.id)
            let downloaded: URL
            if FileManager.default.fileExists(atPath: local.path) { downloaded = local }
            else { downloaded = try await api.downloadFile(String(path.dropFirst()), into: directory, named: selected.id) }
            try Task.checkCancellation()
            guard let original = NSImage(contentsOf: downloaded) else { throw failure("图片无法显示。") }
            guard self.selected?.id == selected.id else { return }
            image = original
            file = downloaded
        } catch is CancellationError { }
        catch { if self.selected?.id == selected.id { self.error = error.localizedDescription } }
    }
}

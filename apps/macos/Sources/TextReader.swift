import AppKit
import SwiftUI
import ImageIO

struct TextReaderRequest {
    let sourceID: String
    let revision: Int?
    let range: NSRange
    let id = UUID()
}

/// Each cached document owns its web view instance and scroll position.
@MainActor final class TextReaderStore {
    private var readers: [String: NativeWebTextReader] = [:]
    private var order: [String] = []
    private var costs: [String: Int] = [:]

    func reader(id: String, revision: Int?, text: String, size: CGFloat, imageLabels: [String: String] = [:], onImageClick: ((String) -> Void)? = nil, imageResources: [ReaderImageResource] = [], imageAPI: API? = nil, presentationSourceURL: String? = nil) -> NativeWebTextReader {
        let reader = readers[id] ?? NativeWebTextReader(frame: NSRect(x: 0, y: 0, width: 720, height: 600))
        reader.configure(revision: revision, text: text, size: size, imageLabels: imageLabels, onImageClick: onImageClick, imageResources: imageResources, imageAPI: imageAPI, presentationSourceURL: presentationSourceURL)
        readers[id] = reader
        costs[id] = text.utf8.count
        order.removeAll { $0 == id }
        order.append(id)
        while order.count > 1 && (order.count > 8 || costs.values.reduce(0, +) > 8 * 1024 * 1024) {
            let evicted = order.removeFirst()
            readers.removeValue(forKey: evicted)
            costs.removeValue(forKey: evicted)
        }
        return reader
    }

    func removeAll() {
        readers.removeAll()
        order.removeAll()
        costs.removeAll()
    }

    func remove(id: String) {
        readers.removeValue(forKey: id)
        order.removeAll { $0 == id }
        costs.removeValue(forKey: id)
    }
}

@MainActor final class LegacyNativeTextReader: NSScrollView, NSTextViewDelegate {
    let textView = NSTextView(usingTextLayoutManager: true)
    private var revision: Int?
    private var configured = false
    private var imageLabels: [String: String] = [:]
    private var presentation = ReaderImagePresentation(text: "", labels: [:])
    private var onImageClick: ((String) -> Void)?
    private var imageResources: [ReaderImageResource] = []
    private var imageAPI: API?
    private var attachments: [String: NSTextAttachment] = [:]
    private var loadedImages: [String: NSImage] = [:]
    private var loadingTask: Task<Void, Never>?
    private var generation = UUID()
    private var boundsObserver: NSObjectProtocol?
    private var fontSize: CGFloat = 0
    private var lastRequestID: UUID?
    private var pendingRequest: TextReaderRequest?
    private(set) var contentAssignments = 0

    init() {
        super.init(frame: NSRect(x: 0, y: 0, width: 720, height: 600))
        drawsBackground = false
        hasVerticalScroller = true
        autohidesScrollers = true
        borderType = .noBorder
        textView.frame = contentView.bounds
        textView.isEditable = false
        textView.isSelectable = true
        textView.isRichText = true
        textView.delegate = self
        textView.drawsBackground = false
        textView.textColor = .labelColor
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainer?.widthTracksTextView = true
        textView.textContainer?.containerSize = NSSize(width: 720, height: CGFloat.greatestFiniteMagnitude)
        textView.textContainerInset = NSSize(width: 28, height: 24)
        documentView = textView
        contentView.postsBoundsChangedNotifications = true
        boundsObserver = NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification,
                                                                 object: contentView, queue: .main) { [weak self] _ in
            MainActor.assumeIsolated { self?.loadVisibleImages() }
        }
    }

    deinit {
        loadingTask?.cancel()
        if let boundsObserver { NotificationCenter.default.removeObserver(boundsObserver) }
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func configure(revision: Int?, text: String, size: CGFloat, imageLabels: [String: String] = [:], onImageClick: ((String) -> Void)? = nil, imageResources: [ReaderImageResource] = [], imageAPI: API? = nil) {
        self.onImageClick = onImageClick
        // Repeated SwiftUI updates must not replace text storage or invalidate layout.
        if !configured || self.revision != revision || self.imageLabels != imageLabels || self.imageResources != imageResources
            || self.imageAPI?.base != imageAPI?.base || self.imageAPI?.token != imageAPI?.token {
            self.revision = revision
            configured = true
            loadingTask?.cancel()
            loadingTask = nil
            generation = UUID()
            loadedImages.removeAll()
            self.imageResources = imageResources
            self.imageAPI = imageAPI
            self.imageLabels = imageLabels
            attachments = Dictionary(uniqueKeysWithValues: imageResources.map { resource in
                let attachment = NSTextAttachment()
                attachment.image = Self.placeholder("正在加载图片…")
                attachment.bounds = Self.imageBounds(width: resource.width ?? 600, height: resource.height ?? 200,
                                                       available: max(100, contentView.bounds.width - 56))
                return (resource.id, attachment)
            })
            presentation = ReaderImagePresentation(text: text, labels: imageLabels, attachments: attachments)
            textView.textStorage?.setAttributedString(presentation.content)
            textView.textColor = .labelColor
            contentAssignments += 1
            textView.setSelectedRange(NSRange(location: 0, length: 0))
            contentView.scroll(to: .zero)
            fontSize = 0
            lastRequestID = nil
            pendingRequest = nil
        }
        if fontSize != size {
            fontSize = size
            textView.font = .systemFont(ofSize: size)
            let paragraph = NSMutableParagraphStyle()
            paragraph.lineSpacing = 7
            textView.defaultParagraphStyle = paragraph
            textView.textStorage?.addAttribute(.paragraphStyle, value: paragraph,
                                              range: NSRange(location: 0, length: textView.string.utf16.count))
        }
    }

    override func layout() {
        super.layout()
        resizeImages()
        loadVisibleImages()
    }

    private static func imageBounds(width: Int, height: Int, available: CGFloat) -> NSRect {
        let scale = min(1, available / CGFloat(max(1, width)), 1000 / CGFloat(max(1, height)))
        return NSRect(x: 0, y: 0, width: CGFloat(max(1, width)) * scale, height: CGFloat(max(1, height)) * scale)
    }

    private static func placeholder(_ label: String) -> NSImage {
        NSImage(size: NSSize(width: 600, height: 180), flipped: false) { rect in
            NSColor.quaternaryLabelColor.setFill()
            rect.fill()
            (label as NSString).draw(at: NSPoint(x: 20, y: 80), withAttributes: [
                .font: NSFont.systemFont(ofSize: 16), .foregroundColor: NSColor.secondaryLabelColor])
            return true
        }
    }

    private func resizeImages() {
        for resource in imageResources {
            guard let attachment = attachments[resource.id] else { continue }
            let bounds = Self.imageBounds(width: resource.width ?? 600, height: resource.height ?? 200,
                                          available: max(100, contentView.bounds.width - 56))
            if attachment.bounds != bounds {
                attachment.bounds = bounds
                invalidateImage(resource.id)
            }
        }
    }

    private func invalidateImage(_ id: String) {
        guard let range = presentation.imageRanges[id], let attachment = attachments[id] else { return }
        textView.textStorage?.addAttribute(.attachment, value: attachment, range: range)
    }

    private func loadVisibleImages() {
        guard window != nil, loadingTask == nil, let api = imageAPI, !imageResources.isEmpty else { return }
        guard let layout = textView.textLayoutManager, let content = layout.textContentManager else { return }
        let viewport = contentView.bounds.insetBy(dx: 0, dy: -400)
        var ranges: [NSRange] = []
        var documentHeight: CGFloat = 0
        layout.enumerateTextLayoutFragments(from: content.documentRange.location, options: [.ensuresLayout]) { fragment in
            let frame = fragment.layoutFragmentFrame.offsetBy(dx: textView.textContainerInset.width,
                                                              dy: textView.textContainerInset.height)
            documentHeight = max(documentHeight, frame.maxY + textView.textContainerInset.height)
            if frame.intersects(viewport) {
                let start = content.offset(from: content.documentRange.location, to: fragment.rangeInElement.location)
                let length = content.offset(from: fragment.rangeInElement.location, to: fragment.rangeInElement.endLocation)
                ranges.append(NSRange(location: start, length: length))
            }
            return true
        }
        let height = max(contentView.bounds.height, documentHeight)
        if abs(textView.frame.height - height) > 1 { textView.setFrameSize(NSSize(width: textView.frame.width, height: height)) }
        let visible = imageResources.filter { resource in
            guard let range = presentation.imageRanges[resource.id] else { return false }
            return ranges.contains { NSLocationInRange(range.location, $0) }
        }
        // Keep decoded images around the viewport only; large documents stay bounded.
        let visibleIDs = Set(visible.map(\.id))
        for id in Array(loadedImages.keys) where !visibleIDs.contains(id) {
            loadedImages.removeValue(forKey: id)
            attachments[id]?.image = Self.placeholder("正在加载图片…")
            invalidateImage(id)
        }
        let pending = visible.filter { loadedImages[$0.id] == nil }
        guard !pending.isEmpty else { return }
        let current = generation
        loadingTask = Task { [weak self] in
            guard let self else { return }
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent("know-inline-images-\(UUID().uuidString)")
            defer {
                try? FileManager.default.removeItem(at: directory)
                if self.generation == current {
                    self.loadingTask = nil
                    DispatchQueue.main.async { [weak self] in self?.loadVisibleImages() }
                }
            }
            for resource in pending {
                do {
                    try Task.checkCancellation()
                    let file = try await api.downloadFile(resource.path, into: directory, named: resource.id)
                    let thumbnail = try await Task.detached(priority: .utility) { () throws -> CGImage in
                        guard let source = CGImageSourceCreateWithURL(file as CFURL, nil),
                              let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                                kCGImageSourceCreateThumbnailFromImageAlways: true,
                                kCGImageSourceThumbnailMaxPixelSize: 1600,
                                kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary) else {
                            throw failure("图片无法显示")
                        }
                        return image
                    }.value
                    try? FileManager.default.removeItem(at: file)
                    try Task.checkCancellation()
                    guard self.generation == current else { return }
                    let image = NSImage(cgImage: thumbnail, size: .zero)
                    self.loadedImages[resource.id] = image
                    self.attachments[resource.id]?.image = image
                    self.invalidateImage(resource.id)
                } catch is CancellationError { return }
                catch {
                    guard self.generation == current else { return }
                    let placeholder = Self.placeholder("图片加载失败，点击查看详情")
                    self.loadedImages[resource.id] = placeholder
                    self.attachments[resource.id]?.image = placeholder
                    self.invalidateImage(resource.id)
                }
            }
        }
    }

    func locate(_ request: TextReaderRequest?) {
        guard let request, lastRequestID != request.id else { return }
        guard window != nil else {
            pendingRequest = request
            return
        }
        let length = presentation.originalLength
        guard request.range.location >= 0, request.range.length > 0,
              request.range.location <= length,
              request.range.length <= length - request.range.location else { return }
        lastRequestID = request.id
        let displayed = presentation.displayRange(for: request.range)
        textView.setSelectedRange(displayed)
        textView.scrollRangeToVisible(displayed)
        reflectScrolledClipView(contentView)
    }

    func textView(_ textView: NSTextView, clickedOnLink link: Any, at charIndex: Int) -> Bool {
        guard let url = (link as? URL) ?? (link as? String).flatMap(URL.init(string:)),
              url.scheme == "know-image", let id = url.host, imageLabels[id] != nil else { return false }
        onImageClick?(id)
        return true
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window == nil {
            loadingTask?.cancel()
        } else {
            loadVisibleImages()
        }
        if window != nil, let pendingRequest {
            self.pendingRequest = nil
            locate(pendingRequest)
        }
    }
}

struct TextReader: NSViewRepresentable {
    let store: TextReaderStore
    let sourceID: String
    let revision: Int?
    let text: String
    let size: CGFloat
    let request: TextReaderRequest?
    var imageLabels: [String: String] = [:]
    var onImageClick: ((String) -> Void)? = nil
    var imageResources: [ReaderImageResource] = []
    var imageAPI: API? = nil
    var presentationSourceURL: String? = nil
    var readingPosition: ReaderTextPosition? = nil
    var onReadingPosition: ((ReaderTextPosition) -> Void)? = nil

    func makeNSView(context: Context) -> NativeWebTextReader {
        let reader = store.reader(id: sourceID, revision: revision, text: text, size: size, imageLabels: imageLabels, onImageClick: onImageClick, imageResources: imageResources, imageAPI: imageAPI, presentationSourceURL: presentationSourceURL)
        reader.bindReadingPosition(readingPosition, onChange: onReadingPosition)
        if request?.sourceID == sourceID && request?.revision == revision { reader.locate(request) }
        return reader
    }

    func updateNSView(_ view: NativeWebTextReader, context: Context) {
        view.configure(revision: revision, text: text, size: size, imageLabels: imageLabels, onImageClick: onImageClick, imageResources: imageResources, imageAPI: imageAPI, presentationSourceURL: presentationSourceURL)
        view.bindReadingPosition(readingPosition, onChange: onReadingPosition)
        if request?.sourceID == sourceID && request?.revision == revision { view.locate(request) }
    }
}

/// Display-only substitutions; the stored Markdown and search offsets remain unchanged.
struct ReaderImagePresentation {
    let content: NSAttributedString
    let originalLength: Int
    let imageRanges: [String: NSRange]
    private let replacements: [(original: NSRange, displayed: NSRange)]

    init(text: String, labels: [String: String], attachments: [String: NSTextAttachment] = [:]) {
        originalLength = text.utf16.count
        let output = NSMutableAttributedString(string: "")
        var changes: [(original: NSRange, displayed: NSRange)] = []
        let original = text as NSString
        let pattern = #"!\[[^\]]*\]\(know-image://([0-9a-fA-F-]{36})\)"#
        let matches = (try? NSRegularExpression(pattern: pattern))?.matches(in: text, range: NSRange(location: 0, length: original.length)) ?? []
        var ranges: [String: NSRange] = [:]
        var cursor = 0
        for match in matches {
            let id = original.substring(with: match.range(at: 1)).lowercased()
            guard let label = labels[id] else { continue }
            output.append(NSAttributedString(string: original.substring(with: NSRange(location: cursor, length: match.range.location - cursor))))
            let replacement = attachments[id].map { NSMutableAttributedString(attachment: $0) }
                ?? NSMutableAttributedString(string: label)
            replacement.addAttribute(.link, value: "know-image://" + id, range: NSRange(location: 0, length: replacement.length))
            let range = NSRange(location: output.length, length: replacement.length)
            ranges[id] = range
            output.append(replacement)
            changes.append((match.range, range))
            cursor = NSMaxRange(match.range)
        }
        output.append(NSAttributedString(string: original.substring(from: cursor)))
        content = output
        replacements = changes
        imageRanges = ranges
    }

    func displayRange(for original: NSRange) -> NSRange {
        func position(_ offset: Int, end: Bool) -> Int {
            var delta = 0
            for change in replacements {
                if offset <= change.original.location { break }
                if offset < NSMaxRange(change.original) {
                    return end ? NSMaxRange(change.displayed) : change.displayed.location
                }
                delta += change.displayed.length - change.original.length
            }
            return offset + delta
        }
        let start = position(original.location, end: false)
        return NSRange(location: start, length: position(NSMaxRange(original), end: true) - start)
    }
}

struct ReaderImageResource: Equatable {
    let id: String
    let path: String
    let width: Int?
    let height: Int?
}

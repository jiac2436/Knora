import PDFKit
import SwiftUI

struct PDFPageRequest {
    let page: Int
    var position: ReaderPDFPosition? = nil
    let id = UUID()
}

struct PDFReader: NSViewRepresentable {
    let view: PDFView
    let document: PDFDocument
    let request: PDFPageRequest?
    @Binding var currentPage: Int
    var onPosition: ((ReaderPDFPosition) -> Void)? = nil

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> PDFView {
        if view.document !== document {
            view.displayMode = .singlePageContinuous
            view.displayDirection = .vertical
            view.displaysPageBreaks = true
            view.pageShadowsEnabled = false
            view.backgroundColor = NSColor(InboxStyle.canvas)
            view.document = document
            view.autoScales = true
        }
        context.coordinator.view = view
        context.coordinator.observer = NotificationCenter.default.addObserver(
            forName: .PDFViewPageChanged, object: view, queue: .main
        ) { [weak coordinator = context.coordinator] _ in
            coordinator?.pageChanged()
        }
        return view
    }

    func updateNSView(_ view: PDFView, context: Context) {
        context.coordinator.parent = self
        if view.document !== document {
            view.document = document
            view.autoScales = true
            context.coordinator.lastRequestID = nil
        }
        if let request, context.coordinator.lastRequestID != request.id,
           request.page > 0, request.page <= document.pageCount,
           let target = document.page(at: request.page - 1) {
            context.coordinator.restore(request, page: target, in: view)
        }
        context.coordinator.observeScrolling()
    }

    final class Coordinator {
        var parent: PDFReader
        weak var view: PDFView?
        var observer: NSObjectProtocol?
        var lastRequestID: UUID?
        private weak var observedClip: NSClipView?
        private var scrollObserver: NSObjectProtocol?
        private var reportScheduled = false
        private var restoring = false

        func restore(_ request: PDFPageRequest, page: PDFPage, in view: PDFView) {
            lastRequestID = request.id
            restoring = true
            // PDFKit lays out and auto-scales after insertion into the window.
            // A destination applied before that pass gets reset to the page top.
            DispatchQueue.main.async { [weak self, weak view] in
                guard let self, let view, self.parent.request?.id == request.id,
                      view.document === self.parent.document else { return }
                view.layoutSubtreeIfNeeded()
                view.layoutDocumentView()
                if let position = request.position, position.page == request.page, position.isValid {
                    let box = page.bounds(for: view.displayBox)
                    let point = CGPoint(x: min(box.maxX, max(box.minX, position.x)),
                                        y: min(box.maxY, max(box.minY, position.y)))
                    view.go(to: PDFDestination(page: page, at: point))
                } else if view.currentPage !== page { view.go(to: page) }
                self.restoring = false
                self.observeScrolling()
            }
        }

        init(_ parent: PDFReader) { self.parent = parent }

        deinit {
            if let observer { NotificationCenter.default.removeObserver(observer) }
            if let scrollObserver { NotificationCenter.default.removeObserver(scrollObserver) }
        }

        func observeScrolling() {
            guard let clip = view?.documentView?.enclosingScrollView?.contentView else { return }
            if observedClip !== clip {
                if let scrollObserver { NotificationCenter.default.removeObserver(scrollObserver) }
                observedClip = clip
                clip.postsBoundsChangedNotifications = true
                scrollObserver = NotificationCenter.default.addObserver(forName: NSView.boundsDidChangeNotification,
                    object: clip, queue: .main) { [weak self] _ in self?.schedulePositionReport() }
            }
            schedulePositionReport()
        }

        private func schedulePositionReport() {
            guard !reportScheduled else { return }
            reportScheduled = true
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.reportScheduled = false
                guard !self.restoring, let view = self.view, view.window != nil, let document = view.document,
                      document === self.parent.document else { return }
                guard let destination = view.currentDestination, let page = destination.page else { return }
                let index = document.index(for: page)
                guard index != NSNotFound else { return }
                let pagePoint = destination.point
                self.parent.onPosition?(ReaderPDFPosition(page: index + 1, x: pagePoint.x, y: pagePoint.y))
            }
        }

        func pageChanged() {
            observeScrolling()
            guard let view, let page = view.currentPage, let document = view.document else { return }
            let number = document.index(for: page) + 1
            guard number > 0 else { return }
            DispatchQueue.main.async { [weak self] in
                guard let self, self.parent.document === document,
                      self.parent.currentPage != number else { return }
                self.parent.currentPage = number
            }
        }
    }
}

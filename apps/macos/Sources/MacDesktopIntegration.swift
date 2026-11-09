import AppKit
import Combine
import UniformTypeIdentifiers

@MainActor struct MacDesktopIntegration: DesktopIntegrating {
    var wakeNotifications: NotificationCenter.Publisher {
        NSWorkspace.shared.notificationCenter.publisher(for: NSWorkspace.didWakeNotification)
    }

    func clipboardFiles() -> [URL] {
        NSPasteboard.general.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
    }

    func clipboardText() -> String? { NSPasteboard.general.string(forType: .string) }

    @discardableResult func copyText(_ value: String) -> Bool {
        NSPasteboard.general.clearContents()
        return NSPasteboard.general.setString(value, forType: .string)
    }

    @discardableResult func open(_ url: URL) -> Bool { NSWorkspace.shared.open(url) }

    func chooseFiles(extensions: [String], multiple: Bool) -> [URL] {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = multiple
        if !extensions.isEmpty { panel.allowedContentTypes = extensions.compactMap { UTType(filenameExtension: $0) } }
        return panel.runModal() == .OK ? panel.urls : []
    }

    func chooseFile(title: String, completion: @escaping (URL) -> Void) {
        let panel = NSOpenPanel()
        panel.title = title
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.begin { response in
            if response == .OK, let url = panel.url { completion(url) }
        }
    }

    func saveFile(title: String, name: String, extensions: [String], completion: @escaping (URL) -> Void) {
        let panel = NSSavePanel()
        panel.title = title
        panel.nameFieldStringValue = name
        if !extensions.isEmpty { panel.allowedContentTypes = extensions.compactMap { UTType(filenameExtension: $0) } }
        panel.canCreateDirectories = true
        panel.isExtensionHidden = false
        panel.begin { response in
            if response == .OK, let url = panel.url { completion(url) }
        }
    }
}

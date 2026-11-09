import Foundation
import Combine

@MainActor protocol DesktopIntegrating {
    var wakeNotifications: NotificationCenter.Publisher { get }
    func clipboardFiles() -> [URL]
    func clipboardText() -> String?
    @discardableResult func copyText(_ value: String) -> Bool
    @discardableResult func open(_ url: URL) -> Bool
    func chooseFiles(extensions: [String], multiple: Bool) -> [URL]
    func chooseFile(title: String, completion: @escaping (URL) -> Void)
    func saveFile(title: String, name: String, extensions: [String], completion: @escaping (URL) -> Void)
}

@MainActor enum DesktopIntegration {
    static let shared: any DesktopIntegrating = MacDesktopIntegration()
}

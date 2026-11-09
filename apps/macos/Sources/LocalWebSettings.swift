import Foundation
import SwiftUI

/// A device preference for each local library, separate from article data and backups.
enum LocalWebConfiguration {
    static func key(_ library: LocalLibraryDescriptor) -> String {
        library.credentialService + ".web.allow-fake-ip"
    }

    static func allowFakeIP(_ library: LocalLibraryDescriptor) -> Bool {
        UserDefaults.standard.bool(forKey: key(library))
    }

    static func save(allowFakeIP: Bool, library: LocalLibraryDescriptor) {
        UserDefaults.standard.set(allowFakeIP, forKey: key(library))
    }

    static func environment(_ library: LocalLibraryDescriptor) -> [String: String] {
        ["PKA_WEB_ALLOW_FAKE_IP": allowFakeIP(library) ? "1" : "0"]
    }
}

@MainActor extension CaptureModel {
    func loadWebNetworkConfiguration() {
        guard usesLocalService else { return }
        do {
            let library = try localService.currentLibrary()
            webAllowFakeIP = LocalWebConfiguration.allowFakeIP(library)
            if let active = localService.activeWebAllowFakeIP {
                webNetworkRequiresRestart = active != webAllowFakeIP
                webNetworkMessage = webNetworkRequiresRestart
                    ? "已自动保存。请等待当前任务完成，用 ⌘Q 退出并重新打开客户端，再点击文章的“重试读取”。"
                    : (webAllowFakeIP ? "代理网络兼容已生效。可重新读取之前失败的网页。" : "当前使用标准网络检查。")
            } else {
                webNetworkRequiresRestart = false
                webNetworkMessage = "设置已自动保存，下次打开本地知识库时生效。"
            }
        } catch {
            webNetworkRequiresRestart = false
            webNetworkMessage = error.localizedDescription
        }
    }

    func setWebAllowFakeIP(_ enabled: Bool) {
        guard usesLocalService, !backupBusy, !trashBusy, !recoveringService else { return }
        do {
            let library = try localService.currentLibrary()
            LocalWebConfiguration.save(allowFakeIP: enabled, library: library)
            loadWebNetworkConfiguration()
        } catch { webNetworkMessage = error.localizedDescription }
    }
}

struct LocalWebNetworkSettingsView: View {
    @ObservedObject var model: CaptureModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("代理网络兼容").font(.headline)
            Toggle("兼容代理的虚拟 IP（Fake-IP）", isOn: Binding(
                get: { model.webAllowFakeIP }, set: { model.setWebAllowFakeIP($0) }))
                .disabled(model.backupBusy || model.trashBusy || model.recoveringService)
                .help("仅允许 HTTPS 域名使用代理的 198.18.0.0/15 虚拟地址；仍拒绝普通内网、本机地址和直接 IP 链接。")
            Text("普通网页与微信读取共用此设置；飞书 CLI 的登录和正文读取不使用此开关。")
                .font(.caption).foregroundStyle(.secondary)
            Text("使用代理时，普通网页或微信公众号可能提示“域名被代理解析为虚拟 IP”。遇到此提示时开启，普通网络保持关闭。")
                .font(.callout).foregroundStyle(.secondary)
            Text(model.webNetworkMessage)
                .font(.callout)
                .foregroundStyle(model.webNetworkRequiresRestart ? Color.orange : Color.secondary)
                .textSelection(.enabled)
            if model.webNetworkRequiresRestart {
                Text("仅关闭窗口不会重启内置服务。")
                    .font(.caption).foregroundStyle(.secondary)
            }
        }.task { model.loadWebNetworkConfiguration() }
    }
}

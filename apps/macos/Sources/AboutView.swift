import AppKit
import SwiftUI

struct AboutView: View {
    private static let icon: NSImage? = {
        guard let url = Bundle.main.url(forResource: "AppIcon", withExtension: "icns") else { return nil }
        return NSImage(contentsOf: url)
    }()

    var body: some View {
        Surface {
            VStack(alignment: .leading, spacing: 24) {
                HStack(spacing: 16) {
                    if let icon = Self.icon {
                        Image(nsImage: icon)
                            .resizable().interpolation(.high).scaledToFit()
                            .frame(width: 72, height: 72)
                            .accessibilityHidden(true)
                    }
                    VStack(alignment: .leading, spacing: 6) {
                        Text(AppBrand.displayName)
                            .font(.system(size: 24, weight: .semibold))
                        if let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String {
                            Text("版本 \(version)")
                                .font(.system(size: 12)).foregroundStyle(InboxStyle.muted)
                        }
                    }
                }
                VStack(alignment: .leading, spacing: 12) {
                    Text("让收藏成为随时可用的知识。")
                        .font(.system(size: 16, weight: .medium))
                    Text("收集网页、笔记与文件，用搜索和 AI 找回知识、提炼要点，也可授权 Agent 使用。")
                        .foregroundStyle(InboxStyle.text)
                    Text("资料保存在本机，AI 分析使用你配置的模型服务。")
                        .foregroundStyle(InboxStyle.muted)
                }
                .font(.system(size: 13))
                .fixedSize(horizontal: false, vertical: true)
            }
        }
        .accessibilityIdentifier("settings-about")
    }
}

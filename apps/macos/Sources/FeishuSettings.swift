import AppKit
import SwiftUI

struct FeishuSettingsView: View {
    @ObservedObject var connection: FeishuConnection
    @State private var confirmDisconnect = false

    var body: some View {
        VStack(alignment: .leading, spacing: 18) {
            Text("粘贴飞书文档链接后，使用本机 CLI 读取正文并保存到当前 \(AppBrand.name) 知识库。其他链接仍使用网页读取设置。")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: 8) {
                Text("CLI 路径").font(.system(size: 12, weight: .medium))
                TextField("留空自动查找", text: $connection.executablePath)
                    .textFieldStyle(InboxFieldStyle())
                    .accessibilityLabel("飞书 CLI 路径")
                    .disabled(connection.busy)
            }
            Button("选择 CLI 文件…") {
                if let url = DesktopIntegration.shared.chooseFiles(extensions: [], multiple: false).first {
                    connection.executablePath = url.path
                    connection.save()
                }
            }.buttonStyle(InboxButtonStyle(.secondary)).disabled(connection.busy)
            VStack(alignment: .leading, spacing: 8) {
                Text("CLI 配置名称").font(.system(size: 12, weight: .medium))
                TextField("留空使用当前配置", text: $connection.profile)
                    .textFieldStyle(InboxFieldStyle())
                    .accessibilityLabel("飞书 CLI 配置名称")
                    .disabled(connection.busy)
            }
            Text("首次使用需先完成 CLI 应用配置。连接申请文档读取和图片下载权限，不会自动创建飞书应用。图片原件会保存到 \(AppBrand.name)，并在本机识别图片文字；附件和嵌入表格暂不下载。")
                .font(.caption).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { connectionButtons }
                VStack(alignment: .leading, spacing: 8) { connectionButtons }
            }
            if connection.busy {
                HStack { ProgressView().controlSize(.small); Text("正在处理…") }
                if connection.authorizationURL != nil {
                    Button("取消本次授权") { connection.cancelAuthorization() }
                        .buttonStyle(InboxButtonStyle(.secondary))
                }
            }
            if let url = connection.authorizationURL { Link("再次打开授权页面", destination: url) }
            Text(connection.message).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(20).frame(maxWidth: .infinity, alignment: .leading)
        .background(InboxStyle.surface, in: RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(InboxStyle.separator))
        .alert("断开此 CLI 配置的飞书连接？", isPresented: $confirmDisconnect) {
            Button("取消", role: .cancel) { }
            Button("断开连接", role: .destructive) { Task { await connection.disconnect() } }
        } message: {
            Text("会清除该配置下所有用户的本地登录凭证，并可能撤销 Token；使用同一配置的其他 Agent 也会受影响。已保存到 \(AppBrand.name) 的资料不会删除，可随时重新授权。")
        }
    }

    @ViewBuilder private var connectionButtons: some View {
        Button("检查连接") { Task { await connection.check() } }
            .buttonStyle(InboxButtonStyle(.secondary)).disabled(connection.busy)
        Button("连接 / 补充读取授权") { connection.connect() }
            .buttonStyle(InboxButtonStyle(.primary)).disabled(connection.busy)
        Button("断开连接", role: .destructive) { confirmDisconnect = true }
            .buttonStyle(InboxButtonStyle(.secondary)).disabled(connection.busy)
    }
}

import Foundation
import CryptoKit

struct LocalKernelInfo: Codable {
    let kernel_version: String
    let contract_version: Int
    let schema_version: Int
    let platform: String
    let architecture: String
    let runtime_id: String
    let capabilities: [String: Bool]
}

enum LocalKernelContract {
    static let version = 1
    static let schemaVersion = 16

    static func validate(_ info: LocalKernelInfo, expectedRuntimeID: String) throws {
        guard info.contract_version == version, info.schema_version == schemaVersion,
              info.platform == "macos", !info.kernel_version.isEmpty,
              ["arm64", "x86_64"].contains(info.architecture),
              info.runtime_id == expectedRuntimeID,
              ["private_files", "file_locks", "owned_processes", "desktop_client", "capture_queue"].allSatisfy({ info.capabilities[$0] == true }) else {
            throw failure("客户端与内核运行包不兼容，请使用同一安装包重新启动。")
        }
    }

    static func runtimeID(_ root: URL) throws -> String {
        let manifest = root.appendingPathComponent("manifest.json")
        guard FileManager.default.fileExists(atPath: manifest.path) else { return "development" }
        let stream = try FileHandle(forReadingFrom: manifest)
        defer { try? stream.close() }
        let data = try stream.read(upToCount: 8 * 1024 * 1024 + 1) ?? Data()
        guard data.count <= 8 * 1024 * 1024 else { throw failure("运行包清单超过大小限制。") }
        return SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
    }
}

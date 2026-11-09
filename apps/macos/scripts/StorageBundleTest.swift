import Foundation

@main struct StorageBundleTest {
    static func main() async throws {
        let app = URL(fileURLWithPath: CommandLine.arguments[1], isDirectory: true)
        let started = Date()
        let snapshot = try await MacStorageScanner.snapshot(StorageScanLocations(roots: [.init(url: app, kind: .application)]))
        precondition(snapshot.totalBytes > 0 && snapshot[.application] > 0 && snapshot[.models] > 0)
        precondition(snapshot.unreadableItems == 0 && snapshot.unclassifiedDatabases == 0)
        precondition(snapshot.totalBytes == snapshot[.application] + snapshot[.models])
        let value: [String: Any] = ["total_bytes": snapshot.totalBytes, "application_bytes": snapshot[.application],
                                  "model_bytes": snapshot[.models], "seconds": Date().timeIntervalSince(started)]
        print(String(data: try JSONSerialization.data(withJSONObject: value, options: [.sortedKeys]), encoding: .utf8)!)
    }
}

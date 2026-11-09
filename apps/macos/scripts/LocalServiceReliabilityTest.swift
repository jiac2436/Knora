import Foundation

@main struct LocalServiceReliabilityTest {
    static func main() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("pka-reliability-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let log = root.appendingPathComponent("local-service.log")
        for index in 1...5 {
            try Data("log-\(index)".utf8).write(to: log)
            try LocalServiceLog.rotate(log, maximumBytes: 1)
        }
        let recent = try String(contentsOf: URL(fileURLWithPath: log.path + ".1"), encoding: .utf8)
        let oldest = try String(contentsOf: URL(fileURLWithPath: log.path + ".3"), encoding: .utf8)
        assert(recent == "log-5" && oldest == "log-3")
        assert(!FileManager.default.fileExists(atPath: log.path + ".4"))
        var policy = LocalRestartPolicy()
        let time = Date()
        assert(policy.nextDelay(now: time) == 1)
        assert(policy.nextDelay(now: time) == 2)
        assert(policy.nextDelay(now: time) == 4)
        assert(policy.nextDelay(now: time) == nil)
        assert(policy.nextDelay(now: time.addingTimeInterval(60)) == 1)

        let runtime = root.appendingPathComponent("runtime")
        let python = runtime.appendingPathComponent("python/bin/python3")
        try FileManager.default.createDirectory(at: python.deletingLastPathComponent(), withIntermediateDirectories: true)
        // A silent owned process proves stop can interrupt a blocked readiness read.
        try Data("#!/bin/sh\n/usr/bin/touch \"$0.started\"\nexec /bin/sleep 30\n".utf8).write(to: python)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: python.path)
        let key = "local.pka.cancel-test.\(UUID().uuidString)"
        let service = LocalService(home: root.appendingPathComponent("home"), credentialService: key)
        defer { service.stop(); TokenStore.delete(service: key) }
        let task = Task.detached { try service.start(runtime: runtime) }
        var started = false
        for _ in 0..<100 {
            if FileManager.default.fileExists(atPath: python.path + ".started") { started = true; break }
            try await Task.sleep(nanoseconds: 50_000_000)
        }
        assert(started)
        let beginning = Date()
        service.stop()
        assert(Date().timeIntervalSince(beginning) < 0.5, "取消不能被启动锁阻塞")
        do { _ = try await task.value; fatalError("取消后不应返回成功连接") }
        catch {}
        assert(Date().timeIntervalSince(beginning) < 3, "启动读取应及时结束")
        print("PASS: 启动取消、阻塞读取退出、日志轮换与恢复次数限制")
    }
}

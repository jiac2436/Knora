import Foundation

@main struct LocalWebSettingsTest {
    @MainActor static func main() async throws {
        guard CommandLine.arguments.count == 2 else { fatalError("需要独立运行包路径") }
        let runtime = URL(fileURLWithPath: CommandLine.arguments[1])
        let manager = FileManager.default
        let root = manager.temporaryDirectory.appendingPathComponent("pka-web-settings-test-\(UUID().uuidString)")
        let home = root.appendingPathComponent("home")
        try manager.createDirectory(at: home, withIntermediateDirectories: true)
        let key = "synthetic.pka.web-settings.\(UUID().uuidString)"
        let store = LocalLibraryStore(home: home, credentialService: key)
        let library = try store.current(), other = try store.allocate()
        let service = LocalService(home: home, credentialService: key, library: library)
        defer {
            service.stop()
            TokenStore.delete(service: key)
            for item in [library, other] { UserDefaults.standard.removeObject(forKey: LocalWebConfiguration.key(item)) }
            try? manager.removeItem(at: root)
        }
        try check(!LocalWebConfiguration.allowFakeIP(library), "默认不得放行虚拟 IP")
        try check(LocalWebConfiguration.environment(library) == ["PKA_WEB_ALLOW_FAKE_IP": "0"], "默认环境错误")

        // A temporary probe host records only this boolean, never credentials.
        let probe = root.appendingPathComponent("runtime")
        try manager.createDirectory(at: probe.appendingPathComponent("python/bin"), withIntermediateDirectories: true)
        try manager.createDirectory(at: probe.appendingPathComponent("backend/app"), withIntermediateDirectories: true)
        try manager.createSymbolicLink(at: probe.appendingPathComponent("python/bin/python3"),
                                      withDestinationURL: runtime.appendingPathComponent("python/bin/python3"))
        let script = """
        import json, os, pathlib, sys
        sys.stdin.readline()
        pathlib.Path(os.environ['HOME']).joinpath('web-setting-probe.json').write_text(
            json.dumps({'allow_fake_ip': os.environ.get('PKA_WEB_ALLOW_FAKE_IP')}))
        print(json.dumps({'event': 'ready', 'address': 'http://127.0.0.1:54321'}), flush=True)
        sys.stdin.read()
        """
        try script.write(to: probe.appendingPathComponent("backend/app/local_host.py"), atomically: true, encoding: .utf8)
        _ = try await Task.detached { try service.start(runtime: probe) }.value
        try check(service.activeWebAllowFakeIP == false, "启动状态应反映已运行进程的设置")
        let model = CaptureModel(localService: service, runtime: probe)
        model.loadWebNetworkConfiguration()
        model.setWebAllowFakeIP(true)
        try check(model.webAllowFakeIP && model.webNetworkRequiresRestart, "保存后应提示待重启")
        try check(model.webNetworkMessage.contains("⌘Q") && service.isRunning, "保存不得重启或中断现有服务")
        try check(LocalWebConfiguration.environment(library) == ["PKA_WEB_ALLOW_FAKE_IP": "1"], "开关未传给启动环境")
        try check(!LocalWebConfiguration.allowFakeIP(other), "不应改变另一物理知识库的设置")
        _ = await Task.detached { service.stopAndWait() }.value
        try check(service.activeWebAllowFakeIP == nil, "服务停止后不应保留有效状态")
        _ = try await Task.detached { try service.start(runtime: probe) }.value
        model.loadWebNetworkConfiguration()
        try check(!model.webNetworkRequiresRestart && service.activeWebAllowFakeIP == true, "重启后应反映实际生效值")
        let body = try JSONSerialization.jsonObject(with: Data(contentsOf: home.appendingPathComponent("web-setting-probe.json"))) as? [String: String]
        try check(body == ["allow_fake_ip": "1"], "子进程没有收到兼容开关")
        model.setWebAllowFakeIP(false)
        try check(model.webNetworkRequiresRestart, "关闭开关也应提示重启")
        model.setWebAllowFakeIP(true)
        try check(!model.webNetworkRequiresRestart, "恢复为运行值应清除待重启提示")
        model.backupBusy = true
        model.setWebAllowFakeIP(false)
        try check(LocalWebConfiguration.allowFakeIP(library), "备份切换期间不得修改当前库配置")
        print("PASS: 默认关闭、库隔离、自动保存、实际子进程环境、重启生效和任务保护")
    }

    static func check(_ condition: Bool, _ message: String) throws {
        if !condition { throw failure(message) }
    }
}

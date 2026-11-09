import Foundation

@main struct FeishuCLITest {
    static func main() async throws {
        let manager = FileManager.default
        let directory = manager.temporaryDirectory.appendingPathComponent("know-feishu-test-\(UUID().uuidString)")
        try manager.createDirectory(at: directory, withIntermediateDirectories: true)
        defer { try? manager.removeItem(at: directory) }
        let fake = directory.appendingPathComponent("lark-cli")
        let script = #"""
        #!/usr/bin/python3
        import json, sys, time, pathlib, base64
        args = sys.argv[1:]
        pathlib.Path(__file__).with_suffix('.args').write_text(json.dumps(args))
        if '--version' in args:
            print('lark-cli version 1.0.97')
        elif 'slow' in args:
            time.sleep(10)
        elif 'failure' in args:
            print(json.dumps({'ok':False,'error':{'type':'authentication','message':'private token must not appear'}}), file=sys.stderr)
            sys.exit(3)
        elif 'export-denied' in args:
            print(json.dumps({'ok':False,'error':{'type':'authorization','subtype':'permission_denied','message':'current identity does not have export permission for this document media','hint':'private token must not appear'}}), file=sys.stderr)
            sys.exit(3)
        elif 'large' in args:
            print('x' * 300000)
        elif '+media-download' in args or '+media-preview' in args:
            token = args[args.index('--token')+1]
            calls = pathlib.Path(__file__).with_suffix('.calls')
            with calls.open('a') as history:
                history.write(('+media-preview' if '+media-preview' in args else '+media-download') + ':' + token + '\n')
            if token in ['PreviewToken', 'PreviewDeniedToken'] and '+media-download' in args:
                print(json.dumps({'ok':False,'error':{'type':'authorization','subtype':'permission_denied','message':'current identity does not have export permission for this document media'}}),file=sys.stderr)
                sys.exit(3)
            if token == 'PreviewDeniedToken' and '+media-preview' in args:
                print(json.dumps({'ok':False,'error':{'type':'permission'}}),file=sys.stderr)
                sys.exit(3)
            if args[args.index('--token')+1] == 'DeniedToken':
                print(json.dumps({'ok':False,'error':{'type':'permission'}}),file=sys.stderr)
                sys.exit(3)
            file = pathlib.Path('image.png')
            file.write_bytes(base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aYZkAAAAASUVORK5CYII='))
            print(json.dumps({'ok':True,'data':{'saved_path':str(file.resolve())}}))
        else:
            content = '# 文档标题\n\n正文与工作方案。\n![示意图](https://feishu.cn/file/ImageToken)\n<img src="DeniedToken"/>'
            if 'xml' in args:
                content = '<title>文档标题</title><img src="ImageToken" id="Block1" name="示意图"/><img src="DeniedToken" id="Block2"/>'
            print(json.dumps({'ok':True,'identity':'user','data':{'document':{'document_id':'Doc','revision_id':12,'content':content}}},ensure_ascii=False))
        """#
        try script.write(to: fake, atomically: true, encoding: .utf8)
        try manager.setAttributes([.posixPermissions: 0o700], ofItemAtPath: fake.path)
        let cli = FeishuCLI(executable: fake, profile: "test-profile")

        assert(FeishuCLI.isFeishuLink(URL(string: "https://my.feishu.cn/wiki/Example")!))
        assert(FeishuCLI.isFeishuLink(URL(string: "https://team.larksuite.com/docx/Example")!))
        assert(!FeishuCLI.isFeishuLink(URL(string: "https://feishu.cn.evil.example/wiki/Example")!))
        assert(!FeishuCLI.isFeishuLink(URL(string: "https://example.com/?url=https://my.feishu.cn")!))
        let resolved = try FeishuCLI.resolve(path: fake.path)
        assert(resolved == fake)
        let document = try await cli.fetch(URL(string: "https://my.feishu.cn/wiki/Example#share-range")!)
        assert(document.title == "文档标题")
        assert(document.markdown.contains("正文与工作方案"))
        assert(document.revisionID == 12 && !document.partial)
        let args = try JSONSerialization.jsonObject(with: Data(contentsOf: fake.appendingPathExtension("args"))) as! [String]
        assert(args.prefix(2) == ["--profile", "test-profile"])
        assert(args.contains("full") && !args.contains(where: { $0.contains("#share") }))
        assert(args.contains("--as") && args.contains("user"))
        assert(args.contains("--revision-id") && args.contains("12"))
        assert(document.images.count == 2 && document.images[0].blockID == "Block1")
        let archived = try await FeishuImages.archive(document, cli: cli) { _, _ in }
        defer { if let directory = archived.imageDirectory { try? manager.removeItem(at: directory) } }
        assert(archived.images[0].file != nil)
        assert(archived.images[1].file == nil && !archived.images[1].failureReason.isEmpty)
        let linkedMarkdown = FeishuImages.markdown(archived)
        assert(linkedMarkdown.contains("know-image://\(archived.images[0].id)"))
        assert(linkedMarkdown.contains("know-image://\(archived.images[1].id)"))
        assert(!linkedMarkdown.contains("ImageToken"))
        var previewSample = document
        previewSample.images = try FeishuImages.references(in: "<img src='PreviewToken' id='PreviewBlock'/><img src='PreviewDeniedToken' id='DeniedPreviewBlock'/>")
        let previews = try await FeishuImages.archive(previewSample, cli: cli) { _, _ in }
        defer { if let directory = previews.imageDirectory { try? manager.removeItem(at: directory) } }
        assert(previews.images[0].file != nil && previews.images[0].retrievalMethod == "preview")
        assert(previews.images[0].metadata["retrieval_method"] as? String == "preview")
        assert(previews.images[0].failureReason.isEmpty)
        assert(previews.images[1].file == nil && previews.images[1].failureReason.contains("官方预览"))
        assert(archived.images[0].retrievalMethod == "download")
        let calls = try String(contentsOf: fake.appendingPathExtension("calls"), encoding: .utf8)
        assert(calls.contains("+media-preview:PreviewToken"))
        assert(!calls.contains("+media-preview:DeniedToken"), "Unrelated permission failures must not trigger preview")
        let quoteImage = try FeishuImages.references(in: "<img src='Token_1' id='Block&amp;1' name='a &quot;b&quot;'/>")
        assert(quoteImage[0].caption == "a \"b\"" && quoteImage[0].blockID == "Block&1")
        do {
            _ = try await cli.fetch(URL(string: "https://my.feishu.cn/base/Example")!)
            assertionFailure("Unsupported resource accepted")
        } catch { }
        do {
            _ = try await cli.fetch(URL(string: "https://example.com/docx/Example")!)
            assertionFailure("Non-Feishu URL accepted")
        } catch { }
        let large = try await cli.run(["large"])
        assert(large.count > 300000)
        do {
            _ = try await cli.json(["failure"])
            assertionFailure("Failure accepted")
        } catch {
            assert(error.localizedDescription.contains("重新连接"))
            assert(!error.localizedDescription.contains("private token"))
        }
        do {
            _ = try await cli.json(["export-denied"])
            assertionFailure("Export restriction ignored")
        } catch {
            assert(error.localizedDescription.contains("图片导出权限"))
            assert(error.localizedDescription.contains("重新登录不能"))
            assert(!error.localizedDescription.contains("private token"))
        }
        let scopeError = FeishuCLI.commandError(["error": ["type": "authorization", "subtype": "insufficient_scope"]])
        assert(scopeError.localizedDescription.contains("补充图片下载授权"))
        let start = Date()
        do {
            _ = try await cli.run(["slow"], timeout: 0.2)
            assertionFailure("Timeout ignored")
        } catch { assert(Date().timeIntervalSince(start) < 3) }
        let task = Task { try await cli.run(["slow"]) }
        try await Task.sleep(nanoseconds: 100_000_000)
        task.cancel()
        do { _ = try await task.value; assertionFailure("Cancellation ignored") }
        catch is CancellationError { }
        let sentinel = directory.appendingPathComponent("injected")
        _ = try await cli.run(["$(touch \(sentinel.path))"])
        assert(!manager.fileExists(atPath: sentinel.path))
        let partial: [String: Any] = ["ok": true, "identity": "user", "data": ["document": [
            "document_id": "Doc", "revision_id": 1, "content": "<title>标题</title>正文", "tips": "degraded"]]]
        let partialDocument = try FeishuCLI.document(partial, version: "1.0.97")
        assert(partialDocument.partial && partialDocument.title == "标题")
        let huge: [String: Any] = ["ok": true, "identity": "user", "data": ["document": [
            "document_id": "Doc", "revision_id": 1, "content": String(repeating: "中", count: 1_000_001)]]]
        do { _ = try FeishuCLI.document(huge, version: "1.0.97"); assertionFailure("Oversized document accepted") }
        catch { assert(error.localizedDescription.contains("未被截断")) }
        if CommandLine.arguments.contains("--preview-live") {
            let live = FeishuCLI(executable: try FeishuCLI.resolve(), profile: "")
            let document = try await live.fetch(URL(string: "https://my.feishu.cn/docx/Mjpgdiit6oTPJ1xyo2LcIpWjnld")!)
            assert(document.images.count == 9)
            let archived = try await FeishuImages.archive(document, cli: live) { done, total in
                print("Live image progress: \(done)/\(total)")
            }
            defer { if let directory = archived.imageDirectory { try? manager.removeItem(at: directory) } }
            let saved = archived.images.filter { $0.file != nil }
            assert(saved.count == document.images.count, "Some live images could not be saved")
            assert(saved.contains { $0.retrievalMethod == "preview" })
            assert(saved.contains { !$0.ocrText.isEmpty })
            print("PASS: live document images \(saved.count)/\(document.images.count), preview fallback \(saved.filter { $0.retrievalMethod == "preview" }.count), local OCR completed; temporary files removed")
        }
        if CommandLine.arguments.contains("--live") {
            let live = FeishuCLI(executable: try FeishuCLI.resolve(), profile: "")
            let result = try await live.fetch(URL(string: "https://my.feishu.cn/wiki/ObDWw96POid0AvktO3icabhHntd")!)
            assert(result.markdown.count > 100_000)
            print("PASS: native bridge read authorized Feishu document (\(result.markdown.count) characters)")
            assert(result.images.count == 39)
            let linked = FeishuImages.markdown(result)
            assert(linked.components(separatedBy: "know-image://").count - 1 == 39)
            if CommandLine.arguments.contains("--image") {
                var sample = result
                sample.images = Array(result.images.prefix(1))
                let archived = try await FeishuImages.archive(sample, cli: live) { _, _ in }
                defer { if let directory = archived.imageDirectory { try? manager.removeItem(at: directory) } }
                assert(archived.images.first?.file != nil)
                print("PASS: live original image download and local OCR (\(archived.images[0].ocrText.count) OCR characters)")
            }
        }
        print("PASS: Feishu routing, full scope, profile, Markdown parsing, large output, failure, timeout, cancellation, no shell injection")
    }
}

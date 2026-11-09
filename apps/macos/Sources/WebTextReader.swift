import AppKit
import Foundation
import CryptoKit
@preconcurrency import WebKit
import SwiftUI

// MARK: - HTML Template Generator (Cross-platform Core)

enum WebReaderTemplate {
    struct Segment {
        let origRange: NSRange
        let isImage: Bool
        let text: String
        let imageID: String?
        let imageLabel: String?
        let width: Int?
        let height: Int?
    }

    static func parseSegments(text: String, labels: [String: String], resources: [ReaderImageResource]) -> [Segment] {
        let resourceMap = Dictionary(uniqueKeysWithValues: resources.map { ($0.id.lowercased(), $0) })
        let pattern = #"!\[[^\]]*\]\(know-image://([0-9a-fA-F-]{36})\)"#
        let original = text as NSString
        let matches = (try? NSRegularExpression(pattern: pattern))?.matches(in: text, range: NSRange(location: 0, length: original.length)) ?? []
        var segments: [Segment] = []
        var cursor = 0
        for match in matches {
            let id = original.substring(with: match.range(at: 1)).lowercased()
            if match.range.location > cursor {
                let range = NSRange(location: cursor, length: match.range.location - cursor)
                segments.append(Segment(origRange: range, isImage: false, text: original.substring(with: range), imageID: nil, imageLabel: nil, width: nil, height: nil))
            }
            let label = labels[id] ?? "图片"
            let res = resourceMap[id]
            segments.append(Segment(origRange: match.range, isImage: true, text: "", imageID: id, imageLabel: label, width: res?.width, height: res?.height))
            cursor = NSMaxRange(match.range)
        }
        if cursor < original.length {
            let range = NSRange(location: cursor, length: original.length - cursor)
            segments.append(Segment(origRange: range, isImage: false, text: original.substring(with: range), imageID: nil, imageLabel: nil, width: nil, height: nil))
        }
        return segments
    }

    static func escapeHTML(_ str: String) -> String {
        str.replacingOccurrences(of: "&", with: "&amp;")
           .replacingOccurrences(of: "<", with: "&lt;")
           .replacingOccurrences(of: ">", with: "&gt;")
           .replacingOccurrences(of: "\"", with: "&quot;")
           .replacingOccurrences(of: "'", with: "&#39;")
    }

    static func buildHTML(text: String, initialFontSize: CGFloat, labels: [String: String], resources: [ReaderImageResource], presentationSourceURL: String? = nil) -> String {
        let xiaohongshu = XiaohongshuReader.isPostURL(presentationSourceURL)
        let resourceDict = resources.map { [
            "id": $0.id.lowercased(),
            "width": $0.width ?? 600,
            "height": $0.height ?? 200
        ] }
        let resourcesJson = (try? String(data: JSONSerialization.data(withJSONObject: resourceDict), encoding: .utf8)) ?? "[]"
        let labelsJson = (try? String(data: JSONSerialization.data(withJSONObject: labels), encoding: .utf8)) ?? "{}"
        let rawMarkdownJson = (try? String(data: JSONSerialization.data(withJSONObject: [text]), encoding: .utf8)) ?? "[\"\"]"

        return """
        <!DOCTYPE html>
        <html>
        <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0, user-scalable=no">
        <style>
          :root {
            --reader-font-size: \(initialFontSize)px;
            --accent-color: #705dcf;
            --text-normal: #2e3338;
            --text-muted: #6e7681;
            --bg-primary: #ffffff;
            --bg-hover: rgba(0, 0, 0, 0.04);
            --border-color: rgba(0, 0, 0, 0.12);
            --table-header-bg: #f6f8fa;
            --table-alt-bg: rgba(0, 0, 0, 0.015);
            --table-hover-bg: rgba(0, 0, 0, 0.035);
            --code-bg: rgba(0, 0, 0, 0.05);
            --code-block-bg: #f8f9fa;
            --code-text: #24292e;
            --blockquote-border: #705dcf;
            --blockquote-bg: rgba(112, 93, 207, 0.06);
            --link-color: #0969da;
            --callout-border-width: 4px;
          }
          * { box-sizing: border-box; }
          html, body {
            margin: 0;
            padding: 0;
            background: transparent;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            font-size: var(--reader-font-size);
            line-height: 1.7;
            -webkit-user-select: text;
            user-select: text;
            word-wrap: break-word;
            overflow-wrap: break-word;
          }
          body {
            padding: 12px 0 48px;
            color: var(--text-normal);
          }
          @media (prefers-color-scheme: dark) {
            :root {
              --accent-color: #a8b6f2;
              --text-normal: #ececec;
              --text-muted: #969696;
              --bg-primary: #181818;
              --bg-hover: rgba(255, 255, 255, 0.05);
              --border-color: rgba(255, 255, 255, 0.07);
              --table-header-bg: #292929;
              --table-alt-bg: rgba(255, 255, 255, 0.018);
              --table-hover-bg: rgba(255, 255, 255, 0.045);
              --code-bg: rgba(255, 255, 255, 0.08);
              --code-block-bg: #202020;
              --code-text: #ececec;
              --blockquote-border: #a8b6f2;
              --blockquote-bg: rgba(168, 182, 242, 0.07);
              --link-color: #a8b6f2;
            }
            body { color: var(--text-normal); }
            mark.evidence-highlight { background-color: #d4a300; color: #fff; }
          }
          .markdown-body {
            max-width: 100%;
            margin: 0 auto;
          }
          h1, h2, h3, h4, h5, h6 {
            font-weight: 600;
            line-height: 1.35;
            margin-top: 1.5em;
            margin-bottom: 0.5em;
            color: inherit;
          }
          h1 {
            font-size: 1.55em;
            font-weight: 600;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 0.3em;
          }
          h2 {
            font-size: 1.25em;
            border-bottom: 1px solid var(--border-color);
            padding-bottom: 0.25em;
          }
          h3 { font-size: 1.12em; }
          h4 { font-size: 1.1em; }
          h5 { font-size: 1.0em; }
          h6 { font-size: 0.9em; color: var(--text-muted); }
          p { margin: 0.9em 0; }
          hr {
            border: none;
            border-top: 1px solid var(--border-color);
            margin: 2em 0;
          }
          blockquote {
            margin: 1.2em 0;
            padding: 10px 18px;
            border-left: 4px solid var(--blockquote-border);
            background: var(--blockquote-bg);
            border-radius: 0 8px 8px 0;
          }
          blockquote > :first-child { margin-top: 0; }
          blockquote > :last-child { margin-bottom: 0; }

          /* Obsidian Callouts */
          .callout {
            margin: 1.2em 0;
            padding: 12px 16px;
            border-radius: 8px;
            border-left: var(--callout-border-width) solid var(--callout-color, var(--accent-color));
            background: var(--callout-bg, var(--blockquote-bg));
            box-shadow: 0 1px 3px rgba(0, 0, 0, 0.04);
          }
          .callout-title {
            display: flex;
            align-items: center;
            gap: 8px;
            font-weight: 600;
            font-size: 0.95em;
            color: var(--callout-color, var(--accent-color));
            margin-bottom: 6px;
          }
          .callout-icon {
            display: inline-flex;
            align-items: center;
            width: 18px;
            height: 18px;
          }
          .callout-icon svg {
            width: 18px;
            height: 18px;
            fill: currentColor;
          }
          .callout-title-text {
            line-height: 1.2;
          }
          .callout-content {
            font-size: 0.95em;
            line-height: 1.65;
          }
          .callout-content > :first-child { margin-top: 0; }
          .callout-content > :last-child { margin-bottom: 0; }

          .callout-note, .callout-info {
            --callout-color: #0a84ff;
            --callout-bg: rgba(10, 132, 255, 0.08);
          }
          .callout-tip, .callout-hint, .callout-important {
            --callout-color: #30d158;
            --callout-bg: rgba(48, 209, 88, 0.08);
          }
          .callout-warning, .callout-caution, .callout-attention {
            --callout-color: #ff9f0a;
            --callout-bg: rgba(255, 159, 10, 0.08);
          }
          .callout-danger, .callout-error, .callout-bug {
            --callout-color: #ff453a;
            --callout-bg: rgba(255, 69, 58, 0.08);
          }
          .callout-success, .callout-check, .callout-done {
            --callout-color: #30d158;
            --callout-bg: rgba(48, 209, 88, 0.08);
          }
          .callout-question, .callout-help, .callout-faq {
            --callout-color: #bf5af2;
            --callout-bg: rgba(191, 90, 242, 0.08);
          }
          .callout-quote, .callout-cite {
            --callout-color: #98989d;
            --callout-bg: rgba(152, 152, 157, 0.08);
          }

          ul, ol {
            padding-left: 1.8em;
            margin: 0.8em 0;
          }
          li { margin: 0.35em 0; }

          /* Task Lists */
          ul.contains-task-list, li.task-list-item {
            list-style: none;
          }
          li.task-list-item {
            display: flex;
            align-items: flex-start;
            gap: 8px;
            margin-left: -1.2em;
          }
          li.task-list-item input[type="checkbox"] {
            margin-top: 5px;
            width: 15px;
            height: 15px;
            accent-color: var(--accent-color);
            cursor: pointer;
          }

          /* Tables (GFM) with Horizontal Scroll */
          .table-wrapper {
            overflow-x: auto;
            margin: 1.4em 0;
            border-radius: 8px;
            border: 1px solid var(--border-color);
          }
          table {
            border-collapse: collapse;
            width: 100%;
            margin: 0;
            font-size: 0.95em;
          }
          th, td {
            border: 1px solid var(--border-color);
            padding: 9px 15px;
            text-align: left;
          }
          th {
            background: var(--table-header-bg);
            font-weight: 600;
            border-bottom: 2px solid var(--border-color);
          }
          tr:nth-child(even) {
            background: var(--table-alt-bg);
          }
          tr:hover {
            background: var(--table-hover-bg);
          }

          /* Code blocks */
          pre {
            background: var(--code-block-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 14px 18px;
            overflow-x: auto;
            font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.9em;
            line-height: 1.55;
            margin: 1.2em 0;
          }
          code {
            background: var(--code-bg);
            border-radius: 4px;
            padding: 2px 6px;
            font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
            font-size: 0.88em;
            color: var(--code-text);
          }
          pre code {
            background: transparent;
            padding: 0;
            border-radius: 0;
            border: none;
            color: inherit;
          }

          /* Links */
          a {
            color: var(--link-color);
            text-decoration: none;
            border-bottom: 1px solid var(--border-color);
            transition: all 0.15s ease-in-out;
            cursor: pointer;
          }
          a:hover {
            border-bottom-color: var(--link-color);
            background-color: var(--bg-hover);
            border-radius: 2px;
          }

          /* Images & Media */
          .image-segment {
            display: block;
            margin: 20px auto;
            cursor: pointer;
            width: 100%;
          }
          .image-box {
            width: 100%;
            max-width: 100%;
            background: rgba(128, 128, 128, 0.12);
            border-radius: 8px;
            overflow: hidden;
            display: flex;
            align-items: center;
            justify-content: center;
            position: relative;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.06);
          }
          .image-box img {
            width: 100%;
            height: 100%;
            object-fit: contain;
            display: block;
            border-radius: 8px;
          }
          img.standard-image {
            max-width: 100%;
            height: auto;
            border-radius: 8px;
            margin: 16px auto;
            display: block;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.08);
          }
          .media-container {
            margin: 1.4em 0;
            width: 100%;
          }
          video {
            width: 100%;
            max-width: 100%;
            border-radius: 8px;
            background: #000;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.1);
            outline: none;
            display: block;
          }
          audio {
            width: 100%;
            border-radius: 8px;
            outline: none;
            display: block;
          }
          .media-caption {
            font-size: 0.85em;
            color: var(--text-muted);
            text-align: center;
            margin-top: 6px;
          }
          .image-placeholder {
            color: var(--text-muted);
            font-size: 14px;
            display: flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            height: 100%;
            padding: 16px;
            text-align: center;
          }
          mark.evidence-highlight {
            background-color: #ffd60a;
            color: #000;
            border-radius: 3px;
            padding: 2px 3px;
            font-weight: 500;
          }
        \(xiaohongshu ? XiaohongshuReader.styles : "")
        </style>
        <script>
        \(MarkedLib.source)
        </script>
        <script>
          window.pkaRawMarkdown = (\(rawMarkdownJson))[0];
          window.pkaResources = {};
          (\(resourcesJson)).forEach(function(r) { window.pkaResources[r.id] = r; });
          window.pkaLabels = \(labelsJson);

          function setFontSize(px) {
            document.documentElement.style.setProperty('--reader-font-size', px + 'px');
          }

          function handleImageClick(id) {
            if (window.webkit && window.webkit.messageHandlers && window.webkit.messageHandlers.readerBridge) {
              window.webkit.messageHandlers.readerBridge.postMessage({ type: 'imageClick', id: id });
            }
          }

          function setImageData(id, dataUrl) {
            var img = document.getElementById('img-' + id);
            var ph = document.getElementById('placeholder-' + id);
            if (img) {
              img.src = dataUrl;
              img.style.display = 'block';
              if (ph) ph.style.display = 'none';
            }
          }

          function updateImageMetadata(resources, labels) {
            var anchor = Array.from(document.querySelectorAll('p,h1,h2,h3,.image-segment')).find(function(el) {
              return el.getBoundingClientRect().bottom > 0;
            });
            var before = anchor ? anchor.getBoundingClientRect().top : 0;
            window.pkaResources = {};
            resources.forEach(function(r) { window.pkaResources[r.id] = r; });
            window.pkaLabels = labels;
            Object.keys(labels).forEach(function(id) {
              var placeholder = document.getElementById('placeholder-' + id);
              if (placeholder) placeholder.textContent = labels[id];
              var img = document.getElementById('img-' + id);
              var res = window.pkaResources[id];
              if (img && res && res.width && res.height) {
                img.parentElement.style.aspectRatio = res.width + ' / ' + res.height;
              }
            });
            if (anchor) window.scrollBy(0, anchor.getBoundingClientRect().top - before);
          }

          function clearHighlights() {
            var marks = document.querySelectorAll('mark.evidence-highlight');
            marks.forEach(function(m) {
              var parent = m.parentNode;
              while (m.firstChild) parent.insertBefore(m.firstChild, m);
              parent.removeChild(m);
              parent.normalize();
            });
          }

          \(ReaderSourceMap.script)
          \(ReadingPositionScript.source)
          \(xiaohongshu ? XiaohongshuReader.script(sourceURL: presentationSourceURL ?? "") : "")

          function locateEvidence(targetStart, targetLength) {
            return locateMappedEvidence(targetStart, targetLength);
          }

          function getCalloutIcon(type) {
            switch (type) {
              case 'tip':
              case 'hint':
              case 'important':
                return '<svg viewBox=\"0 0 24 24\"><path d=\"M9 21c0 .55.45 1 1 1h4c.55 0 1-.45 1-1v-1H9v1zm3-19C8.14 2 5 5.14 5 9c0 2.38 1.19 4.47 3 5.74V17c0 .55.45 1 1 1h6c.55 0 1-.45 1-1v-2.26c1.81-1.27 3-3.36 3-5.74 0-3.86-3.14-7-7-7zm2.85 11.1l-.85.6V16h-4v-1.3l-.85-.6A5.003 5.003 0 0 1 7 9c0-2.76 2.24-5 5-5s5 2.24 5 5c0 1.63-.8 3.16-2.15 4.1z\"/></svg>';
              case 'warning':
              case 'caution':
              case 'attention':
                return '<svg viewBox=\"0 0 24 24\"><path d=\"M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z\"/></svg>';
              case 'danger':
              case 'error':
              case 'bug':
                return '<svg viewBox=\"0 0 24 24\"><path d=\"M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-2h2v2zm0-4h-2V7h2v6z\"/></svg>';
              case 'success':
              case 'check':
              case 'done':
                return '<svg viewBox=\"0 0 24 24\"><path d=\"M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z\"/></svg>';
              case 'question':
              case 'help':
              case 'faq':
                return '<svg viewBox=\"0 0 24 24\"><path d=\"M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 16h-2v-2h2v2zm1.07-7.75l-.9.92C12.45 11.9 12 12.5 12 14h-2v-.5c0-1.1.45-2.1 1.17-2.83l1.24-1.26c.37-.36.59-.86.59-1.41 0-1.1-.9-2-2-2s-2 .9-2 2H7c0-2.76 2.24-5 5-5s5 2.24 5 5c0 .88-.36 1.68-.93 2.25z\"/></svg>';
              case 'quote':
              case 'cite':
                return '<svg viewBox=\"0 0 24 24\"><path d=\"M6 17h3l2-4V7H5v6h3zm8 0h3l2-4V7h-6v6h3z\"/></svg>';
              default:
                return '<svg viewBox=\"0 0 24 24\"><path d=\"M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm1 15h-2v-6h2v6zm0-8h-2V7h2v2z\"/></svg>';
            }
          }

          function renderMarkdown() {
            var renderer = new marked.Renderer();

            renderer.blockquote = function(token) {
              var quote = this.parser.parse(token.tokens);
              var match = quote.match(/^\\s*<p>\\s*(<span\\b[^>]*>)?\\[!([a-zA-Z0-9_-]+)\\]([^\\n<]*)(?:<\\/span>)?(?:<br\\s*\\/?>)?([\\s\\S]*?)<\\/p>([\\s\\S]*)$/);
              if (match) {
                var rawType = match[2].toLowerCase();
                var customTitle = match[3].trim();
                var firstP = match[4] || '';
                var rest = match[5] || '';
                var title = customTitle || rawType.toUpperCase();
                if (match[1] && customTitle) title = match[1] + title + '</span>';
                var contentHtml = (firstP.trim() ? '<p>' + firstP + '</p>' : '') + rest;
                var icon = getCalloutIcon(rawType);
                return '<div class=\"callout callout-' + rawType + '\">' +
                       '  <div class=\"callout-title\">' +
                       '    <span class=\"callout-icon\">' + icon + '</span>' +
                       '    <span class=\"callout-title-text\">' + title + '</span>' +
                       '  </div>' +
                       (contentHtml.trim() ? '  <div class=\"callout-content\">' + contentHtml + '</div>' : '') +
                       '</div>';
              }
              return '<blockquote>' + quote + '</blockquote>';
            };

            renderer.table = function(token) {
              var table = marked.Renderer.prototype.table.call(this, token);
              return '<div class="table-wrapper">' + table + '</div>';
            };

            renderer.link = function(token) {
              var href = token.href, title = token.title, text = this.parser.parseInline(token.tokens);
              var titleAttr = title ? ' title=\"' + title + '\"' : '';
              return '<a href=\"' + href + '\" target=\"_blank\" rel=\"noopener noreferrer\"' + titleAttr + '>' + text + '</a>';
            };

            renderer.image = function(token) {
              var href = token.href, title = token.title, text = token.text;
              if (!href) return '';
              var cleanHref = href.toLowerCase();
              var isAudio = cleanHref.endsWith('.mp3') || cleanHref.endsWith('.wav') || cleanHref.endsWith('.m4a') || cleanHref.endsWith('.aac') || cleanHref.endsWith('.ogg');
              var isVideo = cleanHref.endsWith('.mp4') || cleanHref.endsWith('.mov') || cleanHref.endsWith('.webm') || cleanHref.endsWith('.m4v');

              if (isAudio) {
                return '<div class=\"media-container audio-container\">' +
                       '  <audio controls src=\"' + href + '\" preload=\"metadata\"></audio>' +
                       (text ? '<div class=\"media-caption\">' + text + '</div>' : '') +
                       '</div>';
              }
              if (isVideo) {
                return '<div class=\"media-container video-container\">' +
                       '  <video controls src=\"' + href + '\" preload=\"metadata\"></video>' +
                       (text ? '<div class=\"media-caption\">' + text + '</div>' : '') +
                       '</div>';
              }
              if (href.indexOf('know-image://') === 0) {
                var id = href.replace('know-image://', '').toLowerCase();
                var res = window.pkaResources[id] || {};
                var w = res.width || 600;
                var h = res.height || 200;
                var label = text || window.pkaLabels[id] || '查看图片';
                return '<div class=\"image-segment\" data-image-id=\"' + id + '\" onclick=\"handleImageClick(\\'' + id + '\\')\">' +
                       '  <div class=\"image-box\" style=\"aspect-ratio: ' + w + ' / ' + h + ';\">' +
                       '    <div class=\"image-placeholder\" id=\"placeholder-' + id + '\">' + label + '</div>' +
                       '    <img id=\"img-' + id + '\" style=\"display: none;\" alt=\"' + label + '\" />' +
                       '  </div>' +
                       '</div>';
              }
              var caption = text ? '<div class=\"media-caption\">' + text + '</div>' : '';
              return '<div class=\"image-segment\">' +
                     '  <img class=\"standard-image\" src=\"' + href + '\" alt=\"' + (text || '') + '\" loading=\"lazy\" />' +
                     caption +
                     '</div>';
            };

            if (typeof renderXiaohongshu === 'function' && renderXiaohongshu(renderer)) return;
            var sourceTokens = prepareSourceTokens(renderer);
            document.getElementById('content-area').innerHTML = marked.parser(sourceTokens, { renderer: renderer, gfm: true, breaks: true });
          }
        </script>
        </head>
        <body onload="renderMarkdown()">
          <div class="markdown-body" id="content-area"></div>
        </body>
        </html>
        """
    }
}

private final class WebReaderScriptBridge: NSObject, WKScriptMessageHandler {
    weak var target: NativeWebTextReader?

    init(target: NativeWebTextReader) {
        self.target = target
        super.init()
    }

    func userContentController(_ userContentController: WKUserContentController, didReceive message: WKScriptMessage) {
        target?.handleScriptMessage(message)
    }
}

// MARK: - Native Web Text Reader (WKWebView Implementation)

@MainActor
final class NativeWebTextReader: NSView, WKNavigationDelegate, WKUIDelegate {
    private let webView: WKWebView
    private var scriptBridge: WebReaderScriptBridge?
    private var revision: Int?
    private var currentText: String = ""
    private var presentationSourceURL: String?
    private var configured = false
    private var imageLabels: [String: String] = [:]
    private var imageResources: [ReaderImageResource] = []
    private var imageAPI: API?
    private var onImageClick: ((String) -> Void)?
    private var fontSize: CGFloat = 16
    private var loadedImageIDs: Set<String> = []
    private var isNavigationFinished = false
    private var pendingRequest: TextReaderRequest?
    private var lastRequestID: UUID?
    private var loadingTasks: [String: Task<Void, Never>] = [:]
    private var imageLoadGeneration = UUID()
    private var readingGeneration = UUID()
    private var readingInstalled = false
    private var readingPosition: ReaderTextPosition?
    private var onReadingPosition: ((ReaderTextPosition) -> Void)?
    private var contentSignature = ""

    override init(frame frameRect: NSRect) {
        let config = WKWebViewConfiguration()
        let controller = WKUserContentController()
        config.userContentController = controller
        config.websiteDataStore = WKWebsiteDataStore.nonPersistent()
        
        self.webView = WKWebView(frame: frameRect, configuration: config)
        self.webView.autoresizingMask = [.width, .height]
        super.init(frame: frameRect)

        let bridge = WebReaderScriptBridge(target: self)
        self.scriptBridge = bridge
        controller.add(bridge, name: "readerBridge")
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.setValue(false, forKey: "drawsBackground") // Transparent background

        addSubview(webView)
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        for task in loadingTasks.values { task.cancel() }
        loadingTasks.removeAll()
    }

    func handleScriptMessage(_ message: WKScriptMessage) {
        if message.name == "readerBridge", let body = message.body as? [String: Any] {
            if body["type"] as? String == "readingPosition" {
                guard isNavigationFinished, readingInstalled, window != nil,
                      body["generation"] as? String == readingGeneration.uuidString,
                      let raw = body["position"] as? [String: Any],
                      let fraction = raw["scrollFraction"] as? Double,
                      let element = raw["elementFraction"] as? Double else { return }
                let position = ReaderTextPosition(sourceOffset: raw["sourceOffset"] as? Int,
                    imageID: raw["imageID"] as? String, elementFraction: element,
                    scrollFraction: fraction, contentSignature: contentSignature)
                if position.isValid { onReadingPosition?(position) }
                return
            }
            if let type = body["type"] as? String, let id = body["id"] as? String {
                handleAction(type: type, id: id)
            }
        }
    }

    func handleAction(type: String, id: String) {
        if type == "imageClick" {
            onImageClick?(id)
        }
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration, for navigationAction: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let url = navigationAction.request.url, url.scheme == "http" || url.scheme == "https" {
            DesktopIntegration.shared.open(url)
        }
        return nil
    }

    func webView(_ webView: WKWebView, decidePolicyFor navigationAction: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if navigationAction.navigationType == .linkActivated, let url = navigationAction.request.url {
            if url.scheme == "http" || url.scheme == "https" {
                DesktopIntegration.shared.open(url)
            }
            decisionHandler(.cancel)
            return
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        isNavigationFinished = true
        updateImageMetadata()
        installReadingBridge()
        if let pending = pendingRequest {
            self.pendingRequest = nil
            locate(pending)
        }
        loadAllImages()
    }

    func configure(revision: Int?, text: String, size: CGFloat, imageLabels: [String: String] = [:],
                   onImageClick: ((String) -> Void)? = nil, imageResources: [ReaderImageResource] = [],
                   imageAPI: API? = nil, presentationSourceURL: String? = nil) {
        self.onImageClick = onImageClick
        let imageMetadataChanged = self.imageLabels != imageLabels || self.imageResources != imageResources
        let contentChanged = !configured || self.revision != revision || self.currentText != text ||
                             self.presentationSourceURL != presentationSourceURL ||
                             self.imageAPI?.base != imageAPI?.base || self.imageAPI?.token != imageAPI?.token

        if contentChanged {
            readingGeneration = UUID(); readingInstalled = false
            contentSignature = SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
            imageLoadGeneration = UUID()
            self.revision = revision
            self.currentText = text
            self.presentationSourceURL = presentationSourceURL
            self.imageLabels = imageLabels
            self.imageResources = imageResources
            self.imageAPI = imageAPI
            self.configured = true
            self.fontSize = size
            self.loadedImageIDs.removeAll()
            self.isNavigationFinished = false
            self.lastRequestID = nil
            self.pendingRequest = nil

            for task in loadingTasks.values { task.cancel() }
            loadingTasks.removeAll()

            let html = WebReaderTemplate.buildHTML(text: text, initialFontSize: size, labels: imageLabels, resources: imageResources, presentationSourceURL: presentationSourceURL)
            webView.loadHTMLString(html, baseURL: nil)
        } else {
            if imageMetadataChanged {
                self.imageLabels = imageLabels
                self.imageResources = imageResources
                let retained = Set(imageResources.map(\.id))
                loadedImageIDs.formIntersection(retained)
                for id in Array(loadingTasks.keys) where !retained.contains(id) {
                    loadingTasks.removeValue(forKey: id)?.cancel()
                }
                if isNavigationFinished { updateImageMetadata(); loadAllImages() }
            }
            if fontSize != size {
                fontSize = size
                webView.evaluateJavaScript("setFontSize(\(size));", completionHandler: nil)
            }
        }
    }

    func bindReadingPosition(_ position: ReaderTextPosition?, onChange: ((ReaderTextPosition) -> Void)?) {
        readingPosition = position
        onReadingPosition = onChange
        installReadingBridge()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window != nil {
            installReadingBridge()
            if readingInstalled { webView.evaluateJavaScript("window.pkaReportReadingPosition && window.pkaReportReadingPosition();", completionHandler: nil) }
        }
    }

    override func layout() {
        super.layout()
        installReadingBridge()
    }

    private func installReadingBridge() {
        guard !readingInstalled, isNavigationFinished, onReadingPosition != nil,
              window != nil, bounds.width > 0, bounds.height > 0 else { return }
        var position = pendingRequest == nil ? readingPosition : nil
        if position?.contentSignature != contentSignature {
            // Edited/reparsed text has different source offsets. Preserve only
            // approximate progress rather than jumping to an unrelated paragraph.
            position?.sourceOffset = nil; position?.imageID = nil
        }
        let initial: Any
        if let position, position.isValid,
           let encoded = try? JSONEncoder().encode(position),
           let object = try? JSONSerialization.jsonObject(with: encoded) {
            initial = object
        } else { initial = NSNull() }
        guard let data = try? JSONSerialization.data(withJSONObject: [readingGeneration.uuidString, initial]),
              let json = String(data: data, encoding: .utf8) else { return }
        readingInstalled = true
        webView.evaluateJavaScript("installReadingPosition((\(json))[0],(\(json))[1]);", completionHandler: nil)
    }

    private func updateImageMetadata() {
        let resources: [[String: Any]] = imageResources.map {
            ["id": $0.id, "width": $0.width ?? 600, "height": $0.height ?? 200]
        }
        guard let data = try? JSONSerialization.data(withJSONObject: [resources, imageLabels]),
              let json = String(data: data, encoding: .utf8) else { return }
        webView.evaluateJavaScript("updateImageMetadata((\(json))[0],(\(json))[1]);", completionHandler: nil)
    }

    func locate(_ request: TextReaderRequest?) {
        guard let request, lastRequestID != request.id else { return }
        guard isNavigationFinished else {
            pendingRequest = request
            return
        }
        lastRequestID = request.id
        let js = "if(window.pkaStopReadingRestore) window.pkaStopReadingRestore(); locateEvidence(\(request.range.location), \(request.range.length));"
        webView.evaluateJavaScript(js, completionHandler: nil)
    }

    #if DEBUG || CAPTURE_QUEUE_TESTS
    func isImageLoaded(_ id: String) -> Bool {
        loadedImageIDs.contains(id)
    }
    func evaluateForTesting(_ script: String) async throws -> Any? {
        // WebKit's async importer unwraps nil when JavaScript returns undefined.
        try await withCheckedThrowingContinuation { continuation in
            webView.evaluateJavaScript(script) { result, error in
                if let error { continuation.resume(throwing: error) }
                else { continuation.resume(returning: result) }
            }
        }
    }
    #endif

    private func loadAllImages() {
        guard let api = imageAPI, !imageResources.isEmpty else { return }
        let generation = imageLoadGeneration
        for res in imageResources {
            guard !loadedImageIDs.contains(res.id), loadingTasks[res.id] == nil else { continue }
            let task = Task { [weak self, res, api] in
                guard let self else { return }
                let tempDir = FileManager.default.temporaryDirectory.appendingPathComponent("know-web-img-\(UUID().uuidString)")
                defer {
                    try? FileManager.default.removeItem(at: tempDir)
                    DispatchQueue.main.async { [weak self] in
                        guard self?.imageLoadGeneration == generation else { return }
                        self?.loadingTasks.removeValue(forKey: res.id)
                    }
                }
                do {
                    try Task.checkCancellation()
                    let file = try await api.downloadFile(res.path, into: tempDir, named: res.id)
                    let base64 = try await Task.detached(priority: .utility) { () throws -> String in
                        guard let source = CGImageSourceCreateWithURL(file as CFURL, nil),
                              let thumbnail = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                                kCGImageSourceCreateThumbnailFromImageAlways: true,
                                kCGImageSourceThumbnailMaxPixelSize: 1600,
                                kCGImageSourceCreateThumbnailWithTransform: true] as CFDictionary) else {
                            throw failure("图片解析失败")
                        }
                        let bitmap = NSBitmapImageRep(cgImage: thumbnail)
                        guard let data = bitmap.representation(using: .jpeg, properties: [.compressionFactor: 0.85]) else {
                            throw failure("图片编码失败")
                        }
                        return "data:image/jpeg;base64," + data.base64EncodedString()
                    }.value
                    try? FileManager.default.removeItem(at: file)
                    try Task.checkCancellation()

                    DispatchQueue.main.async { [weak self] in
                        guard let self, self.imageLoadGeneration == generation,
                              self.imageResources.contains(res) else { return }
                        self.loadedImageIDs.insert(res.id)
                        let js = "setImageData('\(res.id)', '\(base64)');"
                        self.webView.evaluateJavaScript(js, completionHandler: nil)
                    }
                } catch {
                    // Ignored on cancellation or failure
                }
            }
            loadingTasks[res.id] = task
        }
    }
}

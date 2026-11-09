import Foundation

struct LocalSearchMatch {
    let preview: String
    let range: NSRange
}

enum SearchLocation {
    static func localMatches(in text: String, query: String, limit: Int = 20) -> [LocalSearchMatch] {
        guard !query.isEmpty else { return [] }
        var matches: [LocalSearchMatch] = []
        var cursor = text.startIndex
        while cursor < text.endIndex,
              let range = text.range(of: query, options: [.caseInsensitive, .diacriticInsensitive],
                                     range: cursor..<text.endIndex),
              matches.count < limit {
            let start = text.index(range.lowerBound, offsetBy: -55, limitedBy: text.startIndex) ?? text.startIndex
            let end = text.index(range.upperBound, offsetBy: 70, limitedBy: text.endIndex) ?? text.endIndex
            matches.append(LocalSearchMatch(
                preview: String(text[start..<end]).replacingOccurrences(of: "\n", with: " "),
                range: NSRange(range, in: text)))
            cursor = range.upperBound
        }
        return matches
    }

    static func resultRange(in text: String, query: String, snippet: String,
                            locatorKind: String?, locatorStart: Int?, locatorEnd: Int?, matchText: String? = nil) -> NSRange? {
        guard !text.isEmpty else { return nil }
        let full = NSRange(location: 0, length: text.utf16.count)
        var anchor: NSRange?
        if matchText != nil && (locatorKind != "text" || locatorStart == nil || locatorEnd == nil) { return nil }
        if locatorKind == "text", let locatorStart, let locatorEnd,
           let start = utf16Offset(forScalarOffset: locatorStart, in: text),
           let end = utf16Offset(forScalarOffset: locatorEnd, in: text), end > start {
            let proposed = NSRange(location: start, length: end - start)
            if let matchText {
                // New passage results carry the actual original span, not just the query.
                guard (text as NSString).substring(with: proposed) == matchText else { return nil }
                return proposed
            }
            anchor = proposed
        } else if matchText != nil {
            return nil
        } else if !snippet.isEmpty {
            let prefix = String(snippet.prefix(80)).trimmingCharacters(in: .whitespacesAndNewlines)
            if !prefix.isEmpty, let found = text.range(of: prefix) {
                anchor = NSRange(found, in: text)
            }
        }
        if let anchor, let match = findQuery(query, in: text, within: anchor) { return match }
        // Semantic retrieval can match a related passage even when the query occurs elsewhere.
        if let anchor { return anchor }
        if let match = findQuery(query, in: text, within: full) { return match }
        return nil
    }

    private static func findQuery(_ query: String, in text: String, within region: NSRange) -> NSRange? {
        guard let range = Range(region, in: text) else { return nil }
        let terms = [query] + query.split(whereSeparator: \.isWhitespace)
            .map(String.init).filter { $0 != query }.sorted { $0.count > $1.count }
        for term in terms where !term.isEmpty {
            if let match = text.range(of: term, options: [.caseInsensitive, .diacriticInsensitive], range: range) {
                return NSRange(match, in: text)
            }
        }
        return nil
    }

    private static func utf16Offset(forScalarOffset offset: Int, in text: String) -> Int? {
        guard offset >= 0,
              let index = text.unicodeScalars.index(text.unicodeScalars.startIndex,
                                                    offsetBy: offset, limitedBy: text.unicodeScalars.endIndex) else {
            return nil
        }
        return index.utf16Offset(in: text)
    }
}

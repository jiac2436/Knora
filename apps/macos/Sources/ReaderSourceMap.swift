import Foundation

/// Source offsets belong to Markdown, not concatenated DOM text. Marked text tokens
/// retain their source range so repeated phrases and inline markup stay unambiguous.
enum ReaderSourceMap {
    static let script = #"""
    function prepareSourceTokens(renderer, sourceStart, sourceEnd) {
      var original = window.pkaRawMarkdown || '';
      var normalized = '', offsets = [], i = sourceStart === undefined ? 0 : sourceStart;
      var limit = sourceEnd === undefined ? original.length : sourceEnd;
      while (i < limit) {
        offsets.push(i);
        if (original[i] === '\r') {
          normalized += '\n'; i += original[i + 1] === '\n' ? 2 : 1;
        } else { normalized += original[i]; i++; }
      }
      offsets.push(limit);
      var tokens = marked.lexer(normalized, { gfm: true, breaks: true });
      var ranges = new WeakMap();
      function annotate(sequence, start, end) {
        var cursor = start;
        sequence.forEach(function(token) {
          if (!token || typeof token !== 'object') return;
          var raw = typeof token.raw === 'string' ? token.raw : '';
          var found = raw ? normalized.indexOf(raw, cursor) : -1;
          var located = found >= cursor && found + raw.length <= end;
          var lower = located ? found : cursor;
          var upper = located ? found + raw.length : end;
          if (located) ranges.set(token, [offsets[lower], offsets[upper]]);
          var children = token.tokens || token.items || null;
          if (Array.isArray(children)) lower = annotate(children, lower, upper);
          if (Array.isArray(token.header)) lower = annotate(token.header, lower, upper);
          if (Array.isArray(token.rows)) token.rows.forEach(function(row) { lower = annotate(row, lower, upper); });
          cursor = located ? upper : lower;
        });
        return cursor;
      }
      annotate(tokens, 0, normalized.length);
      function wrap(method, tag) {
        var base = renderer[method];
        renderer[method] = function(token) {
          var html = base.call(this, token), range = ranges.get(token);
          if (!range) return html;
          return '<' + tag + ' data-pka-start="' + range[0] + '" data-pka-end="' + range[1] + '">' + html + '</' + tag + '>';
        };
      }
      wrap('text', 'span'); wrap('codespan', 'span'); wrap('html', 'span'); wrap('code', 'div');
      return tokens;
    }

    function locateMappedEvidence(targetStart, targetLength) {
      clearHighlights();
      var raw = window.pkaRawMarkdown || '';
      if (!Number.isInteger(targetStart) || !Number.isInteger(targetLength) || targetStart < 0 ||
          targetLength <= 0 || targetStart + targetLength > raw.length) return false;
      var root = document.getElementById('content-area');
      if (!root) return false;
      var targetEnd = targetStart + targetLength, segments = [];
      var decoder = document.createElement('textarea');
      root.querySelectorAll('[data-pka-start]').forEach(function(span) {
        var start = Number(span.dataset.pkaStart), end = Number(span.dataset.pkaEnd);
        if (start >= targetEnd || end <= targetStart) return;
        var cursor = start;
        var walker = document.createTreeWalker(span, NodeFilter.SHOW_TEXT), node;
        while ((node = walker.nextNode())) {
          if (node.parentElement.closest('[data-pka-start]') !== span) continue;
          var value = node.nodeValue, positions = [], j = 0, literal = span.dataset.pkaLiteral === 'true';
          while (j < value.length && cursor < end) {
            if (!literal && raw[cursor] === '\\' && raw[cursor + 1] === value[j]) cursor++;
            if (!literal && raw[cursor] === '&') {
              var entity = raw.slice(cursor, end).match(/^&(?:#\d+|#x[\da-f]+|[a-z][\da-z]+);/i);
              if (entity) {
                decoder.innerHTML = entity[0];
                var decoded = decoder.value;
                if (decoded && value.slice(j, j + decoded.length) === decoded) {
                  for (var k = 0; k < decoded.length; k++) positions.push([cursor, cursor + entity[0].length]);
                  j += decoded.length; cursor += entity[0].length; continue;
                }
              }
            }
            if (raw[cursor] !== value[j]) {
              // Markdown delimiters (e.g. code fences) are not DOM characters.
              var next = raw.indexOf(value[j], cursor);
              if (next < 0 || next >= end) break;
              cursor = next;
            }
            positions.push([cursor, cursor + 1]); cursor++; j++;
          }
          var first = -1, last = -1;
          positions.forEach(function(position, index) {
            if (position[0] < targetEnd && position[1] > targetStart) {
              if (first < 0) first = index;
              last = index + 1;
            }
          });
          if (first >= 0) segments.push({ node: node, start: first, end: last });
        }
      });
      var marks = [];
      // Gather ranges first; mutating the DOM while walking skips adjacent matches.
      segments.forEach(function(segment) {
        var range = document.createRange();
        range.setStart(segment.node, segment.start); range.setEnd(segment.node, segment.end);
        var mark = document.createElement('mark'); mark.className = 'evidence-highlight';
        range.surroundContents(mark); marks.push(mark);
      });
      if (!marks.length) return false;
      var target = marks[0];
      if (window.pkaRevealReadingTarget) {
        var targets = marks.map(window.pkaRevealReadingTarget);
        target = targets.find(function(item) { return item.getClientRects().length; }) || targets[0];
      }
      target.scrollIntoView({ behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' });
      return true;
    }
    """#
}

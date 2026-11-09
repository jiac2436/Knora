import Foundation

/// Tracks a visible paragraph/image anchor without scanning the whole document
/// on every scroll. Pixel-independent offsets also survive window-size changes.
enum ReadingPositionScript {
    static let source = #"""
    function installReadingPosition(generation, initial) {
      if (window.pkaDisposeReadingPosition) window.pkaDisposeReadingPosition();
      var root = document.getElementById('content-area');
      var resume = initial, lastAppliedY = window.scrollY, queued = false, disposed = false;
      var blockSelector = 'p,h1,h2,h3,h4,h5,h6,li,pre,blockquote,table,.image-segment';
      function blockFor(element) {
        return element && (element.closest(blockSelector) || element);
      }
      function position() {
        var height = Math.max(0, document.documentElement.scrollHeight - innerHeight);
        var point = document.elementFromPoint(Math.min(innerWidth - 20, 48), Math.min(innerHeight - 1, 16));
        var caret = document.caretRangeFromPoint && document.caretRangeFromPoint(Math.min(innerWidth - 20, 48), 16);
        var block = point && point.closest('.image-segment');
        if (!block && caret && root.contains(caret.startContainer)) {
          block = blockFor(caret.startContainer.nodeType === 1 ? caret.startContainer : caret.startContainer.parentElement);
        }
        if (!block && point && root.contains(point)) block = blockFor(point);
        var mapped = block && (block.matches('[data-pka-start]') ? block : block.querySelector('[data-pka-start]'));
        var image = block && (block.matches('[data-image-id]') ? block : block.querySelector('[data-image-id]'));
        var rect = block && block.getBoundingClientRect();
        return { sourceOffset: mapped ? Number(mapped.dataset.pkaStart) : null,
                 imageID: image ? image.dataset.imageId : null,
                 elementFraction: rect && rect.height > 0 ? Math.max(-2, Math.min(2, -rect.top / rect.height)) : 0,
                 scrollFraction: height > 0 ? Math.max(0, Math.min(1, window.scrollY / height)) : 0 };
      }
      function report() {
        if (disposed) return;
        window.webkit.messageHandlers.readerBridge.postMessage({type:'readingPosition', generation:generation, position:position()});
      }
      function apply() {
        if (!resume || disposed) return;
        var anchor = resume.imageID ? root.querySelector('[data-image-id="' + resume.imageID + '"]') : null;
        if (!anchor && Number.isInteger(resume.sourceOffset)) anchor = root.querySelector('[data-pka-start="' + resume.sourceOffset + '"]');
        if (anchor && window.pkaRevealReadingTarget) anchor = window.pkaRevealReadingTarget(anchor);
        var block = blockFor(anchor), height = Math.max(0, document.documentElement.scrollHeight - innerHeight);
        var y = block ? window.scrollY + block.getBoundingClientRect().top + block.getBoundingClientRect().height * resume.elementFraction : height * resume.scrollFraction;
        if (resume.scrollFraction === 0) y = 0;
        window.scrollTo(0, Math.max(0, Math.min(height, y)));
        lastAppliedY = window.scrollY;
      }
      function schedule() {
        if (queued || disposed) return;
        queued = true;
        requestAnimationFrame(function() { queued = false; report(); });
      }
      function scrolled() {
        if (resume && Math.abs(window.scrollY - lastAppliedY) > 2) resume = null;
        schedule();
      }
      function stopRestore() { resume = null; }
      var observer = new ResizeObserver(function() { if (resume) apply(); schedule(); });
      observer.observe(root);
      addEventListener('scroll', scrolled, {passive:true});
      ['wheel','touchstart','mousedown','keydown'].forEach(function(type) { addEventListener(type, stopRestore, {passive:true}); });
      window.pkaStopReadingRestore = stopRestore;
      window.pkaReportReadingPosition = report;
      window.pkaDisposeReadingPosition = function() {
        disposed = true; observer.disconnect(); removeEventListener('scroll', scrolled);
        ['wheel','touchstart','mousedown','keydown'].forEach(function(type) { removeEventListener(type, stopRestore); });
      };
      apply(); requestAnimationFrame(function() { apply(); report(); });
    }
    """#
}

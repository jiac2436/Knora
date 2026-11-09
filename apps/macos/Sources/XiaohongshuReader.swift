import Foundation

/// Presentation only. Indexed Markdown and every source offset remain unchanged.
enum XiaohongshuReader {
    static func isPostURL(_ value: String?) -> Bool {
        guard let value, let url = URL(string: value), ["http", "https"].contains(url.scheme?.lowercased() ?? ""),
              url.user == nil, url.password == nil, let host = url.host?.lowercased(),
              host == "xiaohongshu.com" || host.hasSuffix(".xiaohongshu.com") else { return false }
        return url.path.range(of: #"^/(?:explore|discovery/item|search_result)/[A-Za-z0-9]+/?$"#, options: .regularExpression) != nil
    }

    static let styles = #"""
    .xhs-meta { display:flex; align-items:center; gap:12px; margin:8px 0 28px; font-size:12px; color:var(--text-muted); }
    .xhs-brand { background:#e33d55; color:#fff; padding:3px 7px; border-radius:6px; font-size:11px; white-space:nowrap; }
    .xhs-author { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; min-width:0; max-width:240px; }
    .xhs-author p { margin:0; font-size:12px; }
    .xhs-source { margin-left:auto; font-size:12px; white-space:nowrap; color:var(--text-muted)!important; }
    .xhs-hidden { display:none!important; }
    .xhs-body { line-height:1.95; }
    .xhs-topics { display:flex; flex-wrap:wrap; gap:7px; margin:20px 0 28px; }
    .xhs-topic { display:inline-block; color:var(--text-muted); background:var(--bg-hover); padding:3px 9px; font-size:12px; border-radius:5px; }
    .xhs-media-title { font-size:14px; font-weight:600; margin:25px 0 12px; }
    .xhs-media-title small { margin-left:10px; color:var(--text-muted); font-size:12px; font-weight:400; }
    .xhs-gallery { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
    .xhs-gallery p { display:contents; }
    .xhs-gallery .image-segment { margin:0!important; min-width:0; }
    .xhs-gallery .image-box { height:190px; aspect-ratio:auto!important; border-radius:8px; overflow:hidden; }
    .xhs-gallery .image-box img { width:100%; height:100%; object-fit:contain; }
    .xhs-gallery .image-placeholder { font-size:12px; }
    .xhs-comments { margin-top:30px; border-top:1px solid var(--border-color); padding-top:18px; }
    .xhs-comments>summary { cursor:pointer; font-size:14px; font-weight:550; color:var(--text-normal); }
    .xhs-comments>summary small { margin-left:10px; font-weight:400; color:var(--text-muted); font-size:12px; }
    .xhs-coverage { color:var(--text-muted); font-size:12px; margin:15px 0; }
    .xhs-comment { padding:17px 0; border-bottom:1px solid var(--border-color); }
    .xhs-comment-reply { margin:13px 0 0 18px; padding-left:14px; border-left:2px solid var(--border-color); }
    .xhs-comment-heading { color:var(--text-muted); font-size:12px; white-space:nowrap; text-overflow:ellipsis; overflow:hidden; }
    .xhs-comment-copy { font-size:14px; line-height:1.8; white-space:pre-wrap; overflow-wrap:anywhere; margin-top:6px; }
    .xhs-comment-copy>span { display:block; min-height:.6em; }
    .xhs-author-badge { color:var(--text-muted); font-size:10px; margin-left:7px; background:var(--bg-hover); padding:1px 5px; border-radius:3px; }
    .xhs-title-target { outline:2px solid var(--accent-color); outline-offset:5px; border-radius:3px; }
    @media(max-width:520px) { .xhs-gallery { grid-template-columns:repeat(2,minmax(0,1fr)); } .xhs-gallery .image-box { height:160px; } }
    """#

    static func script(sourceURL: String) -> String {
        let json = (try? String(data: JSONSerialization.data(withJSONObject: [sourceURL]), encoding: .utf8)) ?? "[]"
        return "window.pkaPostURL = (" + json.replacingOccurrences(of: "<", with: "\\u003c") + ")[0];\n" + source
    }

    static let source = #"""
    function renderXiaohongshu(renderer) {
      var raw = window.pkaRawMarkdown, root = document.getElementById('content-area');
      var pattern = /^【(标题|作者|正文|话题标签|评论区[^\r\n】]*)】[ \t]*\r?\n/gm;
      var markers = Array.from(raw.matchAll(pattern));
      if (!markers.length || raw.slice(0, markers[0].index).trim()) return false;
      var keys = ['标题','作者','正文','话题标签','评论区'], previous = -1;
      for (var marker of markers) {
        var key = marker[1].startsWith('评论区') ? '评论区' : marker[1], order = keys.indexOf(key);
        if (order <= previous) return false;
        previous = order;
      }
      // Only the final appended image list is a gallery; inline prose stays intact.
      var tail = raw.match(/(?:\s*!\[[^\]\r\n]*\]\(know-image:\/\/[0-9a-fA-F-]{36}\)\s*)+$/);
      // Edited/supplemented legacy documents can place images between sections.
      // Preserve their original reader rather than turn image references into text.
      if (!tail && /know-image:\/\//.test(raw)) return false;
      var mediaStart = tail ? raw.length - tail[0].length : raw.length;
      if (markers.some(function(m) { return m.index >= mediaStart; })) return false;
      function element(tag, cls, parent) { var el=document.createElement(tag); el.className=cls; if(parent)parent.appendChild(el); return el; }
      function mapped(parent, start, end, text) {
        var span=element('span','',parent); span.dataset.pkaStart=start; span.dataset.pkaEnd=end; span.dataset.pkaLiteral='true';
        span.textContent=text===undefined ? raw.slice(start,end) : text; return span;
      }
      function renderRange(parent,start,end) {
        var scoped=Object.assign(Object.create(Object.getPrototypeOf(renderer)),renderer);
        parent.innerHTML=marked.parser(prepareSourceTokens(scoped,start,end),{renderer:scoped,gfm:true,breaks:true});
      }
      var fragment=document.createDocumentFragment(), meta=element('div','xhs-meta',fragment);
      element('span','xhs-brand',meta).textContent='小红书';
      var source=element('a','xhs-source',meta); source.href=window.pkaPostURL; source.textContent='查看原帖 ↗';
      var sections=markers.map(function(m,i){return {key:m[1].startsWith('评论区')?'评论区':m[1],label:m[1],start:m.index,content:m.index+m[0].length,end:i+1<markers.length?markers[i+1].index:mediaStart};});
      var comments=null;
      sections.forEach(function(section){
        var hidden=element('span','xhs-hidden',fragment);hidden.dataset.xhsSection=section.key;mapped(hidden,section.start,section.content);
        if(section.key==='标题'){hidden.dataset.xhsTitle='true';mapped(hidden,section.content,section.end);return;}
        if(section.key==='作者'){
          var author=element('span','xhs-author');author.title=raw.slice(section.content,section.end).trim();
          renderRange(author,section.content,section.end);meta.insertBefore(author,source);return;
        }
        if(section.key==='评论区'){comments=section;return;}
        if(section.key==='话题标签'){
          var topics=element('div','xhs-topics',fragment), text=raw.slice(section.content,section.end), cursor=section.content;
          for(var match of text.matchAll(/#[^\s#]+/g)){
            var start=section.content+match.index;
            if(start>cursor)mapped(element('span','xhs-hidden',topics),cursor,start);
            mapped(element('span','xhs-topic',topics),start,start+match[0].length);cursor=start+match[0].length;
          }
          // Unknown tag formats are preserved rather than silently discarded.
          if(raw.slice(cursor,section.end).trim())mapped(topics,cursor,section.end);
          return;
        }
        renderRange(element('section','xhs-body',fragment),section.content,section.end);
      });
      if(tail){
        var title=element('div','xhs-media-title',fragment);title.textContent='图片';
        var count=(tail[0].match(/know-image:\/\//g)||[]).length;
        element('small','',title).textContent=count+' 张 · 点击查看大图';
        renderRange(element('div','xhs-gallery',fragment),mediaStart,raw.length);
      }
      if(comments){
        var details=element('details','xhs-comments',fragment),summary=element('summary','',details);
        summary.appendChild(document.createTextNode('评论与回复'));
        var count=comments.label.match(/已加载\s*(\d+)\s*条/),total=comments.label.match(/共\s*(\d+)\s*条/);
        element('small','',summary).textContent=(count?count[1]+' 条已采集':'已采集评论')+(total?' / 原帖 '+total[1]+' 条':'');
        var lines=[],position=comments.content;
        raw.slice(comments.content,comments.end).split(/\n/).forEach(function(line){lines.push({text:line.replace(/\r$/,''),start:position,end:position+line.length});position+=line.length+1;});
        var container=details, body=null, rootComment=null, previousBlank=true;
        lines.forEach(function(line){
          if(line.text.startsWith('仅含当前页面已加载的评论及回复')){mapped(element('div','xhs-coverage',details),line.start,line.end);return;}
          var reply=line.text.match(/^\s*\|_\s+(.+)[：:]\s*$/);
          var heading=reply || (previousBlank && line.text.trim().match(/^(.+)[：:]$/));
          if(heading){
            container=element('div',reply?'xhs-comment-reply':'xhs-comment',reply&&rootComment?rootComment:details);
            if(!reply)rootComment=container;
            var name=element('div','xhs-comment-heading',container);name.title=line.text.replace(/^\s*\|_\s*/, '').trim();
            var prefix=reply?line.text.indexOf('|_')+2:0;
            mapped(element('span','xhs-hidden',name),line.start,line.start+prefix);
            mapped(name,line.start+prefix,line.end,raw.slice(line.start+prefix,line.end).trim());
            body=element('div','xhs-comment-copy',container);
          }else if(line.text.trim()){
            if(!body)body=element('div','xhs-comment-copy',details);
            mapped(body,line.start,line.end,line.text.trimStart());
          }
          previousBlank=!line.text.trim();
        });
      }
      root.replaceChildren(fragment);root.classList.add('xhs-reading');
      window.pkaRevealReadingTarget=function(target){
        var hiddenTitle=target.closest('[data-xhs-title]');
        if(hiddenTitle){meta.classList.add('xhs-title-target');setTimeout(function(){meta.classList.remove('xhs-title-target');},2500);return meta;}
        var marker=target.closest('[data-xhs-section]');
        if(marker){
          var selectors={'正文':'.xhs-body','话题标签':'.xhs-topics','评论区':'.xhs-comments'};
          target=root.querySelector(selectors[marker.dataset.xhsSection]||'.xhs-meta')||meta;
          if(target.tagName==='DETAILS')target.open=true;
        }
        var parent=target.parentElement;
        while(parent&&parent!==root){if(parent.tagName==='DETAILS')parent.open=true;parent=parent.parentElement;}
        return target;
      };
      return true;
    }
    """#
}

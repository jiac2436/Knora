from pathlib import Path
import subprocess
root=Path(__file__).resolve().parents[3];folder=Path(__file__).parent;temporary=root/'apps/macos/.build/xhs-reader-fixture';temporary.mkdir(exist_ok=True)
source=(root/'apps/macos/Sources/WebTextReader.swift').read_text().split('private final class WebReaderScriptBridge:')[0]
(temporary/'Template.swift').write_text(source)
(temporary/'Fixture.swift').write_text(r'''
import Foundation
struct ReaderImageResource { let id:String; let path:String; let width:Int?; let height:Int? }
@main struct Fixture {
 static func main() throws {
  let folder=URL(fileURLWithPath:CommandLine.arguments[1])
  let raw="【标题】\n三亚旅行测试标题\n\n【作者】\n合成测试作者\n\n【正文】\n海边的**同一句话**。\n\n下面再出现同一句话，用于验证精确定位。\n\n【话题标签】\n#三亚旅行 #攻略\n\n【评论区 · 已加载 2 条 / 页面显示共 55 条】\n仅含当前页面已加载的评论及回复，不代表完整评论区。\n\n长用户名长用户名长用户名长用户名长用户名长用户名：\n评论中的独立观点。 &amp;\n   |_ 合成测试作者（作者） 回复 访客：\n      回复中也有同一句话。\n\n![图片 1](know-image://11111111-1111-4111-8111-111111111111)"
  for (name,url) in [("xiaohongshu","https://www.xiaohongshu.com/explore/synthetic"),("ordinary","https://example.com/explore/synthetic")]{
   let html=WebReaderTemplate.buildHTML(text:raw,initialFontSize:17,labels:[:],resources:[],presentationSourceURL:url)
   try html.write(to:folder.appendingPathComponent(name+".html"),atomically:true,encoding:.utf8)
  }
  let ambiguous="【正文】\n保留这段正文\n\n【正文】\n用户自己写的分区标记"
  try WebReaderTemplate.buildHTML(text:ambiguous,initialFontSize:17,labels:[:],resources:[],presentationSourceURL:"https://www.xiaohongshu.com/explore/synthetic").write(to:folder.appendingPathComponent("ambiguous.html"),atomically:true,encoding:.utf8)
  let supplemented=raw+"\n\n用户补充内容不能丢失。"
  try WebReaderTemplate.buildHTML(text:supplemented,initialFontSize:17,labels:[:],resources:[],presentationSourceURL:"https://www.xiaohongshu.com/explore/synthetic").write(to:folder.appendingPathComponent("supplemented.html"),atomically:true,encoding:.utf8)
  for value in ["https://example.com/explore/a","https://xiaohongshu.com.evil.test/explore/a","https://www.xiaohongshu.com/","https://www.xiaohongshu.com/user/profile/a"]{precondition(!XiaohongshuReader.isPostURL(value))}
  precondition(XiaohongshuReader.isPostURL("https://www.xiaohongshu.com/explore/abc?xsec_token=synthetic"))
  print("PASS: real Swift template fixtures; strict Xiaohongshu post URL gating")
 }
}
''')
subprocess.run(['xcrun','swiftc','-parse-as-library','-module-cache-path',str(temporary/'module-cache'),str(temporary/'Template.swift'),str(temporary/'Fixture.swift'),*[str(root/'apps/macos/Sources'/name) for name in ('MarkedLib.swift','ReaderSourceMap.swift','ReadingPositionScript.swift','XiaohongshuReader.swift')],'-o',str(temporary/'fixture')],check=True)
subprocess.run([str(temporary/'fixture'),str(folder)],check=True)

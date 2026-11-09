const frame = document.createElement('iframe');
frame.src = './xiaohongshu.html';
frame.style.cssText = 'position:absolute;left:-10000px;width:1000px;height:800px';
document.body.append(frame);
await new Promise(resolve => frame.addEventListener('load', resolve, {once:true}));
while (!frame.contentWindow.fixtureCapture) await new Promise(resolve => setTimeout(resolve, 20));
const complete = frame.contentWindow.fixtureCapture;
complete.noteContent.tags = ["unity"];
const longCommentAuthor = '幽渊雷烬穹灭吞宇惊霄撼世霸极凌尊鸿宇圣主'.repeat(3);
complete.noteContent.comments[0].author = longCommentAuthor;
const imageBase = location.origin + '/tests/fixtures/';
complete.imageUrls = [imageBase+'gallery-one.svg', imageBase+'gallery-two.svg', imageBase+'gallery-missing.png'];
const blocked = {mode:'page',title:'小红书',url:location.href,text:'',imageUrls:[],coverage:'link_only',site:'xiaohongshu',captureBlocked:'xiaohongshu_detail_required',warnings:[{code:'xiaohongshu_detail_required',message:'请先点击一篇小红书帖子打开详情，再点击“读取页面正文”抓取；当前不会采集下方帖子列表。'}],preview:null,htmlSnapshot:null};
const markup = await (await fetch('../../static/popup.html')).text();
const body = new DOMParser().parseFromString(markup,'text/html').body;
body.querySelector('script').remove();
document.body.replaceChildren(...body.childNodes);
document.querySelector('.brand-icon').src='../../static/icons/icon-48.png';
document.querySelector('#capture-site img').src='../../static/xiaohongshu.svg';
const storage = {'pka.local.v1':{kind:'local',accountId:'fixture',displayName:'本地验证账户',apiBaseUrl:location.origin,token:'fixture-only'}};
let scenario = blocked;
let queued = null;
const fixtureNative = async(_host, message) => {
  if(message.action==='connect')return {ok:true,data:storage['pka.local.v1']};
  const headers=new Headers(message.key?{'Idempotency-Key':message.key}:{});
  const response=await window.fetch(location.origin+message.path,{method:message.method,headers,body:message.json?JSON.stringify(message.json):undefined});
  return {ok:response.ok,data:await response.json()};
};
window.chrome = {
  permissions:{contains:async()=>true},
  tabs:{query:async()=>[{id:1,title:'小红书',url:'https://www.xiaohongshu.com/'}]},
  scripting:{executeScript:async({world})=>[{result:world==='MAIN'?null:scenario}]},
  storage:{local:{get:async(key)=>({[key]:storage[key]}),set:async(data)=>Object.assign(storage,data)}},
  runtime:{sendNativeMessage:fixtureNative,getManifest:()=>({version:'0.1.0'}),openOptionsPage:async()=>{},onMessage:{addListener:()=>{}},sendMessage:async(message)=>{if(message.type==='CONNECT_LOCAL')return {ok:true,online:true,binding:storage['pka.local.v1']};if(message.type==='ENQUEUE_CAPTURE') queued=message.item;return {ok:true};}}
};
const nativeFetch=window.fetch.bind(window);
window.fetch=async(url,options)=>{
  if(String(url).includes('/v1/domains')) return new Response(JSON.stringify({items:[{id:'work',name:'工作'}]}),{status:200});
  if(String(url).includes('/v1/projects')) return new Response(JSON.stringify({items:[]}),{status:200});
  return nativeFetch(url,options);
};
await import('../../dist/popup.js');
const wait = async(predicate)=>{ for(let i=0;i<100;i++){if(predicate())return;await new Promise(r=>setTimeout(r,20));}throw new Error('popup fixture timeout: '+document.querySelector('#status')?.textContent); };
const results=[];
const check=(name,passed)=>results.push({name,passed:Boolean(passed)});
await wait(()=>document.querySelector('#save').disabled && document.querySelector('#status').textContent.includes('请先点击'));
check('顶部显示小红书标识',!document.querySelector('#capture-site').hidden);
check('未打开详情显示提示且禁止保存',document.querySelector('#status').dataset.kind==='warning' && document.querySelector('#save').disabled && document.querySelector('#preview-content').hidden);
scenario=complete;
document.querySelector('#capture-page').click();
await wait(()=>document.querySelector('#status').textContent==='已读取小红书帖子详情');
check('打开详情后恢复预览和保存',!document.querySelector('#save').disabled && !document.querySelector('#preview-content').hidden && document.querySelector('#preview-title').textContent==='当前帖子标题');
check('图片列表按钮位于保存文字之后',document.querySelector('#preview-plain').nextElementSibling.id==='preview-images' && !document.querySelector('#preview-images').hidden);
document.querySelector('#preview-images').click();
await wait(()=>document.querySelector('#image-gallery img')?.complete && !document.querySelector('.image-gallery-status').textContent);
check('首张图片与张数展示',document.querySelector('.image-gallery-counter').textContent==='1/3' && document.querySelector('.image-gallery-scroll').children.length===1 && document.querySelector('#preview-frame').hidden && document.querySelector('#preview-text').hidden);
document.querySelector('.image-gallery-next').click();
check('点击箭头切换',document.querySelector('.image-gallery-counter').textContent==='2/3');
document.querySelector('#image-gallery').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}));
await wait(()=>document.querySelector('.image-gallery-status').textContent.includes('无法加载'));
check('键盘切换及失败图片保留位置',document.querySelector('.image-gallery-counter').textContent==='3/3' && document.querySelector('.image-gallery-next').disabled);
document.querySelector('#image-gallery').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));
check('失败后仍可往回切换',document.querySelector('.image-gallery-counter').textContent==='2/3');
document.querySelector('#preview-plain').click();
check('切回文字时隐藏图片',document.querySelector('#image-gallery').hidden && !document.querySelector('#preview-text').hidden && document.querySelector('#preview-images').getAttribute('aria-pressed')==='false');
check('文字预览按标题正文和评论分区',document.querySelector('#preview-text').textContent.includes('【标题】') && document.querySelector('#preview-text').textContent.includes('【正文】') && document.querySelector('#preview-text').textContent.includes('【评论区') && document.querySelector('#preview-text').textContent.includes('模型tripo贴图banana'));
check('只为识别到的内容显示默认全选分类',document.querySelectorAll('#content-sections input').length===5 && document.querySelectorAll('#content-sections input:checked').length===5 && !document.querySelector('#content-sections').hidden);
check('评论采用树状回复，正文保持原分区',document.querySelector('#preview-text [data-section="comments"]').textContent.includes('|_ 测试作者（作者） 回复 '+longCommentAuthor) && document.querySelector('#preview-text [data-section="body"]').textContent==='【正文】\n当前帖子的正文与话题。');
const longHeading=document.querySelector('#preview-text .comment-heading');
check('长用户名单行省略且悬停保留全文',getComputedStyle(longHeading).textOverflow==='ellipsis' && getComputedStyle(longHeading).whiteSpace==='pre' && longHeading.scrollWidth>longHeading.clientWidth && longHeading.getBoundingClientRect().height<=parseFloat(getComputedStyle(longHeading).lineHeight)+1 && longHeading.title.includes(longCommentAuthor));
check('显示省略不截断实际保存文字',document.querySelector('#preview-text').textContent.includes(longCommentAuthor) && document.querySelector('#preview-text').textContent.includes('模型tripo贴图banana'));
const toggle=(id)=>document.querySelector(`#content-sections input[value="${id}"]`).click();
for(const id of ['title','author','tags','comments']) toggle(id);
check('取消勾选即时隐藏对应分区',document.querySelector('#preview-text').textContent==='【正文】\n当前帖子的正文与话题。' && !document.querySelector('#preview-text [data-section="comments"]'));
document.querySelector('#preview-images').click();document.querySelector('#preview-plain').click();
check('切换预览保留勾选状态',document.querySelectorAll('#content-sections input:checked').length===1 && document.querySelector('#content-sections input[value="body"]').checked);
document.querySelector('#domain').value='work';document.querySelector('#save').click();
await wait(()=>queued && !document.querySelector('#capture-page').disabled);
check('实际队列只提交勾选正文且标题不泄漏',queued.payload.content.text==='【正文】\n当前帖子的正文与话题。' && queued.payload.title==='小红书笔记' && queued.htmlSnapshot===null && !JSON.stringify(queued.payload).includes('banana'));
check('仅保留正式保存入口和本地使用说明',!document.querySelector('#test-submit') && !document.querySelector('#test-result') && document.querySelector('#help').getAttribute('href')==='help.html');
check('正常保存仍携带完整图片列表',JSON.stringify(queued.payload.content.image_urls)===JSON.stringify(complete.imageUrls));
toggle('body');
check('全部取消时禁止保存并提示',document.querySelector('#save').disabled && document.querySelector('#preview-text').textContent.includes('至少勾选'));
for(const checkbox of document.querySelectorAll('#content-sections input')) checkbox.click();
check('重新勾选恢复原文和评论',!document.querySelector('#save').disabled && document.querySelector('#preview-text').textContent.includes('模型tripo贴图banana'));
const before=document.querySelector('.image-gallery-counter').textContent;
document.querySelector('#note').dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowLeft',bubbles:true}));
check('备注框方向键不切换图片',document.querySelector('.image-gallery-counter').textContent===before);
scenario={...complete,imageUrls:[]};
document.querySelector('#capture-page').click();
await wait(()=>document.querySelector('#status').textContent==='已读取小红书帖子详情' && !document.querySelector('#capture-page').disabled);
check('重新读取默认全选',document.querySelectorAll('#content-sections input:checked').length===5);
document.querySelector('#preview-images').click();
check('无图片时提供空状态',document.querySelector('.image-gallery-counter').textContent==='0/0' && document.querySelector('.image-gallery-status').textContent.includes('未读取到') && document.querySelector('.image-gallery-next').disabled);
scenario=blocked;
document.querySelector('#capture-page').click();
await wait(()=>document.querySelector('#status').textContent.includes('请先点击') && document.querySelector('#save').disabled);
check('关闭详情重新读取会清除旧预览',document.querySelector('#preview-content').hidden && !document.querySelector('#preview-frame').hasAttribute('srcdoc'));
// Simulate updating popup files while Chrome retains the old permission set.
scenario=complete;
document.querySelector('#capture-page').click();
await wait(()=>!document.querySelector('#capture-page').disabled);
delete storage['pka.local.v1'];
chrome.permissions.contains=async()=>false;
let reloaded=false;
chrome.runtime.reload=()=>{reloaded=true;};
document.querySelector('#connect').click();
await wait(()=>document.querySelector('#connect').textContent==='重新加载插件' && !document.querySelector('#connect').disabled);
check('旧权限明确提示重新加载且首次连接前禁止提交',document.querySelector('#local-connection-status').textContent.includes('尚未加载本地连接权限') && !document.querySelector('#connect').hidden && document.querySelector('#save').disabled);
document.querySelector('#connect').click();
check('重新加载按钮实际调用浏览器重载能力',reloaded);
const output=document.createElement('output');output.id='results';output.textContent=JSON.stringify(results);output.hidden=true;document.body.append(output);
document.body.dataset.ready='true';
// Optional visual QA state: keep the real popup controls and show the comment tree.
if (new URLSearchParams(location.search).has('preview')) {
  scenario=complete;
  document.querySelector('#capture-page').click();
  await wait(()=>document.querySelector('#status').textContent==='已读取小红书帖子详情' && !document.querySelector('#capture-page').disabled);
  document.querySelector('#preview-plain').click();
  document.querySelector('#preview-text').scrollTop=document.querySelector('#preview-text').scrollHeight;
}

// Browser fixture only: use an isolated fake account and local DOM, never a real backend.
const fixture = document.createElement('iframe');
fixture.src = './article.html';
fixture.style.cssText = 'position:absolute;left:-10000px;width:1000px;height:800px';
document.body.append(fixture);
await new Promise(resolve => fixture.addEventListener('load', resolve, {once:true}));
while (!fixture.contentWindow.fixtureCapture) await new Promise(resolve => setTimeout(resolve, 20));
const page = fixture.contentWindow.fixtureCapture;
const selection = fixture.contentWindow.fixtureSelection;
const results = fixture.contentWindow.fixtureResults;
const markup = await (await fetch('../../static/popup.html')).text();
const body = new DOMParser().parseFromString(markup, 'text/html').body;
body.querySelector('script').remove();
document.body.replaceChildren(...body.childNodes);
document.querySelector(".brand-icon").src="../../static/icons/icon-48.png";
const storage = {'pka.local.v1':{kind:'local',accountId:'fixture',displayName:'本地验证账户',apiBaseUrl:location.origin,token:'fixture-only'}};
let queued = null;
const fixtureNative = async(_host, message) => {
  if(message.action==='connect')return {ok:true,data:storage['pka.local.v1']};
  const headers=new Headers(message.key?{'Idempotency-Key':message.key}:{});
  const response=await window.fetch(location.origin+message.path,{method:message.method,headers,body:message.json?JSON.stringify(message.json):undefined});
  return {ok:response.ok,data:await response.json()};
};
window.chrome = {
  permissions:{contains:async()=>true},
  tabs:{query:async()=>[{id:1,title:page.title,url:page.url}]},
  scripting:{executeScript:async({args})=>[{result:args[0]==='selection'?selection:page}]},
  storage:{local:{get:async(key)=>({[key]:storage[key]}),set:async(data)=>Object.assign(storage,data)}},
  runtime:{sendNativeMessage:fixtureNative,getManifest:()=>({version:'0.1.0'}),openOptionsPage:async()=>{},onMessage:{addListener:()=>{}},sendMessage:async(message)=>{
    if(message.type==='CONNECT_LOCAL')return {ok:true,online:true,binding:storage['pka.local.v1']};if(message.type==='ENQUEUE_CAPTURE'){queued=message.item;storage['pka.queue.v1']=[queued];}
    return {ok:true};
  }}
};
const nativeFetch=window.fetch.bind(window);
window.fetch=async(url,options)=>{
  if(String(url).includes('/v1/domains')) return new Response(JSON.stringify({items:[{id:'work',name:'工作'}]}),{status:200});
  if(String(url).includes('/v1/projects')) return new Response(JSON.stringify({items:[]}),{status:200});
  return nativeFetch(url,options);
};
await import('../../dist/popup.js');
const output = document.createElement('output');
output.id='browser-test-results';output.hidden=true;output.textContent=JSON.stringify(results);document.body.append(output);
const artifact=document.createElement('output');artifact.id='fixture-html';artifact.hidden=true;artifact.textContent=page.htmlSnapshot.html;document.body.append(artifact);
document.body.dataset.ready='true';
document.querySelector('#save').addEventListener('click',()=>setTimeout(()=>{
  output.textContent=JSON.stringify([...results,{name:'本地队列保留 HTML，上传载荷仍只有文字',passed:Boolean(queued?.htmlSnapshot?.html && !queued.payload.content.html && !queued.preview)}]);
},50));

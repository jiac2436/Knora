import test from 'node:test';
import assert from 'node:assert/strict';
import { connectLocalApp, discoverLocal, LOCAL_BINDING } from '../dist/local-connection.js';
import { apiRequest } from '../dist/api.js';

test('outdated permissions and worker replies expose a reload action instead of a silent offline state', async () => {
  let granted = false, response, calls = 0;
  globalThis.chrome = {
    permissions: { contains: async permission => {
      assert.deepEqual(permission, {permissions:['nativeMessaging']}); return granted;
    } },
    runtime: { sendMessage: async () => { calls++; return response; } }
  };
  try {
    assert.equal((await connectLocalApp(true)).requiresReload, true);
    assert.equal(calls, 0, 'missing permissions must not attempt launching the App');
    granted = true;
    assert.match((await connectLocalApp()).message, /后台尚未更新/);
    response = {ok:false,error:'不支持的连接来源'};
    assert.equal((await connectLocalApp()).message, response.error);
    response = {ok:true,online:false,message:'本地桥接未安装'};
    assert.deepEqual(await connectLocalApp(), response);
    response = {ok:true,online:true,binding:{kind:'local',accountId:'library'}};
    assert.deepEqual(await connectLocalApp(true), response);
    chrome.runtime.sendMessage = async () => { throw new Error('Receiving end does not exist'); };
    assert.equal((await connectLocalApp()).requiresReload, true);
  } finally { delete globalThis.chrome; }
});

test('local discovery needs no account, stores no credential and preserves offline connection',async()=>{
  const storage={};let writes=0,offline=false;
  globalThis.chrome={storage:{local:{get:async key=>({[key]:storage[key]}),set:async data=>{writes++;Object.assign(storage,data);}}},runtime:{sendNativeMessage:async()=>{
    if(offline)throw new Error('host missing');
    return {ok:true,data:{kind:'local',accountId:'library-id',apiBaseUrl:'http://127.0.0.1:12345',token:'must-not-store'}};
  }}};
  try {
    assert.equal((await discoverLocal()).online,true);
    assert.equal(storage[LOCAL_BINDING].token,undefined);
    await discoverLocal();assert.equal(writes,1,'unchanged discovery must not trigger a storage event loop');
    offline=true;const result=await discoverLocal();assert.equal(result.online,false);assert.equal(result.binding.accountId,'library-id');
    delete storage[LOCAL_BINDING];assert.equal((await discoverLocal()).binding,null);
  }finally{delete globalThis.chrome;}
});

test('local API requests go through the native host with a library scope and no bearer token',async()=>{
  let request;
  globalThis.chrome={runtime:{sendNativeMessage:async(_host,message)=>{request=message;return {ok:true,data:{id:'saved'}};}}};
  try{
    assert.equal((await apiRequest({kind:'local',accountId:'library-id'},'/v1/captures',{method:'POST',headers:{'Idempotency-Key':'key'},body:JSON.stringify({content:{text:'正文'}})})).id,'saved');
    assert.equal(request.library_id,'library-id');assert.equal(request.key,'key');assert.equal(request.json.content.text,'正文');assert.ok(!JSON.stringify(request).includes('Bearer'));
  }finally{delete globalThis.chrome;}
});

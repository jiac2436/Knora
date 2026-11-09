import test from 'node:test';
import assert from 'node:assert/strict';
import { captureText, noteSections, commentTree } from '../dist/note-text.js';
import { createCapturePayload } from '../dist/core.js';
const capture = {mode:'page',site:'xiaohongshu',title:'测试标题',url:'https://www.xiaohongshu.com/explore/test',text:'作者原文',coverage:'partial_text',noteContent:{title:'测试标题',author:'作者甲',body:'作者原文',tags:['unity','Tripo'],commentTotal:8,comments:[{id:'comment-1',author:'读者乙',text:'问题内容',parentId:null},{id:'comment-2',author:'作者甲',isAuthor:true,parentId:'comment-1',text:'作者回复'}]}};

test('preview and upload share classified author prose and comments',()=>{
  const formatted=captureText(capture);
  assert.ok(formatted.text.includes('【正文】\n作者原文\n\n【话题标签】'));
  assert.ok(formatted.text.includes('已加载 2 条 / 页面显示共 8 条'));
  assert.ok(formatted.text.includes('|_ 作者甲（作者） 回复 读者乙'));
  assert.equal(createCapturePayload(capture,{domainId:'work'},'0.1').content.text,formatted.text);
});

test('selection and other sites retain original text',()=>{
  assert.equal(captureText({...capture,mode:'selection',text:'选中的部分'}).text,'选中的部分');
  assert.equal(captureText({...capture,site:null}).text,'作者原文');
});

test('long formatted content is bounded, disclosed and marked partial',()=>{
  const huge={...capture,coverage:'full_text',noteContent:{...capture.noteContent,body:'字'.repeat(100000)}};
  const formatted=captureText(huge);
  assert.equal(formatted.text.length,100000);
  assert.ok(formatted.text.endsWith('后续内容未包含。]'));
  assert.equal(createCapturePayload(huge,{domainId:'work'},'0.1').coverage,'partial_text');
});


test('section IDs filter preview and payload, including the separate title field',()=>{
  assert.deepEqual(noteSections(capture).map(section=>section.id),['title','author','body','tags','comments']);
  const filtered={...capture,selectedSections:['body']};
  assert.equal(captureText(filtered).text,'【正文】\n作者原文');
  const payload=createCapturePayload(filtered,{domainId:'work'},'0.1');
  assert.equal(payload.content.text,captureText(filtered).text);
  assert.equal(payload.title,'小红书笔记');
  for (const excluded of ['测试标题','作者甲','读者乙','unity','问题内容']) assert.ok(!JSON.stringify(payload).includes(excluded));
  assert.equal(payload.coverage,'partial_text');
  assert.throws(()=>createCapturePayload({...capture,selectedSections:[]},{domainId:'work'},'0.1'),/至少勾选/);
  assert.throws(()=>createCapturePayload({...capture,selectedSections:['unknown']},{domainId:'work'},'0.1'),/至少勾选/);
  assert.equal(capture.noteContent.comments.length,2,'Filtering must not destroy source data');
});

test('default selection preserves all sections and available-section detection ignores absent data',()=>{
  assert.equal(captureText({...capture,selectedSections:noteSections(capture).map(s=>s.id)}).text,captureText(capture).text);
  assert.deepEqual(noteSections({...capture,noteContent:{title:'标题',body:'正文'}}).map(s=>s.id),['title','body']);
});

test('comment tree orders replies by parent, indents multiline text and handles cycles',()=>{
  const comments=[{id:'reply',parentId:'root',author:'回复者',text:'第一行\n第二行'}, {id:'root',author:'评论者',text:'问题'}, {id:'orphan',parentId:'missing',author:'另一位',text:'独立文字'}];
  const text=commentTree(comments);
  assert.ok(text.indexOf('评论者：')<text.indexOf('|_ 回复者 回复 评论者：'));
  assert.ok(text.includes('      第二行'));
  assert.ok(text.includes('另一位：'));
  assert.ok(!text.includes('root'));
  const cyclic=commentTree([{id:'a',parentId:'b',author:'A',text:'文本A'},{id:'b',parentId:'a',author:'B',text:'文本B'}]);
  assert.equal(cyclic.split('文本A').length-1,1);
  assert.equal(cyclic.split('文本B').length-1,1);
});

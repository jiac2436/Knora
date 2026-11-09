const { chromium } = require('playwright');
const http = require('http'); const fs = require('fs'); const path = require('path');
const root = path.resolve(__dirname, '../../../apps/extension'); const results = [];
const mime = {'.html':'text/html','.js':'text/javascript','.mjs':'text/javascript','.css':'text/css','.png':'image/png','.svg':'image/svg+xml'};
const server = http.createServer((request, response) => {
  const file = path.resolve(root, '.' + decodeURIComponent(new URL(request.url, 'http://localhost').pathname));
  if (!file.startsWith(root + path.sep)) { response.writeHead(403).end(); return; }
  fs.readFile(file, (error, data) => { if(error){response.writeHead(404).end();return;} response.setHeader('Content-Type',mime[path.extname(file)]||'application/octet-stream');response.end(data); });
});
(async()=>{let browser;try{await new Promise(r=>server.listen(0,'127.0.0.1',r));const base='http://127.0.0.1:'+server.address().port;
browser=await chromium.launch({executablePath:'/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',headless:true});
const page=await browser.newPage({viewport:{width:410,height:900}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
const check=(name,passed)=>{results.push({name,passed:Boolean(passed)});if(!passed)throw Error(name)};
await page.goto(base+'/tests/fixtures/popup.html');await page.waitForSelector('body[data-ready=true]');await page.waitForFunction(()=>document.querySelector('#status').textContent.startsWith('已读取'));
await page.waitForFunction(()=>document.querySelector('.brand-icon').complete&&document.querySelector('.brand-icon').naturalWidth>0);
check('popup brand image renders and header fits 410 px',await page.evaluate(()=>document.documentElement.scrollWidth<=410&&document.querySelector('.brand-icon').naturalWidth===48));
check('no test submission UI',await page.locator('#test-submit,#test-result').count()===0);
check('help entry present and normal save remains enabled',await page.locator('#help').getAttribute('href')==='help.html'&&!await page.locator('#save').isDisabled());
await page.screenshot({path:path.join(__dirname,'popup.png'),fullPage:true});
await page.locator('#domain').selectOption('work');await page.locator('#save').click();await page.waitForFunction(()=>document.querySelector('#status').textContent.includes('已保存在插件队列'));
check('normal save uses existing queue successfully',(await page.locator('#status').innerText()).includes('已保存在插件队列'));
await page.goto(base+'/dist/help.html');check('bundled help sections cover connection collection images and offline queue',await page.locator('h1').innerText()==='使用说明'&&(await page.locator('body').innerText()).includes('本机队列')&&(await page.locator('body').innerText()).includes('保存小红书帖子'));
await page.getByText('第一次连接不上 App，怎么办？',{exact:true}).click();check('first connection help expands',await page.locator('details').first().evaluate(e=>e.open));
check('help narrow screen has no overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
await page.setViewportSize({width:1000,height:900});await page.screenshot({path:path.join(__dirname,'help.png'),fullPage:true});
check('help link targets bundled options page',await page.locator('a[href="options.html"]').count()>=1);
check('no popup/help JavaScript errors',errors.length===0);
console.log(JSON.stringify(results));}catch(e){console.error(e);process.exitCode=1;}finally{fs.writeFileSync(path.join(__dirname,'ui-results.json'),JSON.stringify(results,null,2));if(browser)await browser.close();await new Promise(r=>server.close(r));}})();

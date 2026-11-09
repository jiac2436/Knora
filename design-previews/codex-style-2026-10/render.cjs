const { chromium } = require('playwright');
const path = require('path');
(async()=>{
 const browser=await chromium.launch({channel:'chrome',headless:true});
 const page=await browser.newPage({viewport:{width:1440,height:960},deviceScaleFactor:2});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 for(const scheme of ['a','b']){
  await page.goto('file://'+path.join(__dirname,`scheme-${scheme}.html`));
  await page.screenshot({path:path.join(__dirname,`scheme-${scheme}.png`)});
  await page.evaluate(()=>show('search')); 
  await page.locator('#query').fill('北京');await page.waitForTimeout(550);
  if(!await page.locator('#results').innerText().then(t=>t.includes('北京历史')))throw Error('search missing');
  await page.keyboard.press('Tab');
  if(!await page.locator('#scope').innerText().then(t=>t.includes('当前')))throw Error('scope missing');
  if(scheme==='a')await page.screenshot({path:path.join(__dirname,'scheme-a-search.png')});
  await page.keyboard.press('Escape');
  if(await page.locator('#query').inputValue())throw Error('query not cleared');
  if(scheme==='b'){
   await page.locator('.source').first().click();
   await page.screenshot({path:path.join(__dirname,'scheme-b-source.png')});
  }
  console.log(scheme,'overflow',await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth));
 }
 console.log('pageErrors',errors);await browser.close();
})();

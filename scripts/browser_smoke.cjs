const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require(path.join(process.argv[3], 'playwright'));
const config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

(async () => {
  const browser = await chromium.launch({ executablePath: process.env.CHROMIUM_PATH || '/usr/bin/chromium', headless:true, args:['--no-sandbox'] });
  try {
    const context = await browser.newContext({viewport:{width:1440,height:1000}});
    await context.addCookies(config.cookies);
    const page = await context.newPage();
    const errors = [], external = [], badResponses = [];
    page.on('pageerror', error => errors.push(error.message));
    page.on('request', request => { if (!request.url().startsWith('data:') && new URL(request.url()).origin !== config.url) external.push(request.url()); });
    page.on('response', response => { if (response.status() >= 400 && !response.url().endsWith('/favicon.ico')) badResponses.push({status:response.status(),url:response.url().replace(config.url,'')}); });
    await page.goto(config.url + '/simulator_home');
    await page.waitForFunction(() => Object.keys(personas).length === 3);
    const before = await page.evaluate(() => ({step, positions:JSON.parse(JSON.stringify(logicalPositions))}));
    await page.getByRole('button',{name:'Follow Isabella Rodriguez on the map'}).click();
    await page.waitForFunction(() => document.getElementById('provider-progress').textContent.includes('queued'), null, {timeout:15000});
    await page.screenshot({path:path.join(config.screenshots,'webui-live-queued.png'),fullPage:true});
    await page.waitForFunction(() => phase === 'execute' && step === 0, null, {timeout:40000});
    const during = await page.evaluate(() => ({step, positions:JSON.parse(JSON.stringify(logicalPositions)), spriteX:personas['Isabella Rodriguez'].x}));
    await page.getByRole('button',{name:'Pause',exact:true}).click();
    const frozen = await page.evaluate(() => ({step, x:personas['Isabella Rodriguez'].x}));
    await page.waitForTimeout(300);
    assert.deepEqual(await page.evaluate(() => ({step,x:personas['Isabella Rodriguez'].x})), frozen);
    await page.screenshot({path:path.join(config.screenshots,'webui-live-follow.png'),fullPage:true});
    await page.getByRole('button',{name:'Resume',exact:true}).click();
    await page.waitForFunction(() => step >= 2, null, {timeout:60000});
    await page.getByRole('button',{name:'Pause',exact:true}).click();
    const after = await page.evaluate(() => ({step,positions:JSON.parse(JSON.stringify(logicalPositions)),time:document.getElementById('game-time-content').textContent,activity:document.getElementById('current_action__Isabella_Rodriguez').textContent}));
    assert(after.step >= 2);
    assert.notDeepEqual(after.positions,before.positions);
    assert.match(after.activity,/walking/);
    const environment = await page.evaluate(async () => {
      const response = await fetch('/update_environment/', {method:'POST',headers:{'Content-Type':'application/json','X-CSRFToken':getCookie('csrftoken')},body:JSON.stringify({sim_code,step:step-1})});
      return {status:response.status,body:await response.json()};
    });
    assert.equal(environment.status,200);
    const initialZoom = await page.evaluate(()=>mapScene.cameras.main.zoom);
    await page.getByRole('button',{name:'Zoom in',exact:true}).click();
    assert(await page.evaluate(()=>mapScene.cameras.main.zoom) > initialZoom);
    await page.getByRole('button',{name:'Zoom out',exact:true}).click();
    const canvas = await page.locator('canvas').boundingBox();
    const scrollBeforeDrag = await page.evaluate(()=>mapScene.cameras.main.scrollX);
    await page.mouse.move(canvas.x+canvas.width/2,canvas.y+canvas.height/2);
    await page.mouse.down();
    await page.mouse.move(canvas.x+canvas.width/2+60,canvas.y+canvas.height/2,{steps:4});
    await page.mouse.up();
    assert.notEqual(await page.evaluate(()=>mapScene.cameras.main.scrollX),scrollBeforeDrag);
    await page.getByRole('button',{name:'Whole town'}).click();

    await page.screenshot({path:path.join(config.screenshots,'webui-live-desktop.png'),fullPage:true});
    for (const width of [390,320]) {
      await page.setViewportSize({width,height:844});
      await page.waitForTimeout(150);
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
    }
    await page.setViewportSize({width:390,height:844});
    await page.getByRole('button',{name:'Follow Isabella Rodriguez on the map'}).click();
    await page.screenshot({path:path.join(config.screenshots,'webui-live-mobile.png'),fullPage:true});
    assert.deepEqual(errors,[]);
    assert.deepEqual(external,[]);
    assert.deepEqual(badResponses,[]);
    const report = {before,during,after,errors,external,badResponses,viewportWidths:[1440,390,320],pauseVerified:true,dragAndZoomVerified:true,realMovementEndpointStatus:environment.status};
    fs.writeFileSync(path.join(config.screenshots,'browser-evidence.json'),JSON.stringify(report,null,2)+'\n');
    console.log(JSON.stringify(report));
    await context.close();
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });

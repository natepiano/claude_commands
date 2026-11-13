// Run with node scripts/build_queue/memory_page_proof.mjs [temporary-output-folder].
// The optional third argument names the previous page for comparison images.
// Add --queued as the fourth argument for the queued-section comparison.
// Add --console as the fourth argument for the Builds title comparison.
// Add --step-names as the fourth argument for owner-based step names.
import assert from 'node:assert/strict';
import {readFile, writeFile, mkdtemp, rm, mkdir} from 'node:fs/promises';
import {createServer} from 'node:http';
import {spawn, execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {inflateSync} from 'node:zlib';
import {join, dirname, resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';

const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const callerFolder = process.argv[2];
const folder = callerFolder ? resolve(callerFolder) : await mkdtemp(join(tmpdir(), 'memory-page-proof-'));
try {
  await mkdir(folder, {recursive:true});
  const systemTemporaryRoot = execFileSync(process.env.PYTHON || 'python3', ['-c',
    'from pathlib import Path; import sys; from scripts.build_queue.test_memory_feed import write_browser_fixture; write_browser_fixture(Path(sys.argv[1])); from scripts.lib import conf; print(Path(conf.read("build_folders").text("roots.system")).expanduser())', folder],
    {cwd:repo, env:{...process.env, HERDR_BIN:'/nonexistent/herdr'}, encoding:'utf8'}).trim();
  const page = await readFile(join(folder, 'memory_live.html'));
  const beforePage = process.argv[3] ? (await readFile(process.argv[3], 'utf8'))
    .replace('__MEMORY_FEED_INTERVAL_SECONDS__', '0.125') : null;
  const queuedComparison = process.argv[4] === '--queued';
  const consoleComparison = process.argv[4] === '--console';
  const stepNamesComparison = process.argv[4] === '--step-names';
  const readings = JSON.parse(await readFile(join(folder, 'fixtures.json'), 'utf8'));
  const now = Date.now() / 1000;
  for (const reading of Object.values(readings)) {
    const shift = now - reading.read_at;
    for (const row of reading.walk) {
      if (row.started_at) row.started_at += shift;
      if (row.read_at) row.read_at += shift;
    }
    for (const waiter of reading.waiters) waiter.since += shift;
    reading.read_at = now;
  }
  const base = readings.good;
  base.waiters = structuredClone(readings.spacing.waiters);
  base.walk.find(row => row.queued).progress = {kind:'RowProgress', phase:'testing', done:8, total:10, percent:80, unit:'modules'};
  base.walk.find(row => row.queued).unit = 'fixture-unit';
  base.walk.find(row => row.queued).showrunner = 'fixture-production';
  const swapOwners = Array.from({length:31}, (_, index) => ({name:'group '+index, detail:'fixture owner',
    kind:'owner', bytes:(31-index)*1024**3/64, swap:(31-index)/64}));
  const smaller = swapOwners.slice(base.swap_rows);
  const groupedBytes = smaller.reduce((sum,row)=>sum+row.bytes,0);
  base.swap_total = 8;
  base.swap_used = (swapOwners.reduce((sum,row)=>sum+row.bytes,0)+1031)/1024**3;
  base.swap_by = [...swapOwners.slice(0,base.swap_rows),
    {name:smaller.length+' smaller groups',detail:'fixture owners',kind:'smaller',bytes:groupedBytes,swap:groupedBytes/1024**3},
    {name:'not in a group',detail:'user.slice',kind:'ungrouped',bytes:1024,swap:1024/1024**3},
    {name:'not attributed',detail:'Host swap minus the named groups.',kind:'unattributed',bytes:7,swap:7/1024**3},
    {name:'Total',detail:'SwapTotal minus SwapFree',kind:'total',bytes:base.swap_used*1024**3,swap:base.swap_used}];
  readings.shrink = structuredClone(base);
  const extra = structuredClone(base.walk.find(row => row.queued));
  extra.name = 'second nextest'; extra.pid = 222;
  readings.shrink.walk.unshift(extra);
  readings.shrink.waiters = [];
  function pngPixels(buffer) {
    let width, height, channels;
    const chunks = [];
    for (let offset = 8; offset < buffer.length;) {
      const size = buffer.readUInt32BE(offset);
      const kind = buffer.toString('ascii', offset + 4, offset + 8);
      const data = buffer.subarray(offset + 8, offset + 8 + size);
      if (kind === 'IHDR') {
        width = data.readUInt32BE(0); height = data.readUInt32BE(4);
        channels = data[9] === 2 ? 3 : data[9] === 6 ? 4 : 0;
        if (data[8] !== 8 || !channels) throw new Error('Unsupported screenshot format');
      }
      if (kind === 'IDAT') chunks.push(data);
      offset += size + 12;
    }
    const raw = inflateSync(Buffer.concat(chunks));
    const stride = width * channels;
    const pixels = Buffer.alloc(height * stride);
    let offset = 0;
    const paeth = (a,b,c) => {
      const p=a+b-c, da=Math.abs(p-a), db=Math.abs(p-b), dc=Math.abs(p-c);
      return da<=db&&da<=dc?a:db<=dc?b:c;
    };
    for (let y=0;y<height;y++) {
      const filter=raw[offset++];
      for (let x=0;x<stride;x++) {
        const at=y*stride+x, left=x>=channels?pixels[at-channels]:0;
        const above=y?pixels[at-stride]:0, corner=y&&x>=channels?pixels[at-stride-channels]:0;
        const correction=[0,left,above,Math.floor((left+above)/2),paeth(left,above,corner)][filter];
        pixels[at]=(raw[offset++]+correction)&255;
      }
    }
    return (x,y)=>[...pixels.subarray(y*stride+x*channels,y*stride+x*channels+3)];
  }

  const server = createServer((req, res) => {
    const isData = req.url.startsWith('/memory.json');
    res.writeHead(200, {'Content-Type':isData ? 'application/json' : 'text/html; charset=utf-8'});
    res.end(isData ? JSON.stringify({...base,read_at:Date.now()/1000}) : req.url === '/before.html' ? beforePage : page);
  });
  try {
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    // Chrome needs a short path for its Unix sockets.
    const profile = await mkdtemp(join(systemTemporaryRoot, 'memory-proof-chrome-'));
    try {
      const temporary = join(profile, 'tmp');
      await mkdir(temporary);
      const chrome = spawn(process.env.CHROME || '/run/current-system/sw/bin/google-chrome-stable', [
        '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--hide-scrollbars',
        '--no-first-run', '--disable-extensions', '--disable-background-networking',
        '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank',
      ], {env:{...process.env,TMPDIR:temporary},stdio:['ignore','ignore','pipe']});
      const exited = new Promise(resolve => {
        chrome.once('exit', resolve);
        chrome.once('error', resolve);
      });
      let ws;
      let debuggerError;
      const pending = new Map();
      const events = new Map();
      const rejectPending = error => {
        debuggerError ||= error;
        for (const request of pending.values()) request.reject(debuggerError);
        for (const event of events.values()) event.reject(debuggerError);
        pending.clear();
        events.clear();
      };
      chrome.once('exit', () => rejectPending(new Error('Chrome closed during a debugger request.')));
      chrome.once('error', rejectPending);
      try {
        const endpoint = await new Promise((resolve, reject) => {
          let output = '';
          chrome.stderr.on('data', chunk => {
            output += chunk;
            const match = output.match(/DevTools listening on (ws:\/\/\S+)/);
            if (match) resolve(match[1]);
          });
          chrome.once('error', reject);
          chrome.once('exit', () => reject(new Error('Chrome closed before its debugger started.')));
        });
        ws = new WebSocket(endpoint);
        ws.addEventListener('close', () => rejectPending(new Error('Chrome debugger connection closed.')));
        await Promise.race([once(ws, 'open'), exited.then(() => { throw debuggerError; })]);
        let id = 0;
        ws.addEventListener('message', event => {
          const message = JSON.parse(event.data);
          if (message.id) {
            const request = pending.get(message.id);
            pending.delete(message.id);
            if (!request) return;
            if (message.error) request.reject(new Error(JSON.stringify(message.error)));
            else request.resolve(message.result);
          } else {
            const key = `${message.sessionId || ''}:${message.method}`;
            if (events.has(key)) { events.get(key).resolve(message.params); events.delete(key); }
          }
        });
        const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
          if (debuggerError) { reject(debuggerError); return; }
          const next = ++id;
          pending.set(next, {resolve,reject});
          ws.send(JSON.stringify({id:next,method,params,...(sessionId ? {sessionId} : {})}));
        });
        const {targetId} = await send('Target.createTarget', {url:'about:blank'});
        const {sessionId} = await send('Target.attachToTarget', {targetId,flatten:true});
        const call = (method,params) => send(method,params,sessionId);
        const evaluate = async expression => {
          const result = await call('Runtime.evaluate', {expression,returnByValue:true,awaitPromise:true});
          if (result.exceptionDetails) throw new Error(JSON.stringify(result.exceptionDetails));
          return result.result.value;
        };
        await call('Page.enable');
        await call('Page.addScriptToEvaluateOnNewDocument', {
          source:'window.proofSetInterval = window.setInterval; window.setInterval = () => 0;'});
        await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
        await call('Emulation.setDeviceMetricsOverride', {width:1600,height:1200,deviceScaleFactor:1,mobile:false});
        const navigate = async path => {
          const loaded = new Promise((resolve, reject) => events.set(`${sessionId}:Page.loadEventFired`, {resolve,reject}));
          await Promise.all([loaded, call('Page.navigate', {url:`http://127.0.0.1:${server.address().port}/${path}`})]);
          await evaluate(`(async () => {
          const style = document.createElement('style');
          style.textContent = '* { transition: none !important; }';
          document.head.append(style);
          await tick();
          window.renderReading = render;
          window.proofData = structuredClone(latest);
          window.render = () => {};
          await document.fonts.ready;
          fitLabels();
          })()`);
        };
        await navigate('memory_live.html');

        const proofs = [];
        const show = async (reading, clearTip=true) => evaluate(`(async () => {
          ${clearTip ? "hideTip();" : ""}
          document.body.classList.remove('queue-unavailable');
          const data = ${JSON.stringify(reading)};
          data.read_at = Date.now()/1000;
          latest = data;
          window.renderReading(data);
          fitLabels();
          await document.fonts.ready;
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return true;
        })()`);
        for (const theme of ['light','dark']) {
          await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
          for (const width of [1400,900,390]) {
            await call('Emulation.setDeviceMetricsOverride', {width,height:1200,deviceScaleFactor:1,mobile:false});
            await show(base);
            const timeCells = await evaluate(`({
              short:ago(latest.read_at-15,latest.read_at),
              waiting:[...document.querySelectorAll('tr[data-tip^="wait:"] .duration')].map(cell=>cell.textContent)
            })`);
            assert.equal(timeCells.short,'<1 min');
            assert.ok(timeCells.waiting.length>0);
            assert.ok(timeCells.waiting.every(text=>!text.startsWith('waits ')));
            const proof = await evaluate(`(() => {
              const rect = element => {
                const r = element.getBoundingClientRect();
                return {left:r.left, right:r.right, top:r.top, bottom:r.bottom, width:r.width, height:r.height};
              };
              const text = selector => document.querySelector(selector).textContent;
              const legend = document.querySelector('.legend-box');
              legend.open = true;
              fitLabels();
              const legendRows = [...legend.querySelectorAll('.row')].filter(row => !row.hidden);
              const values = legendRows.filter(row => row.querySelector('.v')).map(row => {
                const label = row.querySelector('.name'), value = row.querySelector('.v');
                return {label:textContent(label), labelBox:rect(label), value:rect(value)};
              });
              function textContent(element) { return element.textContent; }
              const row = document.querySelector('.wrow[data-run]');
              const progress = row.querySelector('.row-progress');
              const cap = document.querySelector('.walk-bud');
              const table = document.querySelector('.walk-table');
              const color = name => {
                const probe = document.createElement('span');
                probe.style.backgroundColor = 'var('+name+')'; document.body.append(probe);
                const result = getComputedStyle(probe).backgroundColor; probe.remove(); return result;
              };
              const luminance = rgb => {
                const values = rgb.match(/[0-9.]+/g).slice(0,3).map(Number).map(v => {
                  const s=v/255; return s<=0.04045?s/12.92:((s+0.055)/1.055)**2.4;
                });
                return values[0]*0.2126+values[1]*0.7152+values[2]*0.0722;
              };
              const contrast = (a,b) => (Math.max(luminance(a),luminance(b))+0.05)/(Math.min(luminance(a),luminance(b))+0.05);
              const swap = [...document.querySelectorAll('#swaprows .row')];
              const small = swap.find(row => row.querySelector('small'))?.querySelector('small');
              const gauges = ['some','full'].map(key => {
                const value=document.querySelector('#gv-'+key), limit=document.querySelector('#limit-'+key);
                return {value:rect(value), limit:rect(limit), text:value.textContent, nowrap:getComputedStyle(value).whiteSpace};
              });
              const lower = [...document.querySelectorAll('#ticks span')].filter(e => !e.hidden).map(e=>Number(e.textContent));
              const owner = row.querySelector('.owner');
              const card = document.querySelector('#walk').closest('.card');
              const metrics = {
                headings:[...table.querySelectorAll('thead th')].map(e=>e.textContent),
                answer:text('#answer'),
                queueHeaders:[...table.querySelectorAll('tbody tr.queue-head th')].map(e=>e.textContent),
                queueHeaderHeight:table.querySelector('tr.queue-head')?.getBoundingClientRect().height,
                queueSpacerHeight:document.querySelector('.walk-chart .queue-head')?.getBoundingClientRect().height,
                stages:[...table.querySelectorAll('tbody tr[data-tip^="row:"] .place')].map(e=>e.textContent),
                layout:document.querySelector('#walk').classList.contains('beside')?'beside':'stacked',
                rowOffsets:[...table.querySelectorAll('tbody tr[data-tip]')].map(metadata=>{
                  const chart=document.querySelector('.wrow[data-tip="'+metadata.dataset.tip+'"]') ||
                    document.querySelector('.wrow.room');
                  return {chart:rect(chart).top-rect(document.querySelector('.walk-chart-head')).bottom,
                    table:rect(metadata).top-rect(table.querySelector('thead')).bottom};
                }),
                machine:table.querySelector('td.machine')?.textContent,
                cap:rect(document.querySelector('#mark')), room:text('#v-room'),
                capColor:getComputedStyle(document.querySelector('#mark'),'::before').borderLeftColor,
                roomBar:rect(document.querySelector('#s-room')), roomColor:color('--room'),
                reportPoints:[...document.querySelectorAll('#history-charts svg circle')].map(point=>({
                  box:rect(point),color:getComputedStyle(point).fill})),
                rowLeft:rect(row.querySelector('.row-label')).left,
                headingLeft:rect(card.querySelector('.section-head')).left,
                reservedAmount:getComputedStyle(row.querySelector('.reserved-amount')).color,
                dimColor:color('--ink-2'),
                leftover:text('#leftover-rows'), history:text('#report-charts'), top:text('#report-top'),
                historyWindow:text('#history-window'), costWindow:text('#cost-window'), pressureReason:text('#pressure-reason'),
                charts:document.querySelectorAll('#history-charts svg').length,
                progress:progress?rect(progress):null, capLine:rect(cap),
                progressColor:progress?getComputedStyle(progress).backgroundColor:null,
                cardColor:color('--card'), reservedColors:[color('--res-a'),color('--res-b')],
                reservedRatios:['--res-a','--res-b'].map(name=>contrast(color(name),color('--card'))),
                swapRows:swap.length, swapText:swap.map(e=>e.textContent),
                swapBytes:swap.map(e=>Number(e.dataset.bytes)),
                swapContrast:small?contrast(getComputedStyle(small).color,color('--card')):null,
                swapBaseline:small?rect(small).bottom-rect(small.parentElement.querySelector('.name')).bottom:0,
                gauges, lower, values,
                name:row.querySelector('.name').textContent, owner:owner?.textContent||'',
                walk:rect(document.querySelector('#walk')), card:rect(card),
                amountHeader:document.querySelector('.walk-amount-head')?.textContent||document.querySelector('#walk').textContent,
                swapBars:document.querySelectorAll('#swap .gt').length,
                legacySwapBar:!!document.querySelector('#swap .swapbar'),
                scroll:document.documentElement.scrollWidth, client:document.documentElement.clientWidth,
              };
              return metrics;
            })()`);
            assert.deepEqual(proof.headings, ['Stage','Done','Step','Duration','Unit','Pid','Machine','vCPU']);
            assert.equal(proof.answer,'Next step room');
            assert.deepEqual(proof.queueHeaders,['#','Done','Step','Waiting','Unit','Pid','Machine','vCPU']);
            assert.equal(proof.queueHeaderHeight,proof.queueSpacerHeight);
            assert.ok(proof.stages.includes('testing'));
            assert.ok(proof.stages.every(stage => ['building','testing','—'].includes(stage)));
            for(const offsets of proof.rowOffsets)
              assert.ok(Math.abs(offsets.chart-offsets.table)<=1,'The chart and table rows differ. '+JSON.stringify(offsets));
            assert.equal(proof.machine, 'fixture');
            assert.match(proof.room, /GiB/);
            for (const age of ['45 min','19 h','2 d']) assert.ok(proof.leftover.includes(age), age);
            assert.match(proof.leftover,/pid 46/);
            assert.ok(!/pid (70|80)\b/.test(proof.leftover), 'The leftover list includes a live build or compiler service.');
            assert.equal(proof.historyWindow,'Oct 31, 2026 – Nov 7, 2026 · America/New_York');
            assert.match(proof.costWindow,/^Nov 4, 2026 – Nov 7, 2026 · America\/New_York/);
            assert.equal(proof.pressureReason,'Pressure is unreadable: /private/proc/pressure/memory');
            assert.ok(proof.charts > 0, 'The history section has no chart.');
            assert.match(proof.top, /Memory wait/);
            assert.match(proof.top, /fixture-owner/);
            assert.ok(proof.progress && proof.progress.width > 0, 'The progress line is absent.');
            assert.ok(proof.progress.height <= 3, 'The progress line is too thick.');
            assert.ok(proof.progress.right <= proof.capLine.left + 1, 'The progress line crosses the cap.');
            assert.equal(proof.swapRows, base.swap_rows + 4);
            assert.match(proof.swapText[base.swap_rows], /25 smaller groups/);
            assert.match(proof.swapText[base.swap_rows], /5\.1 GiB/);
            assert.match(proof.swapText.at(-2), /not attributed7 B/);
            assert.match(proof.swapText.at(-1), /Total7\.8 GiB/);
            assert.equal(proof.swapBytes.slice(0,-1).reduce((sum,bytes)=>sum+bytes,0),proof.swapBytes.at(-1));
            assert.equal(proof.swapBytes.at(-1),base.swap_used*1024**3);
            assert.ok(!proof.legacySwapBar, 'The page repeats the swap bar.');
            assert.equal(proof.swapBars, 3);
            for (const ratio of proof.reservedRatios) assert.ok(ratio >= 3, 'The reservation contrast is below 3:1.');
            assert.ok(proof.swapContrast >= 4.5, 'The swap text contrast is below 4.5:1.');
            assert.ok(Math.abs(proof.swapBaseline) <= 1, 'The swap labels use different baselines.');
            assert.ok(proof.lower.every(value => value < base.cap), 'The lower axis reaches the cap.');
            assert.ok(Math.abs(proof.rowLeft-proof.headingLeft)<=1, 'The rows miss the card text edge.');
            assert.equal(proof.reservedAmount,proof.dimColor);
            assert.ok(!proof.owner.includes('fixture-unit'), 'The page repeats the unit beside the name.');
            assert.ok(proof.amountHeader.includes('Used · reserved'), 'The memory amounts have no header.');
            assert.ok(proof.scroll <= proof.client, 'The page overflows. '+JSON.stringify({theme,width,scroll:proof.scroll,client:proof.client}));
            const roomLegend=proof.values.find(row=>row.label==='Next step room');
            assert.ok(Math.abs(roomLegend.labelBox.top-roomLegend.value.top)<2, 'The room value uses a second line.');
            assert.ok(roomLegend.value.left-roomLegend.labelBox.right<80, 'The legend value is too far from its label.');
            for (const gauge of proof.gauges) assert.equal(gauge.nowrap, 'nowrap');
            const metrics = await call('Page.getLayoutMetrics');
            const shot = await call('Page.captureScreenshot', {format:'png', captureBeyondViewport:true,
              clip:{x:0,y:0,width,height:Math.ceil(metrics.cssContentSize.height),scale:1}});
            const pixels = Buffer.from(shot.data,'base64');
            const pixel=pngPixels(pixels);
            const expected=proof.progressColor.match(/[0-9.]+/g).slice(0,3).map(Number);
            const x=Math.floor(proof.progress.left+proof.progress.width/2);
            const y=Math.floor((proof.progress.top+proof.progress.bottom)/2);
            assert.ok(pixel(x,y).every((value,index)=>Math.abs(value-expected[index])<=1),
              'The progress pixels differ: '+JSON.stringify({x,y,pixel:pixel(x,y),expected}));
            const checkPixel=(x,y,color,label,antialiased=false)=>{
              const expected=color.match(/[0-9.]+/g).slice(0,3).map(Number);
              const actual=pixel(Math.floor(x),Math.floor(y));
              let matches=actual.every((value,index)=>Math.abs(value-expected[index])<=1);
              if(antialiased) {
                const background=proof.cardColor.match(/[0-9.]+/g).slice(0,3).map(Number);
                const delta=expected.map((value,index)=>value-background[index]);
                const coverage=delta.reduce((sum,value,index)=>sum+value*(actual[index]-background[index]),0)/delta.reduce((sum,value)=>sum+value*value,0);
                const residual=Math.max(...actual.map((value,index)=>Math.abs(value-background[index]-coverage*delta[index])));
                matches=coverage>=0.1&&residual<=10;
              }
              assert.ok(matches,label+' '+JSON.stringify({x,y,actual,expected}));
            };
            checkPixel(proof.cap.left+7,proof.cap.top+5,proof.capColor,'The cap pixels differ.');
            checkPixel(proof.roomBar.left+5,(proof.roomBar.top+proof.roomBar.bottom)/2,proof.roomColor,'The room pixels differ.');
            assert.ok(proof.reportPoints.length>0,'The report has no measured points.');
            const point=proof.reportPoints.at(-1);
            checkPixel((point.box.left+point.box.right)/2,(point.box.top+point.box.bottom)/2,point.color,'The report pixels differ.',true);
            proofs.push({theme,width,metrics:proof});

            proof.swapRefresh=await evaluate(`(async () => {
              const originalFetch=window.fetch, originalRender=window.render;
              const data=structuredClone(latest);
              const states=[];
              document.querySelector('#swaprows .smaller details').open=true;
              try {
                window.render=window.renderReading;
                window.fetch=async()=>({ok:true,json:async()=>structuredClone(data)});
                await new Promise((resolve,reject)=>{
                  const timer=window.proofSetInterval(async()=>{
                    try {
                      const index=data.swap_by.findIndex(row=>row.kind==='smaller');
                      data.swap_by.splice(states.length+1,0,data.swap_by.splice(index,1)[0]);
                      await tick();
                      states.push(document.querySelector('#swaprows .smaller details').open);
                      if(states.length===2) { clearInterval(timer); resolve(); }
                    } catch(error) { clearInterval(timer); reject(error); }
                  },SECONDS*1000);
                });
                document.querySelector('#swaprows .smaller details').open=false;
                window.renderReading(data);
                return {states,closed:!document.querySelector('#swaprows .smaller details').open};
              } finally { window.fetch=originalFetch; window.render=originalRender; }
            })()`);
            assert.deepEqual(proof.swapRefresh,{states:[true,true],closed:true},
              'The smaller-owner list loses its state across two refreshes or a row move.');

            await evaluate(`(() => {
              document.querySelector('#history-machine').value='fixture';
              document.querySelector('#history-cause').value='Cold rebuilds';
              const unit=document.querySelector('#history-unit');
              unit.value='perPhase';unit.dispatchEvent(new Event('change'));
              for(const table of document.querySelectorAll('#history-plots details')) table.open=true;
              window.proofControl=unit;
              window.proofTable=document.querySelector('#history-plots details');
            })()`);
            await show(base);
            const sameReport=await evaluate(`({sameControl:window.proofControl===document.querySelector('#history-unit'),
              sameTable:window.proofTable===document.querySelector('#history-plots details'),
              open:[...document.querySelectorAll('#history-plots details')].every(table=>table.open)})`);
            assert.ok(sameReport.sameControl && sameReport.sameTable && sameReport.open,
              'A queue refresh replaces report controls or closes a table.');
            const updatedReport=structuredClone(base);
            updatedReport.report.charts.end+=60000;
            await show(updatedReport);
            const changedReport=await evaluate(`({machine:document.querySelector('#history-machine').value,
              cause:document.querySelector('#history-cause').value,unit:document.querySelector('#history-unit').value,
              open:[...document.querySelectorAll('#history-plots details')].every(table=>table.open)})`);
            assert.deepEqual(changedReport,{machine:'fixture',cause:'Cold rebuilds',unit:'perPhase',open:true},
              'New report data loses the report choices or open tables.');
            proof.reportRefresh={sameReport,changedReport};
            await evaluate(`(() => {
              for(const table of document.querySelectorAll('#history-plots details')) table.open=false;
              document.querySelector('#history-machine').value='';
              document.querySelector('#history-cause').selectedIndex=0;
              const unit=document.querySelector('#history-unit');unit.value='daily';
              unit.dispatchEvent(new Event('change'));
            })()`);

            for(const [name,detail] of [
              ['cloud_stage','tools ready'],
              ['cloud_region','tests start · eu-south-2, zone a'],
              ['cloud_zone','eu-south-2, zone us-east-1b'],
              ['cloud_empty','']]) {
              await show(readings[name]);
              const detailProof=await evaluate(`(() => {
                const row=[...document.querySelectorAll('.walk-table tbody tr')].find(row=>row.textContent.includes('700001'));
                const chart=document.querySelector('.wrow[data-tip="'+row.dataset.tip+'"]');
                const amounts=chart.querySelector('.amounts');
                return {text:amounts.textContent,font:getComputedStyle(amounts).fontSize,
                  ownerFont:getComputedStyle(chart.querySelector('.owner')).fontSize,
                  rootFont:getComputedStyle(document.documentElement).fontSize,
                  beside:document.querySelector('#walk').classList.contains('beside'),
                  detailRight:amounts.getBoundingClientRect().right,
                  tableLeft:document.querySelector('.walk-table').getBoundingClientRect().left,
                  chartHeight:chart.getBoundingClientRect().height,rowHeight:row.getBoundingClientRect().height,
                  scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
              })()`);
              const expected=[detail,'7 of 12 modules','1 failed module','no update for 2 min'].filter(Boolean).join(' · ');
              assert.equal(detailProof.text,expected,'The AWS detail text differs.');
              assert.equal(detailProof.font,detailProof.ownerFont,'The AWS detail font differs from the owner font.');
              assert.equal(parseFloat(detailProof.font),parseFloat(detailProof.rootFont)*0.75);
              if(detailProof.beside) assert.ok(detailProof.detailRight<detailProof.tableLeft,
                'The AWS detail text crosses into the table.');
              assert.ok(Math.abs(detailProof.chartHeight-detailProof.rowHeight)<1,'The AWS row heights differ.');
              assert.ok(detailProof.scroll<=detailProof.client,'The AWS detail text overflows the page.');
              if(name==='cloud_region' && process.env.MEMORY_PAGE_SHOT) {
                const clip=await evaluate(`(() => {
                  const r=document.querySelector('#walk').closest('.card').getBoundingClientRect();
                  return {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height,scale:1};
                })()`);
                const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip});
                await writeFile(join(folder,`${theme}-${width}-aws.png`),Buffer.from(shot.data,'base64'));
              }
            }

            for(const [name,lateAge,modules,failed] of [
              ['cloud','2 min','7 of 12 modules','1 failed module'],
              ['cloud_zero','2 d','123456 of 999999 modules','123456 failed modules']]) {
            await show(readings[name]);
            const cloudRow=readings[name].walk.find(row=>row.kind==='cloud');
            assert.equal(cloudRow.late_age,lateAge);
            assert.ok(!cloudRow.status.includes('no update for'),'The feed mixes the state and late age.');
            const cloudProof=await evaluate(`(() => {
              const table=document.querySelector('.walk-table');
              const rows=[...table.querySelectorAll('tbody tr')];
              const running=rows.find(row=>row.textContent.includes('700001'));
              const waiting=rows.find(row=>row.textContent.includes('700002'));
              const details=row=>row?document.querySelector('.wrow[data-tip="'+row.dataset.tip+'"]'):null;
              const runningDetails=details(running),waitingDetails=details(waiting);
              const cloudText=(running?.textContent || '')+' '+(runningDetails?.textContent || '');
              const stale=[...(runningDetails?.querySelectorAll('*') || [])].find(element=>
                element.childElementCount===0 && element.textContent.includes('no update for'));
              return {headings:[...table.querySelectorAll('thead th')].map(e=>e.textContent),
                running:cloudText,waiting:(waiting?.textContent || '')+' '+(waitingDetails?.textContent || ''),
                machine:running?.querySelector('.machine')?.textContent,
                place:waiting?.querySelector('.place')?.textContent,
                waitingMachine:waiting?.querySelector('.machine')?.textContent,
                waitingVcpus:waiting?.querySelector('.vcpus')?.textContent,
                vcpus:running?.querySelector('.vcpus')?.textContent,
                cloudCard:!!document.querySelector('#cloud'),
                statusInTable:!!running && !!waiting,
                cloudMemory:!!runningDetails?.querySelector('.reserved-amount,.used-amount,.b:not(.row-progress)'),
                progressWidth:runningDetails?.querySelector('.row-progress')?.getBoundingClientRect().width,
                progressTrackWidth:runningDetails?.querySelector('.progress-track')?.getBoundingClientRect().width,
                progressInTrack:!!runningDetails?.querySelector('.track .row-progress'),
                amounts:runningDetails?.querySelector('.amounts')?.textContent,
                trackText:runningDetails?.querySelector('.track')?.textContent,
                waitingBar:!!waitingDetails?.querySelector('.row-progress,.progress-track'),
                height:runningDetails?.getBoundingClientRect().height,
                tableHeight:running?.getBoundingClientRect().height,
                tipsMatch:runningDetails?.querySelector('.track')?.dataset.tip===running?.dataset.tip &&
                  runningDetails?.querySelector('.row-memory')?.dataset.tip===running?.dataset.tip,
                localMemory:!!document.querySelector('.wrow .track .seg-used'),
                staleColor:stale?getComputedStyle(stale).color:null,
                errorColor:(() => {const span=document.createElement('span');span.style.color='var(--cap)';
                  document.body.append(span);const color=getComputedStyle(span).color;span.remove();return color;})(),
                slots:tipText('wait:'+latest.waiters.findIndex(row=>row.head_text),latest).join(' '),
                scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
            })()`);
            assert.deepEqual(cloudProof.headings,['Stage','Done','Step','Duration','Unit','Pid','Machine','vCPU']);
            assert.ok(cloudProof.statusInTable,'Cloud work is absent from the main table.');
            assert.equal(cloudProof.machine,'aws');
            assert.equal(cloudProof.vcpus,'32');
            assert.ok(!/\brunning\b/.test(cloudProof.running),'The cloud row repeats the running state.');
            for(const value of [modules,failed,'no update for '+cloudRow.late_age])
              assert.ok(cloudProof.running.includes(value),value+' is absent from the cloud row.');
            assert.ok(cloudProof.progressInTrack,'The cloud progress line is absent from the track.');
            assert.ok(Math.abs(cloudProof.progressWidth-cloudProof.progressTrackWidth*cloudRow.progress.percent/100)<1,
              'The cloud progress width differs from its percent.');
            assert.ok(cloudProof.amounts.includes(modules),'The cloud module count is absent from the amounts.');
            assert.equal(cloudProof.trackText,'','The cloud track contains text.');
            assert.ok(!cloudProof.waitingBar,'The waiting cloud row has a progress line.');
            assert.ok(Math.abs(cloudProof.height-cloudProof.tableHeight)<1,'The cloud row heights differ.');
            assert.ok(cloudProof.tipsMatch,'The cloud hover keys differ from the local row keys.');
            assert.equal(cloudProof.place,'cloud');
            assert.equal(cloudProof.waitingMachine,'');
            assert.equal(cloudProof.waitingVcpus,'');
            assert.match(cloudProof.waiting,/waits for a cloud slot: 1 of 1 in use/);
            assert.match(cloudProof.slots,/cloud 1 of 1 slots/);
            assert.ok(!cloudProof.cloudCard,'The page retains a separate Cloud card.');
            assert.ok(!cloudProof.cloudMemory,'Cloud work has a memory reservation.');
            assert.ok(cloudProof.localMemory,'The local memory bar is absent.');
            assert.equal(cloudProof.staleColor,cloudProof.errorColor,'The stale age is not red.');
            assert.ok(cloudProof.scroll<=cloudProof.client,'Cloud rows overflow the page.');
            if(process.env.MEMORY_PAGE_SHOT && theme==='light' && width===1400 && name==='cloud') {
              const clip=await evaluate(`(() => {
                const r=document.querySelector('#walk').closest('.card').getBoundingClientRect();
                return {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height,scale:1};
              })()`);
              const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip});
              await mkdir(dirname(process.env.MEMORY_PAGE_SHOT),{recursive:true});
              await writeFile(process.env.MEMORY_PAGE_SHOT,Buffer.from(shot.data,'base64'));
            }
            proof[name]=cloudProof;
            }
            await show(base);

            for(const [name,present] of [['mac_idle',true],['good',false]]) {
              await show(readings[name]);
              const alert=await evaluate(`(() => {
                const element=document.querySelector('#mac-idle-alert');
                const box=element.getBoundingClientRect();
                const probe=document.createElement('span');probe.style.color='var(--cap)';document.body.append(probe);
                const errorColor=getComputedStyle(probe).color;probe.remove();
                return {text:element.textContent,hidden:element.hidden,color:getComputedStyle(element).color,errorColor,
                  height:box.height,bottom:box.bottom,titleTop:document.querySelector('h1').getBoundingClientRect().top,
                  scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
              })()`);
              assert.equal(alert.text,readings[name].mac_idle_alert);
              assert.equal(alert.hidden,!present);
              if(present) {
                assert.match(alert.text,/^MAC IDLE 10 min: 7 steps wait for the Mac\./);
                assert.equal(alert.color,alert.errorColor,'The Mac idle alert is not red.');
                assert.ok(alert.height>0 && alert.bottom<=alert.titleTop,'The alert is not above the title.');
                assert.ok(alert.scroll<=alert.client,'The Mac idle alert overflows the page.');
              } else assert.equal(alert.height,0,'The clear alert keeps an empty line.');
              proof[name+'Alert']=alert;
            }
            for(const [name,expected] of [['priority',"queue priority: research (the user's bump)"],
              ['priority_unmatched',''],['good','']]) {
              await show(readings[name]);
              const priority=await evaluate(`(() => {
                const index=latest.walk.findIndex(row=>row.queued);
                return tipText('row:'+index,latest).join(' ');
              })()`);
              assert.equal(priority.includes("queue priority:"),!!expected);
              if(expected) assert.ok(priority.includes(expected));
              const heading=await evaluate("tipText('walkhead',latest).join(' ')");
              const unmatched=name==='priority_unmatched';
              assert.equal(heading.includes("queue priority:"),unmatched);
              if(unmatched) assert.equal(heading.split("queue priority: research (the user's bump)").length-1,1);
              if(unmatched && theme==='dark' && width===1400) {
                const clip=await evaluate(`(() => {
                  const heading=document.querySelector('[data-tip="walkhead"]');
                  heading.scrollIntoView({block:'start'});
                  const h=heading.getBoundingClientRect();
                  heading.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:h.left+10,clientY:h.bottom}));
                  const tip=document.querySelector('#tip');
                  if(!tip.classList.contains('on') || !tip.textContent.includes(latest.unmatched_priority_text))
                    throw new Error('The heading click loses the unmatched priority.');
                  const r=heading.closest('.card').getBoundingClientRect();
                  return {x:0,y:r.top+scrollY,width:document.documentElement.clientWidth,height:r.height,scale:1};
                })()`);
                const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip});
                await writeFile(join(folder,'unmatched-priority-1400.png'),Buffer.from(shot.data,'base64'));
                await evaluate("hideTip();window.scrollTo(0,0)");
              }
            }
            await show(base);

            for (const state of ['idle','waiting','unmatched','busy']) {
              const ciReading=structuredClone(base);
              ciReading.walk.push({name:'hana CI',ci:true,admitted:state==='busy',queued:false,used:0.125,reserved:0,
                counted:0,started_at:now-60,unit:'hana-ci.slice',machine:'fixture',pid:701,vcpus:4,
                progress:{kind:'NoProgress',reason:'no admission counter'},ci_runners:[{state:state==='waiting'||state==='unmatched'?'busy':state}]});
              ciReading.ci_ram=0.125;
              ciReading.taken=ciReading.used+ciReading.ci_ram+ciReading.reserved;
              await show(ciReading);
              const ciProof=await evaluate(`(() => {
                const key='row:'+(latest.walk.length-1);
                const table=document.querySelector('.walk-table tr[data-tip="'+key+'"]');
                const chart=document.querySelector('.wrow[data-tip="'+key+'"]');
                return {table:!!table,chart:!!chart,stage:table?.querySelector('.place').textContent,
                  taken:document.querySelector('#walk-sum').textContent,ci:latest.ci_ram};
              })()`);
              assert.equal(ciProof.table,state==='busy');
              assert.equal(ciProof.chart,state==='busy');
              if(state==='busy') assert.equal(ciProof.stage,'—');
              assert.equal(ciProof.ci,0.125);
              assert.equal(ciProof.taken,ciReading.taken.toFixed(1)+' of '+ciReading.cap.toFixed(1)+' GiB taken');
            }
            await show(base);

            await show(readings.mac_three);
            const macProof=await evaluate(`(() => {
              const rows=latest.walk.map((row,index)=>({row,index})).filter(({row})=>row.kind==='mac-build');
              const unit=document.querySelector('#barwrap').getBoundingClientRect().width/latest.total;
              return rows.map(({row,index})=>{
                const chart=document.querySelector('.wrow[data-run="'+index+'"]');
                const table=document.querySelector('.walk-table tr[data-tip="row:'+index+'"]');
                return {name:row.name,stage:table.querySelector('.place').textContent,
                  unit:table.querySelector('.unit').textContent,percent:table.querySelector('.done').textContent,
                  underBar:!!chart.querySelector('.progress-amount'),
                  used:chart.querySelector('.seg-remote').getBoundingClientRect().width,
                  color:getComputedStyle(chart.querySelector('.seg-remote')).backgroundColor,
                  expected:row.used*unit,hatched:!!chart.querySelector('.hatch'),
                  tip:tooltipHtml('row:'+index,latest),
                  chartTop:chart.getBoundingClientRect().top-document.querySelector('.walk-chart-head').getBoundingClientRect().bottom,
                  tableTop:table.getBoundingClientRect().top-document.querySelector('.walk-table thead').getBoundingClientRect().bottom};
              });
            })()`);
            assert.equal(macProof.length,3);
            assert.deepEqual(macProof.map(row=>row.name),['hana build','hana nextest','cargo-liner nextest']);
            assert.deepEqual(macProof.map(row=>row.percent),['60.4%','64.7%','98.3%']);
            assert.deepEqual(macProof.map(row=>row.unit),['—','trunk','—']);
            const macLabels=await evaluate(`latest.walk.map((row,index)=>({row,index})).filter(({row})=>row.kind==='mac-build').map(({index})=>({
              repository:document.querySelector('.wrow[data-run="'+index+'"] .owner').textContent,
              jobs:document.querySelector('tr[data-tip="row:'+index+'"] .vcpus').textContent}))`);
            assert.deepEqual(macLabels.map(row=>row.repository),['hana','hana','cargo-liner']);
            assert.deepEqual(macLabels.map(row=>row.jobs),['—','12','6']);
            for(const row of macProof) {
              assert.equal(row.stage,'building');
              assert.ok(Math.abs(row.used-row.expected)<1);
              assert.equal(row.hatched,false);
              assert.equal(row.underBar,false);
              assert.ok(Math.abs(row.chartTop-row.tableTop)<1);
              assert.ok(row.tip.includes('Mac memory does not count against natedev.'));
            }
            await show(readings.mac_failed);
            const failedMac=await evaluate(`({rows:document.querySelectorAll('.wrow[data-run]').length,
              text:document.querySelector('#walk').textContent,
              unknown:document.querySelector('.walk-table tr[data-tip="row:'+latest.walk.findIndex(row=>row.kind==='mac-unavailable')+'"] .place').textContent})`);
            assert.ok(failedMac.text.includes('Mac reading is old'));
            assert.ok(!failedMac.text.includes('private raw error'));
            assert.ok(!failedMac.text.includes('Progress unavailable: Mac process'));
            assert.equal(failedMac.unknown,'—');
            await show(base);

            const positions=()=>evaluate(`(() => {
              const row=document.querySelector('.walk-table tr[data-tip^="row:"]');
              return ['.duration','.unit','.pid'].map(selector=>{
                const r=row.querySelector(selector).getBoundingClientRect(); return {left:r.left,width:r.width};
              });
            })()`);
            const withoutWait=structuredClone(base);
            withoutWait.waiters=[];
            await show(withoutWait);
            const beforeWait=await positions();
            const waiting=structuredClone(base);
            waiting.waiters=structuredClone(readings.spacing.waiters);
            waiting.waiters[0].detail='fixture-unit';waiting.waiters[0].pid=51;
            await show(waiting);
            const afterWait=await positions();
            for(let index=0;index<beforeWait.length;index++) {
              assert.ok(Math.abs(beforeWait[index].left-afterWait[index].left)<=1,'A waiting row moves a table column. '+JSON.stringify({width,theme,beforeWait,afterWait}));
              if(index===0) assert.ok(Math.abs(beforeWait[index].width-afterWait[index].width)<=1,
                'A waiting row changes the Duration width. '+JSON.stringify({width,theme,beforeWait,afterWait}));
            }
            const absent=structuredClone(base);
            const absentRun=absent.walk.find(row=>row.queued);
            absentRun.unit='';absentRun.showrunner='';delete absentRun.machine;
            await show(absent);
            const absentCells=await evaluate(`({unit:document.querySelector('.walk-table td.unit').textContent,
              owner:document.querySelector('.wrow[data-run] .owner').textContent,
              machine:document.querySelector('.walk-table td.machine').textContent})`);
            assert.equal(absentCells.unit,'');assert.equal(absentCells.owner,'');assert.equal(absentCells.machine,'—');

            for (const [name, expected] of [['pressure','/private/proc/pressure/memory'],['spacing','start spacing'],['head','behind']]) {
              await show(readings[name]);
              const state=await evaluate(`({list:document.querySelector('#priority-line,#head-line,#wait-reasons'),
                hover:tipText('wait:0',latest)[1], rows:document.querySelectorAll('.wrow[data-run]').length})`);
              assert.equal(state.list,null,'The removed list remains.');
              assert.ok(state.hover.includes(expected), name+' has no hover reason.');
              assert.ok(state.rows > 0);
            }
            for (const name of ['old_cap','missing_cap','missing_use']) {
              await show(readings[name]);
              const state=await evaluate(`({rows:document.querySelectorAll('.wrow[data-run]').length,
                cap:document.querySelector('#mark').hidden, text:document.body.textContent,
                room:document.querySelector('#v-room').textContent})`);
              assert.ok(state.cap, 'The missing cap has a marker.');
              assert.ok(state.rows > 0);
              assert.equal(state.room, proof.room);
              assert.match(state.text, /unavailable/);
              if (name==='missing_use') assert.match(state.text, /private memory.stat is missing/);
            }
            for (const name of ['empty_report','missing_report']) {
              await show(readings[name]);
              const state=await evaluate(`({history:document.querySelector('#report-charts').textContent,
                top:document.querySelector('#report-top').textContent, rows:document.querySelectorAll('.wrow[data-run]').length})`);
              assert.ok(state.rows>0);
              if(name==='missing_report') {
                assert.match(state.history,/private index is missing/);
                assert.match(state.top,/private index is missing/);
              } else {
                assert.match(state.top,/no data yet/i);
                assert.match(state.top,/2026/);
              }
            }
            await show(readings.shrink);
            assert.equal(await evaluate("document.querySelectorAll('.queue-head').length"),0);
            await evaluate(`showTip('row:${readings.shrink.walk.length-1}',200,200)`);
            const smaller=structuredClone(base);
            smaller.walk=smaller.walk.filter(row=>!row.queued);
            smaller.waiters=[];
            await show(smaller,false);
            const shrink=await evaluate(`({rows:document.querySelectorAll('.wrow[data-run]').length,
              tooltip:document.querySelector('#tip').textContent, text:document.querySelector('#walk').textContent})`);
            assert.equal(shrink.rows, 0, 'The smaller reading keeps an old running row.');
            assert.ok(!shrink.text.includes('second nextest'));
            assert.equal(shrink.tooltip,'','The smaller reading keeps an old hover.');
          }
        }
        await show(base);
        const failures=await evaluate(`(async()=>{
          const originalFetch=window.fetch, originalNow=Date.now;
          const originalRender=window.render;
          const reading=structuredClone(latest);
          let now=Date.now();
          Date.now=()=>now;
          window.render=window.renderReading;
          const states=[];
          const snapshot=name=>states.push({name,status:document.querySelector('#status').textContent,
            stale:document.querySelector('#dot').classList.contains('stale'),
            unavailable:document.body.classList.contains('queue-unavailable'),
            screen:document.querySelector('#queue-error').textContent,
            cards:[...document.querySelectorAll('.card')].map(card=>card.innerHTML).join(''),
            rows:document.querySelectorAll('.wrow').length,
            pill:document.querySelector('#pill-t').textContent,
            pillClass:document.querySelector('#pill').className,
            pillDot:getComputedStyle(document.querySelector('.pd')).display,
            position:getComputedStyle(document.querySelector('.status')).position});
          const good=async()=>{
            reading.read_at=now/1000;
            window.fetch=async()=>({ok:true,json:async()=>reading});
            await tick(); fitLabels();
          };
          try {
            lastRead=0;readingFailure='';
            window.fetch=async()=>({ok:true,json:async()=>({ok:false})});
            await tick();snapshot('initial');
            await good();snapshot('good');
            const goodAt=now;
            window.fetch=async()=>({ok:true,json:async()=>({ok:false})});
            now+=1000;await tick();snapshot('one_failure');
            now=goodAt+(STALE+3)*1000;await tick();snapshot('stale');
            const retained={...reading,read_error:'private socket timeout',error_at:now/1000};
            window.fetch=async()=>({ok:true,json:async()=>retained});
            await tick();snapshot('retained');
            await good();snapshot('recovered');
            window.fetch=async()=>{throw new Error('proof fetch failure');};
            now+=(STALE+3)*1000;await tick();snapshot('fetch_failure');
            await good();snapshot('final');
            return states;
          } finally { window.fetch=originalFetch;Date.now=originalNow;window.render=originalRender; }
        })()`);
        const states=Object.fromEntries(failures.map(state=>[state.name,state]));
        assert.ok(states.initial.unavailable);
        assert.equal(states.initial.screen,'The queue did not answer');
        for(const name of ['one_failure','stale','retained']) {
          assert.equal(states[name].cards,states.good.cards, name+' changes the good reading.');
          assert.equal(states[name].rows,states.good.rows);
          assert.ok(!states[name].unavailable);
        }
        assert.equal(states.one_failure.status,states.good.status);
        for(const name of ['stale','retained','fetch_failure']) {
          assert.ok(states[name].stale);
          assert.match(states[name].status,/Showing the reading from \d{2}:\d{2}:\d{2}, 18 s old\./);
          assert.match(states[name].pill,/^as of \d{2}:\d{2}:\d{2}$/);
          assert.match(states[name].pillClass,/neutral/);
          assert.equal(states[name].pillDot,'none');
          assert.equal(states[name].position,'sticky');
        }
        assert.match(states.fetch_failure.status,/Cannot load the reading: proof fetch failure/);
        for(const name of ['recovered','final']) {
          assert.match(states[name].status,/^Live · read at /);
          assert.ok(!states[name].stale && !states[name].unavailable);
        }
        const capture = async (name, selector) => {
          const clip=await evaluate(`(() => {
            const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
            return {x:Math.max(0,r.left+scrollX),y:r.top+scrollY,width:r.width,height:r.height,scale:1};
          })()`);
          const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip});
          await writeFile(join(folder,`${name}.png`),Buffer.from(shot.data,'base64'));
        };
        const comparisons = [];
        if (stepNamesComparison) {
          await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
          await call('Emulation.setDeviceMetricsOverride', {width:1400,height:1200,deviceScaleFactor:1,mobile:false});
          await navigate('memory_live.html');
          for (const version of ['before','after']) {
            await show(readings['owner_names_'+version]);
            const names = await evaluate(`(() => {
              const rows = [...document.querySelectorAll('.walk-table tbody tr')];
              return {running:rows.filter(row=>row.dataset.tip?.startsWith('row:')).map(row=>row.querySelector('.step').textContent),
                waiting:rows.filter(row=>row.dataset.tip?.startsWith('wait:')).map(row=>row.querySelector('.step').textContent)};
            })()`);
            const expected = version === 'before' ? ['targeted test','merge gate'] : ['panes targeted','showrunner merge'];
            assert.deepEqual(names.running,expected);
            assert.deepEqual(names.waiting,expected);
            await capture(version+'-running-step-names','#walk');
            comparisons.push({version,...names});
          }
        }
        if (consoleComparison) {
          assert.ok(beforePage,'The console comparison needs the previous page.');
          await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
          await call('Emulation.setDeviceMetricsOverride', {width:1400,height:1200,deviceScaleFactor:1,mobile:false});
          for (const version of ['before','after']) {
            await navigate(version==='before'?'before.html':'memory_live.html');
            await show(base);
            const state=await evaluate(`(() => ({
              title:document.title,heading:document.querySelector('h1').textContent,
              navigation:document.querySelectorAll('nav').length,
              outbound:[...document.querySelectorAll('a[href]')].filter(a=>!a.getAttribute('href').startsWith('#'))
                .map(a=>({href:a.getAttribute('href'),target:a.target}))
            }))()`);
            if(version==='after') {
              assert.equal(state.title,'Builds');
              assert.equal(state.heading,'Builds');
              assert.equal(state.navigation,0);
              for(const link of state.outbound) assert.equal(link.target,'_top');
            } else {
              assert.equal(state.heading,'Build memory on natedev');
            }
            const clip=await evaluate(`(() => {
              const status=document.querySelector('.status').getBoundingClientRect();
              return {x:0,y:0,width:innerWidth,height:Math.ceil(status.bottom+16),scale:1};
            })()`);
            const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip});
            await writeFile(join(folder,`${version}-dark-title.png`),Buffer.from(shot.data,'base64'));
            comparisons.push({version,...state});
          }
        }
        if (queuedComparison) {
          assert.ok(beforePage, 'The queued comparison needs the previous page.');
          const cases = [
            ['queued_none','Queued, in order of submission'],
            ['queued_one','Queued, research first, then in submission order'],
            ['queued_two','Queued, research followed by cables and then in submission order'],
          ];
          readings.queued_long = structuredClone(readings.queued_two);
          readings.queued_long.queued_priority_units = ['research-with-a-long-unit-name'.repeat(4),'cables'.repeat(8)];
          cases.push(['queued_long',`Queued, ${readings.queued_long.queued_priority_units.join(' followed by ')} and then in submission order`]);
          for (const theme of ['light','dark']) {
            await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
            for (const width of [1400,900,390]) {
              await call('Emulation.setDeviceMetricsOverride', {width,height:1200,deviceScaleFactor:1,mobile:false});
              await navigate('memory_live.html');
              for (const [name,title] of cases) {
                await show(readings[name]);
                const state=await evaluate(`(() => {
                  const walk=document.querySelector('#walk');
                  const heading=walk.querySelector('.walk-chart .queue-title');
                  const header=heading.parentElement;
                  const columns=walk.querySelector('.walk-table tr.queue-head');
                  const room=walk.querySelector('.wrow.room');
                  const after=getComputedStyle(room,'::after'),before=getComputedStyle(room,'::before');
                  const running=getComputedStyle(document.querySelector('.section-head'));
                  const rows=[...walk.querySelectorAll('.walk-table tr[data-tip]')];
                  const textBottom=element=>{
                    const range=document.createRange();range.selectNodeContents(element);
                    return range.getBoundingClientRect().bottom;
                  };
                  return {title:heading.textContent,font:getComputedStyle(heading).font,
                    hover:heading.title,truncated:heading.scrollWidth>heading.clientWidth,
                    clipped:getComputedStyle(heading).textOverflow==='ellipsis' && getComputedStyle(heading).overflow==='hidden',
                    withinChart:heading.getBoundingClientRect().right<=header.getBoundingClientRect().right+1,
                    headerHeight:header.getBoundingClientRect().height,
                    headerOffset:header.getBoundingClientRect().top-columns.getBoundingClientRect().top,
                    bottomOffset:textBottom(heading)-textBottom(columns.querySelector('th')),
                    nextGap:header.nextElementSibling.getBoundingClientRect().top-header.getBoundingClientRect().bottom,
                    noEmptyRow:!walk.querySelector('tr.queue-title'),
                    noOverlap:heading.getBoundingClientRect().right<=columns.getBoundingClientRect().left ||
                      heading.getBoundingClientRect().bottom<=columns.getBoundingClientRect().top,
                    runningFont:running.font,afterWidth:after.borderTopWidth,beforeWidth:before.borderTopWidth,
                    afterColor:after.borderTopColor,beforeColor:before.borderTopColor,
                    roomTop:room.getBoundingClientRect().top,headingTop:heading.getBoundingClientRect().top,
                    roomBottom:room.getBoundingClientRect().bottom,
                    pageOverflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,
                    aligned:walk.classList.contains('beside') && rows.every(row=>{
                      const chart=walk.querySelector('.wrow[data-tip="'+row.dataset.tip+'"]') || room;
                      return Math.abs(row.getBoundingClientRect().top-chart.getBoundingClientRect().top)<=1;
                    }),beside:walk.classList.contains('beside')};
                })()`);
                assert.equal(state.title,title);
                assert.equal(state.hover,title);
                assert.equal(state.font,state.runningFont);
                assert.equal(state.headerHeight,30);
                assert.equal(state.nextGap,0);
                assert.equal(state.noEmptyRow,true);
                assert.equal(state.withinChart,true);
                assert.equal(state.noOverlap,true,JSON.stringify({theme,width,name,...state}));
                if(name==='queued_long') {
                  assert.equal(state.truncated,true);
                  assert.equal(state.clipped,true);
                }
                assert.equal(state.afterWidth,'1px');
                assert.equal(state.afterWidth,state.beforeWidth);
                assert.equal(state.afterColor,state.beforeColor);
                assert.ok(state.headingTop>=state.roomBottom);
                assert.equal(state.pageOverflow,false);
                if(state.beside) {
                  assert.equal(state.aligned,true);
                  assert.ok(Math.abs(state.headerOffset)<=1,'The title and columns use different rows.');
                  assert.ok(Math.abs(state.bottomOffset)<=2,'The title and columns have different text bottoms.');
                }
                comparisons.push({theme,width,name,...state});
              }
            }
            await call('Emulation.setDeviceMetricsOverride', {width:1400,height:1200,deviceScaleFactor:1,mobile:false});
            let beforeRoomTop, beforeWaitTop;
            for (const version of ['before','after']) {
              await navigate(version==='before'?'before.html':'memory_live.html');
              await show(readings.queued_two);
              const state=await evaluate(`(() => {
                const walk=document.querySelector('#walk');
                return {roomTop:walk.querySelector('.wrow.room').getBoundingClientRect().top,
                  waitTop:walk.querySelector('.wrow.wait').getBoundingClientRect().top,
                  title:walk.querySelector('.walk-chart .queue-title')?.textContent || ''};
              })()`);
              if(version==='before') {
                beforeRoomTop=state.roomTop;
                beforeWaitTop=state.waitTop;
                assert.equal(state.title,cases[2][1]);
              } else {
                assert.ok(Math.abs(state.roomTop-beforeRoomTop)<=1,'The room row moved.');
                assert.equal(state.title,cases[2][1]);
                assert.ok(state.waitTop<beforeWaitTop,'The separate title row remains.');
              }
              await capture(`${version}-${theme}-queued`,'#walk');
            }
          }
        }
        if (beforePage && !queuedComparison && !consoleComparison) {
          const review = structuredClone(readings.spacing);
          review.priority_text = "queue priority: port (the user's bump)";
          review.head_text = 'Head: free 8.5 GiB; reserve 3.0 GiB; need 1.9 GiB · cloud 2 of 9 slots';
          const local = review.walk.find(row => row.queued);
          local.machine = 'natedev';
          const mac = structuredClone(readings.mac_three.walk.find(row => row.kind === 'mac-build'));
          Object.assign(mac, {name:'hana nextest',used:3.5,unit:'trunk',stage:{kind:'MacTestingCounter'},
            progress:{kind:'RowProgress',phase:'testing',unit:'units',done:722,total:727,percent:100*722/727}});
          const aws = structuredClone(readings.cloud.walk.find(row => row.kind === 'cloud'));
          Object.assign(aws, {machine:'aws',used:1.5});
          const ci = {name:'hana CI',ci:true,admitted:false,queued:false,used:0.125,reserved:0,counted:0,
            started_at:now-60,unit:'hana CI',machine:'natedev',pid:701,vcpus:32,
            progress:{kind:'NoProgress',reason:'no counter'},ci_runners:[{state:'busy',job:'doctests'}]};
          review.walk = [local, mac, aws, ci];
          review.waiters = [{...review.waiters[0],name:'hana CI doctests',unit:'hana CI',detail:'hana CI',reason:'free memory',
            head_text:review.head_text,priority_text:review.priority_text}];
          review.ci_ram = 0.125;
          review.taken = review.used + review.ci_ram + review.reserved;
          for (const theme of ['light','dark']) {
            await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
            for (const version of ['before','after']) {
              await call('Emulation.setDeviceMetricsOverride', {width:1800,height:1200,deviceScaleFactor:1,mobile:false});
              await navigate(version === 'before' ? 'before.html' : 'memory_live.html');
              await show(review);
              const comparison = await evaluate(`(() => {
                const table=document.querySelector('.walk-table');
                const mac=document.querySelector('.wrow[data-run="1"]');
                const macBar=mac.querySelector('.seg-remote,.seg-used');
                const awsBar=document.querySelector('.wrow[data-run="2"] .seg-remote');
                return {headers:[...table.querySelectorAll('thead th')].map(cell=>cell.textContent),
                  done:table.querySelector('tr[data-tip="row:1"] .done')?.textContent,
                  underBar:!!mac.querySelector('.progress-amount'),
                  remoteColor:getComputedStyle(macBar).backgroundColor,
                  awsColor:awsBar?getComputedStyle(awsBar).backgroundColor:null,
                  localColor:getComputedStyle(document.querySelector('.wrow[data-run="0"] .seg-used')).backgroundColor,
                  machineColors:[...table.querySelectorAll('tr[data-tip^="row:"] .machine')].map(cell=>getComputedStyle(cell).color),
                  ciVisible:!!table.querySelector('tr[data-tip="row:3"]'),
                  waiting:table.querySelector('tr[data-tip="wait:0"] .unit').textContent,
                  pageOverflow:document.documentElement.scrollWidth>document.documentElement.clientWidth};
              })()`);
              if(version==='after') {
                assert.deepEqual(comparison.headers.slice(0,2),['Stage','Done']);
                assert.equal(comparison.done,'99.3%');
                assert.equal(comparison.underBar,false);
                assert.notEqual(comparison.remoteColor,comparison.localColor);
                assert.equal(comparison.remoteColor,comparison.awsColor);
                const rgb = hex => 'rgb('+hex.slice(1).match(/../g).map(part=>parseInt(part,16)).join(', ')+')';
                assert.equal(comparison.remoteColor,rgb(review.colors[theme].remote_memory));
                assert.deepEqual(comparison.machineColors,['natedev','mac','aws'].map(machine=>rgb(review.colors[theme].machines[machine])));
                assert.equal(new Set(comparison.machineColors).size,3);
                assert.equal(comparison.ciVisible,false);
                assert.equal(comparison.waiting,'hana CI');
                assert.equal(comparison.pageOverflow,false);
              } else {
                assert.equal(comparison.ciVisible,true);
                assert.equal(comparison.underBar,true);
                assert.equal(comparison.remoteColor,comparison.localColor);
              }
              await capture(`${version}-${theme}-c1507-b`,'#walk');
              await capture(`${version}-${theme}-hanaci-row`,'#walk');
              await evaluate(`showTip('row:1',180,160)`);
              const note = await evaluate(`(() => {
                const tip=document.querySelector('#tip');
                const note=tip.querySelector('span');
                const fields=tip.querySelector('dl');
                return {last:tip.lastElementChild===note,nowrap:getComputedStyle(note).whiteSpace,
                  note:note.textContent,fieldsBottom:fields.getBoundingClientRect().bottom,noteTop:note.getBoundingClientRect().top};
              })()`);
              if(version==='after') {
                assert.equal(note.last,true);
                assert.equal(note.nowrap,'nowrap');
                assert.ok(note.noteTop>=note.fieldsBottom);
              }
              await capture(`${version}-${theme}-c1507-a`,'#tip');
              const responsive = [];
              if(version==='after') {
                for(const width of [1400,390]) {
                  await call('Emulation.setDeviceMetricsOverride', {width,height:1200,deviceScaleFactor:1,mobile:false});
                  await show(review);
                  const layout = await evaluate(`(() => {
                    const walk=document.querySelector('#walk'),card=walk.closest('.card');
                    return {tables:walk.querySelectorAll('table').length,
                      lists:card.querySelectorAll('#priority-line,#head-line,#wait-reasons').length,
                      layout:walk.classList.contains('beside')?'beside':'stacked',
                      head:tipText('wait:0',latest).join(' ')};
                  })()`);
                  assert.equal(layout.tables,1);
                  assert.equal(layout.lists,0);
                  assert.equal(layout.layout,width===1400?'beside':'stacked');
                  for(const value of [review.head_text,review.priority_text,'free memory'])
                    assert.equal(layout.head.split(value).length-1,1);
                  responsive.push({width,...layout});
                }
              }
              comparisons.push({version,theme,...comparison,note,responsive});
            }
          }
        }

        await writeFile(join(folder,'proof.json'),JSON.stringify({proofs,comparisons,
          failures:failures.map(({cards,...state})=>state)},null,2)+'\n');
        console.log(JSON.stringify({pass:true,views:proofs.length,...(callerFolder ? {output:folder} : {})}));
      } finally {
        if(ws) ws.close();
        chrome.kill('SIGTERM');
        await exited;
      }
    } finally {
      await rm(profile,{recursive:true,force:true,maxRetries:3});
    }
  } finally {
    server.close();
  }
} finally {
  if (!callerFolder) await rm(folder,{recursive:true,force:true});
}

// Run with node scripts/build_queue/memory_page_proof.mjs [temporary-output-folder].
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
  execFileSync(process.env.PYTHON || 'python3', ['-c',
    'from pathlib import Path; import sys; from scripts.build_queue.test_memory_feed import write_browser_fixture; write_browser_fixture(Path(sys.argv[1]))', folder],
    {cwd:repo, env:{...process.env, HERDR_BIN:'/nonexistent/herdr'}, stdio:'pipe'});
  const page = await readFile(join(folder, 'memory_live.html'));
  const readings = JSON.parse(await readFile(join(folder, 'fixtures.json'), 'utf8'));
  const now = Date.now() / 1000;
  for (const reading of Object.values(readings)) {
    const shift = now - reading.read_at;
    for (const row of reading.walk) if (row.started_at) row.started_at += shift;
    for (const waiter of reading.waiters) waiter.since += shift;
    reading.read_at = now;
  }
  const base = readings.good;
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
  readings.shrink.waiting = 0;
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
    res.end(isData ? JSON.stringify({...base,read_at:Date.now()/1000}) : page);
  });
  try {
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    const profile = await mkdtemp(join(tmpdir(), 'memory-proof-chrome-'));
    try {
      const chrome = spawn(process.env.CHROME || '/run/current-system/sw/bin/google-chrome-stable', [
        '--headless', '--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage', '--hide-scrollbars',
        '--no-first-run', '--disable-extensions', '--disable-background-networking',
        '--remote-debugging-port=0', '--user-data-dir=' + profile, 'about:blank',
      ], {stdio:['ignore','ignore','pipe']});
      const exited = new Promise(resolve => {
        chrome.once('exit', resolve);
        chrome.once('error', resolve);
      });
      let ws;
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
        await once(ws, 'open');
        let id = 0;
        const pending = new Map();
        const events = new Map();
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
            if (events.has(key)) { events.get(key)(message.params); events.delete(key); }
          }
        });
        const send = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
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
        const loaded = new Promise(resolve => events.set(`${sessionId}:Page.loadEventFired`, resolve));
        await call('Page.navigate', {url:`http://127.0.0.1:${server.address().port}/memory_live.html`});
        await loaded;
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
            assert.deepEqual(proof.headings, ['#','Step','Duration','Machine','vCPU','Unit','Pid']);
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
            await writeFile(join(folder, `${theme}-${width}.png`),pixels);
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
                cloudMemory:!!runningDetails?.querySelector('.row-progress,.reserved-amount,.used-amount,.b'),
                localMemory:!!document.querySelector('.wrow .track .seg-used'),
                staleColor:stale?getComputedStyle(stale).color:null,
                errorColor:(() => {const span=document.createElement('span');span.style.color='var(--cap)';
                  document.body.append(span);const color=getComputedStyle(span).color;span.remove();return color;})(),
                slots:document.querySelector('#walk').closest('.card').textContent,
                scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
            })()`);
            assert.deepEqual(cloudProof.headings,['#','Step','Duration','Machine','vCPU','Unit','Pid']);
            assert.ok(cloudProof.statusInTable,'Cloud work is absent from the main table.');
            assert.equal(cloudProof.machine,'aws');
            assert.equal(cloudProof.vcpus,'32');
            for(const value of ['running',modules,failed,'no update for '+cloudRow.late_age])
              assert.ok(cloudProof.running.includes(value),value+' is absent from the cloud row.');
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
            for(const [name,expected] of [['priority',"queue priority: research (the user's bump)"],['good','']]) {
              await show(readings[name]);
              assert.equal(await evaluate("document.querySelector('#priority-line').textContent"),expected);
            }
            await show(base);

            const positions=()=>evaluate(`(() => {
              const row=document.querySelector('.walk-table tr[data-tip^="row:"]');
              return ['.duration','.unit','.pid'].map(selector=>{
                const r=row.querySelector(selector).getBoundingClientRect(); return {left:r.left,width:r.width};
              });
            })()`);
            const beforeWait=await positions();
            const waiting=structuredClone(base);
            waiting.waiters=structuredClone(readings.spacing.waiters);
            waiting.waiters[0].detail='fixture-unit';waiting.waiters[0].pid=51;waiting.waiting=1;
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
              const state=await evaluate(`({text:document.querySelector('#wait-reasons').textContent,
                hover:tipText('wait:0',latest)[1], rows:document.querySelectorAll('.wrow[data-run]').length})`);
              assert.ok(state.text.includes(expected), name+' has no visible reason.');
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
            await evaluate(`showTip('row:${readings.shrink.walk.length-1}',200,200)`);
            const smaller=structuredClone(base);
            smaller.walk=smaller.walk.filter(row=>!row.queued);
            smaller.waiters=[];smaller.waiting=0;
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
        await writeFile(join(folder,'proof.json'),JSON.stringify({proofs,failures},null,2)+'\n');
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

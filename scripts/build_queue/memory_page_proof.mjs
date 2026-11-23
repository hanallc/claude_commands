// Run with node scripts/build_queue/memory_page_proof.mjs [--gate] [temporary-output-folder].
// Use --gate to check the dark desktop view.
// The optional third argument names the previous page for comparison images.
// Add --queued as the fourth argument for the queued-section comparison.
// Add --console as the fourth argument for the Builds title comparison.
// Add --step-names as the fourth argument for owner-based step names.
// Add --owners as the fourth argument for machine owners and table spacing.
// Add --cloud-owner as the fourth argument for cloud worker owner images.
// Add --blocks as the fourth argument for the target block and hold images.
// Add --identity as the fourth argument for the row hover identity block images.
// Add --wait-tip as the fourth argument for the waiting step hover images.
// Add --macroute as the fourth argument for the Mac route and width images.
// Use --verify-call as the third argument for the Verify column proof.
// Use --headcount to compare the Running and Queued headers.
// Use <output-folder> --aws-cost-shots to capture the AWS costs pane only.
// Add a folder with live memory.json, report.json, and memory_live.html for the Day images.
// Add --awscosts as the fourth argument for the AWS costs proof and images.
// Use --rowask to capture the rows and the New item dialog.
// Use <output-folder> --tip-design for the six dark tooltip captures.
// Set SWAP_BEFORE_PAGE to compare the swap tooltip with the previous page.
import assert from 'node:assert/strict';
import {accessSync, constants, statSync} from 'node:fs';
import {readFile, writeFile, mkdtemp, rm, mkdir} from 'node:fs/promises';
import {createServer} from 'node:http';
import {spawn, execFileSync} from 'node:child_process';
import {once} from 'node:events';
import {inflateSync} from 'node:zlib';
import {delimiter, join, dirname, resolve} from 'node:path';
import {tmpdir} from 'node:os';
import {fileURLToPath} from 'node:url';

const memoryTenths = text => text.startsWith("<") ? 1 : Math.round(parseFloat(text) * 10);

function chromeExecutable() {
  if (process.env.CHROME) return process.env.CHROME;
  const directories = (process.env.PATH ?? '').split(delimiter);
  for (const name of ['google-chrome-stable', 'google-chrome', 'chromium']) {
    for (const directory of directories) {
      const path = resolve(directory, name);
      try {
        if (!statSync(path, {throwIfNoEntry:false})?.isFile()) continue;
        accessSync(path, constants.X_OK);
        return path;
      } catch (error) {
        if (!['EACCES', 'ENOTDIR', 'EPERM'].includes(error.code)) throw error;
      }
    }
  }
  throw new Error('No Chrome browser found. Set CHROME or put google-chrome-stable, google-chrome, or chromium on PATH.');
}

const chromePath = chromeExecutable();
const repo = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const args = process.argv.slice(2);
const gateMode = args[0] === '--gate';
if (gateMode) args.shift();
const [callerFolder, ...comparisonArgs] = args;
const tipDesignComparison = !gateMode && comparisonArgs[0] === '--tip-design';
const awsCostShots = !gateMode && comparisonArgs[0] === '--aws-cost-shots';
const liveAwsCostSource = awsCostShots ? comparisonArgs[1] : undefined;
const [previousPage, comparison] = gateMode || tipDesignComparison || awsCostShots ? [] : comparisonArgs;
const requestedShot = gateMode ? undefined : process.env.MEMORY_PAGE_SHOT;
const verifyCallProof = previousPage === '--verify-call';
const themes = gateMode ? ['dark'] : ['light','dark'];
const widths = gateMode ? [1400] : [1400,900,390];
const runningHeaders = ['Stage','Done','Step','Dur','Verify','Pid','Target','vCPU','Slot','Tokens',''];
const queuedHeaders = ['#','Done','Step','Waiting','Verify','Pid','Target','vCPU',''];
const folder = callerFolder ? resolve(callerFolder) : await mkdtemp(join(tmpdir(), 'memory-page-proof-'));
try {
  await mkdir(folder, {recursive:true});
  const fixtureOutput = liveAwsCostSource ? '' : execFileSync(process.env.PYTHON || 'python3', ['-c',
    'from pathlib import Path; import json,sys; from scripts.build_queue.test_memory_feed import write_browser_fixture; write_browser_fixture(Path(sys.argv[1])); from scripts.build_queue.test_memory_hover import RAM_FIXTURE, aws_cost_fixture; (Path(sys.argv[1]) / "legend_ram.json").write_text(json.dumps(RAM_FIXTURE)); (Path(sys.argv[1]) / "aws_cost_fixture.json").write_text(json.dumps(aws_cost_fixture()))' + (verifyCallProof || awsCostShots ? '' : '; from scripts.lib import conf; print(Path(conf.read("build_folders").text("roots.system")).expanduser())'), folder],
    {cwd:repo, env:{...process.env, HERDR_BIN:'/nonexistent/herdr'}, encoding:'utf8'}).trim();
  const systemTemporaryRoot = verifyCallProof || awsCostShots ? tmpdir() : fixtureOutput;
  const page = await readFile(join(liveAwsCostSource ?? folder, 'memory_live.html'));
  const beforePage = previousPage && !verifyCallProof ? (await readFile(previousPage, 'utf8'))
    .replace('__MEMORY_FEED_INTERVAL_SECONDS__', '0.125') : null;
  const swapBeforePage = !gateMode && process.env.SWAP_BEFORE_PAGE
    ? (await readFile(process.env.SWAP_BEFORE_PAGE, 'utf8')).replace('__MEMORY_FEED_INTERVAL_SECONDS__','0.125') : null;
  const queuedComparison = comparison === '--queued';
  const consoleComparison = comparison === '--console';
  const stepNamesComparison = comparison === '--step-names';
  const ownersComparison = comparison === '--owners';
  const cloudOwnerComparison = comparison === '--cloud-owner';
  const blocksComparison = comparison === '--blocks';
  const identityComparison = comparison === '--identity';
  const waitTipComparison = comparison === '--wait-tip';
  const macrouteComparison = comparison === '--macroute';
  const headcountComparison = comparison === '--headcount';
  const rowAskComparison = comparison === '--rowask';
  const awsCostsComparison = comparison === '--awscosts';
  const comparisonImages = Boolean(comparison || beforePage || swapBeforePage || requestedShot);
  const readings = liveAwsCostSource
    ? {good: JSON.parse(await readFile(join(liveAwsCostSource, 'memory.json'), 'utf8'))}
    : JSON.parse(await readFile(join(folder, 'fixtures.json'), 'utf8'));
  const base = readings.good;
  const fixtureTime = base.read_at;
  const clockTime = liveAwsCostSource ? Date.now() : fixtureTime * 1000;
  if (liveAwsCostSource) {
    base.report = JSON.parse(await readFile(join(liveAwsCostSource, 'report.json'), 'utf8'));
  } else {
    await prepareFixtures();
  }

  async function prepareFixtures() {
    const costs = JSON.parse(await readFile(join(folder, 'aws_cost_fixture.json'), 'utf8'));
    readings.aws_costs = {...structuredClone(readings.good), report_version:'aws-cost-proof',
      report:{...readings.good.report, aws_costs:costs}};
    for (const reading of Object.values(readings)) {
      const shift = fixtureTime - reading.read_at;
      for (const row of reading.walk) {
        if (row.started_at) row.started_at += shift;
        if (row.read_at) row.read_at += shift;
        for(const part of row.parts || []) if(part.started_at) part.started_at += shift;
      }
      for (const waiter of reading.waiters) waiter.since += shift;
      reading.read_at = fixtureTime;
    }
    base.waiters = structuredClone(readings.spacing.waiters);
    base.walk.find(row => row.queued).progress = {kind:'RowProgress', phase:'testing', done:8, total:10, percent:80, unit:'modules'};
    base.walk.find(row => row.queued).unit = 'handler';
    base.walk.find(row => row.queued).showrunner = 'sdlc';
    const swapRow = (name, kind, bytes, pool='outside', extra={}) => ({name, detail:'fixture owner', kind, pool,
      category:'', members:[], owner_count:0, bytes, swap:bytes/1024**3, ...extra});
    const swapOwners = Array.from({length:31}, (_, index) => swapRow('group '+index, 'owner', (31-index)*1024**3/64,
      'outside', {owner_count:1}));
    const smaller = swapOwners.slice(base.swap_rows);
    const groupedBytes = smaller.reduce((sum,row)=>sum+row.bytes,0);
    const swapUsed = swapOwners.reduce((sum,row)=>sum+row.bytes,0)+1031;
    base.swap = {used_bytes:swapUsed, capacity_bytes:8*1024**3, builds_ci_bytes:0, outside_bytes:swapUsed,
      reserve_share_percent:50};
    base.swap_by = [...swapOwners.slice(0,base.swap_rows),
      swapRow(smaller.length+' smaller groups', 'smaller', groupedBytes, 'outside',
        {members:smaller.map(row=>({name:row.name, bytes:row.bytes})), owner_count:smaller.length}),
      swapRow('Unclassified groups', 'ungrouped', 1024),
      swapRow('Not attributed', 'unattributed', 7),
      swapRow('All swap used', 'total', swapUsed, 'all')];
    readings.shrink = structuredClone(base);
    const extra = structuredClone(base.walk.find(row => row.queued));
    extra.name = 'second nextest'; extra.pid = 222; extra.key += '-second';
    readings.shrink.walk.unshift(extra);
    readings.shrink.waiters = [];
  }
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

  let servedReading = base;
  let currentFixture = base;
  let submittedItem;
  const server = createServer(async (req, res) => {
    if (req.url === '/console/review/new-item') {
      res.writeHead(200, {'Content-Type':'application/json'});
      if (req.method === 'POST') {
        const chunks = [];
        for await (const chunk of req) chunks.push(chunk);
        submittedItem = JSON.parse(Buffer.concat(chunks));
        res.end(JSON.stringify({id:'Q1'}));
      } else {
        res.end(JSON.stringify({token:'proof-token',recipients:[
          {id:'fixture-hana',name:'hana',proposal:true}, {id:'fixture-builds',name:'builds',proposal:true}
        ]}));
      }
      return;
    }
    const isData = req.url.startsWith('/memory.json'), isReport = req.url.startsWith('/report.json');
    res.writeHead(200, {'Content-Type':isData || isReport ? 'application/json' : 'text/html; charset=utf-8'});
    res.end(isData ? JSON.stringify(servedReading) : isReport ? JSON.stringify(awsCostShots ? servedReading.report : base.report)
      : req.url === '/before.html' ? beforePage : req.url === '/swap-before.html' ? swapBeforePage : page);
  });
  try {
    server.listen(0, '127.0.0.1');
    await once(server, 'listening');
    // Chrome needs a short path for its Unix sockets.
    const profile = await mkdtemp(join(systemTemporaryRoot, 'memory-proof-chrome-'));
    try {
      const temporary = join(profile, 'tmp');
      await mkdir(temporary);
      const chrome = spawn(chromePath, [
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
      browserProof: try {
        const endpoint = await new Promise((resolve, reject) => {
          let output = '';
          chrome.stderr.on('data', chunk => {
            output += chunk;
            const match = output.match(/DevTools listening on (ws:\/\/\S+)/);
            if (match) resolve(match[1]);
          });
          chrome.once('error', reject);
          chrome.once('exit', () => reject(new Error('Chrome closed before its debugger started.\n' + output.trim())));
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
          source:'window.proofSetInterval = window.setInterval; window.setInterval = () => 0;'
            + 'Date.now = () => ' + clockTime + ';'});
        await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
        await call('Emulation.setDeviceMetricsOverride', {width:gateMode ? 1400 : 1600,height:1200,deviceScaleFactor:1,mobile:false});
        const navigate = async path => {
          currentFixture = servedReading;
          const loaded = new Promise((resolve, reject) => events.set(`${sessionId}:Page.loadEventFired`, {resolve,reject}));
          await Promise.all([loaded, call('Page.navigate', {url:`http://127.0.0.1:${server.address().port}/${path}`})]);
          await evaluate(`(async () => {
          const style = document.createElement('style');
          style.textContent = '* { transition: none !important; }';
          document.head.append(style);
          await ${path === 'memory_live.html' ? 'pageRefresh.refresh(true)' : 'tick()'};
          window.proofTip = (kind, index) => kind + ':' + (kind === 'row' ? latest.walk : latest.waiters)[index].key;
          window.proofRun = index => latest.walk[index].key;
          window.renderReading = render;
          window.proofData = structuredClone(latest);
          window.render = () => {};
          await document.fonts.ready;
          const legend = document.querySelector('.legend-box');
          if (${JSON.stringify(path)} === 'memory_live.html' && legend.open) throw new Error('The legend starts open.');
          // The proof measures the legend, so it opens it as a click does.
          if (legend) legend.open = true;
          fitLabels();
          })()`);
        };
        if (awsCostShots) {
          servedReading = liveAwsCostSource ? base : readings.aws_costs;
          await navigate('memory_live.html');
          const shots = await captureAwsCosts(evaluate, call, liveAwsCostSource ? base.report.aws_costs.today : null);
          console.log(JSON.stringify({shots,output:folder}));
          break browserProof;
        }
        if (verifyCallProof) {
          servedReading = readings.verify_calls;
          const views = [];
          for (const theme of ['dark', 'light']) {
            for (const width of [1600, 850, 390]) {
              await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
              await call('Emulation.setDeviceMetricsOverride', {width,height:1100,deviceScaleFactor:1,mobile:false});
              await navigate('memory_live.html');
              const result = await evaluate(`(() => {
                const waiting = latest.waiters.find(row => row.verify);
                const running = latest.walk.find(row => row.queued && row.verify);
                const mac = latest.walk.find(row => row.kind === 'mac-build' && row.verify);
                const direct = latest.waiters.find(row => !row.verify);
                const key = 'wait:' + waiting.key;
                const chart = document.querySelector('.wrow[data-tip="' + key + '"]');
                const cell = (kind, row) => document.querySelector('tr[data-tip="' + kind + ':' + row.key + '"] .verify');
                const waiterCell = cell('wait', waiting);
                if (innerWidth < 900) waiterCell.scrollIntoView({block:'nearest',inline:'center'});
                const position = waiterCell.getBoundingClientRect();
                const rowBounds = waiterCell.closest('tr').getBoundingClientRect();
                const x = innerWidth >= 900 ? Math.min(innerWidth-200, rowBounds.right+220) : position.left+position.width/2;
                showTip(key, x, position.top+15);
                const tooltip = document.querySelector('#tip');
                const tipBounds = tooltip.getBoundingClientRect();
                const role = '[data-color-text="role.unit-director"]';
                const titleStaff = tooltip.querySelector(role);
                const unitColumn = waiterCell.closest('tr').querySelector('.unit');
                const staffBounds = titleStaff?.getBoundingClientRect();
                const staffColor = titleStaff && getComputedStyle(titleStaff).color;
                const fields = Array.from(tooltip.querySelectorAll('dt')).map(label => {
                  const value = label.nextElementSibling;
                  const bounds = value.getBoundingClientRect();
                  const range = document.createRange();
                  range.selectNodeContents(value);
                  const fits = Array.from(range.getClientRects()).every(rect =>
                    rect.left >= bounds.left-1 && rect.right <= bounds.right+1 &&
                    rect.top >= bounds.top-1 && rect.bottom <= bounds.bottom+1);
                  return {label:label.textContent,value:value.textContent,fits,align:getComputedStyle(value).textAlign,
                          right:bounds.right,group:value.classList.contains('group')};
                });
                const headers = Array.from(document.querySelectorAll('th.verify'));
                const bounds = headers.map(header => header.getBoundingClientRect());
                const left = Math.max(0, chart.getBoundingClientRect().left-8);
                const top = Math.max(0, Math.min(tipBounds.top,...bounds.map(box => box.top))-8);
                const right = Math.min(innerWidth, Math.max(tipBounds.right,position.right)+8);
                const bottom = Math.min(innerHeight, Math.max(tipBounds.bottom,rowBounds.bottom)+8);
                const visible = tipBounds.left >= 0 && tipBounds.right <= innerWidth &&
                  tipBounds.top >= 0 && tipBounds.bottom <= innerHeight &&
                  position.left >= 0 && position.right <= innerWidth &&
                  waiterCell.scrollWidth === waiterCell.clientWidth;
                return {fields,visible,headers:headers.map(header => header.textContent),
                  staff:{title:titleStaff?.textContent,unitColumn:!!unitColumn,color:staffColor,
                    visible:staffBounds && staffBounds.width > 0 && staffBounds.left >= tipBounds.left
                      && staffBounds.right <= tipBounds.right && staffColor !== getComputedStyle(tooltip).color},
                  verbs:[cell('row',running).textContent,cell('row',mac).textContent,
                    waiterCell.textContent,cell('wait',direct).textContent],
                  macFields:tooltipRows('row:' + mac.key, latest).map(({label,value}) => [label,value]),
                  facts:waiting.verify,clip:{x:left+scrollX,y:top+scrollY,width:right-left,height:bottom-top,scale:1}};
              })()`);
              assert.deepEqual(result.headers, ['Verify','Verify']);
              assert.deepEqual(result.verbs, ['final','test','final','']);
              assert.deepEqual(result.macFields.slice(1), [['Step','nextest'],['Call','verify test sample --no-cache']]);
              const {fields, facts, staff} = result;
              assert.deepEqual([staff.title, staff.unitColumn, staff.visible], ['builder', false, true]);
              assert.equal(fields.find(field => field.label === 'Runs on').value, 'natedev only');
              assert.equal(fields.find(field => field.label === 'Because').value, 'creates a Linux install');
              assert.equal(fields.find(field => field.label === 'Step').value, `${facts.step} (${facts.position} of ${facts.total})`);
              assert.equal(facts.call, "verify final sample --local 'creates a Linux install'");
              const last = fields.at(-1);
              assert.deepEqual([last.label,last.value,last.group], ['Call',facts.call,true]);
              assert.ok(result.visible && fields.every(field => field.fits && field.align === 'right'
                && Math.abs(field.right-fields[0].right) < 1), JSON.stringify({theme,width,result}));
              views.push({theme,width,staffColor:staff.color});
              if (theme === 'dark' && width === 1600) {
                const shot = await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip:result.clip});
                await writeFile(join(folder,'builds-verify-column-dark.png'),Buffer.from(shot.data,'base64'));
              }
            }
          }
          console.log(JSON.stringify({pass:true,views}));
          break browserProof;
        }
        await checkHeadcounts(navigate, evaluate, call);
        if (headcountComparison) console.log(JSON.stringify({pass:true,comparison:'headcount'}));
        else {
          servedReading = base;
          await checkPage(navigate, evaluate, call);
        }
      } finally {
        if(ws) ws.close();
        chrome.kill('SIGTERM');
        await exited;
      }

      async function checkHeadcounts(navigate, evaluate, call) {
        const headingReading = structuredClone(readings.queued_none);
        const toolsBytes = 6 * 1024 ** 3;
        Object.assign(headingReading.ram, {build_tools_bytes:toolsBytes,rust_analyzer_bytes:toolsBytes,
          sccache_bytes:0,other_build_tools_bytes:0});
        const local = base.walk.find(row => row.queued && !row.ci);
        const aws = readings.cloud.walk.find(row => row.kind === 'cloud');
        const mac = readings.mac_three.walk.find(row => row.kind === 'mac-build');
        const macStatus = readings.mac_failed.walk.find(row => row.kind === 'mac-unavailable');
        assert.ok(local && aws && mac && macStatus);
        headingReading.walk = [local, aws, mac, {...local, queued:false, key:'untracked-step'}];
        const headerState = `({
          running:document.querySelector('[data-tip="walkhead"]').textContent,
          queued:document.querySelector('#walk .queue-title')?.textContent ?? '',
          tools:document.querySelector('#walk .wrow.tools .name')?.textContent,
          free:document.querySelector('#walk .wrow.room .name')?.textContent
        })`;
        const captureHeadings = async name => {
          await evaluate("document.querySelector('[data-tip=walkhead]').scrollIntoView({block:'center'})");
          const clip = await evaluate(`(() => {
            const section = document.querySelector('[data-tip="walkhead"]').closest('section').getBoundingClientRect();
            const queue = document.querySelector('#walk .queue-title').getBoundingClientRect();
            return {x:section.left,y:section.top+scrollY,width:section.width,height:queue.bottom-section.top+8,scale:1};
          })()`);
          const shot = await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip});
          await writeFile(join(folder,`${name}-dark.png`),Buffer.from(shot.data,'base64'));
        };
        await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
        await call('Emulation.setDeviceMetricsOverride', {width:gateMode ? 1400 : 1440,height:1100,deviceScaleFactor:1,mobile:false});
        servedReading = headingReading;
        if (headcountComparison) {
          assert.ok(beforePage, 'The header comparison needs the previous page.');
          await navigate('before.html');
          assert.deepEqual(await evaluate(headerState), {running:'Running, in start order',
            queued:'Queued, in order of submission',tools:'Build tools',free:'Free'});
          await captureHeadings('before');
        }
        await navigate('memory_live.html');
        assert.deepEqual(await evaluate(headerState), {running:'Running, in start order (3)',
          queued:'Queued, in order of submission (4)',tools:'Build tools',free:'Free'});
        if (headcountComparison) await captureHeadings('after');
        await evaluate('window.render = window.renderReading');
        const longUnit = 'research-with-a-long-unit-name'.repeat(8);
        for (const [units, walk, count, queued] of [
          [['research'], [local, aws, macStatus], 2, 'Queued, research first, then in submission order (2)'],
          [[longUnit], [local, aws, macStatus], 2, `Queued, ${longUnit} first, then in submission order (2)`],
          [['research', 'cables'], [local], 1, 'Queued, research followed by cables and then in submission order (1)'],
          [['research'], [macStatus], 0, 'Queued, in order of submission (0)'],
        ]) {
          servedReading = {...headingReading, walk, waiters:headingReading.waiters.slice(0,count), queued_priority_units:units};
          await evaluate('pageRefresh.refresh(true)');
          assert.deepEqual(await evaluate(headerState), {
            running:`Running, in start order (${walk.filter(row=>row.kind!=='mac-unavailable').length})`,
            queued,tools:'Build tools',free:'Free'});
          assert.deepEqual(await evaluate(`[
            document.querySelectorAll('#walk .wrow.wait').length,
            document.querySelectorAll('#walk tr.wait').length
          ]`), [count, count]);
          if (units.includes(longUnit)) {
            assert.deepEqual(await evaluate(`(() => {
              const heading = document.querySelector('#walk .queue-title');
              const [label, count] = heading.children;
              return {
                truncated: label.scrollWidth > label.clientWidth && getComputedStyle(label).textOverflow === 'ellipsis',
                count: count.textContent.trim(),
                countFits: count.getBoundingClientRect().right <= heading.parentElement.getBoundingClientRect().right + 1
              };
            })()`), {truncated:true, count:'(2)', countFits:true});
          }
        }
      }

      async function captureAwsCosts(evaluate, call, liveDay = null) {
        await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
        await call('Emulation.setDeviceMetricsOverride', {width:1400,height:1400,deviceScaleFactor:1,mobile:false});
        const capture = async name => {
          const clip = await evaluate(`(() => {
            const r = document.querySelector('#aws-costs').getBoundingClientRect();
            return {x:r.left+scrollX, y:r.top+scrollY, width:r.width, height:r.height, scale:1};
          })()`);
          const shot = await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip});
          await writeFile(join(folder,name+'.png'),Buffer.from(shot.data,'base64'));
        };
        if (liveDay) {
          const result = await evaluate(`(() => {
            const pane = document.querySelector('#aws-costs');
            const detail = pane.querySelector('[data-aws-day="' + ${JSON.stringify(liveDay)} + '"]');
            const day = awsCostReport.days.find(day => day.day === detail.dataset.awsDay);
            const dayCents = awsCents(day.amount_usd);
            const select = group => detail.querySelector('[data-aws-group-by="' + group + '"]').click();
            const tableTotal = group => {
              select(group);
              const rows = [...detail.querySelectorAll('.aws-group-scroll > table > tbody > tr')];
              const total = rows.pop();
              const cents = row => awsCents(row.cells[3].textContent.replace(/[~$,]/g, ''));
              const sum = rows.reduce((amount, row) => amount + cents(row), 0);
              return {sum, displayed:cents(total)};
            };
            const totals = Object.fromEntries(['region', 'machine', 'job'].map(group => [group, tableTotal(group)]));
            const hana = [...detail.querySelectorAll('tr[data-aws-group] button')]
              .find(button => button.textContent.trim() === 'Hana check and lint');
            const hanaCells = hana.closest('tr').cells;
            const groupTime = hanaCells[2].textContent;
            const hanaRuns = day.runs_list.filter(run => run.kind === 'hana');
            const expectedGroupTime = hanaRuns.length
              ? awsRunTime(hanaRuns.reduce((total, run) => total + run.seconds, 0)) : 'no record';
            const perRun = hanaCells[4].textContent;
            const totalTime = detail.querySelector('.aws-total-row').cells[2].textContent;
            hana.click();
            const expanded = detail.querySelector('.aws-group-detail');
            const rows = [...expanded.querySelectorAll('.aws-run-scroll > table > tbody > tr')];
            const idle = rows.filter(row => row.cells[0].textContent === 'Hana machine with no run');
            const csv = awsRunsCsv(awsCostReport.days).split('\\r\\n');
            const ids = awsCostReport.days.flatMap(day => day.runs_list.map(run => run.id));
            pane.scrollIntoView({block:'start'});
            return {group:hana.textContent.trim(), runs:rows.length - idle.length, expectedRuns:day.hana.runs,
              groupTime, expectedGroupTime, perRun, totalTime, idleTimes:idle.map(row => row.cells[7].textContent),
              idleRows:idle.length, expectedIdleRows:Number(awsCents(day.hana.idle_usd) !== 0), dayCents, totals,
              csvHeader:csv[0], csvRows:csv.length - 2,
              recordedRuns:new Set(ids).size, hasRunIds:ids.every(Boolean),
              fits:[...pane.querySelectorAll('.aws-scroll')].every(box => box.scrollWidth <= box.clientWidth + 1)};
          })()`);
          const reconciled = Object.values(result.totals).every(({sum, displayed}) =>
            sum === result.dayCents && displayed === result.dayCents);
          if (!reconciled || !result.fits || result.runs !== result.expectedRuns || result.idleRows !== result.expectedIdleRows ||
              result.groupTime !== result.expectedGroupTime || result.idleTimes.some(time => time !== 'no record') ||
              !result.hasRunIds || result.csvRows !== result.recordedRuns) {
            throw new Error('The live Day image or CSV is incomplete. ' + JSON.stringify(result));
          }
          const name = 'aws-cost-day-live-dark-6';
          await capture(name);
          console.log(JSON.stringify({day:liveDay, ...result}));
          const historical = await evaluate(`(() => {
            const current = ${JSON.stringify(liveDay)};
            const day = '2026-11-07';
            const periodButton = date => document.querySelector('tr[data-period="day-' + date + '"] button');
            periodButton(current).click();
            if (!periodButton(day)) document.querySelector('#aws-older').click();
            periodButton(day).click();
            const detail = document.querySelector('[data-aws-day="' + day + '"]');
            return [...detail.querySelectorAll('.aws-group-scroll > table > tbody > tr')].map(row =>
              [row.cells[0].textContent.trim(), row.cells[3].textContent]);
          })()`);
          const historicalName = 'aws-cost-nov7-dark';
          await capture(historicalName);
          console.log(JSON.stringify({day:'2026-11-07', charges:historical}));
          return [name + '.png', historicalName + '.png'];
        }
        await evaluate(`(() => {
          const pane = document.querySelector('#aws-costs');
          pane.scrollIntoView({block:'start'});
          const date = pane.querySelector('[data-tip="aws-cost:day-2026-11-16"]');
          const bounds = date.getBoundingClientRect();
          showTip(date.dataset.tip, bounds.right, bounds.top);
          const tip = document.querySelector('#tip');
          showTip(date.dataset.tip, bounds.right + 22 + tip.offsetWidth / 2, bounds.bottom + tip.offsetHeight);
        })()`);
        await capture('aws-cost-day-dark');
        await evaluate(`(() => {
          const pane = document.querySelector('#aws-costs');
          pane.querySelector('[data-aws-view="month"]').click();
          pane.querySelector('tr[data-period^="week-"] button').click();
          pane.querySelector('[data-tip="aws-cost:day-2026-11-17"]').click();
        })()`);
        await capture('aws-cost-month-open-dark');
        return ['aws-cost-day-dark.png', 'aws-cost-month-open-dark.png'];
      }

      async function checkAwsCosts(evaluate, drillDown) {
        const summary = await evaluate(`(() => {
          const pane = document.querySelector('#aws-costs');
          const lines = [...pane.querySelectorAll('.aws-total-value')].map(tile => tile.textContent.trim());
          const body = pane.innerText.replace(/\\s+/g, ' ');
          return {lines, body, fits:pane.scrollWidth <= pane.clientWidth,
            days:pane.querySelectorAll('tr[data-period]').length, open:pane.querySelector('.aws-toggle').getAttribute('aria-expanded')};
        })()`);
        assert.deepEqual(summary.lines, ['~$5.25 of $35.00 limit','~$9.03','~$104.47 of $400.00 limit']);
        assert.equal(summary.days,14);
        assert.equal(summary.open,'true');
        assert.ok(summary.fits, 'The AWS history scrolls inside the pane.');
        assert.match(summary.body, /AWS amounts read Wed Nov 18, 1:43 PM EST/);
        assert.match(summary.body, /Thu Nov 19 · started Wed 7:00 PM EST · ends Thu 7:00 PM EST/);
        assert.match(summary.body, /In Eastern time, a day starts at 7:00 PM on the day before and ends at 7:00 PM\./);
        assert.match(summary.body, /The next AWS read is due Thu Nov 19, 12:43 PM EST/);
        assert.match(summary.body, /Group by.*Download CSV.*Hana check and lint.*Costs with no run.*Total 193/);
        assert.match(summary.body, /Kept resources now cost \$28.20 each month/);
        assert.doesNotMatch(summary.body, /\bgate\b|targeted test/i);
        if (!drillDown) return;
        const result = await evaluate(`(async () => {
          const pane = document.querySelector('#aws-costs');
          const select = view => pane.querySelector('[data-aws-view="' + view + '"]').click();
          const content = pane.querySelector('.aws-rule');
          renderReport(structuredClone(latest.report));
          const sameContent = content === pane.querySelector('.aws-rule');
          const row = id => pane.querySelector('tr[data-period="' + id + '"]');
          const open = id => row(id).querySelector('button').click();
          const fields = key => {
            const trigger = pane.querySelector('[data-tip="aws-cost:' + key + '"]');
            const box = trigger.getBoundingClientRect();
            showTip(trigger.dataset.tip, box.left, box.bottom);
            return [...document.querySelectorAll('#tip dl.tip dt')].map(term => [term.textContent, term.nextElementSibling.textContent]);
          };
          const dayLimit = fields('day-limit');
          const monthLimit = fields('month-limit');
          const dayTip = fields('day-2026-11-19');
          hideTip();
          pane.querySelector('#aws-older').click();
          const count = pane.querySelectorAll('tr[data-period]').length;
          const missing = row('day-2026-11-03').cells[2].textContent.trim();
          const zero = [row('day-2026-11-02').cells[2], row('day-2026-11-02').cells[4]].map(cell => cell.textContent.trim());
          pane.querySelector('#aws-older').click();
          const detail = pane.querySelector('[data-aws-day="2026-11-19"]');
          const groupBy = group => detail.querySelector('[data-aws-group-by="' + group + '"]').click();
          const groupButton = label => [...detail.querySelectorAll('tr[data-aws-group] button')]
            .find(button => button.textContent.trim() === label);
          groupButton('integrate').click();
          const runCount = detail.querySelectorAll('.aws-group-detail tbody tr').length;
          const runFields = [...detail.querySelector('.aws-group-detail tbody tr').cells].slice(1).map(cell => cell.textContent);
          groupButton('integrate').click();
          groupButton('Hana check and lint').click();
          const hanaCells = groupButton('Hana check and lint').closest('tr').cells;
          const hanaTime = hanaCells[2].textContent;
          const hanaPerRun = hanaCells[4].textContent;
          const totalTime = detail.querySelector('.aws-total-row').cells[2].textContent;
          const hanaDetails = [...detail.querySelectorAll('.aws-group-detail tbody tr')].map(row => row.cells[7].textContent);
          const groupNames = () => [...detail.querySelectorAll('tr[data-aws-group] button')].map(button => button.textContent.trim());
          const idleCharge = () => [...detail.querySelectorAll('.aws-group-scroll > table > tbody > tr')]
            .find(row => row.cells[0].textContent === 'Hana machine with no run').cells[2].textContent;
          groupBy('region');
          const regions = groupNames();
          const idleTimes = [idleCharge()];
          groupBy('machine');
          const machines = groupNames();
          idleTimes.push(idleCharge());
          groupBy('unit');
          const units = groupNames();
          idleTimes.push(idleCharge());
          groupBy('none');
          const rawRows = detail.querySelectorAll('.aws-run-scroll tbody tr').length;
          groupBy('job');
          open('day-2026-11-19');
          select('week');
          const week = [...row('week-2026-11-16').cells].slice(1,3).map(cell => cell.textContent);
          select('month');
          const month = [...row('month-2026-11-01').cells].slice(1,3).map(cell => cell.textContent);
          open('week-2026-11-16');
          open('day-2026-11-17');
          const billed = pane.querySelector('.aws-day-detail').innerText.replace(/\\s+/g, ' ');
          select('year');
          const year = [...row('year-2026-01-01').cells].slice(1,3).map(cell => cell.textContent);
          select('day');
          const savedDay = row('day-2026-11-19').querySelector('button').getAttribute('aria-expanded');
          select('month');
          const savedMonthDay = row('day-2026-11-17').querySelector('button').getAttribute('aria-expanded');
          select('day');
          open('day-2026-11-19');
          return {sameContent,dayLimit,monthLimit,dayTip,count,missing,zero,runCount,runFields,hanaTime,hanaPerRun,hanaDetails,
            totalTime,regions,machines,units,idleTimes,rawRows,week,month,year,billed,savedDay,savedMonthDay};
        })()`);
        assert.equal(result.sameContent,true);
        assert.deepEqual(result.dayLimit, [['Daily limit',''],['Limit','$35.00'],['New work stops at','$32.00'],
          ['Starts again','Thu Nov 19, 7:00 PM EST']]);
        assert.deepEqual(result.monthLimit, [['Monthly limit',''],['Limit','$400.00'],['New work stops at','$397.00'],
          ['Starts again','Mon Nov 30, 7:00 PM EST']]);
        assert.deepEqual(result.dayTip, [['Thu Nov 19 (UTC)',''],['Starts','Wed Nov 18, 7:00 PM EST'],
          ['Ends','Thu Nov 19, 7:00 PM EST']]);
        assert.deepEqual([result.count,result.missing,result.runCount,result.rawRows], [18,'no record',84,196]);
        assert.deepEqual(result.zero, ['$0.00','Billed, not final']);
        assert.deepEqual(result.runFields, ['integrate','report','us-east-1','us-east-1a','c7i.8xlarge','Spot','9 min 34 s','~$0.02']);
        assert.equal(result.hanaTime, '21 s');
        assert.equal(result.hanaPerRun, '—');
        assert.equal(result.totalTime, '29 h 39 min 45 s');
        assert.deepEqual(result.hanaDetails, ['0 s','1 s','2 s','3 s','4 s','5 s','6 s','no record']);
        assert.deepEqual(result.idleTimes, ['no record','no record','no record']);
        assert.deepEqual(result.regions, ['us-east-1','us-west-2']);
        assert.deepEqual(result.machines, ['c7i.8xlarge','m7a.8xlarge']);
        assert.deepEqual(result.units, ['report','Unit not recorded']);
        assert.deepEqual(result.week,['934','~$19.87']);
        assert.deepEqual(result.month,['2,986','~$104.47']);
        assert.deepEqual(result.year,result.month);
        assert.match(result.billed,/Job not recorded/);
        assert.doesNotMatch(result.billed,/Our estimate before the AWS amount/);
        assert.deepEqual([result.savedDay,result.savedMonthDay],['false','true']);
      }

      async function checkPage(navigate, evaluate, call) {
        const readPixel = async (x, y) => {
          const clip = {x:Math.floor(x),y:Math.floor(y),width:1,height:1,scale:1};
          const shot = await call('Page.captureScreenshot', {format:'png',captureBeyondViewport:true,clip});
          return pngPixels(Buffer.from(shot.data,'base64'))(0,0);
        };
        const saveScreenshot = async (name, options = {}, enabled = comparisonImages) => {
          if (!enabled) return;
          const shot = await call('Page.captureScreenshot', {format:'png', ...options});
          await writeFile(join(folder,name+'.png'),Buffer.from(shot.data,'base64'));
        };
        await navigate('memory_live.html');
        // The user, 11-16: space pauses every update, a tooltip still opens, and space again resumes.
        const pressSpace = async () => {
          for (const type of ['keyDown','keyUp'])
            await call('Input.dispatchKeyEvent',{type,code:'Space',key:' ',windowsVirtualKeyCode:32,nativeVirtualKeyCode:32});
        };
        // A reading that fails after the pause leaves the page as it was.
        await evaluate(`(()=>{
          window.proofRead=window.fetch;
          window.fetch=()=>new Promise((_,reject)=>{window.proofFail=reject;});
          window.proofPending=pageRefresh.refresh(true);
          scrollTo(0,0);
        })()`);
        await pressSpace();
        const lateFailure = await evaluate(`(async()=>{
          window.proofFail(new Error('proof'));
          await window.proofPending;
          const read=window.proofRead;
          window.proofFetches=0;
          window.fetch=(...request)=>{window.proofFetches++;return read(...request);};
          const line=document.querySelector('[data-page-status]');
          return {state:line.dataset.state,reason:line.querySelector('[data-status-reason]').textContent};
        })()`);
        assert.deepEqual(lateFailure,{state:'Paused',reason:'Press space to resume.'},'A reading that fails after the pause changes the page.');
        // A report that arrives after the pause leaves the charts as they were.
        const lateReport = await evaluate(`(async()=>{
          const draw=window.renderReport, version=reportVersion;
          let drawn=0;
          window.renderReport=()=>{drawn++;};
          await loadReport('proof-late',()=>true);
          window.renderReport=draw;
          window.proofFetches=0;
          return {drawn,kept:reportVersion===version};
        })()`);
        assert.deepEqual(lateReport,{drawn:0,kept:true},'A report that arrives after the pause changes the charts.');
        const pausedPage = await evaluate(`(async()=>{
          const line=document.querySelector('[data-page-status]');
          const word=line.querySelector('[data-status-word]');
          const visibility={checkVisibilityCSS:true,checkOpacity:true};
          const shown=line.dataset.state==='Paused'&&word.textContent==='Paused'
            &&line.checkVisibility(visibility)&&word.checkVisibility(visibility);
          await pageRefresh.refresh();
          await reloadWhenPageChanges();
          const pill=document.querySelector('#pill').getBoundingClientRect();
          return {shown,fetches:window.proofFetches,pill:{x:pill.left+pill.width/2,y:pill.top+pill.height/2}};
        })()`);
        assert.ok(pausedPage.shown,'Space does not show the Paused state.');
        assert.equal(pausedPage.fetches,0,'A paused page still reads.');
        await call('Input.dispatchMouseEvent',{type:'mouseMoved',x:pausedPage.pill.x,y:pausedPage.pill.y});
        const pausedTip = await evaluate(`(()=>{
          const tip=document.querySelector('#tip');
          const on=tip.classList.contains('on');
          if (!${comparisonImages}) return {on};
          const boxes=[tip,document.querySelector('#paused'),document.querySelector('[data-page-status]')].map(item=>item.getBoundingClientRect());
          const left=Math.min(...boxes.map(box=>box.left))-12, top=Math.min(...boxes.map(box=>box.top))-12;
          const right=Math.max(...boxes.map(box=>box.right))+12, bottom=Math.max(...boxes.map(box=>box.bottom))+12;
          return {on,clip:{x:Math.max(0,left),y:Math.max(0,top),width:right-Math.max(0,left),height:bottom-Math.max(0,top),scale:1}};
        })()`);
        assert.ok(pausedTip.on,'A paused page opens no tooltip.');
        await saveScreenshot('paused-tooltip-dark-desktop',{clip:pausedTip.clip});
        await call('Input.dispatchMouseEvent',{type:'mouseMoved',x:4,y:4});
        await pressSpace();
        const resumedPage = await evaluate(`(async()=>{
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          return {state:document.querySelector('[data-page-status]').dataset.state,fetches:window.proofFetches};
        })()`);
        assert.equal(resumedPage.state,'Live','Space does not restore the Live state.');
        assert.ok(resumedPage.fetches>0,'Space does not resume reading.');

        const comparisons = [];
        const proofs = [];
        // An over-cap reading takes more than the cap total, so its legend rows exceed that total.
        const proofTip = (kind, index, reading = currentFixture) => kind + ':' + (kind === 'row' ? reading.walk : reading.waiters)[index].key;
        const show = async (reading, clearTip=true, legendSums=true) => {
          for (const [kind, rows] of [['run', reading.walk], ['wait', reading.waiters]]) {
            const seen = new Set();
            for (const row of rows) {
              row.key ??= `proof-${kind}-${row.pid ?? row.name}`;
              if (seen.has(row.key)) row.key += '-process-' + row.pid;
              seen.add(row.key);
            }
          }
          currentFixture = reading;
          const totals=await evaluate(`(async () => {
          ${clearTip ? "hideTip();" : ""}
          document.body.classList.remove('queue-unavailable');
          const data = ${JSON.stringify(reading)};
          latest = data;
          window.renderReading(data);
          renderReport(data.report);
          fitLabels();
          await document.fonts.ready;
          await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
          return [...document.querySelectorAll('.legend-group')].map(group=>({
            name:group.querySelector('.group-head .name').textContent,
            total:group.querySelector(':scope > .group-head .v,:scope > .row.total .v').textContent,
            rows:[...group.querySelectorAll(':scope > .row:not(.in):not(.total)')]
              .filter(row=>!row.hidden&&row.dataset.tip!=='shortfall')
              .map(row=>row.querySelector('.v').textContent)}));
          })()`);
          for(const group of legendSums?totals:[]) {
            if(group.total==='unavailable') continue;
            const amount=text=>text==='unavailable' ? 0 : memoryTenths(text);
            const sum=group.rows.reduce((total,text)=>total+amount(text),0);
            assert.equal(amount(group.total),sum,
              'A legend total differs from its rows: '+JSON.stringify(group));
          }
          return true;
        };
        // The same step keeps its screen position and hover after work above it changes.
        const steady = structuredClone(base);
        steady.waiters = Array.from({length: 12}, (_, index) => ({...steady.waiters[0],
          key: 'steady-wait-' + index, pid: 8000 + index, name: 'steady waiting ' + index}));
        servedReading = steady;
        await call('Emulation.setDeviceMetricsOverride', {width:1400,height:700,deviceScaleFactor:1,mobile:false});
        await navigate(!gateMode && process.env.STEADY_BASELINE ? 'before.html' : 'memory_live.html');
        const beforeSteady = await evaluate(`(() => {
          const row = [...document.querySelectorAll('.wrow.wait')].find(row => row.querySelector('.name').textContent === 'steady waiting 3');
          window.scrollBy(0, row.getBoundingClientRect().top - 200);
          const key = row.dataset.tip;
          const table = document.querySelector('tr[data-tip="'+key+'"]');
          table.focus({preventScroll:true});
          document.querySelector('#swaprows .smaller details').open = true;
          showTip(key, 700, 500);
          window.render = window.renderReading;
          return {top:row.getBoundingClientRect().top, key, readAt:latest.read_at, tip:document.querySelector('#tip').innerText,
            tipTop:document.querySelector('#tip').style.top,tipLeft:document.querySelector('#tip').style.left};
        })()`);
        const changedSteady = structuredClone(steady);
        changedSteady.walk.unshift(...[1,2].map(index => ({...changedSteady.walk.find(row=>row.queued),
          key:'steady-new-'+index, pid:9000+index, name:'new running '+index})));
        changedSteady.waiters = [...changedSteady.waiters.slice(6), ...changedSteady.waiters.slice(0,6)];
        servedReading = changedSteady;
        const afterSteady = await evaluate(`(async () => {
          await tick();
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          const row=[...document.querySelectorAll('.wrow.wait')].find(row=>row.querySelector('.name').textContent==='steady waiting 3');
          return {top:row.getBoundingClientRect().top,key:row.dataset.tip,readAt:latest.read_at,tip:document.querySelector('#tip').innerText,
            tipTop:document.querySelector('#tip').style.top,tipLeft:document.querySelector('#tip').style.left,
            focus:document.activeElement.dataset.tip,details:document.querySelector('#swaprows .smaller details').open};
        })()`);
        const steadyResult = {before:beforeSteady,after:afterSteady,delta:afterSteady.top-beforeSteady.top};
        assert.equal(beforeSteady.readAt,steady.read_at);
        assert.equal(afterSteady.readAt,changedSteady.read_at);
        if (comparisonImages) await writeFile(join(folder,'steady-refresh.json'),JSON.stringify(steadyResult,null,2)+'\n');
        assert.ok(Math.abs(steadyResult.delta)<1,'The viewed waiting step moves during refresh: '+JSON.stringify(steadyResult));
        assert.equal(afterSteady.key,beforeSteady.key);
        assert.equal(afterSteady.tip,beforeSteady.tip,'The open hover changes to another step.');
        assert.equal(afterSteady.tipTop,beforeSteady.tipTop);
        assert.equal(afterSteady.tipLeft,beforeSteady.tipLeft);
        assert.equal(afterSteady.focus,beforeSteady.key);
        assert.ok(afterSteady.details,'The open details closes during refresh.');
        // Removing that step clears its hover without borrowing the next step.
        servedReading = {...changedSteady,waiters:changedSteady.waiters.filter(row=>row.key!=='steady-wait-3')};
        assert.equal(await evaluate("(async()=>{await tick();return document.querySelector('#tip').classList.contains('on')})()"), false);
        // A focused row out of view does not anchor the page: the first row in view keeps its place.
        servedReading = steady;
        const outOfView = await evaluate(`(async () => {
          await tick();
          hideTip();
          // The reorder moves step 6 up six places and step 0 down six places.
          const viewed=[...document.querySelectorAll('.wrow.wait')].find(row=>row.querySelector('.name').textContent==='steady waiting 6');
          window.scrollBy(0, viewed.getBoundingClientRect().top - 10);
          const far=document.querySelector('tr[data-tip="wait:steady-wait-0"]');
          far.focus({preventScroll:true});
          const bounds=far.getBoundingClientRect();
          const first=[...document.querySelectorAll('.wrow[data-tip]')].find(row=>{const r=row.getBoundingClientRect();return r.top>=0&&r.bottom>0&&r.top<innerHeight;});
          return {farInView:bounds.bottom>0&&bounds.top<innerHeight, key:first.dataset.tip, top:first.getBoundingClientRect().top};
        })()`);
        assert.ok(!outOfView.farInView && outOfView.key==='wait:steady-wait-6', 'The setup is wrong: '+JSON.stringify(outOfView));
        servedReading = changedSteady;
        const outOfViewTop = await evaluate(`(async () => {
          await tick();
          await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
          return document.querySelector('.wrow[data-tip="${outOfView.key}"]').getBoundingClientRect().top;
        })()`);
        assert.ok(Math.abs(outOfViewTop-outOfView.top)<1, 'A focused row out of view moves the rows in view: '+JSON.stringify({outOfView,outOfViewTop}));
        servedReading = base;
        await navigate('memory_live.html');
        // A reload of the same page keeps its scroll; the pixel checks below read the page from its top.
        await evaluate('scrollTo(0,0)');
        for (const theme of themes) {
          await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
          for (const width of widths) {
            await call('Emulation.setDeviceMetricsOverride', {width,height:1200,deviceScaleFactor:1,mobile:false});
            await show(readings.aws_costs);
            await checkAwsCosts(evaluate, theme === 'dark' && width === 1400);
            await show(base);
            await evaluate(`renderAwsCosts(undefined)`);
            assert.equal(await evaluate(`document.querySelector('#aws-cost-content').textContent`), 'no record');
            // A render keeps the viewed row in place, so the scroll of the last width stays; the pixel checks read from the top.
            await evaluate('scrollTo(0,0)');
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
              const small = swap.find(row => row.classList.contains('owner'))?.querySelector('small');
              // A long detail wraps at phone width, so compare the first text lines, not the boxes.
              const firstLine = element => { const range=document.createRange(); range.selectNodeContents(element); return range.getClientRects()[0]; };
              const gauges = ['some','full'].map(key => {
                const value=document.querySelector('#gv-'+key), limit=document.querySelector('#limit-'+key);
                return {value:rect(value), limit:rect(limit), text:value.textContent, nowrap:getComputedStyle(value).whiteSpace};
              });
              const lower = [...document.querySelectorAll('#ticks span')].filter(e => !e.hidden).map(e=>Number(e.textContent));
              const owner = row.querySelector('.owner');
              const card = document.querySelector('#walk').closest('.card');
              const metrics = {
                headings:[...table.querySelectorAll('thead th')].map(e=>e.textContent),
                runningSlots:[...table.querySelectorAll('tr[data-tip^="row:"] .slot')].map(cell=>cell.textContent),
                queuedSlots:table.querySelectorAll('tr[data-tip^="wait:"] .slot,tr.queue-head .slot').length,
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
                    table:rect(metadata).top-rect(table.querySelector('thead tr')).bottom};
                }),
                // The label column is as wide as the widest Running label, Free row or queued word.
                labelColumn:(() => {
                  const walk=document.querySelector('#walk');
                  const width=(source,text)=>{
                    const probe=source.cloneNode(false);probe.textContent=text;
                    Object.assign(probe.style,{position:'absolute',whiteSpace:'nowrap',width:'auto',visibility:'hidden'});
                    walk.append(probe);const result=probe.getBoundingClientRect().width;probe.remove();return result;
                  };
                  const widths=[...walk.querySelectorAll('.row-label .name, .row-label .owner')].flatMap(label=>
                    label.closest('.wrow.wait') ? label.textContent.split(/\s+/).map(word=>width(label,word)) : [width(label,label.textContent)]);
                  const room=walk.querySelector('.wrow.room .row-heading');
                  widths.push(width(room,room.textContent.replace(/(Free)/,'$1 '))+8);
                  return {column:parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--amt')),
                    needed:Math.max(...widths),cap:12*parseFloat(getComputedStyle(document.documentElement).fontSize)};
                })(),
                stageCut:[...table.querySelectorAll('td.place')].filter(cell=>cell.scrollWidth>cell.clientWidth).map(cell=>cell.textContent),
                roomRow:(() => {
                  const room=table.querySelector('tr.room');
                  return room.getClientRects().length?room.textContent:null;
                })(),
                machine:table.querySelector('td.machine')?.textContent,
                capText:text('#cap-value'), legendCap:text('#v-cap'), room:text('#v-room'),
                roomBar:rect(document.querySelector('#s-room')), roomColor:color('--room'),
                rowLeft:rect(row.querySelector('.row-label')).left,
                headingLeft:rect(card.querySelector('.section-head')).left,
                reservedAmount:getComputedStyle(row.querySelector('.reserved-amount')).color,
                dimColor:color('--ink-2'),
                leftover:text('#leftover-rows'), history:text('#report-charts'), top:text('#report-top'),
                costWindow:text('#cost-window'), pressureReason:text('#pressure-reason'),
                charts:document.querySelectorAll('#history-charts svg').length,
                progress:progress?rect(progress):null, capLine:rect(cap),
                progressColor:progress?getComputedStyle(progress).backgroundColor:null,
                reservedColors:[color('--res-a'),color('--res-b')],
                reservedRatios:['--res-a','--res-b'].map(name=>contrast(color(name),color('--card'))),
                swapRows:swap.length, swapText:swap.map(e=>e.textContent),
                swapBytes:swap.filter(e=>e.dataset.bytes).map(e=>Number(e.dataset.bytes)),
                swapContrast:small?contrast(getComputedStyle(small).color,color('--card')):null,
                swapBaseline:small?firstLine(small).bottom-firstLine(small.parentElement.querySelector('.name')).bottom:0,
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
            assert.deepEqual(proof.headings, runningHeaders);
            assert.ok(proof.runningSlots.length>0 && proof.runningSlots.every(slot=>slot==='—'));
            assert.equal(proof.queuedSlots,0);
            assert.equal(proof.answer,'Free for the next step');
            assert.deepEqual(proof.queueHeaders, queuedHeaders);
            assert.equal(proof.queueHeaderHeight,proof.queueSpacerHeight);
            assert.ok(proof.stages.includes('testing'));
            assert.ok(proof.stages.every(stage => ['building','testing','—'].includes(stage)));
            // The table never moves under the chart; a narrow pane scrolls the step chart sideways.
            assert.equal(proof.layout,'beside','The table moved under the chart at '+width+' px.');
            for(const offsets of proof.rowOffsets)
              assert.ok(Math.abs(offsets.chart-offsets.table)<=1,'The chart and table rows differ. '+JSON.stringify({theme,width,offsets,all:proof.rowOffsets}));
            assert.equal(proof.roomRow,'','The table room row is empty beside the chart.');
            assert.deepEqual(proof.stageCut,[],'A Stage value is wider than its column at '+width+' px.');
            assert.ok(proof.labelColumn.column<=Math.min(proof.labelColumn.needed,proof.labelColumn.cap)+2,
              'The label column is wider than its widest label. '+JSON.stringify(proof.labelColumn));
            assert.equal(proof.machine, 'fixture');
            assert.match(proof.room, /GiB/);
            for (const age of ['45 min','19 h','2 d']) assert.ok(proof.leftover.includes(age), age);
            assert.match(proof.leftover,/pid 46/);
            assert.ok(!/pid (70|80)\b/.test(proof.leftover), 'The leftover list includes a live build or compiler service.');
            assert.ok(!proof.history.includes('Build history'));
            assert.match(proof.costWindow,/^Last 3 days: Nov 4, 2026 – Nov 7, 2026 · America\/New_York/);
            assert.equal(proof.pressureReason,'Pressure is unreadable: /private/proc/pressure/memory');
            assert.equal(proof.charts, 0, 'The removed history chart remains.');
            assert.match(proof.top, /Memory wait/);
            assert.match(proof.top, /Memory wait on fixture: /);
            assert.match(proof.top, /fixture nextest paid 75% of it\./);
            assert.match(proof.top, /Action: lower the memory peak of fixture nextest\./);
            assert.ok(proof.progress && proof.progress.width > 0, 'The progress line is absent.');
            assert.ok(proof.progress.height <= 3, 'The progress line is too thick.');
            assert.ok(proof.progress.right <= proof.capLine.left + 1, 'The progress line crosses the cap.');
            assert.equal(proof.swapRows, base.swap_rows + 6);
            assert.match(proof.swapText[0], /Outside builds and CI/);
            assert.match(proof.swapText[base.swap_rows+1], /25 smaller groups/);
            assert.match(proof.swapText[base.swap_rows+1], /5\.1 GiB/);
            assert.match(proof.swapText.at(-3), /Not attributed7\.0 B/);
            assert.match(proof.swapText.at(-2), /Builds and CI/);
            assert.match(proof.swapText.at(-1), /All swap used7\.8 GiB/);
            assert.equal(proof.swapBytes.slice(0,-1).reduce((sum,bytes)=>sum+bytes,0),proof.swapBytes.at(-1));
            assert.equal(proof.swapBytes.at(-1),base.swap.used_bytes);
            assert.equal(proof.capText,'Queue cap '+proof.legendCap);
            assert.ok(!proof.legacySwapBar, 'The page repeats the swap bar.');
            assert.equal(proof.swapBars, 3);
            for (const ratio of proof.reservedRatios) assert.ok(ratio >= 3, 'The reservation contrast is below 3:1.');
            assert.ok(proof.swapContrast >= 4.5, 'The swap text contrast is below 4.5:1.');
            assert.ok(Math.abs(proof.swapBaseline) <= 1, 'The swap labels use different baselines.');
            assert.ok(proof.lower.every(value => value < base.cap), 'The lower axis reaches the cap.');
            assert.ok(Math.abs(proof.rowLeft-proof.headingLeft)<=1, 'The rows miss the card text edge.');
            assert.equal(proof.reservedAmount,proof.dimColor);
            assert.ok(!proof.owner.includes('handler'), 'The page repeats the unit beside the name.');
            assert.ok(proof.amountHeader.includes('Used · reserved'), 'The memory amounts have no header.');
            assert.ok(proof.scroll <= proof.client, 'The page overflows. '+JSON.stringify({theme,width,scroll:proof.scroll,client:proof.client}));
            const roomLegend=proof.values.find(row=>row.label==='Free');
            assert.ok(Math.abs(roomLegend.labelBox.top-roomLegend.value.top)<2, 'The room value uses a second line.');
            assert.ok(roomLegend.value.left-roomLegend.labelBox.right<80, 'The legend value is too far from its label.');
            for (const gauge of proof.gauges) assert.equal(gauge.nowrap, 'nowrap');
            const checkPixel = async (x,y,color,label) => {
              const expected = color.match(/[0-9.]+/g).slice(0,3).map(Number);
              const actual = await readPixel(x,y);
              assert.ok(actual.every((value,index)=>Math.abs(value-expected[index])<=1),
                label+' '+JSON.stringify({x,y,actual,expected}));
            };
            await checkPixel(proof.progress.left+proof.progress.width/2,
              (proof.progress.top+proof.progress.bottom)/2,proof.progressColor,'The progress pixels differ.');
            // Read the room bar above its label.
            await checkPixel(proof.roomBar.left+5,proof.roomBar.top+4,proof.roomColor,
              'The room pixels differ at '+theme+' '+width+' px.');
            if (comparisonImages) proofs.push({theme,width,metrics:proof});
            const slotReview=structuredClone(base);
            const running=slotReview.walk.find(row=>row.queued&&!row.ci);
            slotReview.walk=[
              {...running,name:'hana nextest',unit:'queue',machine:'natedev',slot:2,tokens:6},
              {...running,name:'tools nextest',unit:'tests',machine:'natedev',pid:51,slot:'—'},
            ];
            slotReview.tokens={kind:'TokenPool',total:31,free:4,memory_floor_bytes:12*2**30,paused:false};
            slotReview.token_counts='';
            slotReview.waiters=[];
            slotReview.ram.build_tools_bytes=0;
            await show(slotReview);
            const slotColumn=await evaluate(`(()=>{
              const table=document.querySelector('.walk-table');
              const rows=[...table.querySelectorAll('tr[data-tip^="row:"]')];
              const box=${comparisonImages} ? table.getBoundingClientRect() : null;
              const bottom=box ? rows.at(-1).getBoundingClientRect().bottom : 0;
              return {slots:rows.map(row=>row.querySelector('.slot').textContent),
                tokens:rows.map(row=>row.querySelector('.tokens').textContent),
                pool:[...document.querySelectorAll('#walk-tokens dt')].map(label=>[label.textContent,label.nextElementSibling.textContent]),
                poolAlignment:[...document.querySelectorAll('#walk-tokens dd')].map(value=>getComputedStyle(value).textAlign),
                lastColumns:rows.map(row=>[...row.children].slice(-4).map(cell=>cell.className)),
                overflow:document.documentElement.scrollWidth>document.documentElement.clientWidth,
                clip:box ? {x:Math.floor(box.left)-4,y:Math.floor(box.top+scrollY)-4,
                  width:Math.ceil(box.width)+8,height:Math.ceil(bottom-box.top)+8,scale:1} : undefined};
            })()`);
            assert.deepEqual(slotColumn.slots,['2','—']);
            assert.deepEqual(slotColumn.tokens,['6','—']);
            assert.deepEqual(slotColumn.pool,[['Total','31'],['Available','4']]);
            assert.deepEqual(slotColumn.poolAlignment,['right','right']);
            assert.deepEqual(slotColumn.lastColumns,[['vcpus','slot','tokens','row-action'],['vcpus','slot','tokens','row-action']]);
            assert.equal(slotColumn.overflow,false);
            if(comparisonImages&&theme==='dark'&&width===1400) {
              await saveScreenshot('slot-column-dark-desktop',{captureBeyondViewport:true,clip:slotColumn.clip});
            }
            for(const state of ['active','paused','unavailable']) {
              const tokenReading=structuredClone(slotReview);
              if(state==='paused') {
                tokenReading.tokens.free=0;
                tokenReading.tokens.paused=true;
                tokenReading.token_counts='steve cannot be asked.';
              }
              if(state==='unavailable') tokenReading.tokens={kind:'TokenPoolUnavailable',reason:'steve exited 1.'};
              await show(tokenReading);
              await evaluate("showTip('tokens',200,200)");
              const tokenView=await evaluate(`(() => {
                const tip=document.querySelector('#tip');
                const box=tip.getBoundingClientRect();
                return {rows:tooltipRows('tokens',latest),grids:tip.querySelectorAll('dl.tip').length,
                  values:[...tip.querySelectorAll('dt + dd')].map(value=>getComputedStyle(value).textAlign),
                  clipped:box.left<0||box.right>innerWidth||box.top<0||box.bottom>innerHeight};
              })()`);
              const fields=Object.fromEntries(tokenView.rows.map(row=>[row.label,row.value]));
              assert.equal(fields.Tokens,state==='unavailable'?'unavailable':'');
              assert.equal(tokenView.grids,1);
              assert.ok(tokenView.values.every(value=>value==='right'));
              assert.equal(tokenView.clipped,false);
              if(state==='unavailable') {
                assert.deepEqual(fields,{Tokens:'unavailable'});
              } else {
                assert.equal(fields.Total,'31');
                assert.equal(fields.Available,state==='paused'?'0':'4');
                assert.equal(fields['Pause below'],'12.0 GiB');
                assert.equal(fields.Pool,state);
                assert.equal(fields['Per-build counts'],state==='paused'?'unavailable':'known');
                if(state==='paused') assert.equal(fields.Because,'steve cannot be asked');
              }
              if (comparisonImages) comparisons.push({tokenTooltip:true,theme,width,state,...tokenView});
              if(comparisonImages&&theme==='dark'&&width===1400) {
                const box=await evaluate("(() => {const r=document.querySelector('#tip').getBoundingClientRect();return {x:Math.floor(r.left)-2,y:Math.floor(r.top+scrollY)-2,width:Math.ceil(r.width)+4,height:Math.ceil(r.height)+4,scale:1};})()");
                await saveScreenshot(`tokens-${state}-dark-desktop`,{captureBeyondViewport:true,clip:box});
              }
            }
            await show(base);

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

            const topBefore = await evaluate("document.querySelector('#top-costs').textContent");
            await show(base);
            assert.equal(await evaluate("document.querySelector('#top-costs').textContent"), topBefore);
            const updatedReport = structuredClone(base);
            updatedReport.report.top[0].action = 'Updated cost action.';
            await show(updatedReport);
            assert.match(await evaluate("document.querySelector('#top-costs').textContent"), /Updated cost action/);
            await show(base);

            // The Running chart keeps its layout when the RAM readings disagree.
            const disagreeReading=structuredClone(readings.cloud_region);
            disagreeReading.ram.agreement='disagree';
            const detailLayouts={};
            for(const [name,stage,location,reading] of [
              ['cloud_stage','tools ready','',readings.cloud_stage],
              ['cloud_region','tests start','eu-south-2, zone a',readings.cloud_region],
              ['cloud_region_disagree','tests start','eu-south-2, zone a',disagreeReading],
              ['cloud_zone','testing','eu-south-2, zone us-east-1b',readings.cloud_zone],
              ['cloud_empty','testing','',readings.cloud_empty]]) {
              await show(reading);
              const detailProof=await evaluate(`(() => {
                const row=[...document.querySelectorAll('.walk-table tbody tr')].find(row=>row.textContent.includes('700001'));
                const chart=document.querySelector('.wrow[data-tip="'+row.dataset.tip+'"]');
                const amounts=chart.querySelector('.amounts');
                const fields=Object.fromEntries(tooltipRows(row.dataset.tip,latest).map(({label,value})=>[label,value]));
                return {stage:row.querySelector('.place').textContent,fields,
                  text:amounts.textContent,font:getComputedStyle(amounts).fontSize,
                  ownerFont:getComputedStyle(chart.querySelector('.owner')).fontSize,
                  rootFont:getComputedStyle(document.documentElement).fontSize,
                  beside:document.querySelector('#walk').classList.contains('beside'),
                  detailRight:amounts.getBoundingClientRect().right,
                  detailWidth:amounts.getBoundingClientRect().width,
                  tableLeft:document.querySelector('.walk-table').getBoundingClientRect().left,
                  barHidden:document.querySelector('#barwrap').hidden,
                  bars:[...document.querySelectorAll('#walk .track .b')].map(bar=>{
                    const rect=bar.getBoundingClientRect();return [Math.round(rect.left),Math.round(rect.width)];}),
                  chartHeight:chart.getBoundingClientRect().height,rowHeight:row.getBoundingClientRect().height,
                  scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
              })()`);
              assert.equal(detailProof.stage,stage,'The AWS Stage differs.');
              assert.equal(detailProof.text,'1 failed module · no update for 2 min','The AWS detail text differs.');
              assert.equal(detailProof.fields.Where,location || undefined,'The AWS tooltip location differs.');
              const stageLabel=stage[0].toUpperCase()+stage.slice(1);
              assert.equal(detailProof.fields[stageLabel],'7 of 12 modules','The AWS tooltip module count differs.');
              assert.equal(detailProof.font,detailProof.ownerFont,'The AWS detail font differs from the owner font.');
              assert.equal(parseFloat(detailProof.font),parseFloat(detailProof.rootFont)*0.75);
              if(detailProof.beside) assert.ok(detailProof.detailRight<detailProof.tableLeft,
                'The AWS detail text crosses into the table.');
              assert.ok(Math.abs(detailProof.chartHeight-detailProof.rowHeight)<1,'The AWS row heights differ.');
              assert.ok(detailProof.scroll<=detailProof.client,'The AWS detail text overflows the page.');
              detailLayouts[name]=detailProof;
              if(name==='cloud_region_disagree') {
                const agree=detailLayouts.cloud_region;
                assert.ok(!detailProof.barHidden && !agree.barHidden,'The disagree reading hides the memory bar.');
                for(const key of ['beside','tableLeft','detailWidth','chartHeight'])
                  assert.equal(detailProof[key],agree[key],'The disagree note changes the Running chart '+key+'.');
                assert.deepEqual(detailProof.bars,agree.bars,'The disagree note changes the Running chart bars.');
              }
              if(name.startsWith('cloud_region') && requestedShot) {
                const clip=await evaluate(`(() => {
                  const r=document.querySelector('#walk').closest('.card').getBoundingClientRect();
                  return {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height,scale:1};
                })()`);
                await saveScreenshot(`${theme}-${width}-${name==='cloud_region'?'aws':'aws-disagree'}`,{captureBeyondViewport:true,clip});
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
                stage:running?.querySelector('.place')?.textContent,
                moduleCount:tooltipRows(running.dataset.tip,latest).find(row=>row.label==='Testing')?.value,
                machine:running?.querySelector('.machine')?.textContent,
                place:waiting?.querySelector('.place')?.textContent,
                waitingMachine:waiting?.querySelector('.machine')?.textContent,
                localWaiting:(() => {const index=latest.waiters.findIndex(row=>row.kind!=='cloud');
                  return index<0?null:{shown:table.querySelector('tr[data-tip="'+proofTip('wait',index)+'"] .machine')?.textContent,
                    targets:(latest.waiters[index].targets||[]).join(' or ')};})(),
                beside:document.querySelector('#walk').classList.contains('beside'),
                statusHeight:runningDetails?.querySelector('.amounts')?.getBoundingClientRect().height,
                statusLine:parseFloat(getComputedStyle(runningDetails?.querySelector('.amounts')||document.body).lineHeight),
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
                slots:tipText(proofTip('wait',latest.waiters.findIndex(row=>row.kind==='cloud')),latest).join(' '),
                scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
            })()`);
            assert.deepEqual(cloudProof.headings, runningHeaders);
            assert.ok(cloudProof.statusInTable,'Cloud work is absent from the main table.');
            assert.equal(cloudProof.machine,'aws');
            assert.equal(cloudProof.vcpus,'32');
            assert.ok(!/\brunning\b/.test(cloudProof.running),'The cloud row repeats the running state.');
            assert.equal(cloudProof.stage,'testing');
            assert.equal(cloudProof.amounts,`${failed} · no update for ${lateAge}`,'The AWS detail text differs.');
            assert.equal(cloudProof.moduleCount,modules,'The AWS tooltip module count differs.');
            assert.ok(cloudProof.progressInTrack,'The cloud progress line is absent from the track.');
            assert.ok(Math.abs(cloudProof.progressWidth-cloudProof.progressTrackWidth*cloudRow.progress.percent/100)<1,
              'The cloud progress width differs from its percent.');
            assert.equal(cloudProof.trackText,'','The cloud track contains text.');
            assert.ok(!cloudProof.waitingBar,'The waiting cloud row has a progress line.');
            assert.ok(Math.abs(cloudProof.height-cloudProof.tableHeight)<1,'The cloud row heights differ.');
            assert.ok(cloudProof.tipsMatch,'The cloud hover keys differ from the local row keys.');
            assert.equal(cloudProof.place,'cloud');
            assert.equal(cloudProof.waitingMachine,'aws');
            // The user, 11-15: a queued row names the machines that can take it in the Target column.
            assert.ok(cloudProof.localWaiting && cloudProof.localWaiting.targets,'The fixture has no local waiting row with targets.');
            assert.equal(cloudProof.localWaiting.shown,cloudProof.localWaiting.targets);
            // The user, 11-15: a cloud status wraps one letter to a line when its room before the cap is narrow.
            // A stacked chart gives the status the full card width.
            if(cloudProof.beside) assert.ok(cloudProof.statusHeight<=2*cloudProof.statusLine+1,'The cloud status takes more than two lines: '+cloudProof.statusHeight+' at '+width+' px.');
            assert.equal(cloudProof.waitingVcpus,'');
            assert.match(cloudProof.waiting,/waits for a cloud slot: 1 of 1 in use/);
            assert.match(cloudProof.slots,/Runs on  AWS.*Because  natedev when AWS cannot start/s);
            assert.ok(!cloudProof.cloudCard,'The page retains a separate Cloud card.');
            assert.ok(!cloudProof.cloudMemory,'Cloud work has a memory reservation.');
            assert.ok(cloudProof.localMemory,'The local memory bar is absent.');
            assert.equal(cloudProof.staleColor,cloudProof.errorColor,'The stale age is not red.');
            assert.ok(cloudProof.scroll<=cloudProof.client,'Cloud rows overflow the page.');
            if(requestedShot && theme==='light' && width===1400 && name==='cloud') {
              const clip=await evaluate(`(() => {
                const r=document.querySelector('#walk').closest('.card').getBoundingClientRect();
                return {x:r.left+scrollX,y:r.top+scrollY,width:r.width,height:r.height,scale:1};
              })()`);
              const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip});
              await mkdir(dirname(requestedShot),{recursive:true});
              await writeFile(requestedShot,Buffer.from(shot.data,'base64'));
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
                assert.match(alert.text,/^MAC BLOCKED by research since \d\d:\d\d \(P15 F11 L0 run\): 7 steps wait for the Mac, idle 10 min;/);
                assert.equal(alert.color,alert.errorColor,'The Mac idle alert is not red.');
                assert.ok(alert.height>0 && alert.bottom<=alert.titleTop,'The alert is not above the title.');
                assert.ok(alert.scroll<=alert.client,'The Mac idle alert overflows the page.');
              } else assert.equal(alert.height,0,'The clear alert keeps an empty line.');
              proof[name+'Alert']=alert;
            }
            const signedLegend=structuredClone(base), legendGib=1024**3;
            Object.assign(signedLegend.ram,{desktop_bytes:2*legendGib,agents_bytes:3*legendGib,
              other_programs_bytes:legendGib,programs:[],smaller_programs_bytes:legendGib,
              kernel_bytes:2*legendGib,compressed_swap_bytes:0,kernel_structures_bytes:2*legendGib,
              kernel_counters_bytes:0,kernel_no_counter_bytes:0,cache_bytes:2*legendGib,unattributed_bytes:0});
            await show(signedLegend);
            const unsignedWidths=await evaluate(`RAM_BLOCKS.map(([key])=>document.querySelector('#s-'+key).style.width)`);
            signedLegend.ram.unattributed_bytes=-legendGib;
            await show(signedLegend);
            const signedProof=await evaluate(`({
              total:document.querySelector('#v-g-else').textContent,
              unattributed:document.querySelector('#v-unattributed').textContent,
              tip:tipText('g-else',latest)[0],
              widths:RAM_BLOCKS.map(([key])=>document.querySelector('#s-'+key).style.width)
            })`);
            assert.equal(signedProof.total,'9.0 GiB','The legend total omits the negative reading.');
            assert.equal(signedProof.unattributed,'-1.0 GiB');
            assert.equal(signedProof.tip,'Everything else  9.0 GiB');
            assert.deepEqual(signedProof.widths,unsignedWidths,'The negative reading changes bar widths.');
            proof.signedLegend=signedProof;
            await show({...base, ram:{...base.ram, agreement:'disagree'}});
            const disagree=await evaluate(`(() => {
              const note=document.querySelector('#ram-disagree');
              return {note:note.hidden?'':note.textContent,bar:document.querySelector('#barwrap').getBoundingClientRect().height,
                scale:document.querySelector('#scale').hidden};
            })()`);
            assert.equal(disagree.note,'RAM readings do not agree: the named parts exceed the total.');
            assert.ok(disagree.bar>0,'A disagreement hides the memory bar.');
            assert.equal(disagree.scale,false,'A disagreement hides the scale.');
            proof.disagree=disagree;
            // Build tools and hana CI sit inside the queue cap: more of them leaves less free, and the cap stays.
            const gib=1024**3, capReading=structuredClone(base), known=value=>typeof value==='number'?value:0;
            Object.assign(capReading.ram,{build_tools_bytes:capReading.ram.build_tools_bytes+gib,
              sccache_bytes:capReading.ram.sccache_bytes+gib,ci_bytes:known(capReading.ram.ci_bytes)+gib/2,
              available_bytes:capReading.ram.available_bytes-3*gib/2,
              room_bytes:capReading.ram.room_bytes-3*gib/2});
            capReading.room-=1.5;
            capReading.taken=capReading.cap-capReading.room;
            await show(capReading);
            const capLine=await evaluate(`(() => {
              const line=document.querySelector('#walk .walk-bud');
              const track=document.querySelector('#walk .wrow[data-run] .track').getBoundingClientRect();
              const room=document.querySelector('#walk .wrow.room .roomb')?.getBoundingClientRect();
              const tools=document.querySelector('#walk .wrow.tools .seg-tools').getBoundingClientRect();
              const memoryTools=document.querySelector('#s-tools').getBoundingClientRect();
              const memoryBar=document.querySelector('#barwrap .bar').getBoundingClientRect();
              const memoryCap=document.querySelector('#b-cap').getBoundingClientRect();
              const box=line?.getBoundingClientRect();
              const card=${comparisonImages} ? document.querySelector('#walk').closest('.card').getBoundingClientRect() : null;
              // The line stops at chart text, so the pixel check reads the longest part of it.
              const drawn=[...(line?.querySelectorAll('.cap-part')||[])];
              const part=(drawn.length?drawn:line?[line]:[]).map(element=>({element,box:element.getBoundingClientRect()}))
                .sort((a,b)=>b.box.height-a.box.height)[0];
              return {layout:document.querySelector('#walk').classList.contains('beside')?'beside':'stacked',present:!!line&&getComputedStyle(line).display!=='none'&&box.width>0&&box.height>0,
                left:box?box.left+1:null,top:part?.box.top,bottom:part?.box.bottom,roomRight:room?room.right:null,
                tools:{left:tools.left,right:tools.right,width:tools.width,trackLeft:track.left},
                memoryTools:{left:memoryTools.left,right:memoryTools.right,width:memoryTools.width,
                  barLeft:memoryBar.left,capRight:memoryCap.right},
                expected:track.left+track.width*latest.cap*${gib}/latest.ram.total_bytes,color:part?getComputedStyle(part.element).borderLeftColor:'',
                scrollY,card:card ? {x:Math.floor(card.left),y:Math.floor(card.top+scrollY),width:Math.ceil(card.width)+1,height:Math.ceil(card.height)+1} : undefined};
            })()`);
            assert.ok(capLine.present,'The Running chart has no cap line.');
            assert.ok(capLine.tools.width>0 && capLine.memoryTools.width>0);
            assert.ok(Math.abs(capLine.tools.left-capLine.tools.trackLeft)<=1,'Build tools do not start at the chart left edge.');
            assert.ok(capLine.tools.right<capLine.left,'Build tools extend beyond the chart cap mark.');
            assert.ok(Math.abs(capLine.memoryTools.left-capLine.memoryTools.barLeft)<=1,'Build tools do not start at the memory bar left edge.');
            assert.ok(capLine.memoryTools.right<capLine.memoryTools.capRight,'Build tools extend beyond the memory bar cap mark.');
            if(width===1400) assert.equal(capLine.layout,'beside','The Running chart stays stacked after a narrow page.');
            assert.ok(Math.abs(capLine.left-capLine.expected)<=1.5,'The cap line is off the cap value. '+JSON.stringify(capLine));
            assert.ok(Math.abs(capLine.roomRight-capLine.expected)<=1.5,'The room bar does not end at the cap. '+JSON.stringify(capLine));
            await checkPixel(capLine.left,(capLine.top+capLine.bottom)/2+capLine.scrollY,
              capLine.color,'The cap line pixels differ.');
            if (comparisonImages && width===1400) {
              await saveScreenshot(`${theme}-${width}-cap-line`,{captureBeyondViewport:true,clip:{...capLine.card,scale:1}});
            }
            if(comparisonImages&&theme==='dark'&&width===1400) {
              const clip=await evaluate(`(()=>{
                const memory=document.querySelector('#barwrap').getBoundingClientRect();
                const running=document.querySelector('#walk').closest('.card').getBoundingClientRect();
                return {x:running.left,y:memory.top+scrollY,width:running.width,height:running.bottom-memory.top,scale:1};
              })()`);
              await saveScreenshot('build-tools-inside-cap-dark-desktop',{captureBeyondViewport:true,clip});
              const legendClip=await evaluate(`(()=>{
                const box=document.querySelector('.legend-box .legend').getBoundingClientRect();
                return {x:Math.floor(box.left)-4,y:Math.floor(box.top+scrollY)-4,
                  width:Math.ceil(box.width)+8,height:Math.ceil(box.height)+8,scale:1};
              })()`);
              await saveScreenshot('legend-totals-dark-desktop',{captureBeyondViewport:true,clip:legendClip});
              // The user, 11-16: the legend starts collapsed under the memory bar.
              const closedClip=await evaluate(`(()=>{
                const legend=document.querySelector('.legend-box');
                legend.open=false;
                const card=legend.closest('.card').getBoundingClientRect();
                return {x:card.left,y:card.top+scrollY,width:card.width,height:card.height,scale:1};
              })()`);
              await saveScreenshot('legend-closed-dark-desktop',{captureBeyondViewport:true,clip:closedClip});
              await evaluate(`document.querySelector('.legend-box').open=true`);
            }
            proof.capLine=capLine;
            // Bars that add to more than the cap stop at the cap line; a cut bar ends in the cap color.
            const overReading=structuredClone(base), over=overReading.room+1;
            Object.assign(overReading.ram,{build_tools_bytes:overReading.ram.build_tools_bytes+over*gib,
              available_bytes:overReading.ram.available_bytes-over*gib,room_bytes:0});
            overReading.room=0;
            overReading.taken=overReading.cap+1;
            // A waiting step that needs more than the cap also stops at the cap line.
            overReading.waiters.find(row=>row.kind!=='cloud').need=overReading.cap+10;
            await show(overReading,true,false);
            const overCap=await evaluate(`(() => {
              const edge=document.querySelector('#walk .walk-bud').getBoundingClientRect().left+1;
              const bars=[...document.querySelectorAll('#walk .wrow .track .b:not(.row-progress):not(.seg-remote)')]
                .map(bar=>({cls:bar.className,right:bar.getBoundingClientRect().right}));
              const cut=document.querySelector('#walk .track .b.over-cap');
              return {edge,bars,cut:cut?getComputedStyle(cut).boxShadow:'',
                cap:getComputedStyle(document.querySelector('#walk .walk-bud .cap-part')||document.querySelector('#walk .walk-bud')).borderLeftColor};
            })()`);
            assert.ok(overCap.bars.length>0,'The over-cap chart draws no bars.');
            assert.ok(overCap.bars.some(bar=>bar.cls.includes('waitb')),'The over-cap chart draws no wait bar.');
            for(const bar of overCap.bars) assert.ok(bar.right<=overCap.edge+0.5,'A bar runs past the cap line. '+JSON.stringify({bar,edge:overCap.edge}));
            assert.ok(overCap.cut.includes(overCap.cap),'A bar cut at the cap has no cap-colored end. '+JSON.stringify(overCap));
            proof.overCap=overCap;
            // Build reserves that do not fit under the cap: the Running header names the amount over the cap.
            const shortReading=structuredClone(base);
            // The running build reserves all of free and 4 GiB more, so its reserve fills the cap.
            const shortRun=shortReading.walk.find(row=>row.kind!=='cloud'&&!row.ci&&typeof row.reserved==='number');
            shortRun.reserved+=shortReading.room+4;
            if(typeof shortReading.reserved==='number') shortReading.reserved+=shortReading.room+4;
            Object.assign(shortReading.ram,{build_covered_bytes:shortReading.ram.build_covered_bytes+shortReading.ram.room_bytes,room_bytes:0});
            Object.assign(shortReading.ram,{build_target_bytes:shortReading.ram.build_covered_bytes+4*gib,build_missing_bytes:4*gib,shortfall_bytes:4*gib});
            Object.assign(shortReading,{room:0,taken:shortReading.cap,shortfall:4});
            delete shortReading.build_reserve;
            await show(shortReading);
            if(comparisonImages&&theme==='dark'&&width===1400) {
              const shortClip=await evaluate(`(()=>{
                const alert=document.getElementById('mac-idle-alert').getBoundingClientRect();
                const running=document.querySelector('#walk').closest('.card').getBoundingClientRect();
                return {x:running.left,y:alert.top+scrollY,width:running.width,height:running.bottom-alert.top,scale:1};
              })()`);
              await saveScreenshot('over-cap-dark-desktop',{captureBeyondViewport:true,clip:shortClip});
            }
            // The user, 11-17: a short reserve has a red outline, one alert line names it, and its tooltip names the missing part.
            const overBar=await evaluate(`(() => {
              const outline=id=>({short:document.getElementById(id).classList.contains('short-reserve'),
                shadow:getComputedStyle(document.getElementById(id)).boxShadow});
              const probe=document.createElement('i');
              probe.style.color='var(--error)';
              document.body.append(probe);
              const errorColor=getComputedStyle(probe).color;
              probe.remove();
              const alert=document.getElementById('mac-idle-alert');
              return {reserved:outline('s-reserved'),safety:outline('s-safety'),swap:outline('s-swap'),errorColor,
                alert:alert.hidden?'':alert.textContent,old:['b-overlay','b-over','bc-reserved'].filter(id=>document.getElementById(id)),
                tip:Object.fromEntries(tooltipRows('reserved',latest).map(row=>[row.label,row.value]))};
            })()`);
            assert.equal(overBar.reserved.short,true,'Build reserved has no red outline when it is short.');
            assert.ok(overBar.reserved.shadow.includes(overBar.errorColor),'The short outline is not the error color. '+JSON.stringify(overBar));
            assert.equal(overBar.safety.short,false);
            assert.equal(overBar.swap.short,false);
            assert.deepEqual(overBar.old,[],'The old over-cap drawing is still on the page.');
            assert.match(overBar.alert,/Free RAM is short by 4\.0 GiB\. Build reserved holds [0-9.]+ of [0-9.]+ GiB\./);
            assert.equal(overBar.tip['Not covered'],'4.0 GiB');
            // A small hana CI part stays visible, so the builds parts add up on screen.
            assert.equal(await evaluate(`getComputedStyle(document.getElementById('s-ci')).minWidth`),'3px');
            proof.overBar=overBar;
            const overHeader=await evaluate(`(() => ({sum:document.querySelector('#walk-sum').textContent,
              sumColor:getComputedStyle(document.querySelector('#walk-sum')).color,hover:tipText('walksum',latest)[1],rows:tooltipRows('walksum',latest),
              cap:getComputedStyle(document.querySelector('#walk .walk-bud .cap-part')||document.querySelector('#walk .walk-bud')).borderLeftColor}))()`);
            // Taken counts the full reserve, so it reads 4 GiB more than the cap.
            assert.match(overHeader.sum,/ · 4\.0 GiB over cap$/);
            assert.equal(overHeader.sumColor,overHeader.cap,'The over-cap total is not in the cap color.');
            const fields=Object.fromEntries(overHeader.rows.map(row=>[row.label,row.value]));
            assert.equal(overHeader.sum,`${fields.Taken.split(' ')[0]} of ${fields['Queue cap']} taken · 4.0 GiB over cap`);
            assert.equal(fields['Over cap'],'4.0 GiB');
            assert.equal(fields['+ Full reserve'],await evaluate("ramAmount(shownReading(latest).ram.build_target_bytes)"));
            assert.ok(!('+ Build reserved' in fields),overHeader.hover);
            // Taken includes the full reserve. The difference from the cap is the uncovered reserve.
            const number=(text,pattern)=>Number.parseFloat(text.match(pattern)[1]);
            const header={taken:number(overHeader.sum,/^([0-9.]+) of/),cap:number(overHeader.sum,/of ([0-9.]+) GiB/),over:number(overHeader.sum,/· ([0-9.]+) GiB over cap/)};
            const tip={builds:parseFloat(fields.Builds),reserved:parseFloat(fields['+ Full reserve']),
              taken:parseFloat(fields.Taken),over:parseFloat(fields['Over cap']),cap:parseFloat(fields['Queue cap'])};
            assert.ok(Math.abs(tip.builds+tip.reserved-tip.taken)<=0.15,'Builds + Build reserved is not Taken. '+JSON.stringify(tip));
            assert.ok(Math.abs(tip.taken-tip.cap-tip.over)<=0.15,'Taken - Queue cap is not the amount over the cap. '+JSON.stringify(tip));
            assert.deepEqual([header.taken,header.cap,header.over],[tip.taken,tip.cap,tip.over],'The header and its tooltip differ.');
            const plainHeader=await (async()=>{await show(base);return evaluate(`document.querySelector('#walk-sum').textContent`);})();
            assert.ok(!plainHeader.includes('over cap'),'The header names an over-cap amount when nothing is over the cap.');
            assert.equal(await evaluate(`document.querySelectorAll('.short-reserve').length`),0,'A reserve has a red outline when nothing is short.');
            proof.overHeader=overHeader;
            // Cloud status text and bar labels are as wide as on the live page; none of it may show on the cap line.
            const cloudReading=structuredClone(base);
            cloudReading.walk.push({kind:'cloud',name:'cloud merge',showrunner:'fixture-owner',unit:'cloud',pid:31,detail:'cloud',
              machine:'aws',vcpus:32,state:'setup',status:'setup · boot · us-east-2, zone c',
              progress:{kind:'NoProgress',reason:'no current counter'},stale:false,late_age:'',started_at:base.read_at-60});
            cloudReading.waiters.push({kind:'cloud',name:'cloud wait',showrunner:'fixture-owner',unit:'cloud',pid:32,detail:'cloud',
              machine:'aws',status:'waits for a cloud slot: 9 of 9 in use',progress:{kind:'NoProgress',reason:'waiting'},since:base.read_at-60});
            await show(cloudReading);
            const capCrossings=await evaluate(`(() => { const rect = element => element.getBoundingClientRect();
              return (() => {
                  // Chart text that shows on a part of the cap line.
                  const line=document.querySelector('.walk-chart .walk-bud');
                  // A page without line parts draws the whole line on its own box.
                  const drawn=[...line.querySelectorAll('.cap-part')];
                  const parts=(drawn.length?drawn:[line]).filter(part=>parseFloat(getComputedStyle(part).borderLeftWidth)>0&&
                    getComputedStyle(part).borderLeftColor!=='rgba(0, 0, 0, 0)').map(rect);
                  const walker=document.createTreeWalker(line.parentElement,NodeFilter.SHOW_TEXT);
                  const crossings=[];
                  for(let node=walker.nextNode();node;node=walker.nextNode()){
                    if(!node.textContent.trim()) continue;
                    let right=Infinity;
                    for(let box=node.parentElement;box!==line.parentElement;box=box.parentElement)
                      if(getComputedStyle(box).overflowX!=='visible') right=Math.min(right,rect(box).right);
                    const range=document.createRange(); range.selectNodeContents(node);
                    for(const text of range.getClientRects())
                      if(text.width&&parts.some(part=>text.left<part.right&&Math.min(text.right,right)>part.left&&
                        text.top<part.bottom&&text.bottom>part.top)) crossings.push(node.textContent.trim());
                  }
                  return {parts:parts.length,crossings};
                })(); })()`);
            assert.ok(capCrossings.parts>0,'The cap line shows.');
            assert.deepEqual(capCrossings.crossings,[],'Text shows on the cap line at '+width+' px, '+theme+'.');
            proof.capCrossings=capCrossings;
            await show(capReading);
            // The user, 11-15: the queue cap brace runs from 0 to the end of free, under the bar.
            const barMark=await evaluate(`(() => {
              const cap=document.querySelector('#barwrap #b-cap'), free=document.querySelector('#barwrap #b-room');
              const bar=document.querySelector('#barwrap .bar').getBoundingClientRect(), box=cap?.getBoundingClientRect();
              const card=${comparisonImages} ? document.querySelector('#barwrap').closest('.card').getBoundingClientRect() : null;
              return {present:!!cap&&!cap.hidden&&getComputedStyle(cap).display!=='none'&&box.height>0,
                label:cap?cap.querySelector('.brace-label').textContent:'',left:box?.left,right:box?.right,
                freeRight:free?.getBoundingClientRect().right,barLeft:bar.left,below:box?box.top>=bar.bottom-1:false,
                expected:bar.left+bar.width*latest.cap*${gib}/latest.ram.total_bytes,bottom:box?.bottom,
                color:cap?getComputedStyle(cap).borderBottomColor:'',scrollY,
                card:card ? {x:Math.floor(card.left),y:Math.floor(card.top+scrollY),width:Math.ceil(card.width)+1,height:Math.ceil(card.height)+1} : undefined};
            })()`);
            assert.ok(barMark.present,'The memory bar has no queue cap brace.');
            assert.match(barMark.label,new RegExp(capReading.cap.toFixed(1).replace('.','\\.')+'$'));
            assert.ok(barMark.below,'The queue cap brace is not under the bar.');
            assert.ok(Math.abs(barMark.left-barMark.barLeft)<=1.5,'The queue cap does not start at 0. '+JSON.stringify(barMark));
            assert.ok(Math.abs(barMark.right-barMark.expected)<=1.5,'The queue cap brace is off the cap value. '+JSON.stringify(barMark));
            assert.ok(Math.abs(barMark.freeRight-barMark.expected)<=1.5,'Free does not end at the queue cap. '+JSON.stringify(barMark));
            await checkPixel((barMark.left+barMark.right)/2,barMark.bottom-1+barMark.scrollY,
              barMark.color,'The queue cap brace pixels differ.');
            if (comparisonImages && width===1400) {
              await saveScreenshot(`${theme}-${width}-bar-cap`,{captureBeyondViewport:true,clip:{...barMark.card,scale:1}});
            }
            proof.barMark=barMark;
            for(const [name,expected] of [['priority',"queue priority: research (the user's bump)"],
              ['priority_unmatched',''],['good','']]) {
              await show(readings[name]);
              const priority=await evaluate(`(() => {
                const index=latest.walk.findIndex(row=>row.queued);
                return tipText(proofTip('row',index),latest).join(' ');
              })()`);
              assert.equal(priority.includes("Priority  "),!!expected);
              if(expected) assert.ok(priority.includes("Priority  research"));
              const heading=await evaluate("tipText('walkhead',latest).join(' ')");
              const unmatched=name==='priority_unmatched';
              assert.equal(heading.includes("Priority  "),unmatched);
              if(unmatched) assert.equal(heading.split("Priority  research").length-1,1);
              if(unmatched && theme==='dark' && width===1400) {
                const clip=await evaluate(`(() => {
                  const heading=document.querySelector('[data-tip="walkhead"]');
                  heading.scrollIntoView({block:'start'});
                  const h=heading.getBoundingClientRect();
                  heading.dispatchEvent(new MouseEvent('click',{bubbles:true,clientX:h.left+10,clientY:h.bottom}));
                  const tip=document.querySelector('#tip');
                  if(!tip.classList.contains('on') || !tip.textContent.includes('research'))
                    throw new Error('The heading click loses the unmatched priority.');
                  if (!${comparisonImages}) return;
                  const r=heading.closest('.card').getBoundingClientRect();
                  return {x:0,y:r.top+scrollY,width:document.documentElement.clientWidth,height:r.height,scale:1};
                })()`);
                await saveScreenshot('unmatched-priority-1400',{captureBeyondViewport:true,clip});
                await evaluate("hideTip();window.scrollTo(0,0)");
              }
            }
            await show(base);

            for (const state of ['idle','waiting','unmatched','busy']) {
              const ciReading=structuredClone(base);
              ciReading.walk.push({name:'hana CI',ci:true,admitted:state==='busy',queued:false,used:0.125,reserved:0,
                counted:0,started_at:fixtureTime-60,unit:'hana-ci.slice',machine:'fixture',pid:701,vcpus:4,
                progress:{kind:'NoProgress',reason:'no admission counter'},ci_runners:[{state:state==='waiting'||state==='unmatched'?'busy':state}]});
              ciReading.ci_ram=0.125;
              ciReading.ram.ci_bytes=known(ciReading.ram.ci_bytes)+gib/8;
              ciReading.ram.available_bytes-=gib/8;
              ciReading.ram.room_bytes-=gib/8;
              ciReading.room-=0.125;
              ciReading.taken=ciReading.cap-ciReading.room;
              await show(ciReading);
              const ciProof=await evaluate(`(() => {
                const key=proofTip('row',latest.walk.length-1);
                const table=document.querySelector('.walk-table tr[data-tip="'+key+'"]');
                const chart=document.querySelector('.wrow[data-tip="'+key+'"]');
                return {table:!!table,chart:!!chart,stage:table?.querySelector('.place').textContent,
                  taken:document.querySelector('#walk-sum').textContent,ci:latest.ci_ram,
                  builds:document.querySelector('#v-g-builds').textContent,
                  reserved:document.querySelector('#v-reserved').textContent,cap:document.querySelector('#v-cap').textContent};
              })()`);
              assert.equal(ciProof.table,state==='busy');
              assert.equal(ciProof.chart,state==='busy');
              if(state==='busy') assert.equal(ciProof.stage,'—');
              assert.equal(ciProof.ci,0.125);
              const takenTenths=memoryTenths(ciProof.builds)+memoryTenths(ciProof.reserved);
              assert.equal(ciProof.taken,(takenTenths/10).toFixed(1)+' of '+ciProof.cap.replace(' GiB','')+' GiB taken');
            }
            await show(base);

            await show(readings.mac_three);
            const macProof=await evaluate(`(() => {
              const rows=latest.walk.map((row,index)=>({row,index})).filter(({row})=>row.kind==='mac-build');
              const unit=document.querySelector('#barwrap').getBoundingClientRect().width/latest.total;
              return rows.map(({row,index})=>{
                const chart=document.querySelector('.wrow[data-run="'+proofRun(index)+'"]');
                const table=document.querySelector('.walk-table tr[data-tip="'+proofTip('row',index)+'"]');
                return {name:chart.querySelector('.name').textContent,stage:table.querySelector('.place').textContent,
                  unit:tooltipRows(proofTip('row',index),latest)[0].labelStaffName ?? null,
                  percent:table.querySelector('.done').textContent,
                  underBar:!!chart.querySelector('.progress-amount'),
                  used:chart.querySelector('.seg-remote').getBoundingClientRect().width,
                  color:getComputedStyle(chart.querySelector('.seg-remote')).backgroundColor,
                  expected:row.used*unit,hatched:!!chart.querySelector('.hatch'),
                  tip:tooltipHtml(proofTip('row',index),latest),
                  chartTop:chart.getBoundingClientRect().top-document.querySelector('.walk-chart-head').getBoundingClientRect().bottom,
                  tableTop:table.getBoundingClientRect().top-document.querySelector('.walk-table thead tr').getBoundingClientRect().bottom};
              });
            })()`);
            assert.equal(macProof.length,3);
            assert.deepEqual(macProof.map(row=>row.name),['hana build','hana nextest','cargo-liner nextest']);
            assert.deepEqual(macProof.map(row=>row.percent),['60.4%','64.7%','98.3%']);
            assert.deepEqual(macProof.map(row=>row.unit),[null,'trunk',null]);
            const macLabels=await evaluate(`latest.walk.map((row,index)=>({row,index})).filter(({row})=>row.kind==='mac-build').map(({index})=>({
              repository:document.querySelector('.wrow[data-run="'+proofRun(index)+'"] .owner').textContent,
              jobs:document.querySelector('tr[data-tip="'+proofTip('row',index)+'"] .vcpus').textContent}))`);
            assert.deepEqual(macLabels.map(row=>row.repository),['','hana','']);
            assert.deepEqual(macLabels.map(row=>row.jobs),['—','12','6']);
            for(const row of macProof) {
              assert.equal(row.stage,'building');
              assert.ok(Math.abs(row.used-row.expected)<1);
              assert.equal(row.hatched,false);
              assert.equal(row.underBar,false);
              assert.ok(Math.abs(row.chartTop-row.tableTop)<1);
              assert.ok(/<dt>In queue cap<\/dt><dd>no<\/dd>/.test(row.tip));
            }
            await show(readings.mac_failed);
            const failedMac=await evaluate(`({rows:document.querySelectorAll('.wrow[data-run]').length,
              text:document.querySelector('#walk').textContent,
              unknown:document.querySelector('.walk-table tr[data-tip="'+proofTip('row',latest.walk.findIndex(row=>row.kind==='mac-unavailable'))+'"] .place').textContent})`);
            assert.ok(failedMac.text.includes('Mac reading is old'));
            assert.ok(!failedMac.text.includes('private raw error'));
            assert.ok(!failedMac.text.includes('Progress unavailable: Mac process'));
            assert.equal(failedMac.unknown,'—');
            await show(base);

            const positions=()=>evaluate(`(() => {
              const row=document.querySelector('.walk-table tr[data-tip^="row:"]');
              return ['.duration','.pid','.machine'].map(selector=>{
                const r=row.querySelector(selector).getBoundingClientRect(); return {left:r.left,width:r.width};
              });
            })()`);
            const withoutWait=structuredClone(base);
            withoutWait.waiters=[];
            await show(withoutWait);
            const beforeWait=await positions();
            const waiting=structuredClone(base);
            waiting.waiters=structuredClone(readings.spacing.waiters);
            waiting.waiters[0].detail='handler';waiting.waiters[0].pid=51;
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
            delete absentRun.caller;
            await show(absent);
            const absentCells=await evaluate(`(() => {
              const index=latest.walk.findIndex(row=>row.queued);
              const chart=document.querySelector('.wrow[data-run="'+proofRun(index)+'"]');
              const table=document.querySelector('.walk-table tr[data-tip="'+proofTip('row',index)+'"]');
              return {name:chart.querySelector('.name').textContent,
                colored:!!chart.querySelector('.name > .staff-name'),owner:chart.querySelector('.owner').textContent,
                machine:table.querySelector('.machine').textContent};
            })()`);
            assert.deepEqual(absentCells,{name:absentRun.name,colored:false,owner:'',machine:'—'});

            for (const [name, expected] of [['pressure','memory'],['spacing','its turn'],['head','its turn']]) {
              await show(readings[name]);
              const state=await evaluate(`({list:document.querySelector('#priority-line,#head-line,#wait-reasons'),
                hover:tipText(proofTip('wait', 0),latest)[1], rows:document.querySelectorAll('.wrow[data-run]').length})`);
              assert.equal(state.list,null,'The removed list remains.');
              assert.ok(state.hover.includes(expected), name+' has no hover reason.');
              assert.ok(state.rows > 0);
              if(name==='spacing'&&theme==='dark'&&width===1400) {
                const clip=await evaluate(`(() => {
                  const row=document.querySelector('.walk-table tr[data-tip="' + proofTip('wait', 0) + '"]');
                  row.scrollIntoView({block:'center'});
                  const box=row.getBoundingClientRect();
                  showTip(proofTip('wait', 0),(box.left+box.right)/2,box.top);
                  const tooltip=document.querySelector('#tip');
                  if (!${comparisonImages}) return {text:tooltip.textContent};
                  const tipBox=tooltip.getBoundingClientRect();
                  const left=Math.min(box.left,tipBox.left)-4,top=Math.min(box.top,tipBox.top)-4;
                  return {text:tooltip.textContent,clip:{x:left+scrollX,y:top+scrollY,
                    width:Math.max(box.right,tipBox.right)-left+4,
                    height:Math.max(box.bottom,tipBox.bottom)-top+4,scale:1}};
                })()`);
                assert.ok(clip.text.includes(expected),'The waiting tooltip omits the explanation.');
                await saveScreenshot('start-spacing-dark-desktop',{captureBeyondViewport:true,clip:clip.clip});
                await evaluate("hideTip();window.scrollTo(0,0)");
              }
            }
            await show(readings.cargo_folder);
            const folderWait = await evaluate(`(() => {
              const tip = proofTip('wait', 0);
              const row = document.querySelector('.wrow.wait[data-tip="' + tip + '"]');
              const result={note:row.querySelector('.wait-note')?.textContent,reason:tipText(tip,latest)[1]};
              if (!${comparisonImages}) return result;
              row.scrollIntoView({block:'center'});
              const box = row.getBoundingClientRect();
              return {...result,clip:{x:box.x+scrollX,y:box.y+scrollY,width:box.width,height:box.height,scale:1}};
            })()`);
            assert.equal(folderWait.note, 'waits for the build in the same Cargo folder');
            assert.match(folderWait.reason, /the build in the same Cargo folder/);
            if (theme === 'dark' && width === 1400) {
              await saveScreenshot('cargo-folder-dark-desktop',{captureBeyondViewport:true,clip:folderWait.clip});
            }
            for(const name of ['head_memory','head_shortfall','head_swap_shortfall']) {
              await show(readings[name]);
              const headProof=await evaluate(`(() => {
                const rows=latest.waiters[0].head_room.rows;
                const group=document.querySelector('.cap-group');
                const legend=Object.fromEntries([...group.querySelectorAll('.row')].map(row=>[
                  row.querySelector('.name').textContent,{value:row.querySelector('.v').textContent}]));
                return {removed:document.querySelector('#head-line,#admission-terms,#admission-check-rows')===null,
                  values:Object.fromEntries(tooltipRows(proofTip('wait', 0),latest).filter(row=>rows.some(term=>term.label===row.label))
                    .map(row=>[row.label.replace(/^[−=] /,''),row.value])),
                  all:latest.waiters[0].head_room.rows.map(row=>row.gib),
                  hover:tipText(proofTip('wait', 0),latest)[1],capHover:tipText('cap',latest)[1],legend,reservedNote:document.querySelector('#reserved-note').textContent,
                  covered:document.querySelector('#v-reserved').textContent,
                  scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
              })()`);
              assert.ok(headProof.removed,'The removed Head line returns.');
              assert.deepEqual(Object.keys(headProof.values),['Queue cap','Builds','Build reserved','Free']);
              for(const [label,value] of Object.entries(headProof.values)) {
                assert.ok(headProof.hover.includes(label+'  '+value),'The head tooltip omits the term: '+label);
              }
              const tenths=memoryTenths;
              assert.ok(Math.abs(tenths(headProof.values['Queue cap'])-tenths(headProof.values.Builds)
                -tenths(headProof.values['Build reserved'])-tenths(headProof.values.Free))<=2);
              for(const [label,value] of Object.entries(headProof.values))
                assert.equal(value,headProof.legend[label].value,'The head changes a legend amount: '+label);
              assert.ok(!headProof.hover.includes('Safety net') && !headProof.hover.includes('Swap coming back'));
              assert.ok(!headProof.capHover.includes('Safety net') && !headProof.capHover.includes('Swap coming back'));
              const ram=readings[name].ram;
              assert.ok(!headProof.hover.includes('outside the cap'),'The head tooltip explains the queue cap: '+headProof.hover);
              assert.ok(headProof.capHover.includes('What builds may use'),'The queue cap tooltip omits its caption');
              assert.equal(headProof.values['Build reserved'],(ram.build_covered_bytes/gib).toFixed(1)+' GiB');
              assert.ok(ram.build_target_bytes>ram.build_covered_bytes,'The fixture has no uncovered build reserve.');
              if(name==='head_swap_shortfall') assert.ok(ram.swap_target_bytes>ram.swap_covered_bytes);
              assert.ok(headProof.scroll<=headProof.client,'The admission requirements overflow the page.');
              if(name==='head_memory'&&theme==='dark'&&width===1400) {
                const clip=await evaluate(`(() => {
                  const card=document.querySelector('#barwrap').closest('.card').getBoundingClientRect();
                  const legend=document.querySelector('.legend-box').getBoundingClientRect();
                  const row=document.querySelector('.walk-table tr[data-tip="' + proofTip('wait', 0) + '"]').getBoundingClientRect();
                  const groups=[...document.querySelectorAll('.legend-group')].map(group=>group.getBoundingClientRect());
                  showTip(proofTip('wait', 0),card.right-140,row.top);
                  const popup=document.querySelector('#tip');
                  const legendRight=Math.max(...groups.map(group=>group.right));
                  const beside=card.right-legendRight>=popup.offsetWidth+24;
                  const top=beside?legend.top+36:Math.max(...groups.map(group=>group.bottom))+18;
                  placeTip(card.right-popup.offsetWidth/2-8,top+popup.offsetHeight+14);
                  const tip=popup.getBoundingClientRect();
                  if(groups.some(group=>group.left<tip.right&&group.right>tip.left&&group.top<tip.bottom&&group.bottom>tip.top)) {
                    throw new Error('The head tooltip covers a legend group.');
                  }
                  if (!${comparisonImages}) return;
                  return {x:card.left,y:card.top+scrollY,width:Math.max(card.right,tip.right+8)-card.left,
                    height:Math.max(row.bottom,tip.bottom)-card.top+8,scale:1};
                })()`);
                await saveScreenshot('head-tooltip-dark-desktop',{captureBeyondViewport:true,clip});
              }
              proof[name]=headProof;
            }
            await show(base);
            const reserveNotes=await evaluate(`['reserved-note','safety-note','swap-note'].map(id=>document.getElementById(id).textContent)`);
            assert.deepEqual(reserveNotes,['Running builds grow into it.','A fixed setting.','RAM held for swap that comes back.']);
            for (const name of ['old_cap','missing_cap','missing_use']) {
              await show(readings[name]);
              const state=await evaluate(`({rows:document.querySelectorAll('.wrow[data-run]').length,
                cap:document.querySelector('#cap-value').textContent, text:document.body.textContent,
                room:document.querySelector('#v-room').textContent})`);
              assert.equal(state.cap, 'Queue cap unavailable', 'The missing cap has a value.');
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
            assert.equal(await evaluate("document.querySelector('#walk .queue-title').textContent"),
              'Queued, in order of submission (0)');
            assert.equal(await evaluate("document.querySelectorAll('#walk .wrow.wait, #walk tr.wait').length"),0);
            // The tooltip shows 'second nextest', a step that the smaller reading no longer has.
            await evaluate(`showTip('${proofTip('row',0,readings.shrink)}',200,200)`);
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
          const refresh=createPageRefresh(tick);
          const line=document.querySelector('[data-page-status]');
          const part=name=>line.querySelector('[data-status-'+name+']');
          const ageLimit=JSON.parse(line.dataset.statusOptions).ageLimit;
          const states=[];
          const snapshot=name=>states.push({name,state:line.dataset.state,
            reason:part('reason').textContent,age:part('age').textContent,ageHidden:part('age').hidden,
            cards:[...document.querySelectorAll('.card')].map(card=>card.innerHTML).join('')
              .replace(/<div class="last-reading"[^>]*>[^<]*<.div>/,'').replace(/--divider-top: [^;]*;/g,''),
            cardsVisible:[...document.querySelectorAll('.card')].map(card=>card.checkVisibility({checkVisibilityCSS:true,checkOpacity:true})),
            lastReading:document.querySelector('#last-reading').hidden?'':document.querySelector('#last-reading').textContent,
            rows:document.querySelectorAll('.wrow').length,
            word:part('word').textContent,
            icons:[...line.querySelectorAll('[data-status-icon]')].filter(icon=>!icon.hidden).map(icon=>icon.dataset.statusIcon),
            timingsHidden:part('trip').hidden&&part('render').hidden,
            position:getComputedStyle(line).position});
          const good=async()=>{
            reading.read_at=now/1000;
            window.fetch=async()=>({ok:true,json:async()=>reading});
            await refresh.refresh(); fitLabels();
          };
          try {
            window.fetch=async()=>({ok:true,json:async()=>({ok:false})});
            await refresh.refresh();snapshot('initial');
            await good();snapshot('good');
            const goodAt=now;
            window.fetch=async()=>({ok:true,json:async()=>({ok:false})});
            now+=1000;await refresh.refresh();snapshot('one_failure');
            now=goodAt+(ageLimit+3)*1000;await refresh.refresh();snapshot('stale');
            const retained={...reading,read_error:'private socket timeout',error_at:now/1000};
            window.fetch=async()=>({ok:true,json:async()=>retained});
            await refresh.refresh();snapshot('retained');
            await good();snapshot('recovered');
            window.fetch=async()=>{throw new Error('proof fetch failure');};
            now+=(ageLimit+3)*1000;await refresh.refresh();snapshot('fetch_failure');
            await good();snapshot('final');
            return states;
          } finally { window.fetch=originalFetch;Date.now=originalNow;window.render=originalRender; }
        })()`);
        const states=Object.fromEntries(failures.map(state=>[state.name,state]));
        assert.equal(states.initial.state,'Failed');
        assert.equal(states.initial.reason,'The queue did not answer.');
        assert.match(states.retained.lastReading,/^Last reading: /);
        assert.equal(states.good.lastReading,'');
        for(const name of ['one_failure','stale','retained']) {
          assert.equal(states[name].cards,states.good.cards, name+' changes the good reading.');
          assert.equal(states[name].rows,states.good.rows);
          assert.ok(!states[name].ageHidden);
          assert.deepEqual(states[name].cardsVisible,states.good.cardsVisible,name+' changes card visibility.');
        }
        assert.equal(states.one_failure.state,'Failed');
        for(const name of ['stale','retained','fetch_failure']) {
          assert.equal(states[name].state,'Failed');
          assert.equal(states[name].age,'18 s ago');
          assert.equal(states[name].word,'Failed');
          assert.deepEqual(states[name].icons,['Failed']);
          assert.ok(states[name].timingsHidden);
          assert.equal(states[name].position,'sticky');
        }
        assert.equal(states.fetch_failure.reason,'proof fetch failure');
        for(const name of ['recovered','final']) {
          assert.equal(states[name].state,'Live');
          assert.ok(!states[name].ageHidden && states[name].reason==='');
        }
        const capture = async (name, selector, enabled = comparisonImages) => {
          if (!enabled) return;
          await evaluate(`document.querySelector(${JSON.stringify(selector)}).scrollIntoView({block:'center'})`);
          const clip=await evaluate(`(() => {
            const r=document.querySelector(${JSON.stringify(selector)}).getBoundingClientRect();
            return {x:Math.max(0,r.left+scrollX),y:r.top+scrollY,width:r.width,height:r.height,scale:1};
          })()`);
          await saveScreenshot(name,{captureBeyondViewport:true,clip},enabled);
        };
        if (cloudOwnerComparison) {
          await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
          await call('Emulation.setDeviceMetricsOverride', {width:1600,height:1200,deviceScaleFactor:1,mobile:false});
          for (const version of ['before','after']) {
            const reading=structuredClone(readings.step_owners);
            reading.walk=reading.walk.filter(row=>row.kind==='cloud');
            reading.waiters=reading.waiters.filter(row=>row.kind==='cloud');
            assert.equal(reading.walk.length,1);
            assert.equal(reading.waiters.length,1);
            if(version==='before') for(const row of [...reading.walk,...reading.waiters]) {
              row.name='nextest';row.unit='owner not recorded';row.showrunner='owner not recorded';
            }
            await show(reading);
            const row=await evaluate(`(() => {
              const chart=document.querySelector('.wrow[data-run="' + proofRun(0) + '"]');
              const table=document.querySelector('tr[data-tip="' + proofTip('row', 0) + '"]');
              return {name:chart.querySelector('.name').textContent,
                small:chart.querySelector('.owner').textContent,
                unit:chart.querySelector('.name > .staff-name')?.textContent ?? null,
                unitColor:chart.querySelector('.name > .staff-name')?.dataset.colorText ?? null,
                machine:table.querySelector('.machine').textContent};
            })()`);
            assert.equal(row.machine,'aws');
            assert.equal(row.name,version==='before'?'nextest':'research nextest');
            assert.equal(row.unit,version==='before'?null:'research');
            assert.equal(row.unitColor,version==='before'?null:'role.unit-director');
            if(version==='after') assert.equal(row.small,'hana');
            await capture(version+'-aws-row','#walk');
            await evaluate(`showTip(proofTip('row', 0),180,160)`);
            const tooltip=await evaluate(`document.querySelector('#tip').innerText`);
            if(version==='after') {
              assert.match(tooltip,/showrunner\s*:?\s*hana/);
              assert.match(tooltip,/unit\s*:?\s*research/);
            } else assert.match(tooltip,/owner not recorded/);
            await capture(version+'-aws-tooltip','#tip');
            if (comparisonImages) comparisons.push({version,...row,tooltip});
          }
        }
        if (ownersComparison) {
          assert.ok(beforePage, 'The owner comparison needs the previous page.');
          await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
          await call('Emulation.setDeviceMetricsOverride', {width:1600,height:1200,deviceScaleFactor:1,mobile:false});
          for (const version of ['before','after']) {
            await navigate(version==='before' ? 'before.html' : 'memory_live.html');
            const reading=structuredClone(readings.step_ci);
            const mac=reading.walk.find(row=>row.kind==='mac-build');
            if(version==='before') {
              mac.name='hana nextest';mac.unit='—';mac.showrunner='—';delete mac.ci_label;
              for(const row of [...reading.walk,...reading.waiters]) if(row.kind==='cloud') row.name='nextest';
            }
            await show(reading);
            const state=await evaluate(`(() => {
              const index=latest.walk.findIndex(row=>row.kind==='mac-build');
              const chart=document.querySelector('.wrow[data-run="'+proofRun(index)+'"]');
              const headers=[...document.querySelectorAll('.walk-table thead th')].map(cell=>cell.textContent);
              const waiting=[...document.querySelectorAll('.walk-table tr.queue-head th')].map(cell=>cell.textContent);
              const spacings=[...document.querySelectorAll('.walk-table thead th,.walk-table tr.queue-head th')]
                .filter(cell=>getComputedStyle(cell).display!=='none' && cell.nextElementSibling &&
                  !cell.nextElementSibling.matches('.action-spacer,.row-action'))
                .map(cell=>parseFloat(getComputedStyle(cell).paddingRight));
              const queued=document.querySelector('.wrow.wait[data-tip="' + proofTip('wait', 0) + '"]');
              return {index,name:chart.querySelector('.name').textContent,small:chart.querySelector('.owner').textContent,
                queuedName:queued.querySelector('.name').textContent,
                unitColored:!!chart.querySelector('.name > .staff-name'),
                queuedUnitColored:!!queued.querySelector('.name > .staff-name'),
                headers,waiting,spacings,scroll:document.documentElement.scrollWidth,client:innerWidth,
                tip:tooltipHtml(proofTip('row',index),latest)};
            })()`);
            if(version==='after') {
              assert.equal(state.name,'CI nextest');
              assert.equal(state.queuedName,'CI nextest');
              assert.equal(state.unitColored,false);
              assert.equal(state.queuedUnitColored,false);
              assert.match(state.small,/hana CI.*hana-mac-1.*test-hana/);
              assert.deepEqual(state.headers, runningHeaders);
              assert.deepEqual(state.waiting, queuedHeaders);
              assert.ok(state.spacings.every(gap=>gap===12), JSON.stringify(state.spacings));
              assert.ok(state.tip.includes('CI') && state.tip.includes('hana-mac-1'));
              assert.ok(state.scroll<=state.client, 'The owner table overflows.');
            }
            await capture(version+'-running-queued','#walk');
            await capture(version+'-table-columns','.walk-table');
            await capture(version+'-mac-ci','.wrow[data-tip="'+proofTip('row',state.index)+'"]');
            await evaluate(`showTip(proofTip('row',${state.index}),700,420)`);
            if(version==='after') {
              const ciTip=await evaluate(`document.querySelector('#tip').textContent`);
              assert.match(ciTip,/hana CI/);
            }
            await capture(version+'-mac-ci-tooltip','#tip');
            const owners=structuredClone(readings.step_owners);
            if(version==='before') owners.walk.find(row=>row.kind==='mac-build').name='hana nextest';
            await show(owners);
            const key=await evaluate(`proofTip('row',latest.walk.findIndex(row=>row.kind==='mac-build'))`);
            await evaluate(`showTip(${JSON.stringify(key)},700,420)`);
            if(version==='after') {
              const tips=await evaluate(`[...latest.walk.map((row,index)=>proofTip('row',index)),...latest.waiters.map((row,index)=>proofTip('wait',index))].map(key=>tooltipHtml(key,latest))`);
              for(const tip of tips) {
                assert.match(tip,/<dl class="identity"><dt>showrunner<\/dt><dd class="colon" aria-hidden="true">:<\/dd><dd>hana<\/dd><dt>unit<\/dt><dd class="colon" aria-hidden="true">:<\/dd><dd>research<\/dd>/);
              }
              const allTips=await evaluate(`(() => {
                const keys=[...new Set([...document.querySelectorAll('[data-tip]')].map(node=>node.dataset.tip))];
                return keys.map(key=>({key,html:tooltipHtml(key,latest)}));
              })()`);
              for(const tip of allTips) assert.ok(/showrunner/i.test(tip.html) && /unit/i.test(tip.html) || /CI/.test(tip.html), tip.key);
              const nativeTips=await evaluate(`[...document.querySelectorAll('[title],svg title')].map(node=>node.localName==='title'?node.textContent:node.getAttribute('title'))`);
              assert.ok(nativeTips.length>0);
              for(const tip of nativeTips) {assert.match(tip,/Showrunner/);assert.match(tip,/Unit/);}
              const swap=await evaluate(`tooltipHtml('swap:'+latest.swap_by.findIndex(row=>row.unit==='research'),latest)`);
              assert.match(swap,/hana/);assert.match(swap,/research/);
            }
            await capture(version+'-owner-tooltip','#tip');
            if (comparisonImages) comparisons.push({version,...state});
          }
          for(const width of [600,800,1200,1600]) {
            await call('Emulation.setDeviceMetricsOverride',{width,height:1200,deviceScaleFactor:1,mobile:false});
            await show(readings.step_unknown);
            const unknownNames=await evaluate(`(() => {
              const rows=[...document.querySelectorAll('.wrow[data-run],.wrow.wait')];
              return rows.filter(row=>stepRow(row.dataset.tip,latest).unit==='owner not recorded').map(row=>{
                const name=row.querySelector('.name');
                return {text:name.textContent,expected:stepRow(row.dataset.tip,latest).name,
                  colored:!!name.querySelector('.staff-name'),scroll:name.scrollWidth,width:name.clientWidth};
              });
            })()`);
            assert.ok(unknownNames.length>0);
            for(const name of unknownNames) {
              assert.equal(name.text,name.expected);
              assert.equal(name.colored,false);
              assert.ok(name.scroll<=name.width, 'The step label cuts its text. '+JSON.stringify({width,name}));
            }
          }
          await call('Emulation.setDeviceMetricsOverride',{width:600,height:1200,deviceScaleFactor:1,mobile:false});
          await show(readings.step_ci);
          await capture('after-narrow-owner-ci','#walk');
          const live=await readFile(join(folder,'live-mac-ci.json'),'utf8').then(JSON.parse).catch(error=>{
            if(error.code==='ENOENT') return {row:null};
            throw error;
          });
          if(live.row) {
            const reading=structuredClone(readings.step_ci);
            reading.walk=[live.row];reading.waiters=[];
            await call('Emulation.setDeviceMetricsOverride',{width:1600,height:1200,deviceScaleFactor:1,mobile:false});
            await show(reading);
            const displayed=await evaluate(`({pid:latest.walk[0].pid,
              name:document.querySelector('.wrow[data-run="' + proofRun(0) + '"] .name').textContent,
              small:document.querySelector('.wrow[data-run="' + proofRun(0) + '"] .owner').textContent,
              unitColored:!!document.querySelector('.wrow[data-run="' + proofRun(0) + '"] .name > .staff-name'),
              tooltip:tooltipHtml(proofTip('row', 0),latest)})`);
            assert.match(displayed.name,/^CI /);assert.match(displayed.small,/hana CI/);
            assert.equal(displayed.unitColored,false);assert.match(displayed.tooltip,/hana CI/);
            await writeFile(join(folder,'live-mac-ci-page.json'),JSON.stringify(displayed,null,2));
            await capture('live-mac-ci','#walk');
            await evaluate(`showTip(proofTip('row', 0),700,420)`);
            await capture('live-mac-ci-tooltip','#tip');
            console.log('LIVE_MAC_CI:',JSON.stringify(displayed));
          }
        }
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
            if (comparisonImages) comparisons.push({version,...names});
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
              const status=document.querySelector(${JSON.stringify(version==='before'?'.status':'[data-page-status]')}).getBoundingClientRect();
              return {x:0,y:0,width:innerWidth,height:Math.ceil(status.bottom+16),scale:1};
            })()`);
            const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,clip});
            await writeFile(join(folder,`${version}-dark-title.png`),Buffer.from(shot.data,'base64'));
            if (comparisonImages) comparisons.push({version,...state});
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
                    hover:tipText(heading.dataset.tip,latest).join('\\n'),truncated:heading.firstElementChild.scrollWidth>heading.firstElementChild.clientWidth,
                    clipped:getComputedStyle(heading.firstElementChild).textOverflow==='ellipsis' && getComputedStyle(heading.firstElementChild).overflow==='hidden',
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
                assert.equal(state.title,`${title} (${readings[name].waiters.length})`);
                assert.ok(state.hover.startsWith('Queued  ' + readings[name].waiters.length + '\n'));
                assert.match(state.hover,/Order  submission order/);
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
                if (comparisonImages) comparisons.push({theme,width,name,...state});
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
                assert.equal(state.title,`${cases[2][1]} (${readings.queued_two.waiters.length})`);
                assert.ok(state.waitTop<beforeWaitTop,'The separate title row remains.');
              }
              await capture(`${version}-${theme}-queued`,'#walk');
            }
          }
        }
        if (blocksComparison) {
          assert.ok(beforePage, 'The block comparison needs the previous page.');
          const blockReason='research P16 Hana palette Command-shortcut shots (kept binary, no cargo)';
          const blockLine='natedev or Mac';
          const blocked=structuredClone(readings.spacing);
          Object.assign(blocked,{room:1.0,pressure:'some stall above its limit',psi:{some:31.0,full:6.2},
            mac_idle_alert:'MAC IDLE 8 min: 1 steps wait for the Mac. Blocked by research since 15:32 ('+blockReason
              +'); /build:mac unblock clears it.',
            targets:[
              {target:'mac',state:'Blocked',reason:blockReason,holder:'research',unit:'research',showrunner:'hana',
               since:'since 15:32 EST',until:'awaiting unblock; requested expiry was 16:30 EST'},
              {target:'natedev',state:'Held',reason:'disk walk of ~/.local/state',holder:'report',unit:'report',showrunner:'builds',
               since:'since 15:20 EST',until:'expected release 16:00 EST'},
              {target:'natedev',state:'Held',reason:'cargo sweep of the shared target folder',holder:'cables',unit:'cables',
               showrunner:'builds',since:'start not recorded',until:'until released'}]});
          blocked.waiters[0].runs_on='natedev or Mac';
          const after=structuredClone(blocked);
          after.mac_idle_alert='';
          const contrast=`(() => {
            const channel=value=>{value/=255;return value<=0.03928?value/12.92:((value+0.055)/1.055)**2.4;};
            const luminance=text=>{const [r,g,b]=text.match(/\\d+(\\.\\d+)?/g).map(Number);
              return 0.2126*channel(r)+0.7152*channel(g)+0.0722*channel(b);};
            const state=document.querySelector('.target-state');
            const ground=getComputedStyle(state.closest('.card')).backgroundColor;
            const [high,low]=[luminance(getComputedStyle(state).color),luminance(ground)].sort((a,b)=>b-a);
            return (high+0.05)/(low+0.05);
          })()`;
          for (const theme of ['light','dark']) {
            await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
            for (const version of ['before','after']) {
              await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1200,deviceScaleFactor:1,mobile:false});
              await navigate(version==='before'?'before.html':'memory_live.html');
              await show(version==='before'?blocked:after);
              const state=await evaluate(`(() => {
                const alert=document.querySelector('#mac-idle-alert');
                const targets=document.querySelector('#targets');
                const memory=document.querySelector('#room').closest('.card');
                return {alert:alert.hidden?'':alert.textContent,
                  targets:targets?targets.hidden?'hidden':targets.innerText:'absent',
                  rows:targets?[...targets.querySelectorAll('.target-row')].map(row=>row.innerText):[],
                  bottom:memory.getBoundingClientRect().bottom+scrollY,
                  scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
              })()`);
              if(version==='after') {
                assert.equal(state.alert,'');
                assert.equal(state.rows.length,3);
                assert.match(state.rows[0],/mac[\s\S]*Blocked[\s\S]*Unit research · Showrunner hana/);
                assert.match(state.rows[2],/Start not recorded/);
                assert.ok(!/idle/i.test(state.targets),'The block card says idle.');
                const ratio=await evaluate(contrast);
                assert.ok(ratio>=4.5,'The state label contrast is '+ratio+' in '+theme+'.');
                state.contrast=ratio;
              } else assert.match(state.alert,/^MAC IDLE 8 min/);
              assert.equal(await evaluate(`document.querySelector('#pill-t').textContent`),'pressure: queue holds');
              assert.ok(state.scroll<=state.client,'The page overflows.');
              await evaluate('scrollTo(0,0)');
              const shot=await call('Page.captureScreenshot',{format:'png',captureBeyondViewport:true,
                clip:{x:0,y:0,width:1400,height:Math.ceil(state.bottom)+16,scale:1}});
              await writeFile(join(folder,`${version}-${theme}-blocks.png`),Buffer.from(shot.data,'base64'));
              await evaluate(`showTip(proofTip('wait', 0),700,420)`);
              const tooltip=await evaluate(`document.querySelector('#tip').innerText`);
              if(version==='after') assert.ok(tooltip.includes(blockLine),tooltip);
              else assert.ok(!tooltip.includes('Mac:'),tooltip);
              await capture(`${version}-${theme}-waiter-tooltip`,'#tip');
              if (comparisonImages) comparisons.push({version,theme,...state,tooltip});
            }
            await call('Emulation.setDeviceMetricsOverride',{width:390,height:1200,deviceScaleFactor:1,mobile:false});
            await navigate('memory_live.html');
            await show(after);
            const narrow=await evaluate(`(() => {
              const rows=[...document.querySelectorAll('.target-row')].map(row=>{
                const top=selector=>row.querySelector(selector).getBoundingClientRect().top;
                return {owner:top('.target-owner'),since:top('.target-since'),until:top('.target-until'),
                  reason:top('.target-reason')};
              });
              return {rows,scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth};
            })()`);
            assert.ok(narrow.scroll<=narrow.client,'The narrow page overflows.');
            for(const row of narrow.rows) assert.ok(row.reason<row.owner && row.owner<row.since && row.since<row.until,
              'The narrow row order is wrong: '+JSON.stringify(row));
            await capture(`after-${theme}-narrow-targets`,'#targets');
            if (comparisonImages) comparisons.push({version:'after',theme,width:390,...narrow});
          }
          await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
          await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1200,deviceScaleFactor:1,mobile:false});
          const lines=['natedev or Mac','natedev only','natedev only (Mac state unknown)'];
          for (const [index,line] of lines.entries()) {
            const reading=structuredClone(after);
            reading.waiters[0].runs_on=line;
            await show(reading);
            await evaluate(`showTip(proofTip('wait', 0),700,420)`);
            const tooltip=await evaluate(`document.querySelector('#tip').innerText`);
            assert.ok(tooltip.includes(line),tooltip);
            await capture(`after-mac-line-${index+1}`,'#tip');
            if (comparisonImages) comparisons.push({line,tooltip});
          }
        }
        if (waitTipComparison) {
          assert.ok(beforePage, 'The wait hover comparison needs the previous page.');
          await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1200,deviceScaleFactor:1,mobile:false});
          const owner={name:'hana nextest',showrunner:'hana',unit:'research',runner:'unit director',worker:'',detail:'research',
            pid:4242,need:16.3,since:fixtureTime-16*60,reason:'free memory',fits:false};
          const priority="queue priority: research (the user's bump)";
          const note='Other programs have 9.8 GiB of memory in swap. It comes back into RAM when they run, so the queue holds RAM for it.';
          const oldRoom={rows:[{label:'free RAM',gib:28.4},{label:'− safety net',gib:3.0},
            {label:'− swap coming back',gib:9.8},{label:'− build reserved',gib:8.5},
            {label:'= free',gib:7.1},{label:'needs',gib:16.3}],note};
          const room={unavailable:'',
            rows:[{label:'Queue cap',gib:17.1},{label:'− Builds',gib:1.5},{label:'− Build reserved',gib:8.5},
            {label:'= Free for builds',gib:7.1}]};
          const behind={...owner,name:'trunk nextest',unit:'trunk',detail:'trunk',pid:4243,need:2.0,
            reason:'behind #1: the first step in line gets memory first',runs_on:'natedev or Mac'};
          const head={...owner,place:1,first_in_line:true,why:'only 7.1 GiB is free for builds. It starts when 16.3 GiB is free.'};
          const second={...behind,place:2,why:'it is behind the first build in line, which gets memory first.'};
          const cloud=structuredClone(readings.cloud.waiters.find(row=>row.kind==='cloud')||readings.step_owners.waiters.find(row=>row.kind==='cloud'));
          Object.assign(cloud,{since:fixtureTime-3*60,name:'claude gate',unit:'builds',reason:'waits for a cloud slot: 9 of 9 in use',
            runs_on:'AWS (natedev only when AWS cannot start the gate)'});
          const cases=[['before','before.html',[{...owner,priority_text:priority,priority_entries:[{unit:'research'}],runs_on:'natedev only',head_room:oldRoom},behind,cloud]],
            ['after','memory_live.html',[{...head,priority_text:priority,priority_entries:[{unit:'research'}],runs_on:'natedev only',head_room:room},second,cloud]]];
          for (const theme of ['light','dark']) {
            await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
            for (const [version,path,waiters] of cases) {
              await navigate(path);
              await show({...structuredClone(readings.spacing),waiters});
              for (const index of waiters.keys()) {
                await evaluate(`showTip(proofTip('wait',${index}),700,420)`);
                const tooltip=await evaluate(`document.querySelector('#tip').innerText`);
                if (version==='after') {
                  assert.ok(!tooltip.includes('Head'),tooltip);
                  assert.ok(index<2 ? tooltip.includes('Why it waits: ') : /\nreason\s/.test(tooltip),tooltip);
                  assert.ok(tooltip.includes('can run on'),tooltip);
                  assert.equal(tooltip.includes('= Free for builds'),index===0,tooltip);
                  assert.ok(!tooltip.includes('outside the cap'),tooltip);
                  assert.ok(!/needs\s/.test(tooltip),tooltip);
                }
                await capture(`${version}-${theme}-wait-${index+1}`,'#tip');
                if (comparisonImages) comparisons.push({version,theme,index,tooltip});
              }
            }
          }
        }
        if (identityComparison) {
          assert.ok(beforePage, 'The identity comparison needs the previous page.');
          await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
          await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1200,deviceScaleFactor:1,mobile:false});
          const reading=structuredClone(readings.step_owners);
          const mac=reading.walk.findIndex(row=>row.kind==='mac-build');
          const local=reading.walk.findIndex(row=>!row.kind);
          assert.ok(mac>=0 && local>=0 && reading.waiters.length>0);
          Object.assign(reading.walk[mac],{name:'hana nextest',showrunner:'hana',unit:'research',runner:'worker',worker:'impl2',
            used:9.7,started_at:reading.read_at-13*60,read_at:reading.read_at,stage:{kind:'MacTesting'},
            progress:{kind:'RowProgress',phase:'testing',done:891,total:894,percent:100*891/894,unit:'units'}});
          Object.assign(reading.walk[local],{name:'builds merge gate',showrunner:'builds',unit:'—',runner:'showrunner',worker:''});
          Object.assign(reading.waiters[0],{name:'report nextest',showrunner:'builds',unit:'report',runner:'unit director',worker:''});
          // A colon is its own cell on the after page; the before page draws it after the label.
          const identity=key=>evaluate(`(() => {
            showTip(${JSON.stringify(key)},700,420);
            const tip=document.querySelector('#tip');
            const block=tip.querySelector('.identity');
            const colonLeft=cell=>{
              const colon=cell.nextElementSibling;
              if(colon?.classList.contains('colon')) return colon.getBoundingClientRect().left;
              const style=getComputedStyle(cell,'::after');
              const probe=document.createElement('span');
              probe.textContent=':';
              probe.style.font=style.font;
              document.body.append(probe);
              const width=probe.getBoundingClientRect().width;
              probe.remove();
              return cell.getBoundingClientRect().right-width;
            };
            return {text:tip.innerText,fields:block?[...block.querySelectorAll('dt')].map(cell=>[cell.textContent,
              (cell.nextElementSibling?.classList.contains('colon')?cell.nextElementSibling.nextElementSibling:cell.nextElementSibling).textContent,
              colonLeft(cell)]):[],
              first:tip.children[1]?.className||''};
          })()`);
          const lefts=state=>[...new Set(state.fields.map(([, , left])=>left.toFixed(2)))];
          for (const [key,name,expected] of [
            [proofTip('row',mac,reading),'mac-row',[['showrunner','hana'],['unit','research'],['worker','impl2']]],
            [proofTip('wait',0,reading),'queued-row',[['showrunner','builds'],['unit','report'],['worker','this run is from the unit director']]]]) {
            for (const version of ['before','after']) {
              await navigate(version==='before'?'before.html':'memory_live.html');
              await show(reading);
              const state=await identity(key);
              assert.equal(state.first,'identity');
              assert.deepEqual(state.fields.map(([field,value])=>[field,value]),expected);
              if(version==='after') assert.equal(lefts(state).length,1,`The colons do not align: ${lefts(state)}`);
              await capture(`${version}-colons-${name}`,'#tip');
              if (comparisonImages) comparisons.push({version,row:name,colonLefts:lefts(state),...state});
            }
          }
          await navigate('memory_live.html');
          await show(reading);
          const showrunnerState=await identity(proofTip('row',local,reading));
          assert.deepEqual(showrunnerState.fields.map(([field,value])=>[field,value]),
            [['showrunner','builds'],['unit','this run is from the showrunner']]);
          assert.equal(lefts(showrunnerState).length,1);
        }
        if (beforePage && !queuedComparison && !consoleComparison && !ownersComparison && !blocksComparison && !identityComparison && !waitTipComparison && !macrouteComparison && !rowAskComparison) {
          const review = structuredClone(readings.spacing);
          review.priority_text = "queue priority: port (the user's bump)";
          review.head_text = 'Head: need 1.9 GiB > room 0.0 GiB (free 8.5 GiB − safety 3.0 GiB − 6.0 GiB swapped outside builds) · cloud 2 of 9 slots';
          const local = review.walk.find(row => row.queued);
          local.machine = 'natedev';
          const mac = structuredClone(readings.mac_three.walk.find(row => row.kind === 'mac-build'));
          Object.assign(mac, {name:'hana nextest',used:3.5,unit:'trunk',stage:{kind:'MacTestingCounter'},
            progress:{kind:'RowProgress',phase:'testing',unit:'units',done:722,total:727,percent:100*722/727}});
          const aws = structuredClone(readings.cloud.walk.find(row => row.kind === 'cloud'));
          Object.assign(aws, {machine:'aws',used:1.5});
          const ci = {name:'hana CI',ci:true,admitted:false,queued:false,used:0.125,reserved:0,counted:0,
            started_at:fixtureTime-60,unit:'hana CI',machine:'natedev',pid:701,vcpus:32,
            progress:{kind:'NoProgress',reason:'no counter'},ci_runners:[{state:'busy',job:'doctests'}]};
          review.walk = [local, mac, aws, ci];
          review.waiters = [{...review.waiters[0],name:'CI doctests',unit:'',showrunner:'',
            ci_label:'hana CI · fixture-runner · doctests',detail:'',reason:'free memory',place:1,
            why:'only 0.0 GiB is free for builds. It starts when 1.9 GiB is free.',
            head_text:review.head_text,priority_text:review.priority_text,first_in_line:true,
            head_room:{unavailable:'',
              rows:[{label:'Queue cap',gib:0.0},{label:'− Builds',gib:0.0},{label:'− Build reserved',gib:0.0},
              {label:'= Free for builds',gib:0.0}],note:''}}];
          review.ci_ram = 0.125;
          review.taken = review.cap - review.room;
          for (const theme of ['light','dark']) {
            await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
            for (const version of ['before','after']) {
              await call('Emulation.setDeviceMetricsOverride', {width:1800,height:1200,deviceScaleFactor:1,mobile:false});
              await navigate(version === 'before' ? 'before.html' : 'memory_live.html');
              await show(review);
              const comparison = await evaluate(`(() => {
                const table=document.querySelector('.walk-table');
                const mac=document.querySelector('.wrow[data-run="' + proofRun(1) + '"]');
                const macBar=mac.querySelector('.seg-remote,.seg-used');
                const awsBar=document.querySelector('.wrow[data-run="' + proofRun(2) + '"] .seg-remote');
                return {headers:[...table.querySelectorAll('thead th')].map(cell=>cell.textContent),
                  done:table.querySelector('tr[data-tip="' + proofTip('row', 1) + '"] .done')?.textContent,
                  underBar:!!mac.querySelector('.progress-amount'),
                  remoteColor:getComputedStyle(macBar).backgroundColor,
                  awsColor:awsBar?getComputedStyle(awsBar).backgroundColor:null,
                  localColor:getComputedStyle(document.querySelector('.wrow[data-run="' + proofRun(0) + '"] .seg-used')).backgroundColor,
                  machineColors:[...table.querySelectorAll('tr[data-tip^="row:"] .machine')].map(cell=>getComputedStyle(cell).color),
                  ciVisible:!!table.querySelector('tr[data-tip="' + proofTip('row', 3) + '"]'),
                  waiting:document.querySelector('.wrow.wait[data-tip="' + proofTip('wait', 0) + '"] .name').textContent,
                  waitingOwner:document.querySelector('.wrow.wait[data-tip="' + proofTip('wait', 0) + '"] .owner').textContent,
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
                assert.equal(comparison.waiting,'CI doctests');
                assert.equal(comparison.waitingOwner,'hana CI');
                assert.equal(comparison.pageOverflow,false);
              } else {
                assert.equal(comparison.ciVisible,true);
                assert.equal(comparison.underBar,true);
                assert.equal(comparison.remoteColor,comparison.localColor);
              }
              await capture(`${version}-${theme}-c1507-b`,'#walk');
              await capture(`${version}-${theme}-hanaci-row`,'#walk');
              await evaluate(`showTip(proofTip('row', 1),180,160)`);
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
                      head:tipText(proofTip('wait', 0),latest).join(' ')};
                  })()`);
                  assert.equal(layout.tables,1);
                  assert.equal(layout.lists,0);
                  assert.equal(layout.layout,'beside');
                  for(const value of ['= Free for builds',review.priority_text,'Why it waits: '])
                    assert.equal(layout.head.split(value).length-1,1);
                  responsive.push({width,...layout});
                }
              }
              if (comparisonImages) comparisons.push({version,theme,...comparison,note,responsive});
            }
          }
        }

        await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
        await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1100,deviceScaleFactor:1,mobile:false});
        for(const version of beforePage ? ['before','after'] : ['after']) {
          await navigate(version==='before' ? 'before.html' : 'memory_live.html');
          const reading=structuredClone(readings.macroute_running);
          const row=reading.walk.find(row=>row.kind==='mac-build');
          row.started_at = reading.read_at - 600;
          if(version==='before') row.name += ' · '+row.gate_label;
          await show(reading);
          const index=reading.walk.findIndex(row=>row.kind==='mac-build');
          await evaluate(`showTip(proofTip('row',${index}),700,420)`);
          const state=await evaluate(`(() => {
            const tip=document.querySelector('#tip');
            const head=tip.querySelector('dt.title');
            return {title:head.textContent,
              duration:head.nextElementSibling.textContent,width:tip.getBoundingClientRect().width,
              scroll:tip.scrollWidth,client:tip.clientWidth};
          })()`);
          if(version==='after') {
            assert.equal(state.title,'fps nextest');
            assert.equal(state.duration,'running 10 min');
            assert.ok(state.width<=24*16+22,JSON.stringify(state));
            assert.ok(state.scroll<=state.client,JSON.stringify(state));
          }
          await saveScreenshot(`macroute-${version}-running-desktop`);
          await capture(`macroute-${version}-running-tip`,'#tip');
          if (comparisonImages) comparisons.push({version,...state});
        }
        await navigate('memory_live.html');
        await show(readings.macroute);
        const routeLines = [
          'natedev or Mac',
          'edits the tree',
          'direct cargo command'];
        const routeTips = [];
        for (const [index,line] of routeLines.entries()) {
          await evaluate(`showTip(proofTip('wait',${index}),700,420)`);
          const tip = await evaluate(`(() => {
            const node=document.querySelector('#tip');
            return {text:node.innerText,html:${comparisonImages} ? node.innerHTML : undefined};
          })()`);
          assert.ok(tip.text.includes(line),tip.text);
          if(index>0) assert.ok(!tip.text.includes("Mac: no,"),tip.text);
          if (comparisonImages) routeTips.push(tip.html);
        }
        if (comparisonImages) {
          await evaluate(`(() => {
            document.querySelector('#tip').style.display='none';
            const panel=document.createElement('div');
            panel.style.cssText='position:fixed;left:24px;right:24px;top:250px;display:flex;align-items:flex-start;gap:18px;z-index:10000';
            for(const html of ${JSON.stringify(routeTips)}) {
              const tip=document.createElement('div');
              tip.id='tip';
              tip.className='on';
              tip.style.cssText='position:relative;left:auto;top:auto;opacity:1;flex:0 1 auto;min-width:0';
              tip.innerHTML=html;
              panel.append(tip);
            }
            document.body.append(panel);
          })()`);
          await saveScreenshot('macroute-dark-desktop');
        }
        if (comparisonImages) comparisons.push({routeLines});

        for (const theme of themes) {
          await call('Emulation.setEmulatedMedia',{features:[{name:'prefers-color-scheme',value:theme}]});
          for (const width of [...widths].reverse()) {
            await call('Emulation.setDeviceMetricsOverride',{width,height:1100,deviceScaleFactor:1,mobile:false});
            await navigate('memory_live.html');
            const review = await evaluate(`(() => {
              const fractional={...latest,read_at:220,cap:5.2,ram:{...latest.ram,
                build_tools_bytes:1.04*GIB,rust_analyzer_bytes:1.04*GIB,sccache_bytes:0,other_build_tools_bytes:0,
                queued_builds_bytes:1.04*GIB,ci_bytes:1.04*GIB,
                swap_covered_bytes:GIB,swap_target_bytes:2*GIB,swap_missing_bytes:GIB,build_covered_bytes:1.04*GIB,room_bytes:1.04*GIB},
                swap:{...latest.swap,window_seconds:0},
                unmatched_priority_entries:[{unit:'research'},{unit:'cables'}],
                walk:[{key:'fractional-run',name:'Codex servers',group:'codex',used:0.4,parts:[
                  {who:'codex',pid:123,started_at:100,gib:0.2},{who:'codex',pid:456,started_at:100,gib:0.2}]}],
                waiters:[{key:'fractional-wait',name:'fractional',first_in_line:true,need:1.06,free:1.04,head_room:{rows:[
                  {label:'Queue cap',gib:5.2},{label:'− Builds',gib:3.12},
                  {label:'− Build reserved',gib:1.04},{label:'= Free',gib:1.04}]}},
                  {key:'fractional-slot',name:'slot',kind:'repository-slot',need:0,runs_on:'natedev'}]};
              window.renderReading(fractional);
              return {...Object.fromEntries(['g-builds','cap','room','swap','swap-pool:outside','walkhead','row:fractional-run','wait:fractional-wait','wait:fractional-slot']
                .map(key=>[key,tooltipRows(key,fractional)])),
                legend:Object.fromEntries(['builds','reserved','room'].map(key=>[key,document.querySelector('#v-cap-'+key).textContent]))};
            })()`);
            const amounts=rows=>Object.fromEntries(rows.map(row=>[row.label,row.value]));
            const builds=review['g-builds'].filter(row=>row.kind!=='caption');
            assert.deepEqual(builds.map(row=>row.value),['3.1 GiB','1.0 GiB','1.0 GiB','1.0 GiB']);
            const wait=amounts(review['wait:fractional-wait']);
            assert.equal(wait['Short by'],'<0.1 GiB');
            const cap=amounts(review.cap);
            assert.equal(cap.Builds,review.legend.builds);
            assert.equal(cap['+ Build reserved'],review.legend.reserved);
            assert.equal(cap['+ Free'],review.legend.room);
            for(const [label,capLabel] of [['Queue cap','Queue cap'],['− Builds','Builds'],
              ['− Build reserved','+ Build reserved'],['= Free','+ Free']]) assert.equal(wait[label],cap[capLabel]);
            for(const key of ['cap','room','wait:fractional-wait']) assert.ok(!review[key].some(row=>/Safety net|Swap coming back/.test(row.label)));
            assert.equal(review.swap.find(row=>row.kind==='caption').value,'RAM held for swap that comes back');
            const swap=amounts(review['swap-pool:outside']);
            assert.equal(swap['Swap coming back'],'1.0 GiB');
            assert.equal(swap.Target,'2.0 GiB');
            assert.equal(amounts(review.walkhead).Priority,'research, cables');
            assert.equal(amounts(review['wait:fractional-slot'])['Waits for'],'build slot');
            assert.ok(!review['wait:fractional-slot'].some(row=>['Needs','Free','Short by'].includes(row.label)));
            assert.ok(review['row:fractional-run'].some(row=>row.label==='codex · 123 · 2 min'));
            assert.ok(review['row:fractional-run'].some(row=>row.label==='codex · 456 · 2 min'));
            const reserveProof = await evaluate(`(() => {
              const cases=[];
              for(const [key,reserve] of [['reserved','build'],['safety','safety'],
                ['swap','swap'],['swap-pool:outside','swap']]) {
                for(const target of [2,3]) {
                  // Without a recorded basis, a Target row shows only when it differs from the title.
                  const reading={...latest,build_reserve:undefined,swap:{...latest.swap,outside_bytes:2*GIB,window_seconds:0},
                    ram:{...latest.ram,[reserve+'_covered_bytes']:2*GIB,
                      [reserve+'_target_bytes']:target*GIB,[reserve+'_missing_bytes']:(target-2)*GIB}};
                  cases.push({key,target,rows:tooltipRows(key,reading)});
                }
              }
              return cases;
            })()`);
            const basisProof = await evaluate(`(() => {
              const reading={...latest,
                build_reserve:{growth_bytes:5*GIB,share_percent:80,between_steps_bytes:GIB,unread_bytes:0},
                swap:{...latest.swap,reserve_share_percent:20,window_seconds:600,swap_in_window_bytes:10*GIB},
                ram:{...latest.ram,build_covered_bytes:5*GIB,build_target_bytes:5*GIB,build_missing_bytes:0,
                  swap_covered_bytes:2*GIB,swap_target_bytes:2*GIB,swap_missing_bytes:0}};
              return Object.fromEntries(['reserved','swap'].map(key=>[key,tooltipRows(key,reading)]));
            })()`);
            // With a recorded basis, each tooltip ends in its sum, also when the sum equals the title.
            assert.deepEqual(basisProof.reserved.filter(row=>row.kind!=='title'&&row.kind!=='caption')
              .map(row=>[row.label,row.value]),
              [['Room to grow','5.0 GiB'],['× Share held','80.0%'],['+ Between steps','1.0 GiB'],['= Target','5.0 GiB']]);
            assert.deepEqual(basisProof.swap.filter(row=>row.kind!=='title'&&row.kind!=='caption')
              .map(row=>[row.label,row.value]),
              [['Lookback','10 min'],['Swap-in','10.0 GiB'],['× Share held','20.0%'],['= Target','2.0 GiB']]);
            for(const {key,target,rows} of reserveProof) {
              const title=rows.find(row=>row.kind==='title');
              const targetRow=rows.find(row=>row.label==='Target');
              assert.equal(title.value,'2.0 GiB',key);
              if(target===2) assert.equal(targetRow,undefined,key);
              else {
                assert.equal(targetRow.value,'3.0 GiB',key);
                assert.notEqual(targetRow.value,title.value,key);
              }
            }
            const leafProof = await evaluate(`(() => {
              const reading={...latest,cap:3.06,taken:2.04,build_reserve:undefined,ram:{...latest.ram,
                rust_analyzer_bytes:1.02*GIB,sccache_bytes:0,other_build_tools_bytes:0,
                build_tools_bytes:1.02*GIB,queued_builds_bytes:0,ci_bytes:0,
                build_covered_bytes:1.02*GIB,build_target_bytes:1.02*GIB,room_bytes:1.02*GIB,
                desktop_bytes:10*GIB,agents_bytes:0,programs:[],smaller_programs_bytes:0,other_programs_bytes:0,
                compressed_swap_bytes:0,kernel_structures_bytes:0,kernel_counters_bytes:0,kernel_bytes:0,
                cache_bytes:0,unattributed_bytes:-GIB,kernel_no_counter_bytes:0}};
              const ram=reading.ram;
              ram.available_bytes=ram.build_covered_bytes+ram.room_bytes+ram.safety_covered_bytes+ram.swap_covered_bytes;
              ram.total_bytes=ram.build_tools_bytes+ram.available_bytes+ram.desktop_bytes+ram.unattributed_bytes;
              window.renderReading(reading);
              const keys=['tools','g-builds','cap','walksum','g-free','g-else','budget'];
              return {tips:Object.fromEntries(keys.map(key=>[key,tooltipRows(key,reading)])),
                legend:Object.fromEntries(['tools','g-builds','g-free','g-else','unattributed','cap']
                  .map(key=>[key,document.querySelector('#v-'+key).textContent]))};
            })()`);
            const tenths=memoryTenths;
            for(const [key,rows] of Object.entries(leafProof.tips)) {
              const values=rows.filter(row=>row.value.endsWith(' GiB')).map(row=>tenths(row.value));
              assert.ok(Math.abs(values[0]-values.slice(1).reduce((sum,value)=>sum+value,0))<=values.length/2,key);
              if(key in leafProof.legend) assert.equal(rows[0].value,leafProof.legend[key]);
            }
            assert.equal(leafProof.tips.cap[0].value,'3.1 GiB');
            assert.equal(leafProof.tips.walksum[0].value,'2.0 GiB');
            assert.equal(leafProof.tips['g-else'][0].value,'9.0 GiB');
            assert.equal(leafProof.legend.unattributed,'-1.0 GiB');
            assert.equal(leafProof.tips.tools[0].value,amounts(leafProof.tips['g-builds'])['Build tools']);
            if (comparisonImages) comparisons.push({leafRounding:true,theme,width,...leafProof});
            if (comparisonImages) comparisons.push({reviewFindings:true,theme,width,rows:review});
          }
        }

        const localReason = 'edits the merged checkout on natedev with mend and fmt, and the push goes from there';
        const legendReading={...structuredClone(base),cap:31,
          ram:JSON.parse(await readFile(join(folder,'legend_ram.json'),'utf8'))};
        for (const theme of themes) {
          await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:theme}]});
          for (const width of [...widths].reverse()) {
            await call('Emulation.setDeviceMetricsOverride',{width,height:1100,deviceScaleFactor:1,mobile:false});
            await navigate('memory_live.html');
            for (const [kind,reading,key] of [
              ['waiting table',readings.head_memory,proofTip('wait',0,readings.head_memory)],
              ['waiting row',readings.macroute_reason,proofTip('wait',2,readings.macroute_reason)],
              ['running row',readings.step_owners,proofTip('row',0,readings.step_owners)],
              ['Mac row',readings.macroute_running,'row:'+readings.macroute_running.walk.find(row=>row.kind==='mac-build').key],
              ...(await evaluate('Object.keys(TIPS)')).map(key=>['legend or key',legendReading,key]),
              ...readings.step_owners.walk.map(row=>['chart row',readings.step_owners,'row:'+row.key]),
              ...readings.step_ci.walk.map(row=>['CI row',readings.step_ci,'row:'+row.key]),
              ...readings.cloud.walk.map(row=>['AWS row',readings.cloud,'row:'+row.key]),
              ...base.swap_by.map((row,index)=>['swap row',base,'swap:'+index]),
              ...['builds_ci','outside'].map(pool=>['swap pool',base,'swap-pool:'+pool])]) {
              await show(reading);
              const alignment=await evaluate(`(() => {
                showTip(${JSON.stringify(key)},${width/2},420);
                const tip=document.querySelector('#tip'), rect=tip.getBoundingClientRect();
                return {grids:tip.querySelectorAll('dl.tip').length,rows:tooltipRows(${JSON.stringify(key)},latest),
                  text:tipText(${JSON.stringify(key)},latest),
                  htmlText:[...tip.querySelectorAll('dd')].map(cell=>cell.previousElementSibling?.tagName==='DT'?
                    (cell.previousElementSibling.textContent+'  '+cell.textContent).trimEnd():cell.textContent).join('\\n'),
                  values:[...tip.querySelectorAll('dd:not(.caption):not(.long)')].map(cell=>({
                  text:cell.textContent,align:getComputedStyle(cell).textAlign,
                  right:cell.getBoundingClientRect().right,group:cell.parentElement.className})),
                  labels:[...tip.querySelectorAll('dt')].map(cell=>({text:cell.textContent,
                    align:getComputedStyle(cell).textAlign})),
                  left:rect.left,right:rect.right,scroll:tip.scrollWidth,client:tip.clientWidth};
              })()`);
              assert.equal(alignment.grids,1,`${kind} ${key}: more than one grid`);
              assert.equal(alignment.rows.filter(row=>row.kind==='title').length,1);
              assert.ok(!alignment.rows.some(row=>/:$/.test(row.label)||/[a-z]\.$/.test(row.value)),
                `${kind} ${key}: sentence punctuation`);
              assert.equal(alignment.htmlText,alignment.text.filter(Boolean).join('\n'));
              for(const row of alignment.rows) if(row.kind==='caption') assert.ok(row.value.length<=44);
              assert.equal(new Set(alignment.rows.map(row=>row.label+'  '+row.value)).size,alignment.rows.length,
                `${kind} ${key}: a repeated row`);
              assert.ok(alignment.values.length>0,`${kind} ${key} has no value cells.`);
              for(const cell of alignment.values) assert.equal(cell.align,'right',`${kind} ${key}: ${cell.text}`);
              for(const label of alignment.labels) assert.equal(label.align,'left',`${kind} ${key}: ${label.text}`);
              for(const group of new Set(alignment.values.map(cell=>cell.group))) {
                const rights=alignment.values.filter(cell=>cell.group===group).map(cell=>cell.right);
                assert.ok(Math.max(...rights)-Math.min(...rights)<=1,`${kind} ${key}: value edges differ.`);
              }
              assert.ok(alignment.left>=0 && alignment.right<=width && alignment.scroll<=alignment.client,
                `${kind} ${key}: `+JSON.stringify(alignment));
              if (comparisonImages) comparisons.push({tooltipAlignment:true,kind,key,theme,width,...alignment});
            }
            await show(readings.macroute_reason);
            await evaluate(`showTip(proofTip('wait', 2),${width/2},420)`);
            const tip = await evaluate(`(() => {
              const node=document.querySelector('#tip'), rect=node.getBoundingClientRect();
              return {text:node.innerText,left:rect.left,right:rect.right,scroll:node.scrollWidth,client:node.clientWidth};
            })()`);
            assert.ok(tip.text.includes('natedev only') && tip.text.includes(localReason),tip.text);
            assert.equal(tip.text.split(localReason).length-1,1,tip.text);
            assert.ok(!tip.text.includes('Mac: no,'),tip.text);
            const memory=await evaluate(`Object.fromEntries(tooltipRows(proofTip('wait', 2),latest).map(row=>[row.label,row.value]))`);
            // The queue admits one step at a time, so a step after the first shows only what it needs.
            assert.ok(memory.Needs && !('Free' in memory) && !('Short by' in memory),`A later step shows its room: ${tip.text}`);
            assert.ok(tip.left>=0 && tip.right<=width && tip.scroll<=tip.client,JSON.stringify(tip));
            if (comparisonImages) comparisons.push({localReason:true,theme,width,...tip});
          }
        }
        await saveScreenshot('direct-cargo-local-reason-dark-desktop');
        await capture('direct-cargo-local-reason-dark-tooltip','#tip');

        await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
        await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1100,deviceScaleFactor:1,mobile:false});
        await navigate('memory_live.html');
        const mockState = structuredClone(legendReading);
        const shotAt = mockState.read_at;
        const bytes = gib => gib * 1024**3;
        Object.assign(mockState,{cap:18.3,total:51.4,taken:18.3,used:10.2,reserved:8.1,room:0,shortfall:0});
        Object.assign(mockState.ram,{queued_builds_bytes:bytes(10.2),build_tools_bytes:0,ci_bytes:0,
          rust_analyzer_bytes:0,sccache_bytes:0,other_build_tools_bytes:0,
          safety_target_bytes:bytes(4),safety_covered_bytes:bytes(4),safety_missing_bytes:0,
          swap_target_bytes:bytes(2.1),swap_covered_bytes:bytes(2.1),swap_missing_bytes:0,
          build_covered_bytes:bytes(8.1),build_target_bytes:bytes(8.1),build_missing_bytes:0,
          available_bytes:bytes(14.2),total_bytes:bytes(51.4),cap_bytes:bytes(18.3),room_bytes:0,shortfall_bytes:0});
        mockState.build_reserve = {growth_bytes:bytes(10.125),share_percent:80,between_steps_bytes:0,unread_bytes:0};
        mockState.waiters = [
          {name:'fps test',unit:'fps',showrunner:'hana',since:shotAt-720,place:1,why:'memory',need:1,free:0,
            runs_on:'natedev or Mac',first_in_line:true,head_room:{unavailable:'',rows:[{label:'Queue cap',gib:18.3},
              {label:'− Builds',gib:10.2},
              {label:'− Build reserved',gib:8.1},{label:'= Free',gib:0}]}},
          {name:'cables build',unit:'cables',showrunner:'hana',since:shotAt-51*3600,
            place:2,why:'its turn',need:1,free:0,runs_on:'natedev only',because:localReason}];
        const waitingState = {...mockState,waiters:[{...mockState.waiters[1],place:1,why:'memory',
          first_in_line:true,because:'edits the merged checkout'}]};
        mockState.walk = [{name:'cables build',unit:'cables',showrunner:'hana',runner:'worker',worker:'codex2',
          started_at:shotAt-840,used:3.2,reserved:2,counted:5.2,queued:true,machine:'natedev',
          progress:{kind:'RowProgress',phase:'testing',done:412,total:993,percent:100*412/993,unit:'tests'}}];
        await show(mockState);
        const reserveDisplay = await evaluate(`(() => {
          const reading=structuredClone(latest);
          const {build,swap}=shownReading(reading).reserves;
          build.covered='8.2 GiB';
          swap.covered='2.2 GiB';
          window.renderReading(reading);
          return {
            cap:tooltipRows('cap',reading).find(row=>row.label==='+ Build reserved').value,
            swap:document.querySelector('#swap-reserve').textContent
          };
        })()`);
        assert.deepEqual(reserveDisplay,{cap:'8.2 GiB',swap:'Swap coming back: 2.2 GiB'});
        // Compare every global legend reference in both covered and shortfall states.
        for(const state of [mockState,readings.head_memory,readings.head_shortfall,readings.head_swap_shortfall]) {
          await show(state);
          const references = await evaluate(`(() => {
            const legend = Object.fromEntries([
              ...RAM_BLOCKS.map(([key,name])=>[name,document.getElementById('v-'+key).textContent]),
              ['Builds',document.querySelector('#v-g-builds').textContent],
              ['Free RAM',document.querySelector('#v-g-free').textContent],
              ['System reserved',document.querySelector('#v-system').textContent],
              ['Queue cap',document.querySelector('#v-cap').textContent],
              ['Everything else',document.querySelector('#v-g-else').textContent]]);
            const keys=Object.keys(TIPS).filter(key=>key!=='shortfall')
              .concat(latest.waiters.flatMap((row,index)=>row.head_room?[proofTip('wait',index)]:[]));
            return keys.flatMap(key=>tooltipRows(key,latest).flatMap(row=> {
              const label=row.label.replace(/^[+−=] /,'');
              return label in legend ? [{key,label,value:row.value,legend:legend[label]}] : [];
            }));
          })()`);
          for(const reference of references) assert.equal(reference.value,reference.legend,
            'A tooltip changes a legend amount: '+JSON.stringify(reference));
          const capSums=await evaluate(`Object.keys(TIPS).concat((latest.waiters||[]).map((row,index)=>proofTip('wait',index)))
            .map(key=>({key,rows:tooltipRows(key,latest)})).filter(tip=>tip.rows.some(row=>row.label==='Queue cap'))`);
          for(const tip of capSums) assert.ok(!tip.rows.some(row=>/Safety net|Swap coming back/.test(row.label)),
            'A cap sum includes a system reserve: '+tip.key);
          if (comparisonImages) comparisons.push({legendReferences:references});
        }
        await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
        const mockTips = [['queue-cap','cap'], ['waiting',null],
          ['first-in-line',proofTip('wait',0,mockState)], ['running',proofTip('row',0,mockState)],
          ['build-reserved','reserved'], ['long-reason',proofTip('wait',1,mockState)]];
        await show(mockState);
        for (const [name,mockKey] of gateMode ? mockTips.slice(0,1) : mockTips) {
          if (name==='waiting') await show(waitingState);
          const key = mockKey ?? proofTip('wait',0,waitingState);
          const shape = await evaluate(`(() => {
            showTip(${JSON.stringify(key)},700,700);
            const grid = document.querySelector('#tip > .tip');
            const style = getComputedStyle(grid);
            const labels = [...grid.querySelectorAll(':scope > dt')];
            const values = labels.map(label=>label.nextElementSibling);
            const long = grid.querySelector('.long');
            const box = grid.getBoundingClientRect();
            return {grids: grid.querySelectorAll('dl').length + 1, columns:style.gridTemplateColumns.split(' ').length,
              title:grid.querySelectorAll('dt.title').length, dark:matchMedia('(prefers-color-scheme: dark)').matches,
              labels:labels.map(cell=>[getComputedStyle(cell).textAlign,cell.getBoundingClientRect().left]),
              values:values.map(cell=>[getComputedStyle(cell).textAlign,cell.getBoundingClientRect().right]),
              fits:grid.scrollWidth<=grid.clientWidth && box.left>=0 && box.right<=innerWidth &&
                box.top>=0 && box.bottom<=innerHeight,
              long:long ? [getComputedStyle(long).textAlign,getComputedStyle(long).gridColumn] : null,
              rows:tooltipRows(${JSON.stringify(key)},latest)};
          })()`);
          const {rows, labels, values} = shape;
          assert.deepEqual([shape.grids,shape.columns,shape.title,shape.dark,shape.fits],[1,2,1,true,true],name);
          assert.ok(labels.every(([align,left])=>align==='left' && left===labels[0][1]),name);
          assert.ok(values.every(([align,right])=>align==='right' && right===values[0][1]),name);
          assert.ok(rows.every(({label,value})=>!label.includes(':') && !/[.!?]$/.test(value)),name);
          assert.equal(new Set(rows.map(({label,value})=>label+'  '+value)).size,rows.length,name);
          assert.deepEqual(shape.long,name==='long-reason' ? ['left','1 / -1'] : null,name);
          await capture(name,'#tip',comparisonImages || tipDesignComparison);
          console.log(JSON.stringify({tooltip:name,rows:rows.map(({label,value})=>[label,value]),dark:shape.dark}));
          const amounts=Object.fromEntries(rows.map(row=>[row.label,row.value]));
          if(name==='waiting') assert.deepEqual([amounts['Place in line'],amounts.Free,amounts['Short by']],
            ['1','0.0 GiB','1.0 GiB']);
          const tenths=memoryTenths;
          if(amounts.Free||amounts['= Free']) assert.equal(tenths(amounts['Short by']||'0'),
            Math.max(0,tenths(amounts.Needs)-tenths(amounts.Free||amounts['= Free'])));
          if(name==='first-in-line') assert.equal(tenths(amounts['= Free']),
            tenths(amounts['Queue cap'])-tenths(amounts['− Builds'])-tenths(amounts['− Build reserved']));
          if(name==='running') assert.equal(tenths(amounts['= In queue cap']),tenths(amounts.Uses)+tenths(amounts['+ Build reserved']));
          if(name==='queue-cap') assert.equal(tenths(amounts['Queue cap']),
            tenths(amounts.Builds)+tenths(amounts['+ Build reserved'])+tenths(amounts['+ Free']));
          if(name==='build-reserved') assert.equal(tenths(amounts['= Target']),
            Math.round(tenths(amounts['Room to grow'])*parseFloat(amounts['× Share held'])/100));
          if (comparisonImages) comparisons.push({mock:name,rows});
          if (name==='waiting') await show(mockState);
        }

        // Both swap images use the same current reading and dark desktop viewport.
        mockState.swap.reserve_share_percent=20;
        for(const version of ['before','after']) {
          if(version==='before' && !swapBeforePage) continue;
          await navigate(version==='before'?'swap-before.html':'memory_live.html');
          await show(mockState);
          await evaluate("showTip('swap',700,700)");
          const rows=version==='after' ? await evaluate("tooltipRows('swap',latest)")
            : await evaluate("document.querySelector('#tip').innerText");
          if(version==='after') {
            const share=rows.findIndex(row=>row.label==='Share held');
            assert.equal(rows[share].value,'20.0%');
            assert.ok(!rows.some(row=>row.label==='Target'));
            assert.equal(rows.find(row=>row.kind==='title').value,'2.1 GiB');
            const alignment=await evaluate("getComputedStyle(document.querySelector('#tip dt').nextElementSibling).textAlign");
            assert.equal(alignment,'right');
          }
          await capture('swap-'+version,'#tip');
          if (comparisonImages) comparisons.push({swapShare:version,rows});
        }

        // A gate job's tooltip says what the job does and what it changes, below its title (#109).
        await call('Emulation.setEmulatedMedia', {features:[{name:'prefers-color-scheme',value:'dark'}]});
        await call('Emulation.setDeviceMetricsOverride',{width:1400,height:1100,deviceScaleFactor:1,mobile:false});
        const gateReading=structuredClone(base);
        const gateIndex=gateReading.walk.findIndex(row=>row.queued);
        assert.ok(gateIndex>=0,'The base reading has no queued run.');
        const gateAbout='merges the handler checkpoint into the sdlc branch, tests every module, then pushes sdlc';
        const gateRow=gateReading.walk[gateIndex];
        Object.assign(gateRow,{name:'natemerge integrate',unit:'natemerge',title:{unit:'natemerge',step:'integrate'},
          about:gateAbout,changes:'sdlc branch',showrunner:'sdlc',machine:'natedev',used:6.2,reserved:2.8,counted:9,
          started_at:gateReading.read_at-600});
        delete gateRow.caller;
        // The user, 11-16: "Uses 0.0 GiB? ... has to be a bug". The gate row shows a real use, not memory parts.
        delete gateRow.parts;
        await show(gateReading);
        await evaluate(`showTip(proofTip('row', ${gateIndex}),700,420)`);
        const gateTip=await evaluate("[...document.querySelectorAll('#tip dt, #tip dd')].map(cell=>cell.textContent)");
        assert.ok(gateTip[0].startsWith('natemerge · integrate'),'The gate tooltip title: '+gateTip[0]);
        assert.ok(gateTip.includes(gateAbout),'The gate tooltip omits its description.');
        assert.equal(gateTip[gateTip.indexOf('Changes')+1],'sdlc branch');
        assert.equal(gateTip[gateTip.indexOf('Uses')+1],'6.2 GiB');
        const gateFit=await evaluate(`[...document.querySelectorAll('#tip dt, #tip dd')].every(cell=>cell.scrollWidth<=cell.clientWidth)
          && document.querySelector('#tip .tip').scrollWidth<=document.querySelector('#tip .tip').clientWidth`);
        assert.ok(gateFit,'The gate tooltip clips its text.');
        await capture('gate-tooltip-dark-desktop','#tip');
        const rowAskWidth = rowAskComparison ? 1440 : 1400;
        const dataColumns = () => evaluate(`[...document.querySelectorAll('.walk-table th[scope="col"]:not(.row-action)')].map(cell => {
          const box = cell.getBoundingClientRect();
          return {text:cell.textContent,left:box.left,width:box.width};
        })`);
        let beforeColumns;
        if (rowAskComparison) {
          assert.ok(beforePage, 'The row comparison needs the previous page.');
          await call('Emulation.setDeviceMetricsOverride', {width:rowAskWidth,height:1100,deviceScaleFactor:1,mobile:false});
          servedReading = gateReading;
          await navigate('before.html');
          assert.equal(await evaluate("document.querySelectorAll('.row-add').length"), 0);
          beforeColumns = await dataColumns();
          await capture('before-rows-dark','#walk');
          await navigate('memory_live.html');
        }
        const gateButton = `'tr[data-tip="' + proofTip('row', ${gateIndex}) + '"] .row-add'`;
        assert.deepEqual(await evaluate(`(() => {
          const button = document.querySelector(${gateButton});
          return {label:button.getAttribute('aria-label'),icon:!!button.querySelector('.lucide-plus'),
            queued:document.querySelectorAll('tr.wait .row-add').length};
        })()`), {label:'New item for natemerge integrate',icon:true,queued:gateReading.waiters.length});
        const actions = await evaluate(`[...document.querySelectorAll('.row-add')].map(button => {
          const cell = button.closest('td');
          const box = button.getBoundingClientRect();
          return {last:cell === cell.parentElement.lastElementChild, column:cell.className,
            left:box.left, right:box.right, width:box.width};
        })`);
        assert.ok(actions.length > 1);
        for (const action of actions) {
          assert.equal(action.last, true);
          assert.equal(action.column, 'row-action');
          assert.equal(action.left, actions[0].left);
          assert.equal(action.width, 22);
          assert.ok(action.right <= rowAskWidth, 'The action column is outside the desktop view.');
        }
        if (rowAskComparison) {
          assert.deepEqual(await dataColumns(), beforeColumns, 'The action column moved a data column.');
          await capture('after-rows-dark','#walk');
        }
        const rowAskStep = async (step, expression) => {
          let timer;
          try {
            return await Promise.race([evaluate(expression), new Promise((_, reject) => {
              timer = setTimeout(() => reject(new Error(step + ' timed out.')), 5000);
            })]);
          } finally {
            clearTimeout(timer);
          }
        };
        const openRowAsk = () => rowAskStep('Load New item recipients', `(async () => {
          const dialog = document.querySelector('#review-new-item');
          const loaded = new Promise(resolve => {
            const options = dialog.querySelector('[data-recipients]');
            const observer = new MutationObserver(() => {
              if (options.querySelector('input')) { observer.disconnect(); resolve(); }
            });
            observer.observe(options, {childList:true});
          });
          document.querySelector(${gateButton}).click();
          await loaded;
          const preview = dialog.querySelector('.new-item-evidence .tip');
          const tooltip = tooltipHtml(proofTip('row', ${gateIndex}), latest);
          const bounds = dialog.getBoundingClientRect();
          const regions = [dialog.querySelector('.new-item-body'), preview, ...preview.children];
          return {open:dialog.matches(':modal'),
            recipient:dialog.querySelector('[name="recipient_id"]:checked').value,
            kind:dialog.querySelector('[name="kind"]:checked').value, sameTip:preview.outerHTML === tooltip,
            fits:bounds.left >= 0 && bounds.right <= innerWidth && bounds.top >= 0 && bounds.bottom <= innerHeight &&
              regions.every(region => region.scrollWidth <= region.clientWidth),
            evidence:dialog.querySelector('.new-item-evidence').textContent};
        })()`);
        const checkRowAsk = dialog => {
          assert.deepEqual({...dialog,evidence:undefined},
            {open:true,recipient:'fixture-builds',kind:'question',sameTip:true,fits:true,evidence:undefined});
          assert.ok(dialog.evidence.includes(gateAbout) && dialog.evidence.includes('Overall memory'));
        };
        checkRowAsk(await openRowAsk());
        if (rowAskComparison) await capture('after-dialog-dark','#review-new-item');
        await rowAskStep('Send New item evidence', `(async () => {
          const dialog = document.querySelector('#review-new-item');
          const created = new Promise(resolve => dialog.addEventListener('review-item-created', resolve, {once:true}));
          const words = dialog.querySelector('textarea');
          words.value = 'Can we check this gate?';
          words.dispatchEvent(new Event('input', {bubbles:true}));
          dialog.querySelector('[type="submit"]').click();
          await created;
        })()`);
        assert.equal(submittedItem.recipient_id, 'fixture-builds');
        assert.equal(submittedItem.words, 'Can we check this gate?');
        const evidence = submittedItem.evidence;
        assert.equal(evidence.kind, 'build-step');
        assert.equal(evidence.step_id, gateRow.key);
        assert.equal(evidence.title, gateRow.name);
        assert.equal(evidence.owner, gateRow.showrunner);
        assert.ok(evidence.cells.some(cell => cell.label === 'Target' && cell.value === 'natedev'));
        assert.ok(evidence.cells.every(cell => cell.label), 'Evidence includes the action column.');
        assert.ok(evidence.memory.segments.some(segment => segment.label === 'Queued builds' && segment.gib > 0));
        assert.ok(evidence.tip.some(entry => entry.label === 'Changes' && entry.value === 'sdlc branch'));
        assert.ok(evidence.tip.some(entry => entry.kind === 'detail' && entry.text === gateAbout));
        if (!gateMode) {
          await call('Emulation.setDeviceMetricsOverride', {width:390,height:844,deviceScaleFactor:1,mobile:false});
          const mobileActions = await evaluate(`(async () => {
            await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            const scroll = document.querySelector('.walk-scroll');
            const buttons = [...scroll.querySelectorAll('.row-add')];
            return {width:innerWidth, pageFits:document.documentElement.scrollWidth <= innerWidth,
              buttons:buttons.map(button => {
                button.scrollIntoView({block:'center',inline:'nearest'});
                scroll.scrollLeft = scroll.scrollWidth;
                const bounds = button.getBoundingClientRect(), clip = scroll.getBoundingClientRect();
                const hit = document.elementFromPoint(bounds.left + bounds.width / 2, bounds.top + bounds.height / 2);
                const visible = bounds.width === 22 && bounds.height === 22 && bounds.left >= clip.left &&
                  bounds.right <= clip.right && bounds.left >= 0 && bounds.right <= innerWidth && button.contains(hit);
                return {visible,bounds:bounds.toJSON(),clip:clip.toJSON(),hit:hit?.tagName};
              })};
          })()`);
          assert.equal(mobileActions.width, 390);
          assert.ok(mobileActions.pageFits && mobileActions.buttons.length === actions.length &&
            mobileActions.buttons.every(button => button.visible),
            'A plus button is clipped or the page overflows at 390 px. ' + JSON.stringify(mobileActions));
          await evaluate(`document.querySelector(${gateButton}).scrollIntoView({block:'center',inline:'nearest'})`);
          checkRowAsk(await openRowAsk());
          await evaluate("document.querySelector('#review-new-item [data-cancel]').click()");
          await call('Emulation.setDeviceMetricsOverride',
            {width:rowAskWidth,height:1100,deviceScaleFactor:1,mobile:false});
        }
        if (awsCostsComparison) {
          servedReading = readings.aws_costs;
          await navigate('memory_live.html');
          await show(readings.aws_costs);
          await captureAwsCosts(evaluate, call);
        }
        if (comparisonImages) {
          await show(base);
          await evaluate('hideTip()');
          const endClip=await evaluate(`(() => {
            const start=document.querySelector('#swap').getBoundingClientRect();
            const bottom=document.querySelector('main').getBoundingClientRect().bottom;
            return {x:0,y:start.top+scrollY,width:innerWidth,height:bottom-start.top+24,scale:1};
          })()`);
          await saveScreenshot('after-dark-desktop',{captureBeyondViewport:true,clip:endClip});
        }

        if (comparisonImages) {
          await writeFile(join(folder,'proof.json'),JSON.stringify({proofs,comparisons,
            failures:failures.map(({cards,...state})=>state)},null,2)+'\n');
        }
        console.log(JSON.stringify({pass:true,views:themes.length*widths.length,...(callerFolder ? {output:folder} : {})}));
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

"""The shared page status keeps state, age and request timing together."""
from __future__ import annotations

import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from typing import cast, override

from scripts.review_links import page_frame
from scripts.lib import conf
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS

NO_DATA_FILE: dict[str, object] = {}


class PageStatusTests(unittest.TestCase):
    @override
    def setUp(self) -> None:
        _ = cast(object, self.enterContext(patch.dict(os.environ, {"CONF_DIR": str(Path(__file__).resolve().parents[2] / "config")})))

    def test_title_places_one_status_directly_after_the_title(self) -> None:
        for page in ('accounts', 'aws', 'builds', 'changes', 'disks', 'images', 'investigations', 'list', 'questions'):
            with self.subTest(page=page):
                markup = page_frame.title(page.title(), page)
                self.assertIn('</h1><p class="page-status"', markup)
                self.assertEqual(markup.count('data-page-status '), 1)
                self.assertIn('role="status" aria-live="off"', markup)
                self.assertEqual(markup.count('data-status-icon='), 5)
                self.assertIn('aria-live="polite" data-status-announcement', markup)

    def test_each_page_uses_the_same_states_and_retains_the_last_good_read(self) -> None:
        for page in ('accounts', 'aws', 'builds', 'changes', 'disks', 'images', 'investigations', 'list', 'questions'):
            with self.subTest(page=page):
                assert_component_states(self, page_frame.title(page.title(), page))

    def test_only_the_configured_status_pages_have_age_limits(self) -> None:
        limits = {key for key in conf.read_or_repository("console").values() if key.startswith("status.") and key.endswith(".age_limit_seconds")}
        self.assertEqual(limits, {f"status.{page}.age_limit_seconds" for page in (
            "accounts", "aws", "builds", "changes", "disks", "images", "investigations", "list", "questions", "review")})

    def test_invalid_initial_facts_report_failed_without_an_unhandled_rejection(self) -> None:
        markup = page_frame.title("AWS", "aws")
        match = re.search(r'data-status-options="([^"]+)"', markup)
        assert match is not None
        for value in ('{readAt:now/1000,renderSeconds:NaN}', '{readAt:NaN,renderSeconds:.1}'):
            with self.subTest(initial=value):
                source = self.environment(html.unescape(match[1])) + page_frame.REFRESH_SCRIPT + """
createPageRefresh(async()=>null,()=>0,()=>'',INITIAL);
setImmediate(()=>{
 if(line.dataset.state!=='Failed' || parts.render.textContent.includes('NaN')) process.exitCode=1;
 else console.log('PASS');
});
""".replace('INITIAL', value)
                node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
                done = subprocess.run([node, '--unhandled-rejections=strict', '-e', source],
                                      text=True, capture_output=True, timeout=20)
                self.assertEqual((done.returncode, done.stdout.strip()), (0, 'PASS'), done.stderr)

    def test_failed_visibility_refresh_keeps_initial_facts_before_the_first_frames(self) -> None:
        markup = page_frame.title("Investigations", "investigations")
        match = re.search(r'data-status-options="([^"]+)"', markup)
        assert match is not None
        environment = self.environment(html.unescape(match[1])).replace(
            'const requestAnimationFrame=run=>run();',
            'const frames=[];const requestAnimationFrame=run=>frames.push(run);')
        source = environment + page_frame.REFRESH_SCRIPT + r"""
(async()=>{
 visible='hidden';
 createPageRefresh(async()=>{throw new Error('HTTP 502');},()=>15,()=>'',
  {readAt:now/1000-10,renderSeconds:.1});
 if(frames.length!==1) throw new Error('The initial frames must remain pending.');
 visible='visible';handlers.visibilitychange();
 for(let i=0;i<12;i++) await Promise.resolve();
 if(line.dataset.state!=='Failed' || parts.age.hidden || parts.age.textContent!=='10 s ago')
  throw new Error('A failed visibility refresh must keep the initial data time.');
 frames.shift()();frames.shift()();
 for(let i=0;i<12;i++) await Promise.resolve();
 if(line.dataset.state!=='Failed' || parts.age.hidden || parts.age.textContent!=='10 s ago')
  throw new Error('The older initial frames must not replace the failed refresh.');
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
"""
        node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
        done = subprocess.run([node, '--unhandled-rejections=strict', '-e', source],
                              text=True, capture_output=True, timeout=20)
        self.assertEqual((done.returncode, done.stdout.strip()), (0, 'PASS'), done.stderr)

    @staticmethod
    def environment(options: str) -> str:
        return 'const options=' + json.dumps(options) + r''';
let now=1794847200000,perf=0,visible='visible',id=0;
const handlers={},timeouts=new Map(),intervals=new Map(),frameElement={hidden:false};
function node(){return {hidden:false,textContent:'',title:'',dataset:{},
 classList:{values:new Set(),toggle(key,on){if(on)this.values.add(key);else this.values.delete(key);},has(key){return this.values.has(key);}}};}
const parts=Object.fromEntries(['word','age','trip','render','reason','announcement'].map(key=>[key,node()]));
const icons=['Live','Reading','Late','Failed','Paused'].map(state=>({...node(),dataset:{statusIcon:state}}));
const line={dataset:{statusOptions:options},querySelector:selector=>parts[selector.match(/data-status-(\w+)/)[1]],
 querySelectorAll:()=>icons};
const document={visibilityState:'visible',querySelector:()=>line,addEventListener:(key,run)=>handlers[key]=run};
Object.defineProperty(document,'visibilityState',{get:()=>visible});
const window={frameElement,addEventListener:(key,run)=>handlers[key]=run},parent={},location={origin:'http://fixture.invalid'};
Date.now=()=>now;
const performance={now:()=>perf,getEntriesByType:()=>[]};
const setTimeout=run=>{timeouts.set(++id,run);return id;},clearTimeout=key=>timeouts.delete(key);
const setInterval=run=>{intervals.set(++id,run);return id;},clearInterval=key=>intervals.delete(key);
const requestAnimationFrame=run=>run();
'''


def assert_component_states(case: unittest.TestCase, markup: str) -> None:
    """Check the states with the settings from a page's rendered header."""
    node = shutil.which('node') or str(next(Path('/nix/store').glob('*nodejs*/bin/node')))
    match = re.search(r'data-status-options="([^"]+)"', markup)
    assert match is not None
    options = html.unescape(match[1])
    source = PageStatusTests.environment(options) + page_frame.REFRESH_SCRIPT + r'''
(async()=>{
 const checks=[],check=(ok,label)=>{if(!ok) checks.push(label);};
 const settle=async()=>{for(let i=0;i<12;i++) await Promise.resolve();};
 let result={readAt:now/1000,renderSeconds:.03,renderMedian:.02,renderCount:10},hold='',release;
 let load=async()=>result;
 const refresh=createPageRefresh((value,current)=>load(value,current),()=>15,()=>hold);
 check(line.dataset.state==='Reading','the first read has no data');
 check(parts.age.hidden && parts.trip.hidden && parts.render.hidden,'the first read shows only Reading');
 await refresh.refresh();
 check(line.dataset.state==='Live','a good read is Live');
 check(parts.age.textContent==='0 s ago','the age starts at the data time');
 check(parts.render.textContent==='render 0.03 s','the render time comes from the server');
 check(parts.trip.textContent==='round trip 0.00 s','the round trip starts at the request');
 check(parts.render.title==='Median 0.02 s of the last 10 renders.','the median is hover text');
 const announcement=parts.announcement.textContent;
 now+=4000;refresh.changed();
 check(parts.age.textContent==='4 s ago','the age advances without a read');
 check(parts.announcement.textContent===announcement,'age updates do not speak');
 load=()=>new Promise(resolve=>release=resolve);const slow=refresh.refresh();
 [...timeouts.values()].forEach(run=>run());
 check(line.dataset.state==='Reading' && !parts.age.hidden && !parts.render.hidden,'a slow read keeps the old facts');
 perf+=210;release(result);await slow;
 check(parts.trip.textContent==='round trip 0.21 s','the round trip includes the complete read');
 now+=(JSON.parse(line.dataset.statusOptions).ageLimit+61)*1000;refresh.changed();
 check(line.dataset.state==='Late','old data is Late');
 check(parts.age.textContent.includes('min ago') && parts.reason.textContent==='The feed has stopped.','Late keeps time before its reason');
 load=async()=>{throw new Error('HTTP 502');};await refresh.refresh();
 check(line.dataset.state==='Failed' && parts.reason.textContent==='HTTP 502','a failed read has its reason');
 check(!parts.age.hidden && parts.trip.hidden && parts.render.hidden,'Failed keeps data time');
 hold='You have text in a field.';refresh.changed();
 check(line.dataset.state==='Paused' && parts.reason.textContent===hold,'a held page is Paused before Failed');
 result={readAt:now/1000,renderSeconds:5};load=async()=>result;await refresh.refresh();
 check(line.dataset.state==='Paused','new data waits while held');
 hold='';refresh.changed();await settle();
 check(line.dataset.state==='Live' && parts.render.textContent==='render 5.00 s','a released page uses its held data');
 check(parts.render.classList.has('page-status-slow'),'a slow render uses the warning color');
 let calls=0;load=async()=>{calls++;return result;};visible='hidden';await refresh.refresh();
 check(calls===0 && line.dataset.state==='Paused','a hidden tab does not read');
 visible='visible';handlers.visibilitychange();await settle();check(calls===1,'a visible tab reads at once');
 frameElement.hidden=true;await refresh.refresh();check(calls===1,'a hidden console frame does not read');
 frameElement.hidden=false;
 const pending=[],currentReads=[];load=(value,current)=>{currentReads.push(current);return new Promise(resolve=>pending.push(resolve));};
 const older=refresh.refresh(),skip=refresh.refresh(),newer=refresh.refresh(true);
 check(pending.length===2,'timed reads do not overlap and an explicit read can supersede');
 check(!currentReads[0]() && currentReads[1](),'loaders can refuse an older response before changing content');
 pending[1]({readAt:now/1000,renderSeconds:.04});await newer;
 const following=refresh.refresh();
 check(pending.length===3,'a completed replacement permits a read while the old request remains pending');
 pending[2]({readAt:now/1000,renderSeconds:.04});await following;
 pending[0]({readAt:1,renderSeconds:8});await older;await skip;
 check(parts.render.textContent==='render 0.04 s','an older response cannot replace a newer response');
 if(checks.length) throw new Error(checks.join('\n'));
 console.log('PASS');
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
    done = subprocess.run([node, '-e', source], capture_output=True, text=True, timeout=20)
    case.assertEqual(done.returncode, 0, done.stdout + done.stderr)
    case.assertEqual(done.stdout.strip(), 'PASS')



def assert_status_states(case: unittest.TestCase, markup: str, period_seconds: float,
                         reading: dict[str, object] = NO_DATA_FILE, failed_markup: str = "") -> None:
    """Run the complete page with its loader and its configured request period."""
    setup = r'''<script>
const nativeFetch=fetch, nativeTimeout=setTimeout, intervals=new Map();
let timerId=0, wall=Date.now(), perf=0, statusVisibility='visible', mode='good', reads=0, measureFails=false, saveFails=false;
const pending=[], errors=[];
const original=MARKUP, savedReading=READING, expectedPeriod=PERIOD, failedPage=FAILED_PAGE;
if(original.includes('<title>Builds</title>')) mode='retained';
if(original.includes('<title>Disks</title>')) measureFails=true;
Date.now=()=>wall;
Object.defineProperty(performance,'now',{value:()=>perf});
Object.defineProperty(document,'visibilityState',{get:()=>statusVisibility});
window.setInterval=(run,ms)=>{intervals.set(++timerId,{run,ms});return timerId;};
window.clearInterval=key=>intervals.delete(key);
window.requestAnimationFrame=run=>nativeTimeout(()=>run(perf),0);
addEventListener('error',event=>errors.push(event.message));
addEventListener('unhandledrejection',event=>errors.push(String(event.reason)));
const payload=()=>({...savedReading,read_at:wall/1000,measured_at:wall/1000,render_seconds:.42});
function response(status=200){
 const doc=new DOMParser().parseFromString(mode==='cached' ? failedPage : original,'text/html');
 const facts=doc.querySelector('[data-page-status]');
 facts.dataset.readAt=wall/1000;facts.dataset.renderSeconds=.42;
 const root=doc.querySelector('[data-accounts],[data-review-content],[data-images],[data-investigations],[data-list],[data-review]');
 if(root) root.dataset.loaded='true';
 const aws=doc.querySelector('#aws-prices-data');
 if(aws) aws.textContent=JSON.stringify({...JSON.parse(aws.textContent),read_at:wall/1000,render_seconds:.42});
 return {ok:status===200,status,text:async()=>doc.documentElement.outerHTML,json:async()=>payload(),
  headers:{get:()=>null}};
}
window.fetch=async(url,options={})=>{
 if(options.method==='HEAD') return response();
 if(String(url).includes('report.json')) return {ok:false,status:404};
 if(options.method==='POST' && String(url)!=='measure')
  return saveFails ? {...response(502),json:async()=>({reason:'HTTP 502'})} : response();
 if(String(url)==='measure') return {ok:!measureFails,status:measureFails ? 502 : 200,
  json:async()=>({state:'current',reason:measureFails ? 'HTTP 502' : ''})};
 reads++;
 if(mode==='pending') return new Promise(resolve=>pending.push(resolve));
 if(mode==='failed') return response(502);
 if(mode==='missing') return response(404);
 if(mode==='retained') return {...response(),json:async()=>({...payload(),read_error:'queue unavailable'})};
 if(mode==='invalid') return {...response(),text:async()=>
  (await response().text()).replace('data-render-seconds="0.42"','data-render-seconds="bad"')};
 return response();
};
</script>'''
    setup = (setup.replace('MARKUP', json.dumps(markup).replace('</', '<\\/'))
             .replace('READING', json.dumps(reading).replace('</', '<\\/'))
             .replace('PERIOD', str(period_seconds * 1000))
             .replace('FAILED_PAGE', json.dumps(failed_markup).replace('</', '<\\/')))
    probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 const flush=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>nativeTimeout(resolve,0))));
 const line=document.querySelector('[data-page-status]');
 const part=name=>line.querySelector('[data-status-'+name+']');
 const age=()=>part('age').textContent;
 const state=()=>line.dataset.state;
 const shown=()=>dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 const requestTimer=()=>[...intervals.values()].find(timer=>timer.ms===expectedPeriod);
 const clock=()=>[...intervals.values()].find(timer=>timer.ms===1000);
 const read=async()=>{if(expectedPeriod){perf+=expectedPeriod+1;requestTimer().run();}else shown();await flush();};
 try{
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  await flush();
  if(mode==='retained' || measureFails){
   check(state()==='Failed' && !part('age').hidden && age()==='0 s ago',
    'a first failed read with shown data keeps its data time');
   mode='good';measureFails=false;
  }
  shown();await flush();
  check(reads>0 && state()==='Live','the real loader completes a good read');
  check(part('render').textContent==='render 0.42 s','the real loader supplies render time');
  const root=document.querySelector('[data-accounts],[data-review-content],[data-images],[data-investigations],[data-list],[data-review]');
  if(root) check(root.dataset.loaded==='true','the real loader replaces page content');
  if(expectedPeriod) check(!!requestTimer(),'the real request period runs');
  else check([...intervals.values()].every(timer=>timer.ms===1000),'the zero-period page has only its age timer');
  const count=reads;
  wall+=(JSON.parse(line.dataset.statusOptions).ageLimit+65)*1000;clock().run();
  check(state()==='Late' && age().includes('min ago'),'the shown data becomes Late');
  check(reads===count,'the age clock does not read');
  mode='failed';await read();
  check(state()==='Failed' && !part('age').hidden && part('reason').textContent==='HTTP 502',
   'a real failed read retains the shown data time');
  mode='good';await read();
  check(state()==='Live','a later real read recovers');
  if(document.querySelector('[data-review-content]')){
   const field=document.querySelector('textarea,input[type=text]');
   if(field){field.value='Keep this text.';field.dispatchEvent(new Event('input',{bubbles:true}));
    await read();check(state()==='Paused','a real review field holds the page');
    field.value='';field.dispatchEvent(new Event('input',{bubbles:true}));await flush();
    check(state()==='Live','clearing the review field applies its held page');}
  }
  if(expectedPeriod){
   mode='pending';await read();shown();await flush();
   check(pending.length===2,'a visible page supersedes the unresolved read');
   pending[1](response());await flush();
   const before=reads;await read();
   check(reads===before+1,'the next scheduled read ignores the superseded request');
   pending[2]?.(response());await flush();pending[0](response());await flush();mode='good';
  }
  const failedSaveForm=document.querySelector('[data-read-form]');
  if(failedSaveForm){
   mode='pending';saveFails=true;const first=pending.length;await read();
   failedSaveForm.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();
   check(failedSaveForm.querySelector('[data-save-status]').textContent==='Not saved: HTTP 502',
    'the failed investigation save retains its error');
   const before=reads;await read();
   check(reads===before+1,'a failed investigation save permits the next scheduled read while the older read remains pending');
   pending[first+1]?.(response());await flush();pending[first](response());await flush();
   mode='good';saveFails=false;
  }
  const readForm=document.querySelector('[data-read-form]');
  if(readForm){
   mode='pending';const first=pending.length;await read();
   readForm.dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}));await flush();
   check(pending.length===first+2,'a saved investigation supersedes its older read');
   pending[first+1](response());await flush();
   const before=reads;await read();
   check(reads===before+1,'a saved investigation permits a scheduled read while the older read remains pending');
   pending[first+2]?.(response());await flush();pending[first](response());await flush();mode='good';
  }
  if(root){
   mode='invalid';await read();
   check(state()==='Failed' && part('reason').textContent==='The data has no render time.',
    'a real HTML read rejects an invalid render time');
   mode='good';await read();
  }
  if(failedPage){
   mode='cached';await read();
   check(state()==='Failed' && !part('age').hidden && part('reason').textContent==='The Accounts render failed.',
    'a cached Accounts response reports its render failure');
   check(document.querySelector('[data-accounts]').textContent.includes('board'),
    'the Accounts board stays visible after a render failure');
   mode='good';await read();check(state()==='Live','a successful Accounts render clears failure');
  }
  if(document.title==='Builds'){
   mode='retained';await read();
   check(state()==='Failed' && !part('age').hidden && age()==='0 s ago',
    'a retained Builds reading has its data time after a queue failure');
   check(document.querySelector('#v-cap').textContent!=='unavailable','the retained Builds data remains visible');
  }
  if(document.title==='Disks'){
   check(document.querySelector('#drives .card')!==null,'the cached drive remains visible');
   mode='missing';const kept=document.querySelector('#drives').innerHTML;await read();
   check(state()==='Failed' && part('reason').textContent==='HTTP 404','a missing Disks file reports failure');
   check(document.querySelector('#drives').innerHTML===kept,'a missing Disks file keeps the shown drives');
   mode='good';measureFails=true;await read();
   check(state()==='Failed' && !part('age').hidden && age()==='0 s ago',
    'a failed Disks measure retains the time of the shown data');
  }
  statusVisibility='hidden';document.dispatchEvent(new Event('visibilitychange'));await flush();
  check(state()==='Paused','a hidden page pauses');
 }catch(error){failures.push(String(error));}
 failures.push(...errors);
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
    page = markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>').encode()
    results: list[list[str]] = []
    complete = threading.Event()

    class BrowserProbe(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            if self.path == '/complete':
                _ = complete.wait(TEST_HANG_GUARD_SECS)
            elif self.path == '/':
                _ = self.wfile.write(b'<iframe src="/page"></iframe><img hidden src="/complete">')
            else:
                _ = self.wfile.write(page)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers['Content-Length']))
            results.append(cast(list[str], json.loads(body)))
            complete.set()
            self.send_response(204)
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(('127.0.0.1', 0), BrowserProbe) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            profile = Path(temporary) / 'profile'
            profile.mkdir()
            done = subprocess.run(['firefox', '--headless', '--no-remote', '--profile', str(profile),
                                   '--screenshot', str(Path(temporary) / 'page.png'),
                                   f'http://127.0.0.1:{server.server_port}/'],
                                  capture_output=True, text=True, timeout=45)
            case.assertEqual(done.returncode, 0, done.stderr)
        finally:
            server.shutdown()
            thread.join(TEST_HANG_GUARD_SECS)
    case.assertEqual(results, [[]], 'The real page loader must complete every status check.')


if __name__ == '__main__':
    _ = unittest.main()

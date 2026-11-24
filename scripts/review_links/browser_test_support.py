"""Share browser and status test helpers across console pages."""
from __future__ import annotations

import base64
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread
from typing import cast, override
from unittest.mock import patch
from uuid import uuid4

from scripts.lib import conf
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS


DEFAULT_VIEWPORT = (1366, 768)
CHECK_INTERVAL = 0.1


def reap_firefox(root: Path) -> None:
    """Stop the browser group and remove its profile when the test pipe closes."""
    root.mkdir(mode=0o700)
    try:
        profile = root / "profile"
        profile.mkdir()
        _ = (profile / "user.js").write_text(
            'user_pref("marionette.port", 0);\n'
            + 'user_pref("browser.sessionhistory.max_total_viewers", 0);\n')
        with (root / "firefox.log").open("wb") as log:
            process = subprocess.Popen(
                ["firefox", "--headless", "--no-remote", "--profile", str(profile),
                 "--marionette", "--remote-allow-system-access", "about:blank"],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, process_group=0)
        try:
            while process.poll() is None:
                readable, _, _ = select.select([sys.stdin], [], [], CHECK_INTERVAL)
                if readable:
                    break
        finally:
            with suppress(ProcessLookupError):
                send_process_group(process.pid, signal.SIGKILL)
            _ = process.wait()
    finally:
        with suppress(FileNotFoundError):
            shutil.rmtree(root)


class Firefox:
    def __init__(self, root: Path, reaper: subprocess.Popen[bytes], deadline: float) -> None:
        self.resources: ExitStack = ExitStack()
        self.closed: bool = False
        self.reaper: subprocess.Popen[bytes] = reaper
        self.deadline: float = deadline
        self.buffer: bytearray = bytearray()
        self.log: Path = root / "firefox.log"
        assert reaper.stdin is not None
        _ = self.resources.enter_context(reaper.stdin)
        try:
            port = self.read_port(root / "profile" / "MarionetteActivePort")
            self.connection: socket.socket = self.resources.enter_context(
                socket.create_connection(("127.0.0.1", port), timeout=self.remaining()))
            hello = self.read()
            if not isinstance(hello, dict) or cast(dict[str, object], hello).get("applicationType") != "gecko":
                raise RuntimeError(f"Firefox sent an invalid Marionette greeting: {hello}")
            self.sequence: int = 0
            _ = self.command("WebDriver:NewSession", capabilities={"alwaysMatch": {"pageLoadStrategy": "none"}})
            initial = cast(dict[str, str], self.command("WebDriver:GetWindowHandle"))
            self.initial: str = initial["value"]
        except BaseException:
            self.close()
            raise

    def read_port(self, port_file: Path) -> int:
        while True:
            with suppress(FileNotFoundError):
                if port_text := port_file.read_text():
                    port = int(port_text)
                    if not 0 < port <= 65535:
                        raise RuntimeError(f"Firefox reported an invalid Marionette port: {port}")
                    return port
            time.sleep(min(0.02, self.remaining()))

    def close(self) -> None:
        self.closed = True
        self.resources.close()

    def remaining(self) -> float:
        if self.reaper.poll() is not None:
            raise ConnectionError("Firefox exited during the probe")
        seconds = self.deadline - time.monotonic()
        if seconds <= 0:
            raise TimeoutError("Firefox probe exceeded its deadline")
        return seconds

    def receive(self) -> None:
        self.connection.settimeout(self.remaining())
        data = self.connection.recv(65536)
        if not data:
            raise ConnectionError("Firefox closed the Marionette connection")
        self.buffer.extend(data)

    def read(self) -> object:
        while (separator := self.buffer.find(b":")) == -1:
            self.receive()
        start = separator + 1
        end = start + int(self.buffer[:separator])
        while len(self.buffer) < end:
            self.receive()
        result = cast(object, json.loads(self.buffer[start:end]))
        del self.buffer[:end]
        return result

    def command(self, method: str, **parameters: object) -> object:
        self.sequence += 1
        message = json.dumps([0, self.sequence, method, parameters]).encode()
        self.connection.settimeout(self.remaining())
        self.connection.sendall(str(len(message)).encode() + b":" + message)
        kind, sequence, error, result = cast(list[object], self.read())
        if kind != 1 or sequence != self.sequence or error is not None:
            raise RuntimeError(f"{method}: {error}")
        return result

    def script(self, source: str, *arguments: object, asynchronous: bool = False) -> object:
        method = "WebDriver:ExecuteAsyncScript" if asynchronous else "WebDriver:ExecuteScript"
        result = cast(dict[str, object], self.command(method, script=source, args=arguments))
        return result["value"]

    def prepare_tab(self, width: int, height: int, *, dark: bool, reduced_motion: bool | None) -> None:
        _ = self.command("Marionette:SetContext", value="chrome")
        cleared = self.script("""
Services.prefs.setIntPref("ui.systemUsesDarkTheme", arguments[0]);
if (arguments[1] === null) {
    Services.prefs.clearUserPref("ui.prefersReducedMotion");
} else {
    Services.prefs.setIntPref("ui.prefersReducedMotion", arguments[1]);
}
const flags = Ci.nsIClearDataService;
Services.clearData.deleteData(
    flags.CLEAR_COOKIES | flags.CLEAR_DOM_STORAGES | flags.CLEAR_ALL_CACHES | flags.CLEAR_HISTORY |
    flags.CLEAR_PERMISSIONS | flags.CLEAR_CONTENT_PREFERENCES,
    arguments[2]);
""", int(dark), None if reduced_motion is None else int(reduced_motion), asynchronous=True)
        if cleared != 0:
            raise RuntimeError(f"Firefox could not clear profile data: {cleared}")
        _ = self.command("Marionette:SetContext", value="content")
        window = cast(dict[str, str], self.command("WebDriver:NewWindow", type="tab", focus=True))
        _ = self.command("WebDriver:SwitchToWindow", handle=window["handle"])
        frame = cast(list[int], self.script("return [outerWidth - innerWidth, outerHeight - innerHeight];"))
        _ = self.command("WebDriver:SetWindowRect", width=max(width, 600) + frame[0], height=height + frame[1])
        # Set the content size directly. Firefox clamps the desktop window width.
        _ = self.command("Marionette:SetContext", value="chrome")
        _ = self.script("""
const browser = window.gBrowser.selectedBrowser;
browser.style.setProperty("min-width", arguments[0] + "px");
browser.style.setProperty("max-width", arguments[0] + "px");
browser.style.setProperty("min-height", arguments[1] + "px");
browser.style.setProperty("max-height", arguments[1] + "px");
browser.getBoundingClientRect();
""", width, height)
        _ = self.command("Marionette:SetContext", value="content")
        actual = self.script("""
const [width, height, done] = arguments;
const size = () => [innerWidth, innerHeight];
const finish = () => {
    if (innerWidth !== width || innerHeight !== height) return;
    removeEventListener("resize", finish);
    clearTimeout(timer);
    done(size());
};
const timer = setTimeout(() => {
    removeEventListener("resize", finish);
    done(size());
}, 2000);
addEventListener("resize", finish);
finish();
""", width, height, asynchronous=True)
        if actual != [width, height]:
            raise RuntimeError(f"Firefox viewport is {actual}. Expected {[width, height]}.")

    def close_tabs(self) -> None:
        handles = cast(list[str], self.command("WebDriver:GetWindowHandles"))
        if not handles:
            raise ConnectionError("Firefox has no open tabs")
        if self.initial not in handles:
            _ = self.command("WebDriver:SwitchToWindow", handle=handles[0])
            window = cast(dict[str, str], self.command("WebDriver:NewWindow", type="tab", focus=True))
            self.initial = window["handle"]
        while extras := [handle for handle in handles if handle != self.initial]:
            _ = self.command("WebDriver:SwitchToWindow", handle=extras[0])
            _ = self.command("WebDriver:ReleaseActions")
            handles = cast(list[str], self.command("WebDriver:CloseWindow"))
        _ = self.command("WebDriver:SwitchToWindow", handle=self.initial)

    def screenshot(self) -> bytes:
        result = cast(dict[str, str], self.command("WebDriver:TakeScreenshot", full=False))
        return base64.b64decode(result["value"])

    def wait_for_load(self, destination: str) -> None:
        while True:
            if self.script("""
const navigation = performance.getEntriesByType("navigation")[0];
return document.readyState === "complete" &&
    [navigation?.name, location.href].some(url => url?.split("#", 1)[0] === arguments[0]);
""", destination.partition("#")[0]) is True:
                return
            time.sleep(min(0.02, self.remaining()))

    def wait_for_result(self, complete: Event) -> None:
        while not complete.wait(min(CHECK_INTERVAL, self.remaining())):
            pass


class ModuleFirefox:
    def __init__(self) -> None:
        self.browser: Firefox | None = None
        self.reapers: list[subprocess.Popen[bytes]] = []

    def get(self, deadline: float) -> Firefox:
        browser = self.browser
        if browser is not None:
            if not browser.closed and browser.reaper.poll() is None:
                browser.deadline = deadline
                return browser
            browser.close()
        if len(self.reapers) == 2:
            raise RuntimeError("Firefox exhausted its module restart")
        root = Path(tempfile.gettempdir()) / f"fp-{uuid4().hex[:16]}"
        # A separate session lets the reaper survive a signal to the test group.
        reaper = subprocess.Popen(
            [sys.executable, "-m", __name__, str(root)],
            cwd=Path(__file__).resolve().parents[2], stdin=subprocess.PIPE, start_new_session=True)
        self.reapers.append(reaper)
        browser = Firefox(root, reaper, deadline)
        self.browser = browser
        return browser

    @contextmanager
    def page(self, destination: str, width: int, height: int, *, dark: bool = False,
             reduced_motion: bool | None = None, timeout: float = 45) -> Generator[Firefox]:
        browser = self.get(time.monotonic() + timeout)
        try:
            browser.prepare_tab(width, height, dark=dark, reduced_motion=reduced_motion)
            _ = browser.command("WebDriver:Navigate", url=destination)
        except BaseException:
            browser.close()
            raise
        error: BaseException | None = None
        try:
            yield browser
        except BaseException as failure:
            error = failure
            raise
        finally:
            try:
                browser.close_tabs()
            except (OSError, RuntimeError, ValueError) as cleanup_error:
                browser.close()
                if error is None:
                    raise
                error.add_note(f"Firefox cleanup failed: {cleanup_error}")

    def close(self) -> None:
        if self.browser is not None:
            self.browser.close()
        for reaper in self.reapers:
            _ = reaper.wait(timeout=5)


_modules: dict[str, ModuleFirefox] = {}


def close_module(module: str) -> None:
    _modules.pop(module).close()


@contextmanager
def firefox_page(case: unittest.TestCase, destination: str, width: int, height: int, *, dark: bool = False,
                 reduced_motion: bool | None = None, timeout: float = 45) -> Generator[Firefox]:
    module = type(case).__module__
    if module not in _modules:
        _modules[module] = ModuleFirefox()
        unittest.addModuleCleanup(close_module, module)
    with _modules[module].page(destination, width, height, dark=dark,
                               reduced_motion=reduced_motion, timeout=timeout) as browser:
        yield browser


def load_firefox(case: unittest.TestCase, destination: str, complete: Event | None = None, *,
                 size: tuple[int, int] = DEFAULT_VIEWPORT, dark: bool = False,
                 reduced_motion: bool | None = None, timeout: float = 45,
                 screenshot: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Load a fixture and wait for its server to receive the probe result."""
    try:
        with firefox_page(case, destination, *size, dark=dark, reduced_motion=reduced_motion,
                          timeout=timeout) as browser:
            if complete is None:
                browser.wait_for_load(destination)
            else:
                browser.wait_for_result(complete)
            if screenshot is not None:
                _ = screenshot.write_bytes(browser.screenshot())
    except (OSError, RuntimeError, ValueError) as error:
        return subprocess.CompletedProcess(destination, 1, "", str(error))
    return subprocess.CompletedProcess(destination, 0, "", "")


def firefox_probe_results(case: unittest.TestCase, page: bytes) -> list[list[str]]:
    """Load the page in a headless Firefox frame and return the failure lists its probe posts."""
    results: list[list[str]] = []
    complete = Event()

    class BrowserProbe(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if self.path == "/complete":
                _ = complete.wait(TEST_HANG_GUARD_SECS)
            elif self.path == "/":
                _ = self.wfile.write(b'<iframe src="/accounts"></iframe><img hidden src="/complete">')
            else:
                _ = self.wfile.write(page)

        def do_POST(self) -> None:
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            results.append(cast(list[str], json.loads(payload)))
            self.send_response(204)
            self.end_headers()
            complete.set()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), BrowserProbe) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            run = load_firefox(case, f"http://127.0.0.1:{server.server_port}/", complete)
            if run.returncode:
                raise AssertionError(run.stderr)
        finally:
            server.shutdown()
            thread.join(TEST_HANG_GUARD_SECS)
    return results


NO_DATA_FILE: dict[str, object] = {}
STATUS_PAGES = (
    ("accounts", "Accounts", "users", "Accounts", 45),
    ("aws", "AWS", "cloud", "AWS", 3900),
    ("aws_machines", "AWS Machines", "server", "AWS Machines", 3900),
    ("builds", "Builds", "hammer", "Builds", 15),
    ("disks", "Disks", "hard-drive", "Disks", 600),
    ("images", "Images", "image", "Images", 45),
    ("list", "List", "list-checks", "The List", 45),
    ("review", "Review", "inbox", "Review", 45),
)


@contextmanager
def status_configuration() -> Generator[None]:
    with tempfile.TemporaryDirectory(prefix="page-status-config-") as temporary:
        root = Path(temporary)
        pages = "".join(
            f"page.{name}.label={label}\npage.{name}.icon={icon}\npage.{name}.title={title}\n"
            + f"# range: 0.001..86400\nstatus.{name}.age_limit_seconds={age}\n"
            for name, label, icon, title, age in STATUS_PAGES
        )
        _ = (root / "console.conf").write_text(
            "# range: 0.001..86400\nstatus.round_trip_limit_seconds=1\n"
            + "# range: 0.001..86400\nstatus.render_limit_seconds=1\n"
            + "# range: 0.001..86400\nstatus.reading_delay_seconds=1\n"
            + pages)
        _ = (root / "review_links.conf").write_text(
            "# range: 5..300\npage_refresh_seconds=15\n"
            + "# range: 0.5..60\nframe_retry_seconds=2\n")
        with patch.dict(os.environ, {"CONF_DIR": temporary, "USER_ZONE": "America/New_York"}):
            yield


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
 // The Disks page also reads the Builds data; the status checks count only the page's own reads.
 if(original.includes('<title>Disks</title>') && String(url).includes('memory.json')) return {ok:false,status:404};
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
 const measure=line.querySelector('#measure-now');
 const checkMeasure=()=>{
  if(document.title!=='Disks') return;
  const bar=line.getBoundingClientRect(),button=measure?.getBoundingClientRect();
  check(measure===line.querySelector('#measure-now') && measure?.checkVisibility() &&
   measure.innerText.trim()==='Measure now' && button && Math.abs(button.right-bar.right)<1 &&
   button.top>=bar.top && button.bottom<=bar.bottom,
   'the '+state()+' line shows the same labeled Measure now button at its right edge');
 };
 const shown=()=>dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 const requestTimer=()=>[...intervals.values()].find(timer=>timer.ms===expectedPeriod);
 const clock=()=>[...intervals.values()].find(timer=>timer.ms===1000);
 const read=async()=>{if(expectedPeriod){perf+=expectedPeriod+1;requestTimer().run();}else shown();await flush();};
 try{
  checkMeasure();
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  await flush();
  if(mode==='retained' || measureFails){
   check(state()==='Failed' && !part('age').hidden && age()==='0 s ago',
    'a first failed read with shown data keeps its data time');
   mode='good';measureFails=false;
  }
  shown();await flush();
  check(reads>0 && state()==='Live','the real loader completes a good read');
  checkMeasure();
  check(part('render').textContent==='render 0.42 s','the real loader supplies render time');
  check(!part('trip').hidden && /^round trip [\d.]+ s$/.test(part('trip').textContent),
   'the Live line shows round trip before render time');
  const positions=[...line.children].filter(node=>!node.hidden && !node.matches('[data-status-announcement]'));
  check(positions.length===4 && positions[0].matches('.page-status-state') && positions[1]===part('age') &&
   positions[2]===part('trip') && positions[3]===part('render'),'the Live line has the same four positions');
  const title=document.querySelector('.page-title'),titleBox=title.getBoundingClientRect(),lineBox=line.getBoundingClientRect();
  const headingBox=(title.closest('.page-heading') || title).getBoundingClientRect();
  const main=document.querySelector('main'),mainBox=main.getBoundingClientRect(),width=document.documentElement.clientWidth;
  // A wide page uses the pane width; a text page keeps the 1124 px column.
  check(mainBox.width===(main.classList.contains('page-wide')?width:Math.min(width,1124)) &&
   Math.abs(mainBox.left-(width-mainBox.right))<1,'the page uses the shared centered column');
  check(main.classList.contains('page-wide')===(document.title==='Builds'),'only the Builds page is wide');
  check(titleBox.x===mainBox.x+32 && headingBox.y+scrollY===32,'the title starts at the shared column edge');
  const searchField=main.querySelector('[data-review] #list-search');
  const search=searchField?.getBoundingClientRect();
  if(search){
   check(lineBox.x===titleBox.x && search.top===headingBox.bottom+8,'the status and search row follows the title');
   check(lineBox.right<=search.left && Math.abs((lineBox.top+lineBox.bottom)-(search.top+search.bottom))<2,
    'the live status and search share the row');
  }else{
   check(lineBox.x===titleBox.x && lineBox.y===headingBox.bottom+8,'the line follows the title row in the same column');
   check(lineBox.width===headingBox.width,'the line uses the full title row width');
  }
  check(lineBox.height===21,'the line uses the shared height');
  check(getComputedStyle(title).fontSize==='26px' && getComputedStyle(title).fontWeight==='700',
   'the title uses the shared size and weight');
  check(getComputedStyle(line).fontSize==='14px' && getComputedStyle(line).lineHeight==='21px' &&
   getComputedStyle(line).marginBottom===(search ? '0px' : '20px'),'the line uses the shared font and the expected lower space');
  const ink=document.createElement('span');ink.style.color='var(--ink)';line.append(ink);
  check(getComputedStyle(title).color===getComputedStyle(ink).color,'the title uses the shared ink color');ink.remove();
  check(getComputedStyle(document.body).backgroundColor===getComputedStyle(line).backgroundColor,
   'the line uses the page background without a separate band');
  const content=document.querySelector('[data-review-content],[data-investigations],[data-image-layout],[data-list],[data-review]');
  if(content) check(content.getBoundingClientRect().x===titleBox.x,'the page content starts at the title edge');
  const icon=line.querySelector('[data-status-icon=Live] svg').getBoundingClientRect();
  check(icon.width===16 && icon.height===16,'the Live icon uses the shared size');
  const root=document.querySelector('[data-accounts],[data-review-content],[data-images],[data-investigations],[data-list],[data-review]');
  if(root) check(root.dataset.loaded==='true','the real loader replaces page content');
  if(expectedPeriod) check(!!requestTimer(),'the real request period runs');
  else check([...intervals.values()].every(timer=>timer.ms===1000),'the zero-period page has only its age timer');
  const checkReviewSticky=()=>{
   if(!searchField) return;
   const spacer=document.createElement('div');spacer.style.height='200vh';
   root.querySelector('.review-queue').append(spacer);
   const top=scrollY;
   scrollTo({top:innerHeight,behavior:'instant'});
   check(scrollY>0 && title.getBoundingClientRect().bottom<0 && Math.abs(line.getBoundingClientRect().top)<1,
    'The '+state()+' status stays visible when the Review queue scrolls.');
   scrollTo({top,behavior:'instant'});spacer.remove();
  };
  const count=reads;
  wall+=(JSON.parse(line.dataset.statusOptions).ageLimit+65)*1000;clock().run();
  check(state()==='Late' && age().includes('min ago'),'the shown data becomes Late');
  checkReviewSticky();
  checkMeasure();
  check(reads===count,'the age clock does not read');
  mode='failed';await read();
  check(state()==='Failed' && !part('age').hidden && part('reason').textContent==='HTTP 502',
   'a real failed read retains the shown data time');
  checkMeasure();
  checkReviewSticky();
  if(searchField){
   const originalWidth=frameElement.style.width,reason=part('reason'),originalReason=reason.textContent;
   frameElement.style.width='801px';
   reason.textContent='Could not read /fixtures/'+ 'unavailable-source-path'.repeat(20);
   const bounds=reason.getBoundingClientRect(),searchBox=searchField.getBoundingClientRect();
   check(innerWidth===801 && bounds.height>21 && bounds.right<=searchBox.left &&
    reason.scrollWidth<=reason.clientWidth && document.documentElement.scrollWidth<=innerWidth,
    'A long error wraps without overlapping Search just above the layout breakpoint.');
   frameElement.style.width=originalWidth;reason.textContent=originalReason;
  }
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
  checkMeasure();
 }catch(error){failures.push(String(error));}
 failures.push(...errors);
 await nativeFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();</script>'''
    page = markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>').encode()
    match = re.search(r'<title>([^<]+)</title>', markup)
    assert match is not None
    identity = 'list' if match[1] == 'The List' else match[1].split()[0].lower()
    if identity in ('changes', 'questions', 'investigations'):
        identity = 'review'
    from scripts.review_links import console

    with status_configuration():
        settings = conf.read("console").values()
        pages = tuple(console.ConsolePage(name, settings[f"page.{name}.label"], settings[f"page.{name}.icon"],
                                         settings[f"page.{name}.title"], console.AvailablePage('/page'))
                      for name, *_ in STATUS_PAGES)
        shell = console.render(pages, console.UnavailablePage()).replace(b'</body>', b'<img hidden src="/complete"></body>')
    results: list[list[str]] = []
    complete = Event()

    class BrowserProbe(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            if self.path == '/complete':
                _ = complete.wait(TEST_HANG_GUARD_SECS)
            elif self.path.startswith('/?page='):
                _ = self.wfile.write(shell)
            else:
                _ = self.wfile.write(page)

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers['Content-Length']))
            if self.path != '/results':
                self.send_response(200)
                self.end_headers()
                _ = self.wfile.write(b'{"new":[]}')
                return
            results.append(cast(list[str], json.loads(body)))
            complete.set()
            self.send_response(204)
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(('127.0.0.1', 0), BrowserProbe) as server:
        thread = Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            done = load_firefox(case, f'http://127.0.0.1:{server.server_port}/?page={identity}',
                                complete, size=(1600, 900))
            case.assertEqual(done.returncode, 0, done.stderr)
        finally:
            server.shutdown()
            thread.join(TEST_HANG_GUARD_SECS)
    case.assertEqual(results, [[]], 'The real page loader must complete every status check.')


if __name__ == "__main__":
    reap_firefox(Path(sys.argv[1]))

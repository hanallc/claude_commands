"""Browser test helpers that several console page tests share."""
from __future__ import annotations

import base64
import json
import os
import re
import select
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, cast, override
from urllib.parse import urlsplit
from unittest.mock import patch

from scripts.lib import conf
from scripts.lib.process_group import run_process_group, stop_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.review_links import console

class _Browser:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process: subprocess.Popen[bytes] = process
        self.closed: bool = False
        self.resources: ExitStack = ExitStack()
        _ = self.resources.callback(stop_process_group, process)

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self.resources.close()

    @contextmanager
    def stop_on_error(self) -> Generator[None]:
        try:
            yield
        except BaseException as error:
            try:
                self.close()
            except BaseException as cleanup_error:
                error.add_note(f"Browser cleanup failed: {cleanup_error}")
            raise


class _ChromeCommandError(RuntimeError):
    def __init__(self, method: str, error: dict[str, object]) -> None:
        super().__init__(f"{method}: {error}")
        self.error: dict[str, object] = error


class Chrome(_Browser):
    """Keep one fixture tab and close the extra tabs after each test."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.pending: bytearray = bytearray()
        self.loaded: set[tuple[str, str]] = set()
        self.origins: set[str] = set()
        self.sequence: int = 0
        self.blank: bool = True
        self.session_storage_written: bool = False
        with (root / "chrome.log").open("wb") as log:
            process = subprocess.Popen(
                ["sh", "-c", 'exec 3<&0 4>&1; exec "$@" 1>&2', "chrome-fixture",
                 "google-chrome", "--headless", "--no-sandbox", "--disable-gpu", "--disable-background-networking",
                 "--no-first-run", "--allow-file-access-from-files", "--remote-debugging-pipe",
                 f"--user-data-dir={root / 'chrome'}", "about:blank"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, bufsize=0,
                start_new_session=True)
        super().__init__(process)
        with self.stop_on_error():
            writer = process.stdin
            assert writer is not None
            os.set_blocking(writer.fileno(), False)
            self.context: str = cast(str, self.command("Target.createBrowserContext", {})["browserContextId"])
            self.target: str
            self.session: str
            self.open_fixture_tab()
            # The fixture tab keeps Chrome open after the startup tab closes.
            for tab in self.tabs():
                if tab["targetId"] != self.target:
                    self.close_tab(tab["targetId"])

    def open_fixture_tab(self) -> None:
        target = self.command("Target.createTarget", {"url": "about:blank", "browserContextId": self.context})
        self.target = cast(str, target["targetId"])
        self.session = cast(str, self.command("Target.attachToTarget",
            {"targetId": self.target, "flatten": True})["sessionId"])
        _ = self.command("Page.enable", {}, self.session)
        _ = self.command("Page.setLifecycleEventsEnabled", {"enabled": True}, self.session)
        _ = self.command("DOMStorage.enable", {}, self.session)
        self.blank = True

    def tabs(self) -> list[dict[str, str]]:
        targets = cast(list[dict[str, str]], self.command("Target.getTargets", {})["targetInfos"])
        return [target for target in targets if target["type"] == "page"]

    def close_tab(self, target: str) -> None:
        try:
            _ = self.command("Target.closeTarget", {"targetId": target})
        except _ChromeCommandError as error:
            if error.error.get("code") != -32602 or error.error.get("message") != "No target with given id found":
                raise

    def close_test_pages(self) -> None:
        if self.closed:
            return
        with self.stop_on_error():
            for opened in self.tabs():
                if opened["targetId"] != self.target:
                    self.close_tab(opened["targetId"])
            if not self.blank:
                self.navigate("about:blank")
            if self.session_storage_written:
                # Closing the tab discards session storage for all its documents.
                old_target = self.target
                self.open_fixture_tab()
                self.close_tab(old_target)
            _ = self.command("Page.resetNavigationHistory", {}, self.session)
            _ = self.command("Emulation.clearDeviceMetricsOverride", {}, self.session)
            _ = self.command("Emulation.setEmulatedMedia", {"media": "", "features": []}, self.session)
            _ = self.command("Network.clearBrowserCookies", {}, self.session)
            _ = self.command("Network.clearBrowserCache", {}, self.session)
            for origin in sorted(self.origins):
                _ = self.command("Storage.clearDataForOrigin",
                                 {"origin": origin, "storageTypes": "all"}, self.session)
            self.origins.clear()
            self.loaded.clear()
            self.session_storage_written = False

    def read(self, deadline: float) -> dict[str, object]:
        reader = self.process.stdout
        assert reader is not None
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Chrome did not answer")
            end = self.pending.find(b"\0")
            if end >= 0:
                reply = cast(dict[str, object], json.loads(self.pending[:end]))
                del self.pending[:end + 1]
                method = reply.get("method")
                if method == "Page.lifecycleEvent":
                    lifecycle = cast(dict[str, str], reply["params"])
                    if lifecycle["name"] == "DOMContentLoaded":
                        self.loaded.add((cast(str, reply["sessionId"]), lifecycle["loaderId"]))
                elif method == "Page.frameNavigated":
                    navigation = cast(dict[str, dict[str, str]], reply["params"])
                    origin = navigation["frame"]["securityOrigin"]
                    if urlsplit(origin).scheme in {"http", "https", "file"}:
                        self.origins.add(origin)
                elif method in {"DOMStorage.domStorageItemAdded", "DOMStorage.domStorageItemUpdated"}:
                    storage = cast(dict[str, dict[str, bool]], reply["params"])
                    if not storage["storageId"]["isLocalStorage"]:
                        self.session_storage_written = True
                if time.monotonic() >= deadline:
                    raise TimeoutError("Chrome did not answer")
                return reply
            if not select.select([reader], [], [], remaining)[0]:
                raise TimeoutError("Chrome did not answer")
            chunk = os.read(reader.fileno(), 65536)
            if not chunk:
                raise RuntimeError((self.root / "chrome.log").read_text(errors="replace"))
            self.pending.extend(chunk)

    def command(self, method: str, parameters: dict[str, object], session: str = "", *,
                timeout: float = 20) -> dict[str, object]:
        self.sequence += 1
        request: dict[str, object] = {"id": self.sequence, "method": method, "params": parameters}
        if session:
            request["sessionId"] = session
        writer = self.process.stdin
        assert writer is not None
        data = memoryview(json.dumps(request).encode() + b"\0")
        deadline = time.monotonic() + timeout
        while data:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([], [writer], [], remaining)[1] or time.monotonic() >= deadline:
                raise TimeoutError(f"{method}: Chrome did not accept the command")
            try:
                written = os.write(writer.fileno(), data)
            except BlockingIOError:
                continue
            data = data[written:]
        while True:
            reply = self.read(deadline)
            if reply.get("id") == self.sequence:
                if "error" in reply:
                    raise _ChromeCommandError(method, cast(dict[str, object], reply["error"]))
                return cast(dict[str, object], reply["result"])

    def navigate(self, destination: str) -> None:
        self.blank = False
        self.loaded.clear()
        navigation = self.command("Page.navigate", {"url": destination}, self.session)
        if "errorText" in navigation:
            raise RuntimeError(f"Page.navigate: {navigation['errorText']}")
        load = (self.session, cast(str, navigation["loaderId"]))
        deadline = time.monotonic() + 20
        while load not in self.loaded:
            _ = self.read(deadline)
        self.blank = destination == "about:blank"

    def dom(self, page: Path | str, marker: str, *, reduced_motion: bool,
            window_size: tuple[int, int] | None) -> subprocess.CompletedProcess[str]:
        with self.stop_on_error():
            session = self.session
            if window_size is not None:
                width, height = window_size
                _ = self.command("Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}, session)
            else:
                _ = self.command("Emulation.clearDeviceMetricsOverride", {}, session)
            features = [{"name": "prefers-reduced-motion", "value": "reduce"}] if reduced_motion else []
            _ = self.command("Emulation.setEmulatedMedia", {"features": features}, session)
            destination = page.as_uri() if isinstance(page, Path) else page
            # Evaluate the promise in the new document, after the navigation.
            marker_literal = json.dumps(marker)
            script = (
                "try { sessionStorage.clear(); localStorage.clear(); }"
                "catch (error) { if (error.name !== 'SecurityError') throw error; }"
                "globalThis.fixtureDOM = new Promise(resolve => {"
                "const observer = new MutationObserver(() => {"
                f"const result = document.getElementById({marker_literal});"
                "if (result) { observer.disconnect(); resolve(document.documentElement.outerHTML); }"
                "});"
                "observer.observe(document, {childList: true, subtree: true});"
                "});"
            )
            hook = self.command("Page.addScriptToEvaluateOnNewDocument", {"source": script}, session)
            self.navigate(destination)
            try:
                response = self.command("Runtime.evaluate",
                    {"expression": "globalThis.fixtureDOM", "awaitPromise": True, "returnByValue": True}, session,
                    timeout=TEST_HANG_GUARD_SECS)
            except TimeoutError as error:
                raise TimeoutError(f"Chrome did not find {marker} in {destination}") from error
            result = cast(dict[str, object], response["result"])
            _ = self.command("Page.removeScriptToEvaluateOnNewDocument", {"identifier": hook["identifier"]}, session)
            self.navigate("about:blank")
            return subprocess.CompletedProcess(destination, 0, str(result.get("value", "")), "")


class Firefox(_Browser):
    """Keep Firefox open and control its private profile through Marionette."""

    def __init__(self, root: Path, *, timeout: float | None = None) -> None:
        profile = root / "firefox"
        profile.mkdir()
        _ = (profile / "user.js").write_text(
            'user_pref("marionette.port", 0);\n'
            + 'user_pref("browser.sessionhistory.max_total_viewers", 0);\n')
        with (root / "firefox.log").open("wb") as log:
            process = subprocess.Popen(
                ["firefox", "--headless", "--no-remote", "--profile", str(profile),
                 "--marionette", "--remote-allow-system-access", "about:blank"],
                stdout=log, stderr=log, start_new_session=True)
        super().__init__(process)
        with self.stop_on_error():
            port_file = profile / "MarionetteActivePort"
            deadline = time.monotonic() + (30 if timeout is None else timeout)
            while not port_file.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError((root / "firefox.log").read_text(errors="replace"))
                time.sleep(0.02)
            address = ("127.0.0.1", int(port_file.read_text()))
            self.connection: socket.socket = self.resources.enter_context(
                socket.create_connection(address, timeout=45 if timeout is None else timeout))
            self.stream: BinaryIO = self.resources.enter_context(self.connection.makefile("rb"))
            _ = self.read()
            self.sequence: int = 0
            capabilities: dict[str, object] = {"pageLoadStrategy": "none"}
            if timeout is not None:
                capabilities["timeouts"] = {"script": int(timeout * 1000)}
            _ = self.command("WebDriver:NewSession", {"capabilities": {"alwaysMatch": capabilities}})
            initial = cast(dict[str, str], self.command("WebDriver:GetWindowHandle"))
            self.initial: str = initial["value"]

    def read(self) -> object:
        size = bytearray()
        while (byte := self.stream.read(1)) != b":":
            if not byte or not byte.isdigit():
                raise RuntimeError("Firefox closed the Marionette connection")
            size.extend(byte)
        return cast(object, json.loads(self.stream.read(int(size))))

    def command(self, method: str, parameters: dict[str, object] | None = None) -> object:
        self.sequence += 1
        message = json.dumps([0, self.sequence, method, parameters or {}]).encode()
        self.connection.sendall(str(len(message)).encode() + b":" + message)
        kind, sequence, error, result = cast(list[object], self.read())
        if kind != 1 or sequence != self.sequence or error is not None:
            raise RuntimeError(f"{method}: {error}")
        return result

    def script(self, source: str, arguments: list[object]) -> object:
        result = cast(dict[str, object], self.command("WebDriver:ExecuteScript", {"script": source, "args": arguments}))
        return result["value"]

    @contextmanager
    def page(self, destination: str, width: int, height: int, *, dark: bool = False,
             reduced_motion: bool = False) -> Generator[None]:
        with self.stop_on_error():
            _ = self.command("Marionette:SetContext", {"value": "chrome"})
            cleared = cast(dict[str, object], self.command("WebDriver:ExecuteAsyncScript", {
                "script": """
Services.prefs.setIntPref("ui.systemUsesDarkTheme", arguments[0]);
Services.prefs.setIntPref("ui.prefersReducedMotion", arguments[1]);
const flags = Ci.nsIClearDataService;
Services.clearData.deleteData(
    flags.CLEAR_COOKIES | flags.CLEAR_DOM_STORAGES | flags.CLEAR_ALL_CACHES | flags.CLEAR_HISTORY,
    arguments[2]);
""", "args": [int(dark), int(reduced_motion)],
            }))
            if cleared["value"] != 0:
                raise RuntimeError(f"Firefox could not clear profile data: {cleared['value']}")
            _ = self.command("Marionette:SetContext", {"value": "content"})
            window = cast(dict[str, str], self.command("WebDriver:NewWindow", {"type": "tab", "focus": True}))
            # If the switch fails, close the group to close the new tab.
            _ = self.command("WebDriver:SwitchToWindow", {"handle": window["handle"]})
            dimensions = cast(list[int], self.script("return [outerWidth - innerWidth, outerHeight - innerHeight];", []))
            _ = self.command("WebDriver:SetWindowRect", {"width": width + dimensions[0], "height": height + dimensions[1]})
            _ = self.command("WebDriver:Navigate", {"url": destination})
            yield
            _ = self.command("WebDriver:CloseWindow")
            _ = self.command("WebDriver:SwitchToWindow", {"handle": self.initial})

    def screenshot(self, path: Path) -> None:
        result = cast(dict[str, str], self.command("WebDriver:TakeScreenshot", {"full": False}))
        _ = path.write_bytes(base64.b64decode(result["value"]))

    def wait_for_load(self, destination: str) -> None:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if self.script("""
// The console can change the query with history.replaceState.
const navigation = performance.getEntriesByType('navigation')[0];
return navigation?.name.split('#', 1)[0] === arguments[0] && document.readyState === 'complete';
""", [destination.partition("#")[0]]) is True:
                return
            time.sleep(0.02)
        raise TimeoutError(f"Firefox did not load {destination}")


NO_DATA_FILE: dict[str, object] = {}
UNDO_ACTION_SCRIPT = r"""
function fixtureUndo(){
 const realFetch=window.fetch.bind(window),posts=[];
 window.fetch=async(url,options={})=>{
  if(options.method!=='POST') return realFetch(url,options);
  if(String(url).endsWith('/undo')) return Response.json({outcome:'undone'});
  if(new Headers(options.headers).get('X-Undo-Hold')!=='1') throw new Error('The action sent no hold header.');
  posts.push(new URLSearchParams(options.body));
  return Response.json({hold:'a'.repeat(24),seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
 };
 const until=async(test,label)=>{
  const end=performance.now()+8000;
  while(!test()){
   if(performance.now()>end) throw new Error(label);
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 const toast=()=>window.top.document.querySelector('[data-send-undo]');
 async function undo(){
  await until(()=>toast()?.querySelector('button'),'The Undo toast did not show.');
  const current=toast();
  const button=current.querySelector('button');
  if(current.querySelector('span').textContent!=='Saved.' || button.textContent!=='Undo')
   throw new Error('The toast must say Saved and offer Undo.');
  button.click();
  await until(()=>!current.isConnected,'The Undo toast did not close.');
 }
 return {realFetch,posts,until,undo};
}
"""

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


def firefox_probe_results(page: bytes, *, window_size: tuple[int, int] | None = None,
                          dark: bool = False) -> list[list[str]]:
    """Load the page in a headless Firefox frame and return the failure lists its probe posts."""
    results: list[list[str]] = []
    complete = threading.Event()

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
            complete.set()
            self.send_response(204)
            self.end_headers()

        @override
        def log_message(self, format: str, *args: object) -> None:
            pass

    with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(("127.0.0.1", 0), BrowserProbe) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        thread.start()
        try:
            profile = Path(temporary) / "profile"
            profile.mkdir()
            options: list[str] = []
            environment = os.environ.copy()
            if window_size is not None:
                width, height = window_size
                options.extend(("--window-size", f"{width},{height}"))
                environment.update(MOZ_HEADLESS_WIDTH=str(width), MOZ_HEADLESS_HEIGHT=str(height))
            if dark:
                _ = (profile / "user.js").write_text('user_pref("ui.systemUsesDarkTheme", 1);\n')
            run = run_process_group(["firefox", "--headless", "--no-remote", "--profile", str(profile), *options,
                                     "--screenshot", str(Path(temporary) / "page.png"), f"http://127.0.0.1:{server.server_port}/"],
                                    timeout=45, env=environment)
            if run.returncode:
                raise AssertionError(run.stderr)
        finally:
            server.shutdown()
            thread.join(TEST_HANG_GUARD_SECS)
    return results


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
 const checkMeasureFailure=()=>{
  const notice=document.querySelector('#measure-failure');
  check(state()==='Live' && !part('age').hidden && age()==='0 s ago' &&
   notice.checkVisibility() && notice.textContent==='HTTP 502',
   'a refused Disks measure shows its notice and keeps the live data time');
 };
 const shown=()=>dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 const requestTimer=()=>[...intervals.values()].find(timer=>timer.ms===expectedPeriod);
 const clock=()=>[...intervals.values()].find(timer=>timer.ms===1000);
 const read=async()=>{if(expectedPeriod){perf+=expectedPeriod+1;requestTimer().run();}else shown();await flush();};
 try{
  checkMeasure();
  if(document.readyState!=='complete') await new Promise(resolve=>addEventListener('load',resolve,{once:true}));
  await flush();
  if(mode==='retained'){
   check(state()==='Failed' && !part('age').hidden && age()==='0 s ago',
    'a first failed read with shown data keeps its data time');
   mode='good';
  }
  if(measureFails){
   checkMeasureFailure();
   measureFails=false;
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
  const originalScroll=scrollY;
  scrollTo({top:0,behavior:'instant'});
  const title=document.querySelector('.page-title'),titleBox=title.getBoundingClientRect(),lineBox=line.getBoundingClientRect();
  const header=title.closest('.page-top');
  const headingBox=(title.closest('.page-heading') || title).getBoundingClientRect();
  const main=document.querySelector('main'),mainBox=main.getBoundingClientRect(),width=document.documentElement.clientWidth;
  // A wide page uses the pane width; a text page keeps the 1124 px column.
  check(mainBox.width===(main.classList.contains('page-wide')?width:Math.min(width,1124)) &&
   Math.abs(mainBox.left-(width-mainBox.right))<1,'the page uses the shared centered column');
  check(main.classList.contains('page-wide')===(document.title==='Builds'),'only the Builds page is wide');
  check(titleBox.x===mainBox.x+32 && headingBox.y===32,'the title starts at the shared column edge');
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
  check(getComputedStyle(line).fontSize==='14px' && getComputedStyle(line).lineHeight==='21px',
   'the line uses the shared font');
  const lowerSpace=header.getBoundingClientRect().bottom-Math.max(lineBox.bottom,search?.bottom ?? lineBox.bottom)
   +parseFloat(getComputedStyle(header).marginBottom);
  check(Math.abs(lowerSpace-(search ? 12 : 20))<1,'the header keeps the expected space below the status row');
  const ink=document.createElement('span');ink.style.color='var(--ink)';line.append(ink);
  check(getComputedStyle(title).color===getComputedStyle(ink).color,'the title uses the shared ink color');ink.remove();
  check(getComputedStyle(document.body).backgroundColor===getComputedStyle(line).backgroundColor,
   'the line uses the page background without a separate band');
  const content=document.querySelector('[data-review-content],[data-investigations],[data-image-layout],[data-list],[data-review]');
  if(content) check(content.getBoundingClientRect().x===titleBox.x,'the page content starts at the title edge');
  const icon=line.querySelector('[data-status-icon=Live] svg').getBoundingClientRect();
  check(icon.width===16 && icon.height===16,'the Live icon uses the shared size');
  scrollTo({top:originalScroll,behavior:'instant'});
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
   const frozenTop=header.getBoundingClientRect();
   const frozenTitle=title.getBoundingClientRect(), frozenLine=line.getBoundingClientRect();
   check(scrollY>0 && Math.abs(frozenTop.top)<1 && frozenTitle.top>=0 &&
    frozenLine.top>=frozenTitle.bottom && frozenLine.bottom<=innerHeight,
    'The title and '+state()+' status stay visible when the Review queue scrolls.');
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
   checkMeasureFailure();
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
    with status_configuration():
        settings = conf.read("console").values()
        pages = tuple(console.ConsolePage(name, settings[f"page.{name}.label"], settings[f"page.{name}.icon"],
                                         settings[f"page.{name}.title"], console.AvailablePage('/page'))
                      for name, *_ in STATUS_PAGES)
        shell = console.render(pages, console.UnavailablePage()).replace(b'</body>', b'<img hidden src="/complete"></body>')
    results: list[list[str]] = []
    complete = threading.Event()

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

    with tempfile.TemporaryDirectory() as temporary, ThreadingHTTPServer(('127.0.0.1', 0), BrowserProbe) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        try:
            profile = Path(temporary) / 'profile'
            profile.mkdir()
            done = run_process_group(['firefox', '--headless', '--no-remote', '--profile', str(profile),
                                      '--window-size', '1600,900',
                                      '--screenshot', str(Path(temporary) / 'page.png'),
                                      f'http://127.0.0.1:{server.server_port}/?page={identity}'],
                                     timeout=45)
            case.assertEqual(done.returncode, 0, done.stderr)
        finally:
            server.shutdown()
            thread.join(TEST_HANG_GUARD_SECS)
    case.assertEqual(results, [[]], 'The real page loader must complete every status check.')

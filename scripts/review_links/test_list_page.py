"""Test The List page with private records and a real browser."""
from __future__ import annotations

import base64
import html
import io
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout, suppress
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import BinaryIO, ClassVar, cast, override
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
from xml.etree import ElementTree

from scripts.accounts import accounts
from scripts.agents.remote import local_machine
from scripts.lib import atomic_file, conf, file_lock
from scripts.lib.process_group import send_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.user_zone import user_zone
from scripts.message import send_result
from scripts.review_links.test_page_question import FakeQuestionTransport, question_outcome
from scripts.production import change_review, change_routing, delivery_state, investigation, record_store, session_staff, showrunners, staff, the_list
from scripts.production.update_registration import QuestionStaff
from scripts.review_links import console, list_page, page_frame, serve, staff_names
from scripts.review_links import change_browser, change_page, new_item_dialog, page_question, question_record
from scripts.review_links import review_items, review_page
from scripts.review_links.test_change_page import BrowserForms
from scripts.review_links.test_page_frame import assert_status_states
from scripts.review_links.test_record_table import save_notice_failures
from scripts.review_links.test_staff_names import without_role_colors
from scripts.statusline import counts_row, rebuild_row

MORTY = staff.StaffRow(staff.StaffId("536997b5-4415-4cb1-b683-d777e02e9a22"), "producer", "morty", staff.StaffRoot(),
                        current_session=staff.StaffId("536997b5-4415-4cb1-b683-d777e02e9a22"))
OWNER = staff.StaffRow(staff.StaffId("a0882376-677b-4da5-adbb-f11db9146021"), "showrunner", "sdlc", staff.StaffRoot(),
                        current_session=staff.StaffId("a0882376-677b-4da5-adbb-f11db9146021"))
OTHER = staff.StaffRow(staff.StaffId("f5636030-6745-4ff4-aeda-4c2773d18da8"), "showrunner", "builds", staff.StaffRoot(),
                        current_session=staff.StaffId("f5636030-6745-4ff4-aeda-4c2773d18da8"))
DWIGHT = staff.StaffRow(staff.StaffId("920b0a64-6eee-479e-8c07-e03e4dcd4ef6"), "producer", "dwight", staff.StaffRoot(),
                        current_session=staff.StaffId("920b0a64-6eee-479e-8c07-e03e4dcd4ef6"))
NOW = datetime(2026, 11, 16, 18, tzinfo=UTC)
FIREFOX_MIN_SHARED_WIDTH = 500
CHROME = shutil.which("google-chrome")
LIST_BROWSER = CHROME or shutil.which("firefox")

WATCH_ACTIONS = """<script>
function waitForChange(element,ready,options){
 if(ready()) return Promise.resolve();
 return new Promise(resolve=>{
  const observer=new MutationObserver(()=>{
   if(ready()){observer.disconnect();resolve();}
  });
  observer.observe(element,options);
 });
}
let submittedForm;
document.addEventListener('submit',event=>{submittedForm=event.target;},true);
function waitForSave(form){
 return waitForChange(form,()=>!form.hasAttribute('data-sending'),
  {attributes:true,attributeFilter:['data-sending']});
}
function waitForRefresh(){
 const list=document.querySelector('[data-list]');
 return waitForChange(list.parentElement,()=>!list.isConnected,{childList:true});
}
function respondToSave(form,respond){
 const saved=waitForSave(form);
 respond();
 return saved;
}
async function finishAnimations(animations){
 const finished=Promise.allSettled(animations.map(animation=>animation.finished));
 for(const animation of animations) animation.finish();
 await finished;
}
</script>"""


def _private_firefox_command(profile: Path, width: int, height: int, *, dark: bool) -> tuple[list[str], dict[str, str]]:
    profile.mkdir()
    if dark:
        _ = (profile / "user.js").write_text('user_pref("ui.systemUsesDarkTheme", 1);\n')
    command = ["firefox", "--headless", "--no-remote", "--profile", str(profile), "--window-size", f"{width},{height}"]
    return command, {**os.environ, "MOZ_HEADLESS_WIDTH": str(width), "MOZ_HEADLESS_HEIGHT": str(height)}


def _stop_browser(process: subprocess.Popen[bytes]) -> None:
    try:
        with suppress(ProcessLookupError):
            send_process_group(process.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            _ = process.wait(timeout=10)
        # The browser can exit before its child processes.
        with suppress(ProcessLookupError):
            send_process_group(process.pid, signal.SIGKILL)
        _ = process.wait(timeout=10)
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


class _Browser:
    def __init__(self, process: subprocess.Popen[bytes]) -> None:
        self.process: subprocess.Popen[bytes] = process
        self.closed: bool = False
        self.resources: ExitStack = ExitStack()
        _ = self.resources.callback(_stop_browser, process)

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


class _Chrome(_Browser):
    """Keep one Chrome target and start each probe in a new document."""

    def __init__(self, root: Path) -> None:
        self.root: Path = root
        self.pending: bytearray = bytearray()
        self.loaded: set[tuple[str, str, str]] = set()
        self.frame_loaders: dict[str, str] = {}
        self.sequence: int = 0
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
            context = self.command("Target.createBrowserContext", {})["browserContextId"]
            target = self.command("Target.createTarget", {"url": "about:blank", "browserContextId": context})
            self.session: str = cast(str, self.command("Target.attachToTarget",
                {"targetId": target["targetId"], "flatten": True})["sessionId"])
            _ = self.command("Page.enable", {}, self.session)
            _ = self.command("Page.setLifecycleEventsEnabled", {"enabled": True}, self.session)
            _ = self.command("Page.bringToFront", {}, self.session)

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
                if reply.get("method") == "Page.lifecycleEvent":
                    parameters = cast(dict[str, str], reply["params"])
                    if parameters["name"] in ("DOMContentLoaded", "load"):
                        self.loaded.add((cast(str, reply["sessionId"]), parameters["loaderId"], parameters["name"]))
                elif reply.get("method") == "Page.frameNavigated":
                    frame = cast(dict[str, dict[str, str]], reply["params"])["frame"]
                    if "parentId" not in frame:
                        self.frame_loaders[frame["id"]] = frame["loaderId"]
                if time.monotonic() >= deadline:
                    raise TimeoutError("Chrome did not answer")
                return reply
            if not select.select([reader], [], [], remaining)[0]:
                raise TimeoutError("Chrome did not answer")
            chunk = os.read(reader.fileno(), 65536)
            if not chunk:
                raise RuntimeError((self.root / "chrome.log").read_text(errors="replace"))
            self.pending.extend(chunk)

    def command(self, method: str, parameters: dict[str, object], session: str = "") -> dict[str, object]:
        self.sequence += 1
        request: dict[str, object] = {"id": self.sequence, "method": method, "params": parameters}
        if session:
            request["sessionId"] = session
        writer = self.process.stdin
        assert writer is not None
        data = memoryview(json.dumps(request).encode() + b"\0")
        deadline = time.monotonic() + 20
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
                    raise RuntimeError(f"{method}: {reply['error']}")
                return cast(dict[str, object], reply["result"])

    def navigate(self, destination: str, ready: str = "DOMContentLoaded") -> None:
        self.frame_loaders.clear()
        navigation = self.command("Page.navigate", {"url": destination}, self.session)
        if "errorText" in navigation:
            raise RuntimeError(f"Page.navigate: {navigation['errorText']}")
        frame = cast(str, navigation["frameId"])
        loader = cast(str, navigation["loaderId"])
        deadline = time.monotonic() + 20
        while (self.session, self.frame_loaders.get(frame, loader) if ready == "load" else loader,
               ready) not in self.loaded:
            _ = self.read(deadline)

    @contextmanager
    def page(self, destination: str, *, reduced_motion: bool = False, dark: bool = False,
             window_size: tuple[int, int] | None = None, ready: str = "DOMContentLoaded") -> Generator[None]:
        with self.stop_on_error():
            session = self.session
            if window_size is not None:
                width, height = window_size
                _ = self.command("Emulation.setDeviceMetricsOverride",
                    {"width": width, "height": height, "deviceScaleFactor": 1, "mobile": False}, session)
            else:
                _ = self.command("Emulation.clearDeviceMetricsOverride", {}, session)
            features = [{"name": "prefers-color-scheme", "value": "dark"}] if dark else []
            if reduced_motion:
                features.append({"name": "prefers-reduced-motion", "value": "reduce"})
            _ = self.command("Emulation.setEmulatedMedia", {"features": features}, session)
            self.navigate(destination, ready)
            yield
            self.navigate("about:blank")
            _ = self.command("Page.resetNavigationHistory", {}, session)
            self.loaded.clear()

    def screenshot(self, path: Path) -> None:
        result = self.command("Page.captureScreenshot", {"captureBeyondViewport": False}, self.session)
        _ = path.write_bytes(base64.b64decode(cast(str, result["data"])))

    def probe(self, page: Path, marker: str, *, reduced_motion: bool = False,
              window_size: tuple[int, int] | None = None) -> str:
        with self.stop_on_error():
            session = self.session
            destination = page.as_uri()
            marker_literal = json.dumps(marker)
            script = (
                "try { sessionStorage.clear(); localStorage.clear(); }"
                "catch (error) { if (error.name !== 'SecurityError') throw error; }"
                "globalThis.fixtureDOM = new Promise(resolve => {"
                "const observer = new MutationObserver(() => {"
                f"const result = document.getElementById({marker_literal});"
                "if (result) { observer.disconnect(); resolve(result.textContent); }"
                "});"
                "observer.observe(document, {childList: true, subtree: true});"
                "});"
            )
            hook = self.command("Page.addScriptToEvaluateOnNewDocument", {"source": script}, session)
            with self.page(destination, reduced_motion=reduced_motion, window_size=window_size):
                _ = self.command("Page.removeScriptToEvaluateOnNewDocument", {"identifier": hook["identifier"]}, session)
                try:
                    response = self.command("Runtime.evaluate",
                        {"expression": "globalThis.fixtureDOM", "awaitPromise": True, "returnByValue": True}, session)
                except TimeoutError as error:
                    raise TimeoutError(f"Chrome did not find {marker} in {destination}") from error
            result = cast(dict[str, object], response["result"])
            return str(result.get("value", ""))


class _Firefox(_Browser):
    """Keep Firefox open and control its private profile through Marionette."""

    def __init__(self, root: Path) -> None:
        profile = root / "firefox"
        profile.mkdir()
        _ = (profile / "user.js").write_text(
            'user_pref("marionette.port", 0);\n'
            + 'user_pref("remote.log.level", "Info");\n'
            + 'user_pref("browser.sessionhistory.max_total_viewers", 0);\n')
        process = subprocess.Popen(
            ["firefox", "--headless", "--no-remote", "--profile", str(profile),
             "--marionette", "--remote-allow-system-access", "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        super().__init__(process)
        with self.stop_on_error():
            ready = threading.Event()
            ports: list[int] = []
            log_path = root / "firefox.log"
            assert process.stdout is not None
            output = cast(BinaryIO, process.stdout)

            def read_output() -> None:
                try:
                    with log_path.open("wb") as log:
                        while line := output.readline():
                            _ = log.write(line)
                            if found := re.search(rb"Marionette\s+INFO\s+Listening on port (\d+)", line):
                                ports.append(int(found[1]))
                                ready.set()
                finally:
                    ready.set()

            threading.Thread(target=read_output, daemon=True).start()
            if not ready.wait(30) or not ports:
                raise RuntimeError(log_path.read_text(errors="replace"))
            address = ("127.0.0.1", ports[0])
            self.connection: socket.socket = self.resources.enter_context(socket.create_connection(address, timeout=45))
            self.stream: BinaryIO = self.resources.enter_context(self.connection.makefile("rb"))
            _ = self.read()
            self.sequence: int = 0
            _ = self.command("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"pageLoadStrategy": "normal"}}})
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

class ListPageTests(unittest.TestCase):
    root: Path = Path()
    browser_root: ClassVar[Path]
    _chrome: ClassVar[_Chrome | None] = None
    _firefox: ClassVar[_Firefox | None] = None
    _http_server: ClassVar[ThreadingHTTPServer | None] = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.browser_root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="list-page-class-")))
        cls._chrome = None
        cls._firefox = None
        cls._http_server = None
        config = cls.browser_root / "config"
        config.mkdir()
        _ = (config / "production.conf").write_text("# whole: 20..\nquestion_title_max_chars=100\n")
        _ = (config / "statusline.conf").write_text(
            "role.showrunner=#a379fd\nrole.unit-director=#ffaf02\n"
            + "role.producer=#90fe42\nrole.worker=#ff0404\n")
        _ = (config / "review_links.conf").write_text(
            "# whole: 1..20\ndelivery_tries=3\n# range: 0..300\ndelivery_pause_seconds=0\n"
            + "# whole: 1..20\nfeedback_rows=5\n# whole: 1..20\nreview_words_rows=4\n"
            + "# whole: 1..1440\nlater_soon_minutes=60\n"
            + "later_evening=18:00\nlater_evening_until=17:00\nlater_morning=09:00\n"
            + "later_week_day=Monday\nlater_week_time=09:00\n"
            + "# range: 5..300\npage_refresh_seconds=15\n# whole: 1..80\nlist_log_max_height_rem=16\n"
            + "# whole: 1..300\npage_request_timeout_seconds=15\n"
            + "# whole: 1..5000\ncard_fade_out_ms=450\n# whole: 1..5000\ncard_fade_in_ms=600\n"
            + "# range: 0.5..60\nframe_retry_seconds=2\n"
            + "# range: 0.001..\ntailscale_check_seconds=60\n"
            + "# range: 0.001..\nsnapshot_sweep_seconds=3600\n"
            + "# range: 0..\nsnapshot_retention_seconds=604800\n"
            + "# range: 0..365\nchange_image_unclaimed_days=14\n"
            + "# range: 1..600\nstaff_snapshot_seconds=60\n"
            + "# range: 0.001..\ncode_check_seconds=5\n"
            + "# whole: 1..65535\nserver_port=8765\n"
            + "changes_first_root=natedev\nreview_attachments_root=review-attachments\n")
        _ = (config / "console.conf").write_text(
            "page.list.label=List\npage.list.icon=list-checks\npage.list.title=The List\n"
            + "page.list.url=python:scripts.review_links.list_page:page_url\n"
            + "page.list.items=python:scripts.review_links.list_page:waiting_updates\n"
            + "# range: 0.001..86400\nstatus.round_trip_limit_seconds=1\n"
            + "# range: 0.001..86400\nstatus.render_limit_seconds=1\n"
            + "# range: 0.001..86400\nstatus.reading_delay_seconds=1\n"
            + "# range: 0.001..86400\nstatus.list.age_limit_seconds=45\n"
            + "# range: 0.001..86400\nstatus.review.age_limit_seconds=45\n")

    @classmethod
    def chrome(cls) -> _Chrome:
        if cls._chrome is not None:
            if not cls._chrome.closed and cls._chrome.process.poll() is None:
                return cls._chrome
            cls._chrome.close()
        root = Path(tempfile.mkdtemp(dir=cls.browser_root, prefix="chrome-"))
        cls._chrome = _Chrome(root)
        cls.addClassCleanup(cls._chrome.close)
        return cls._chrome

    @classmethod
    def firefox(cls) -> _Firefox:
        if cls._firefox is not None:
            if not cls._firefox.closed and cls._firefox.process.poll() is None:
                return cls._firefox
            cls._firefox.close()
        root = Path(tempfile.mkdtemp(dir=cls.browser_root, prefix="firefox-"))
        cls._firefox = _Firefox(root)
        cls.addClassCleanup(cls._firefox.close)
        return cls._firefox

    @classmethod
    @contextmanager
    def probe_server(cls, prefix: str, handler: type[BaseHTTPRequestHandler]) -> Generator[ThreadingHTTPServer]:
        server = cls._http_server
        if server is None:
            with ExitStack() as startup:
                server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
                _ = startup.callback(server.server_close)
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                _ = startup.callback(thread.join, TEST_HANG_GUARD_SECS)
                _ = startup.callback(server.shutdown)
                cls.addClassCleanup(startup.pop_all().close)
            cls._http_server = server

        def parse_request(request: BaseHTTPRequestHandler) -> bool:
            if not handler.parse_request(request):
                return False
            if not request.path.startswith(prefix + "/"):
                request.send_error(404)
                return False
            request.path = request.path[len(prefix):]
            return True

        case_handler = type("CaseHandler", (handler,), {"parse_request": parse_request})
        with patch.object(server, "RequestHandlerClass", case_handler):
            yield server

    @contextmanager
    def browser_page(self, destination: str, width: int, height: int, *, dark: bool = False,
                     firefox_only: bool = False, ready: str = "load") -> Generator[_Chrome | _Firefox]:
        if CHROME and not firefox_only:
            browser = self.chrome()
            with browser.page(destination, window_size=(width, height), ready=ready, dark=dark):
                yield browser
        else:
            firefox = self.firefox()
            with firefox.page(destination, width, height, dark=dark):
                yield firefox

    def browser_results(self, destination: str, width: int, height: int, complete: threading.Event,
                        *, dark: bool = False, firefox_only: bool = False) -> None:
        if (firefox_only or not CHROME) and width < FIREFOX_MIN_SHARED_WIDTH:
            with tempfile.TemporaryDirectory(dir=self.root, prefix="firefox-view-") as temporary:
                root = Path(temporary)
                command, environment = _private_firefox_command(root / "profile", width, height, dark=dark)
                # Firefox uses the exact viewport size in screenshot mode.
                browser = subprocess.run([*command, "--screenshot", str(root / "page.png"), destination],
                                         capture_output=True, text=True, timeout=45, env=environment)
                self.assertEqual(browser.returncode, 0, browser.stderr)
                self.assertTrue(complete.is_set(), f"Firefox did not report a result for {destination}")
            return
        with self.browser_page(destination, width, height, dark=dark, firefox_only=firefox_only, ready="DOMContentLoaded"):
            if not complete.wait(45):
                raise TimeoutError(f"The probe did not report a result for {destination}")

    def browser_screenshot(self, destination: str, width: int, height: int, path: Path,
                           complete: threading.Event, *, dark: bool = False) -> None:
        with self.browser_page(destination, width, height, dark=dark, ready="DOMContentLoaded") as browser:
            if not complete.wait(45):
                raise TimeoutError(f"The probe did not report a result for {destination}")
            browser.screenshot(path)

    def test_review_links_use_the_card_body_for_every_state(self) -> None:
        source = review_items.ProposalLedgerSource(self.root / "proposals.md")
        ready = review_items.ReviewItem("proposal/sdlc/P10", "sdlc/P10", review_items.ReviewType.PROPOSAL,
            OWNER.id, OWNER.id, "Check this proposal", review_items.ReviewState.OPEN,
            review_items.ReviewTimes(NOW, NOW), (), (), source)
        for state in review_items.ReviewState:
            with self.subTest(state=state):
                review = replace(ready, state=state)
                link = ElementTree.fromstring(list_page.review_card(review.key, {review.key: review}))
                place = urlsplit(link.attrib["href"])
                self.assertEqual(place.path, review_page.PAGE_PATH)
                self.assertEqual(parse_qs(place.query), {"body": [review.key], "view": ["card"]})
                self.assertFalse(place.fragment)
                self.assertNotIn("target", link.attrib)
                self.assertEqual(link.attrib["aria-haspopup"], "dialog")
                self.assertIn("data-list-review-card", link.attrib)
        item = replace(self.add("Linked work"), reviews=("proposal/sdlc/P10", "change/sdlc/C1830"))
        for state in review_items.ReviewState:
            with self.subTest(summary=state):
                reviews = {key: replace(ready, key=key, state=state) for key in item.reviews}
                self.assertNotIn("<a ", list_page.review_summary(item, reviews, NOW))

    def test_review_summary_reports_a_missing_link_as_unavailable(self) -> None:
        item = replace(self.add("Linked work"), reviews=("sdlc/C1", "sdlc/C2"))
        done = review_items.ReviewItem("sdlc/C1", "sdlc/C1", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Completed work", review_items.ReviewState.DONE,
            review_items.ReviewTimes(NOW, NOW), (), (), review_items.ProposalLedgerSource(self.root / "review.md"))
        for reviews in ({}, {done.key: done}):
            with self.subTest(reviews=tuple(reviews)):
                summary = list_page.review_summary(item, reviews, NOW)
                self.assertIn(list_page.REVIEW_UNAVAILABLE, summary)
                self.assertNotIn("Reviews done", summary)

    def test_list_ids_use_the_shared_no_wrap_cell(self) -> None:
        _ = self.add("Readable item number")
        body = list_page.render(self.root / "reviews").decode()
        self.assertIn('<td class="record-id" data-label="#">', body)
        self.assertIn('.list-table .record-id{white-space:nowrap;overflow-wrap:normal;width:1%}', body)
        self.assertIn('.list-toggle{white-space:nowrap}', body)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_shows_one_line_of_eta_with_full_text_and_overdue_style(self) -> None:
        past = NOW - timedelta(hours=1)
        future = NOW + timedelta(hours=1)
        text = 'Wait for "QA" & <release approval> before the producer sets a time. ' * 3
        for number, eta in enumerate((
            the_list.EtaRange(NOW + timedelta(days=1), NOW + timedelta(days=2)),
            past, future, the_list.EtaDescription(text.strip()),
            the_list.EtaRange(past, future), the_list.EtaRange(past - timedelta(hours=1), past),
            the_list.EtaDescription("After build " + "0123456789abcdef" * 10),
            datetime(2026, 11, 20, 8, 30, tzinfo=UTC), the_list.EtaStatus.PAUSED,
            datetime(2026, 12, 3, 8, 30, tzinfo=UTC),
            date(2026, 11, 20), NOW.date(), NOW.date() - timedelta(days=1),
        ), start=1):
            item = self.add(f"ETA example {number}", number=number, next_check=future if number == 3 else past)
            _ = the_list.update(MORTY.id, item.id, {"eta": eta}, source="morty")
            if number == 8:
                _ = the_list.park(MORTY.id, item.id, datetime(2026, 11, 20, 14, tzinfo=UTC), source="nate")
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        probe = r'''<script>
const failures=[];
const check=(condition,message)=>{if(!condition) failures.push(message);};
const row=number=>document.querySelector('[data-number="'+number+'"]');
const eta=number=>row(number).querySelector('.list-eta');
const etaLine=number=>eta(number).querySelector('.list-eta-text');
const range='2026-11-17 13:00 EST – 2026-11-18 13:00 EST';
const desktop=innerWidth>960;
const longNumber=desktop ? 4 : 7;
const long=eta(longNumber),line=etaLine(longNumber);
const style=getComputedStyle(line);
check(etaLine(3).textContent==='Nov 16 14:00 EST','The cell shows the date for today.');
check(etaLine(8).textContent==='Nov 20 03:30 EST','The cell shows the time in the user zone.');
const checkLine=number=>row(number).querySelector('[data-list-check]');
check(checkLine(3).textContent==='Check Nov 16 14:00 EST','The next check uses the ETA date form.');
check(checkLine(2).textContent==='Check overdue Nov 16 12:00 EST' && checkLine(2).getClientRects().length>0,
 'An unparked item shows the overdue check in the same date form.');
check(row(8).querySelector('[data-parked-note]').textContent==='Parked until Nov 20 09:00 EST',
 'The parked time uses the ETA date form.');
check(!checkLine(8),'A date park omits the overdue check.');
check(etaLine(9).textContent==='Paused','The cell shows the status as stored.');
check(etaLine(10).textContent==='Dec 3 03:30 EST','The cell omits the leading zero from the day.');
check(etaLine(11).textContent==='Nov 20' && eta(11).title==='2026-11-20',
 'The cell shows the date without a time or zone.');
check(etaLine(1).textContent==='Nov 17 13:00 EST – Nov 18 13:00 EST',
 'An old range shows both ends in the short format.');
if(desktop){
 const timestamp=etaLine(8);
 check(getComputedStyle(timestamp).whiteSpace==='nowrap' && timestamp.scrollWidth===timestamp.clientWidth,
  'The short desktop timestamp fits on one line.');
 check(style.whiteSpace==='nowrap' && style.textOverflow==='ellipsis' && style.overflowX==='hidden'
  && line.scrollWidth>line.clientWidth,'The long desktop ETA ends with an ellipsis on one line.');
}else{
 const cell=long.closest('td');
 const range=document.createRange();
 range.selectNodeContents(line);
 const bounds=line.getBoundingClientRect();
 const fragments=[...range.getClientRects()];
 check(getComputedStyle(cell,'::before').content.includes('ETA'),'The phone row keeps the ETA label.');
 check(long.closest('tbody').querySelector('[data-detail]').hidden && document.activeElement!==long
  && line.textContent===long.title && fragments.length>1
  && fragments.every(fragment=>fragment.right<=bounds.right+1 && fragment.bottom<=bounds.bottom+1),
  'The phone row shows the full ETA before focus or expansion.');
 check(document.documentElement.scrollWidth<=innerWidth,'The long phone ETA fits without horizontal scrolling.');
}
check(eta(1).title===range && eta(8).title==='2026-11-20 03:30 EST'
 && eta(4).title.includes('"QA" & <release approval>'),
 'The hover text preserves the full dates and the original text.');
for(const target of [long,eta(8)]){
 const full=target.querySelector('.list-eta-full');
 check(!full.getClientRects().length,'The full text starts hidden.');
 target.focus();
 check(document.activeElement===target && full.getClientRects().length && full.textContent===target.title,
  'Keyboard focus shows the full ETA.');
 target.blur();
 check(!full.getClientRects().length,'The full text hides when focus leaves.');
}
const warning=getComputedStyle(document.querySelector('[data-label="State"] .warning')).color;
for(const number of [2,6,13]){
 check(eta(number).classList.contains('warning') && getComputedStyle(eta(number)).color===warning,
  'Past ETA '+number+' uses the Check overdue color.');
}
for(const number of [1,3,4,5,8,11,12]) check(!eta(number).classList.contains('warning'),
 'ETA '+number+' stays in the normal color.');
const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
document.body.append(result);
</script>'''
        for width in (1440, 500):
            with self.subTest(width=width):
                self.browser_actions(markup, "", probe, window_size=(width, 900))

    def test_delivery_park_omits_the_overdue_check(self) -> None:
        item = self.add("Publish the reviewed change", number=123, next_check=NOW - timedelta(hours=1))
        _ = the_list.park(MORTY.id, item.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        row = markup.split('data-number="123"', 1)[1].split("</tbody>", 1)[0]
        self.assertIn("Parked until delivered", row)
        self.assertNotIn("<small data-list-check>", row)
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["check_html"], "")
        _ = the_list.unpark(MORTY.id, item.id, source="nate")
        check = list_page.saved_action_state(MORTY.id, item.id)["check_html"]
        self.assertIn('<span class="warning">Check overdue Nov 16 12:00 EST</span>', check)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_new_item_opens_the_dialog_and_reveals_the_saved_row(self) -> None:
        root = self.root / "reviews"
        _ = self.add("An existing List item")
        initial = list_page.render(root, include_counts=False).decode()
        item = self.add("A new List item")
        fresh = list_page.render(root, include_counts=False).decode()
        setup = r"""<script>
const created=__KEY__,owner=__OWNER__,fresh=__FRESH__,requests=[];
let finishRowSave,reads=0;
window.setInterval=()=>0;
window.fetch=async (path,options={})=>{
 if(path===location.pathname){
  if(options.method==='POST') return new Promise(resolve=>{finishRowSave=resolve;});
  reads++;
  return new Response(fresh);
 }
 const request=new URL(path,location.href);
 requests.push([request.pathname+request.search,options.body && JSON.parse(options.body)]);
 if(request.search==='?page=list') return Response.json({token:'fixture',recipients:[{id:owner,name:'morty'}]});
 if(options.body && JSON.parse(options.body).page_capture) return Response.json({});
 return Response.json({id:'Q1',key:'fixture-review-key',list_key:created});
};
</script>""".replace("__KEY__", json.dumps(list_page.ListRecord(MORTY.id, item).key)).replace(
            "__OWNER__", json.dumps(MORTY.id)).replace("__FRESH__", json.dumps(fresh).replace("<", r"\u003c"))
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const until=(target,test)=>new Promise(resolve=>{
  const observer=new MutationObserver(()=>{if(test()){observer.disconnect();resolve();}});
  observer.observe(target,{subtree:true,childList:true,attributes:true});
  if(test()){observer.disconnect();resolve();}
 });
 const button=document.querySelector('.page-heading-actions [data-list-new]');
 check(button?.textContent==='+New item','The New item button is beside the List title.');
 const dialog=document.querySelector('#review-new-item');
 check(document.querySelectorAll('#review-new-item').length===1,'The List mounts one shared dialog.');
 const existing=document.querySelector('[data-list-item]');
 existing.querySelector('.list-toggle').click();
 const tell=existing.querySelector('button[value="tell"]'),form=tell.form;
 form.elements.words.value='Check the existing item.';
 form.elements.words.dispatchEvent(new Event('input',{bubbles:true}));
 form.requestSubmit(tell);
 check(typeof finishRowSave==='function','A row save is in progress.');
 const search=document.querySelector('#list-search');
 search.value='existing';
 search.dispatchEvent(new Event('input',{bubbles:true}));
 button.click();
 check(dialog.open,'The button opens the dialog.');
 const recipient=()=>dialog.querySelector('[name="recipient_id"]:checked');
 await until(dialog,recipient);
 check(recipient().value===owner && recipient().labels[0].textContent==='morty','To selects the List page owner.');
 check(requests[0][0]==='/console/review/new-item?page=list','The dialog requests List recipients.');
 dialog.querySelector('[name="include_page"]').click();
 const words=dialog.querySelector('textarea');
 words.value='A new List item';
 words.dispatchEvent(new Event('input',{bubbles:true}));
 dialog.querySelector('[type="submit"]').click();
 await until(dialog,()=>!dialog.open);
 check(reads===0,'The List waits for the row save before it refreshes.');
 const reply=__ROW_REPLY__;
 finishRowSave(Response.json(reply.body,{status:reply.status}));
 const row=()=>document.querySelector('[data-list-item][data-key="'+CSS.escape(created)+'"]');
 await until(document.body,()=>row() && !row().hidden && !row().querySelector('[data-detail]').hidden);
 check(reads===1,'The New item save refreshes the List once after the row save ends.');
 check(!dialog.open,'Send closes the dialog.');
 const saved=requests.find(([,body])=>body?.words);
 check(saved?.[0]==='/console/review/new-item' && saved[1].recipient_id===owner && saved[1].page==='list',
       'Send uses the shared endpoint and the selected List owner.');
 check(row().textContent.includes('A new List item'),'The filtered List reveals the saved row.');
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
 document.body.append(result);
})();
</script>"""
        for status, body in ((200, {"item_state": "Working"}), (409, {"reason": "The row save failed."})):
            with self.subTest(status=status):
                self.browser_actions(initial, setup, probe.replace("__ROW_REPLY__", json.dumps({
                    "status": status, "body": body})))

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_list_title_actions_fit_at_320_pixels(self) -> None:
        owner = replace(MORTY, name="cargo-liner")
        markup = page_frame.with_owner(list_page.render(self.root / "reviews", include_counts=False),
                                       "list", owner, staff.StaffSnapshot((owner,)), new_item_dialog.render)
        probe = """<script>
(async()=>{
 const failures=[];
 try{
  await document.fonts.ready;
  const title=document.querySelector('.page-heading h1'), button=document.querySelector('[data-list-new]');
  const heading=title.parentElement.getBoundingClientRect(), bounds=title.getBoundingClientRect();
  const line=parseFloat(getComputedStyle(title).lineHeight);
  if(innerWidth!==320 || Math.abs(bounds.height-line)>1){
   failures.push('The List title fits on one line at 320 px.');
  }
  const controls=[button,document.querySelector('[data-owner-new]'),document.querySelector('.page-owner-name')];
  if(controls.some(control=>{
   const box=control.getBoundingClientRect();
   return box.width<=0 || box.left<bounds.right || box.right>heading.right+1 ||
          box.top<heading.top || box.bottom>heading.bottom;
  }) || bounds.height>heading.height || heading.height>button.offsetHeight+1){
   failures.push('The List controls fit beside the title at 320 px.');
  }
  if(button.innerText.trim()!=='+' || button.getAttribute('aria-label')!=='New item' || button.title!=='New item'){
   failures.push('The narrow New item button shows only + and keeps its name.');
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        for in_console in (False, True):
            with self.subTest(in_console=in_console):
                self.browser_answer_visibility(markup.decode(), "", probe, reload_console=in_console,
                                               viewport_width=320)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_row_controls_show_chevrons_and_accessible_labels(self) -> None:
        _ = self.add("First item", number=1)
        _ = self.add("Second item", number=2)
        self.browser_row_expansion('''
 check(!document.querySelector('[data-list-expansion], [aria-label="Expand all"], [aria-label="Collapse all"]'),
  'The List uses individual row controls.');
 for(const row of rows){
  const button=toggle(row),detail=row.querySelector('[data-detail]');
  check(button.tagName==='BUTTON' && button.getAttribute('aria-label')==='Show item '+row.dataset.number,
   'The button names its item.');
  check(button.getAttribute('aria-controls')===detail.id && isExpanded(row,false),
   'The button points to its closed details.');
  check(button.querySelector('.list-chevron')?.getAttribute('aria-hidden')==='true',
   'The chevron is decorative.');
 }
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_opens_one_item_with_show_chips_and_words(self) -> None:
        for title in ("Deploy alpha", "Deploy beta", "Inspect logs"):
            _ = self.add(title, the_list.ItemState.WAITING_FOR_NATE)
        _ = self.add("Deploy gamma", the_list.ItemState.WAITING_FOR_NATE, owner=OTHER.id)
        _ = self.add("Deploy delta")
        self.browser_row_expansion(r'''
 chip('show','needs-me').click();
 selectOwner(OWNER);
 const words=document.querySelector('#list-search');
 const filtered=new Promise(resolve=>words.addEventListener('filterchange',resolve,{once:true}));
 words.value='deploy';words.dispatchEvent(new Event('input'));
 await filtered;
 const matches=rows.filter(row=>!row.hidden);
 check(matches.length===2,'Two items match all filters.');
 for(const selected of matches){
  toggle(selected).click();
  check(rows.every(row=>isExpanded(row,row===selected)),'Only the selected item opens.');
 }
'''.replace('OWNER', json.dumps(OWNER.id)))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_keeps_a_filtered_item_open_until_another_item_opens(self) -> None:
        _ = self.add("Deploy alpha")
        _ = self.add("Deploy beta")
        _ = self.add("Deploy gamma", owner=OTHER.id)
        self.browser_row_expansion(r'''
 const other=rows.find(row=>row.dataset.owner!==OWNER);
 toggle(other).click();
 selectOwner(OWNER);
 const matches=rows.filter(row=>!row.hidden);
 check(matches.length===2 && other.hidden && isExpanded(other,true),'The filter keeps the hidden item open.');
 selectOwner('');
 check(!other.hidden && isExpanded(other,true),'Clearing the filter reveals the open item.');
 selectOwner(OWNER);
 toggle(matches[0]).click();
 toggle(matches[0]).click();
 selectOwner('');
 check(rows.every(row=>isExpanded(row,false)),'Opening another item closes the previous item.');
'''.replace('OWNER', json.dumps(OWNER.id)))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_filter_chip_applies_words_before_the_search_timer_ends(self) -> None:
        _ = self.add("Deploy alpha", number=1)
        _ = self.add("Deploy beta", number=2)
        self.browser_row_expansion(r'''
 const alpha=rows.find(row=>row.dataset.number==='1'),beta=rows.find(row=>row.dataset.number==='2');
 const words=document.querySelector('#list-search');
 let changes=0;
 words.addEventListener('filterchange',()=>changes++);
 words.value='alpha';words.dispatchEvent(new Event('input'));
 chip('show','working').click();
 check(changes===0,'The Show chip applies the filter before the search timer fires.');
 check(!alpha.hidden && beta.hidden,'The new words filter the items.');
 toggle(alpha).click();
 check(isExpanded(alpha,true) && isExpanded(beta,false),'Only the selected match opens.');
''')

    def browser_row_expansion(self, steps: str) -> None:
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        probe = r'''<script>
(async()=>{
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
const rows=[...document.querySelectorAll('[data-list-item]')];
const chip=(group,value)=>document.querySelector('[data-filter-row="'+group+'"] [data-filter-value="'+value+'"]');
const selectOwner=value=>chip('owner',value ? 'staff:'+value : 'all').click();
const toggle=row=>row.querySelector('.list-toggle');
const isExpanded=(row,expanded)=>row.querySelector('[data-detail]').hidden===!expanded &&
 toggle(row).getAttribute('aria-expanded')===String(expanded);
try{
''' + steps + '''
}catch(error){failures.push(String(error));}
const result=document.createElement('p');
result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, "", probe)

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_browser_review_links_open_one_card_without_leaving_the_list(self) -> None:
        root = self.root / "reviews"
        doc = self.root / "sdlc.md"
        listing = self.root / f"{change_review.PREFIX}sdlc.md"
        _ = doc.write_text(f'# Production\n- **Showrunner checkout:** `{self.root}`\n- **Change review:** `{listing}`\n')
        now = datetime.now(UTC)
        original = (change_review.BODY.replace("\n## Reviewed",
            f"| C1 | Check the List review overlay | {now.date().isoformat()} | Open the linked change. |\n\n## Reviewed")
            + "| C2 | A completed change | works | 2026-11-15 |\n")
        members = staff.StaffSnapshot((MORTY, DWIGHT, replace(OWNER, doc=staff.LiveDoc(local_machine(), doc)), OTHER))
        item = self.add("Check the linked reviews", number=111)
        _ = the_list.link(MORTY.id, item.id, ("sdlc/C1", "sdlc/C2"), source="morty")
        _ = self.add("An unrelated List row", number=112)
        waiting = investigation.file(OWNER.id, "Check the waiting review", "What caused the error?",
                                     waiting_why="The owner checks the error.")
        waiting_key = f"investigation/{waiting.owner}/{waiting.id}"
        waiting_item = self.add("Check the linked reviews that are waiting", number=113)
        _ = the_list.link(MORTY.id, waiting_item.id, (waiting_key,), source="morty")
        settings = self.root / "config/review_links.conf"
        overlay_settings = settings.read_text()
        for key, value in (("page_request_timeout_seconds", 7), ("card_fade_out_ms", 180), ("card_fade_in_ms", 260)):
            overlay_settings = re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", overlay_settings)
        _ = settings.write_text(overlay_settings)
        pages = (console.ConsolePage("list", "List", "list-checks", "The List", console.AvailablePage(list_page.PAGE_PATH)),
                 console.ConsolePage("review", "Review", "inbox", "Review", console.AvailablePage(review_page.PAGE_PATH)))
        shell = console.render(pages, console.UnavailablePage()).decode()
        list_place = list_page.PAGE_PATH + '#' + urlencode({"show": "working", "item": f"{MORTY.id}/{item.id}"})
        review_place = review_page.PAGE_PATH + '#' + urlencode({"show": "Done", "type": "question", "q": "keep"})
        destination = "/proof" + console.place_query("list", list_place) + "&" + urlencode({"at.review": review_place})
        probe = r'''<script>
const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
const shot=new URLSearchParams(location.search).has('shot');
const waitingKey=WAITING_KEY;
const report=async()=>{await fetch('/results',{method:'POST',body:JSON.stringify(failures)});};
const listFrame=document.querySelector('iframe:not([hidden])');
const direct=!listFrame;
const animationFrames=win=>new Promise(resolve=>win.requestAnimationFrame(()=>win.requestAnimationFrame(resolve)));
async function inspect(){
 const doc=listFrame?.contentDocument || document,win=doc.defaultView;
 const dialog=doc.querySelector('[data-list-review-dialog]');
 const row=()=>doc.querySelector('[data-number="111"]');
 const link=key=>[...row().querySelectorAll('a[data-list-review-card]')].find(link=>
  new URL(link.href).searchParams.get('body')===key);
 const changed=(node,ready,description)=>new Promise((resolve,reject)=>{
  const observer=new win.MutationObserver(checkReady);
  const timer=setTimeout(()=>{
   observer.disconnect();reject(new Error('Timed out: '+description));
  },8000);
  function checkReady(){
   try{
    if(ready()){clearTimeout(timer);observer.disconnect();resolve();}
   }catch(error){clearTimeout(timer);observer.disconnect();reject(error);}
  }
  observer.observe(node,{attributes:true,childList:true,subtree:true});checkReady();
 });
 const readCard=source=>{
  source.click();
  const frame=dialog.querySelector('iframe:not([hidden])');
  if(!frame) throw new Error('The card frame is missing.');
  return new Promise(resolve=>frame.addEventListener('load',()=>resolve(frame.contentDocument),{once:true}));
 };
 check(!dialog.querySelector('iframe'),'List loads no card before a click.');
 for(const cell of doc.querySelectorAll('.record-id')){
  if(cell.closest('tbody').hidden) continue;
  const button=cell.querySelector('.list-toggle');
  check(win.getComputedStyle(cell).whiteSpace==='nowrap','The ID cell does not wrap.');
  check(button.scrollWidth<=cell.clientWidth,'The ID and arrow fit in their column.');
  const range=doc.createRange();range.selectNodeContents(button.lastChild);
  check(range.getClientRects().length===1,'The ID text has one line.');
 }
 await animationFrames(win);
 const listURL=win.location.href,historyLength=history.length;
 const reviewPlace=()=>new URL(location.href).searchParams.get('at.review');
 let savedReviewPlace=reviewPlace();
 const search=doc.querySelector('#list-search');
 search.value='linked reviews';search.dispatchEvent(new win.Event('input',{bubbles:true}));
 await changed(doc.body,()=>link('sdlc/C1') && link('sdlc/C2'),'the List loads the linked cards');
 const waitingLink=doc.querySelector('[data-number="113"] [data-label="State"] a[data-list-review-card]');
 const waitingCard=await readCard(waitingLink);
 check(dialog.open && waitingCard.querySelector('[data-key="'+waitingKey+'"][data-state="Waiting"]'),
  'The summary opens the linked Waiting card.');
 dialog.querySelector('button').click();
 const doneLink=link('sdlc/C2'),done=await readCard(doneLink);
 check(dialog.open && done.querySelector('[data-state="Done"]'),'Done opens in the dialog.');
 check(!done.querySelector('[data-review-form] textarea'),'The Done card has no action.');
 check(!done.querySelector('[data-review-index]'),'The dialog loads one card.');
 const close=dialog.querySelector('button');
 done.body.tabIndex=-1;done.body.focus();
 done.body.dispatchEvent(new done.defaultView.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 check(!dialog.open && doc.activeElement===doneLink,'Escape closes the dialog and focuses the same link.');
 let readyLink=link('sdlc/C1');
 const ready=await readCard(readyLink);
 await animationFrames(win);
 check(ready.querySelector('[data-review-body]').textContent.includes('Open the linked change.'),'The open card shows the Review body.');
 const form=ready.querySelector('[data-review-form]');
 check(!!form.querySelector('[value="works"]') && !!form.querySelector('[value="feedback"]') &&
  !!form.querySelector('[data-later-toggle]'),'The card shows the Review actions.');
 const phone=direct || document.querySelector('.mobile-header').getClientRects().length>0;
 check(phone===(direct || new URLSearchParams(location.search).has('phone')),'The List uses the expected layout.');
 if(phone){
  const box=dialog.getBoundingClientRect();
  const top=listFrame?.getBoundingClientRect().top ?? box.top;
  check(Math.abs(box.width-innerWidth)<=1 && Math.abs(box.height-innerHeight)<=1 && top===0,
   'The phone dialog fills the screen. '+JSON.stringify({width:innerWidth,height:innerHeight,box,top}));
  check(ready.documentElement.scrollWidth<=ready.defaultView.innerWidth,'The card fits the phone width.');
 }else{
  const box=dialog.getBoundingClientRect();
  check(box.width<=1060 && Math.abs(box.left+box.right-win.innerWidth)<=20,'The desktop dialog is centered.');
  check(ready.documentElement.scrollWidth<=ready.defaultView.innerWidth,'The card fits the desktop width.');
 }
 if(direct){await report();return;}
 if(shot){
  check(win.matchMedia('(prefers-color-scheme:dark)').matches,'The desktop uses dark mode.');
  await report();return;
 }
 if(!phone){
  dialog.dispatchEvent(new win.PointerEvent('pointerdown',{clientX:0,clientY:0,bubbles:true}));
  dialog.dispatchEvent(new win.MouseEvent('click',{clientX:0,clientY:0,bubbles:true}));
  check(!dialog.open && doc.activeElement===readyLink,'A click outside closes the dialog and focuses the link.');
  await readCard(readyLink);
 }
 close.click();
 check(!dialog.open && doc.activeElement===readyLink,'Close returns focus to the link.');
 check(reviewPlace()===savedReviewPlace,'Opening and closing a card keeps the saved Review address.');
 if(!phone){
  await readCard(readyLink);
  document.querySelector('.nav-entry[data-page="review"]').click();
  const reviewFrame=document.querySelector('iframe[data-console-page="review"]');
  await new Promise(resolve=>reviewFrame.addEventListener('load',resolve,{once:true}));
  await animationFrames(window);
  const entries=JSON.parse(reviewFrame.contentDocument.querySelector('[data-review-index]').textContent).r;
  check(entries.some(entry=>entry[0]===waitingKey) && entries.every(entry=>entry[0]!=='sdlc/C2'),
   'The Review index includes the Waiting card and excludes the Done card.');
  check(listFrame.hidden && !dialog.open,'Changing the console page closes an idle card.');
  const oldRow=row();
  document.querySelector('.nav-entry[data-page="list"]').click();
  await changed(doc.body,()=>row()!==oldRow,'the List refreshes on return');
  check(!listFrame.hidden && !dialog.open && !dialog.querySelector('iframe'),'Returning to List shows no old card.');
  savedReviewPlace=reviewPlace();
  await changed(doc.body,()=>link('sdlc/C1'),'the refreshed List loads the linked card');
  readyLink=link('sdlc/C1');
  const draft=await readCard(readyLink);
  draft.querySelector('[name=feedback]').value='Keep this draft when I switch tabs.';
  const beforeVisibilityRefresh=row();
  for(const visibility of ['hidden','visible']){
   Object.defineProperty(doc,'visibilityState',{configurable:true,value:visibility});
   doc.dispatchEvent(new win.Event('visibilitychange'));
   await animationFrames(win);
   check(dialog.open && dialog.querySelector('iframe')?.contentDocument===draft &&
     draft.querySelector('[name=feedback]').value==='Keep this draft when I switch tabs.',
    'A browser tab switch keeps the open card and its draft.');
  }
  delete doc.visibilityState;
  await changed(doc.body,()=>row()!==beforeVisibilityRefresh && link('sdlc/C1'),
   'the visible List refreshes the row and reloads the link');
  document.querySelector('.nav-entry[data-page="review"]').click();
  await animationFrames(window);
  check(listFrame.hidden && dialog.open,'Changing console pages keeps a card that has a draft.');
  const beforeReturn=row(),fetchBeforeReturn=win.fetch.bind(win);
  let releaseCounts,countsStarted;
  const heldCounts=new Promise(resolve=>{releaseCounts=resolve;});
  const readingCounts=new Promise(resolve=>{countsStarted=resolve;});
  win.fetch=async(url,options)=>{
   if(String(url).includes('?counts=')){countsStarted();await heldCounts;}
   return fetchBeforeReturn(url,options);
  };
  document.querySelector('.nav-entry[data-page="list"]').click();
  await changed(doc.body,()=>row()!==beforeReturn,'the List replaces the row before loading the links');
  await readingCounts;
  check(draft.querySelector('[name=feedback]').value==='Keep this draft when I switch tabs.',
   'The draft is still available on the List.');
  close.click();
  check(!dialog.open && !link('sdlc/C1') && doc.activeElement===row().querySelector('.list-toggle'),
   'Close focuses the row while the List loads its links.');
  releaseCounts();
  await changed(doc.body,()=>link('sdlc/C1'),'the pending link appears');
  await animationFrames(win);
  check(doc.activeElement===link('sdlc/C1'),'The loaded link receives focus.');
  win.fetch=fetchBeforeReturn;
  readyLink=link('sdlc/C1');
 }
 const fetchList=win.fetch.bind(win);
 if(!phone){
  const timeout=win.AbortSignal.timeout,deadlines=[];
  win.AbortSignal.timeout=milliseconds=>{deadlines.push(milliseconds);return timeout(milliseconds);};
  for(const dismiss of ['close','escape','outside']){
   const saved=await readCard(link('sdlc/C1')),savedWindow=saved.defaultView;
   const savedForm=saved.querySelector('[data-review-form]');
   let finishRead,beginRead;
   const reading=new Promise(resolve=>{beginRead=resolve;});
   const heldRead=new Promise(resolve=>{finishRead=resolve;});
   win.fetch=(url,options)=>{
    if(url===win.location.pathname && options?.signal){
     beginRead();return heldRead;
    }
    return fetchList(url,options);
   };
   savedWindow.fetch=()=>Promise.resolve(new savedWindow.Response(JSON.stringify({answer:'Works'})));
   savedForm.requestSubmit(savedForm.querySelector('[value=works]'));
   await reading;
   check(dialog.open && !close.disabled,'A saved verdict enables Close before the List refresh ends.');
   if(dismiss==='close') close.click();
   if(dismiss==='escape') saved.body.dispatchEvent(new savedWindow.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
   if(dismiss==='outside'){
    dialog.dispatchEvent(new win.PointerEvent('pointerdown',{bubbles:true}));
    dialog.dispatchEvent(new win.MouseEvent('click',{bubbles:true}));
   }
   check(!dialog.open && doc.activeElement===link('sdlc/C1'),'The pending List refresh allows '+dismiss+'.');
   const pageStatus=doc.querySelector('[data-page-status]');
   const readFailed=changed(pageStatus,()=>pageStatus.dataset.state==='Failed','the held refresh fails');
   finishRead(new win.Response(null,{status:503}));
   await readFailed;
   await animationFrames(win);
   if(dialog.open) close.click();
  }
  check(deadlines.length>=3 && deadlines.every(value=>value===7000),
   'List refresh uses the configured seven-second request timeout.');
  win.AbortSignal.timeout=timeout;
  win.fetch=fetchList;
  readyLink=link('sdlc/C1');
 }
 let releaseList,releaseRefresh,listReads=0,delayRefresh=false;
 const listSave=new Promise(resolve=>{releaseList=resolve;});
 const listRefresh=new Promise(resolve=>{releaseRefresh=resolve;});
 if(!phone){
  win.fetch=async(url,options)=>{
   if(url===win.location.pathname){
    if(options?.method==='POST') return listSave;
    if(delayRefresh){listReads++;await listRefresh;}
   }
   return fetchList(url,options);
  };
  const listWords=row().querySelector('textarea[name="words"]');
  listWords.value='A List action is pending.';listWords.dispatchEvent(new win.Event('input',{bubbles:true}));
  const listForm=listWords.form;
  listForm.querySelector('[value="tell"]').click();
  check(!!listForm.dataset.sending,'The List action starts before the Review save.');
 }
 const card=await readCard(readyLink),cardWin=card.defaultView;
 const formInCard=()=>card.querySelector('[data-review-form]');
 const status=()=>card.querySelector('[data-send-status]').textContent;
 const button=outcome=>formInCard().querySelector('[value="'+outcome+'"]');
 const submit=outcome=>formInCard().requestSubmit(button(outcome));
 let reviewPosts=0;
 const fades=[];
 if(!phone){
  for(const realm of [win,cardWin]){
   const animate=realm.Element.prototype.animate;
   realm.Element.prototype.animate=function(keyframes,options){
    if(this.matches('[data-review-card] tbody, [data-list-review-dialog] iframe'))
     fades.push([this.tagName,options.duration]);
    return animate.call(this,keyframes,options);
   };
  }
  const fetchCard=cardWin.fetch.bind(cardWin);
  let releaseFailure;
  const failure=new Promise(resolve=>{releaseFailure=resolve;});
  cardWin.fetch=(url,options)=>{
   if(options?.method==='POST' && ++reviewPosts===1) return failure;
   return fetchCard(url,options);
  };
  const words='Keep these words through the failure and conflict.';
  formInCard().elements.feedback.value=words;
  submit('feedback');
  close.click();
  dialog.dispatchEvent(new win.Event('cancel',{cancelable:true}));
  card.body.dispatchEvent(new cardWin.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  dialog.dispatchEvent(new win.PointerEvent('pointerdown',{bubbles:true}));
  dialog.dispatchEvent(new win.MouseEvent('click',{bubbles:true}));
  check(dialog.open && close.disabled,'Close, Escape, and a click outside keep a saving card open.');
  releaseFailure(new cardWin.Response(JSON.stringify({reason:'The test save failed.'}),{status:503}));
  await changed(card.body,()=>status()==='Not saved: The test save failed.' && !close.disabled,'the failure remains visible');
  check(dialog.open && formInCard().elements.feedback.value===words,'The open card shows the failure and keeps the words.');
  const revision=formInCard().elements.revision.value;
  const updated=await fetch('/advance-overlay',{method:'POST'});
  check(updated.ok,'The source change is updated.');
  submit('works');
  await changed(dialog,()=>!close.disabled,'the card recovers from a conflict');
  check(formInCard().elements.revision.value!==revision &&
   status().startsWith('Not saved:') && formInCard().elements.feedback.value===words &&
   card.querySelector('[data-review-body]').textContent.includes('The source changed while the card was open.'),
   'A conflict loads the new revision and keeps the words and the reason. '+status());
  if(formInCard().elements.revision.value===revision){await report();return;}
  delayRefresh=true;
 }
 const before=row();
 const refreshed=new Promise((resolve,reject)=>{
  const notice=card.querySelector('[data-send-status]');
  const saveObserver=new card.defaultView.MutationObserver(()=>{
   if(notice.textContent.startsWith('Not saved:')){
    saveObserver.disconnect();observer.disconnect();reject(new Error(notice.textContent));
   }
  });
  const observer=new win.MutationObserver(()=>{
   if(row()!==before && !dialog.open){saveObserver.disconnect();observer.disconnect();resolve();}
  });
  saveObserver.observe(notice,{childList:true});
  observer.observe(doc.body,{childList:true,subtree:true,attributes:true,attributeFilter:['open']});
 });
 submit('works');
 if(!phone){
  await changed(card.body,()=>status().startsWith('Saved:'),'the verdict is saved');
  await animationFrames(win);
  check(dialog.open && listReads===0 && row()===before,'The Review save waits for the pending List action.');
  releaseList(new win.Response(JSON.stringify({reason:'The test List action was refused.'}),{status:400}));
  await changed(doc.body,()=>!row().querySelector('textarea[name="words"]').form.dataset.sending,
   'the refused List action ends');
  check(listReads===1 && dialog.open && !close.disabled && button('works').disabled,
   'A refused List action starts one refresh and keeps the saved form disabled.');
  submit('works');
  check(reviewPosts===3,'The saving form sends no second POST during the refresh.');
  releaseRefresh();
 }
 await refreshed;
 if(!phone) check(JSON.stringify(fades)===JSON.stringify([['TBODY',180],['TBODY',260],['IFRAME',180]]),
  'The overlay uses the configured card fade durations.');
 check(row().querySelector('[data-label="State"]').textContent.includes('Reviews done'),'The saved verdict updates the List row.');
 check(doc.activeElement===link('sdlc/C1'),'The refreshed link receives focus.');
 check(location.pathname==='/proof' && listFrame.contentDocument===doc && history.length===historyLength,
  'Opening, closing, and saving do not navigate.');
 check(reviewPlace()===savedReviewPlace,'The Review filters stay the same.');
 const filters=new URLSearchParams(win.location.hash.slice(1));
 check(new URL(listURL).pathname===win.location.pathname && filters.get('show')==='working' &&
  filters.get('q')==='linked reviews' && doc.querySelector('#list-search').value==='linked reviews' &&
  !row().querySelector('[data-detail]').hidden,'The List keeps the filters, search, and open row.');
 await report();
}
(listFrame || window).addEventListener('load',()=>{void inspect().catch(error=>{failures.push(String(error));void report();});},{once:true});
if(shot){const image=new Image();image.hidden=true;image.src='/complete';document.body.append(image);}
</script>'''.replace('WAITING_KEY', json.dumps(waiting_key))
        shell = shell.replace('</body>', probe + '</body>')
        proof = os.environ.get("LIST_OVERLAY_SCREENSHOT")
        _ = (self.root / "config/scratch.conf").write_text(
            f"visible_root={self.root / 'scratch'}\nholds_file={self.root / 'holds.json'}\n"
            + "# whole: 1..\ndefault_life_seconds=86400\n# whole: 1..\nrepo_depth=3\n"
            + "# whole: 1..\nsuffix_length=6\nrepo_skip=target node_modules .git\n"
            + "# whole: 1..\nrecord_lock_attempts=3\n# whole: 1..\nremoval_remote_timeout_seconds=30\n")
        for size in ("1440,1000", "600,900", "430,900", *(("screenshot",) if proof else ())):
            with self.subTest(size=size):
                _ = listing.write_text(original)
                browser_root = root / size.replace(",", "x")
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(serve.ReviewLinksHandler):
                    @override
                    def html_response(self, status: int, content: bytes) -> None:
                        if self.path == list_page.PAGE_PATH + "?direct=1":
                            hold = b'<img hidden src="/complete">' if not CHROME else b""
                            content = content.replace(b'</body>', hold + probe.encode() + b'</body>')
                        super().html_response(status, content)

                    @override
                    def send_head(self) -> BinaryIO | None:
                        path = urlsplit(self.path).path
                        if path == "/proof":
                            self.html_response(200, shell.encode())
                            return None
                        if path == "/complete":
                            _ = complete.wait(25)
                            self.json_response(200, {})
                            return None
                        return super().send_head()

                    @override
                    def do_POST(self) -> None:
                        if self.path == "/advance-overlay":
                            _ = listing.write_text(original.replace('Open the linked change.',
                                'The source changed while the card was open.'))
                            self.json_response(200, {})
                            return
                        if self.path == "/results":
                            results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                            self.json_response(200, {})
                            complete.set()
                            return
                        super().do_POST()

                with (patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(browser_root)}),
                      patch.object(time, "time", return_value=now.timestamp()),
                      patch.object(serve, "ReviewLinksHandler", Probe),
                      patch.object(serve, "loaded_module_stamps", return_value={}),
                      patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
                      patch.object(change_page, "recover_pending"), patch.object(list_page, "recover_pending"),
                      patch.object(change_page, "deliver"), patch.object(change_review, "_commit"),
                      patch.object(change_review, "lists_directory", return_value=self.root),
                      patch.object(staff, "snapshot", return_value=members),
                      patch.object(list_page, "counts_strip", return_value=""),
                      serve.ReviewLinksServer(("127.0.0.1", 0), browser_root, initialize_review=True) as server):
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        viewport = "1440,1000" if size == "screenshot" else size
                        width, height = map(int, viewport.split(","))
                        route = destination
                        if size == "430,900":
                            route = list_place.replace("#", "?direct=1#", 1)
                        elif size == "600,900":
                            route += "&phone=1"
                        url = f"http://127.0.0.1:{server.server_port}" + route
                        if size == "screenshot":
                            assert proof is not None
                            self.browser_screenshot(url + "&shot=1", width, height, Path(proof), complete, dark=True)
                        else:
                            self.browser_results(url, width, height, complete, dark=True)
                    finally:
                        complete.set()
                        server.shutdown()
                        thread.join(5)
                self.assertEqual(results, [[]])
                if size in ("1440,1000", "600,900"):
                    reviewed = {row["#"]: row["Verdict"] for row in change_review.review_snapshot(listing).reviewed}
                    self.assertIn("works", reviewed["C1"].casefold())

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_browser_list_links_show_the_target_inside_the_console(self) -> None:
        source = self.add("Follow the linked items", number=1)
        for number in range(2, 29):
            _ = self.add(f"Fixture item {number}", number=number)
        closed = self.add("Closed linked item", number=29, owner=OTHER.id)
        _ = the_list.close(MORTY.id, closed.id, "The work is complete.", source="morty")
        _ = self.add("Open blocker", number=30)
        other_producer = self.add("Linked item from another producer", number=31)
        _ = the_list.update(MORTY.id, other_producer.id, {"producer": DWIGHT.id}, source="morty")
        _ = the_list.update(MORTY.id, source.id, {"related_to": (29,),
            "blocked_by": (the_list.ItemDependency(30, "The blocker is complete."),)}, source="morty")
        records = list_page.records(include_closed=True)
        for index, entry in enumerate(records):
            if entry.item.id == source.id:
                records[index] = list_page.ListRecord(entry.keeper, replace(entry.item, related_to=(2, 29, 31, 99), unread_since=NOW))
            elif entry.item.id == "L30":
                records[index] = list_page.ListRecord(entry.keeper, replace(entry.item, unread_since=NOW))
        with patch.object(list_page, "records", return_value=records), patch.object(list_page, "counts_strip", return_value=""):
            listing = list_page.render(self.root / "reviews")
        listing = listing.replace(b'data-page-facts ', b'data-page-facts data-read-error="Test read failed" ', 1)
        listing = listing.replace(b'</body>', b'''<script>
addEventListener('message',event=>{
 if(event.source!==parent || event.origin!==location.origin || !event.data.clickListLink) return;
 if(event.data.pendingWords){
  const search=document.querySelector('#list-search');
  search.value=event.data.pendingWords;search.dispatchEvent(new Event('input'));
 }
 const links=document.querySelectorAll('[data-number="1"] [data-detail] a');
 [...links].find(link=>link.textContent==='#'+event.data.clickListLink).click();
});
</script></body>''')
        page = console.ConsolePage("list", "List", "list-checks", "The List", console.AvailablePage("CASE_PATH" + list_page.PAGE_PATH))
        shell = console.render((page,), console.UnavailablePage()).decode()
        probe = r'''<script>
const params=new URLSearchParams(location.search),number=params.get('number'),scenario=params.get('case');
const proof=scenario==='proof';
const frame=document.querySelector('iframe');
const frames=win=>new Promise(done=>win.requestAnimationFrame(()=>win.requestAnimationFrame(done)));
const changed=win=>new Promise(done=>win.addEventListener('hashchange',done,{once:true}));
frame.addEventListener('load',async()=>{
 const failures=[];
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 try{
  const doc=frame.contentDocument,win=frame.contentWindow;
  const row=id=>doc.querySelector('[data-number="'+id+'"]');
  const source=row(1),target=row(number),search=doc.querySelector('#list-search');
  const chip=(name,value)=>doc.querySelector('[data-filter-row="'+name+'"] [data-filter-value="'+value+'"]');
  const selected=name=>doc.querySelector('[data-filter-row="'+name+'"] [aria-pressed="true"]').dataset.filterValue;
  const unread=doc.getElementById('list-unread');
  const filters=()=>[selected('owner'),selected('producer'),search.value,unread.checked];
  const pendingWords=scenario==='pending-words' ? 'item' : '';
  const originalFilters=filters();
  if(pendingWords) originalFilters[2]=pendingWords;
  const link=[...source.querySelectorAll('a')].find(a=>a.textContent==='#'+number);
  const expanded=item=>item.querySelector('.list-toggle').getAttribute('aria-expanded')==='true';
  const inView=element=>{
   const rect=element.getBoundingClientRect();
   return rect.top>=0 && rect.bottom<=win.innerHeight;
  };
  const originalShow='working';
  check(!source.hidden && expanded(source) && selected('show')===originalShow,'the source starts open in the selected view');
  check(!!document.querySelector('.sidebar .nav-entry'),'the console navigator is visible');
  link.scrollIntoView({block:'center'});
  if(scenario==='history') win.scrollTo(0,120);
  const unavailableProducer=scenario==='unavailable-producer';
  if(unavailableProducer){
   const saved=new URL(win.location.href),params=new URLSearchParams(saved.hash.slice(1));
   params.set('producer','unavailable');
   saved.hash=String(params);
   win.history.replaceState(win.history.state,'',saved);
   win.history.scrollRestoration='manual';
   win.scrollTo(0,270);
   check(win.scrollY===270,'the previous page can scroll to 270');
  }
  const banner=doc.querySelector('[data-page-status]');
  if(scenario==='banner') banner.style.minHeight='60px';
  const originalTop=win.scrollY,beforeJump=win.location.href;
  const original=new URL(beforeJump),params=new URLSearchParams(original.hash.slice(1));
  if(pendingWords){params.set('q',pendingWords);original.hash=String(params);}
  const originalPlace=original.href;
  const debounced=pendingWords ? new Promise(done=>search.addEventListener('filterchange',done,{once:true})) : null;
  const consolePlace=()=>new URL(new URLSearchParams(location.search).get('at.list'),location.href).href;
  const updated=new Promise((resolve,reject)=>{
   const done=()=>{
    clearTimeout(timeout);removeEventListener('message',record);resolve();
   };
   const record=event=>{
    if(event.source===win && event.data==='console-place' && win.location.href!==beforeJump &&
       consolePlace()===win.location.href) done();
   };
   addEventListener('message',record);
   const timeout=setTimeout(()=>{
    removeEventListener('message',record);reject(new Error('The console did not record the List place.'));
   },8000);
  });
  win.postMessage({clickListLink:number,pendingWords},location.origin);await updated;await frames(win);
  if(debounced){
   await debounced;
   check(!target.hidden && expanded(target) && search.value==='','the target stays open after the pending input settles');
  }
  const missing=doc.querySelector('[data-missing-item]');
  check(doc===frame.contentDocument,'the click stays inside the same console frame');
  check(consolePlace()===win.location.href,'the console recorded '+consolePlace()+' instead of '+win.location.href);
  if(number==='99' || number==='29'){
   const message=number==='29' ? 'Item #29 is closed' : 'Item #99 is not on the List';
   check(!target && !missing.hidden && missing.textContent===message && inView(missing),'the item notice is in view');
  }else{
   const title=target.querySelector('.list-row');
   const fallback=['producer','open','pending-words','unread-words'].includes(scenario);
   check(selected('show')===(fallback ? 'all' : 'working'),'the view after the click is '+selected('show'));
   check(!target.hidden && expanded(target),'the target is visible and open');
   check(inView(title),'the target row is outside the viewport at '+title.getBoundingClientRect().top);
   check(title.getAnimations().some(animation=>animation.playState==='running'),'the target row has a short highlight');
   if(scenario==='banner'){
    const bottom=banner.getBoundingClientRect().bottom,top=title.getBoundingClientRect().top;
    check(win.getComputedStyle(banner).position==='sticky' && bottom>=60,'the tall status banner is sticky');
    check(top>=bottom+15,'the target top '+top+' leaves a gap below the banner bottom '+bottom);
   }
  }
  const expected=originalFilters.slice();
  if(['producer','open','pending-words'].includes(scenario)) expected[0]='all';
  if(scenario==='producer') expected[1]='all';
  if(['open','unread-words'].includes(scenario) || pendingWords) expected[2]='';
  check(filters().every((value,index)=>value===expected[index]),'the jump keeps each matching filter: '+JSON.stringify(filters()));
  if(scenario==='repeat'){
   const jumpPlace=win.location.href,jumpHistoryLength=win.history.length;
   link.click();await frames(win);
   check(win.location.href===jumpPlace && win.history.length===jumpHistoryLength,'a second click stays in the same history entry');
  }
  if(scenario==='history'){
   row(3).querySelector('.list-toggle').click();
   const item=new URLSearchParams(win.location.hash.slice(1)).get('item');
   check(item===row(3).dataset.key && expanded(row(3)) && !expanded(target),
    'Opening row 3 closes the linked row and records row 3 in the address.');
  }
  if(scenario==='closed'){
   const missingPlace=win.location.href;
   const valid=[...source.querySelectorAll('[data-record-jump]')].find(link=>link.textContent==='#2');
   valid.click();
   check(expanded(row(2)) && !expanded(source) && missing.hidden,'A valid link opens its row and clears the notice.');
   const backToMissing=changed(win);
   history.back();await backToMissing;await frames(win);
   check(win.location.href===missingPlace && expanded(source) && !expanded(row(2)) &&
    !missing.hidden && missing.textContent==='Item #29 is closed',
    'Back restores the open source and the notice for the closed item.');
  }
  if(!proof){
   const back=changed(win);
   history.back();await back;await frames(win);
   if(unavailableProducer){
    const restored=new URLSearchParams(win.location.hash.slice(1));
    check(!restored.has('producer'),'Back removes the unavailable Producer from the frame address');
    check(win.history.state.recordTable.top===270,'the corrected history state keeps top: 270');
    check(win.scrollY===270,'Back restores scroll to 270, got '+win.scrollY);
   }else{
    check(win.location.href===originalPlace,'Back restores the frame address');
    check(inView(link) && Math.abs(win.scrollY-originalTop)<=1,'Back restored scroll '+win.scrollY+' instead of '+originalTop);
   }
   check(selected('show')===originalShow && !source.hidden && expanded(source),'Back restores the open source');
   check(filters().every((value,index)=>value===originalFilters[index]),'Back restores the staff chips, search and switch.');
   check(!target || !expanded(target),'Back collapses the target row');
   if(scenario==='history') check(!expanded(row(3)),'Back closes row 3.');
  }
  if(scenario==='closed'){
   const navigated=changed(win);
   win.location.hash=link.hash;
   await navigated;
   check(!source.hidden && expanded(source) && !missing.hidden && missing.textContent==='Item #29 is closed',
    'A direct link to a closed item shows the notice and keeps the source open.');
  }
  if(scenario==='missing'){
   const missingKey=new URLSearchParams(link.hash.slice(1)).get('item');
   const saved=new URLSearchParams(new URL(originalPlace).hash.slice(1));
   saved.set('key',source.dataset.key);saved.set('item',missingKey);
   const clearSearch=()=>new Promise(done=>{
    search.addEventListener('filterchange',done,{once:true});
    search.value='';search.dispatchEvent(new win.Event('input'));
   });
   const changes=[
    ['Show',()=>chip('show','all').click()],
    ['Unread only',()=>unread.click()],
    ['Owner',()=>chip('owner','all').click()],
    ['Producer',()=>chip('producer','all').click()],
    ['Search',clearSearch],
    ['sort',()=>doc.querySelector('[data-sort]').click()],
   ];
   for(const [label,select] of changes){
    const restored=changed(win);win.location.hash=String(saved);await restored;
    check(!missing.hidden && doc.querySelector('[data-missing-keys]').hidden,'the restored missing item shows one notice');
    await select();
    const after=new URLSearchParams(win.location.hash.slice(1));
    check(missing.hidden && !doc.querySelector('[data-missing-keys]').textContent,label+' clears the missing item notice');
    check(after.get('item')!==missingKey && after.getAll('key').join()===source.dataset.key,label+' omits the missing item from the address');
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('CASE_PATH/list-jump-result',{method:'POST',body:JSON.stringify(failures)});
},{once:true});
</script><img hidden src="CASE_PATH/list-jump-ready">'''
        shell = shell.replace('</body>', probe + '</body>')
        proof = os.environ.get("LIST_JUMP_PROOF")
        cases = [(30, "open"), (29, "closed"), (31, "producer"), (99, "missing"), (29, "repeat"),
                 (2, "history"), (2, "banner"), (30, "pending-words"), (30, "unavailable-producer"),
                 (30, "unread-words")]
        if proof:
            cases.append((29, "proof"))
        for number, case in cases:
            with self.subTest(link=case):
                prefix = "/" + uuid4().hex
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(BaseHTTPRequestHandler):
                    result_rows: ClassVar[list[list[str]]] = results
                    result_event: ClassVar[threading.Event] = complete
                    console_page: ClassVar[bytes] = shell.replace("CASE_PATH", prefix).encode()

                    def do_GET(self) -> None:
                        path = urlsplit(self.path).path
                        if path == '/list-jump-ready':
                            _ = self.result_event.wait(35)
                        body = listing if path == list_page.PAGE_PATH else self.console_page if path == "/console/" else b'{}'
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8' if body != b'{}' else 'application/json')
                        self.end_headers()
                        with suppress(BrokenPipeError):
                            _ = self.wfile.write(body)

                    def do_POST(self) -> None:
                        if self.path == '/list-jump-result':
                            self.result_rows.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                            self.result_event.set()
                        self.send_response(204)
                        self.end_headers()

                    @override
                    def log_message(self, format: str, *args: object) -> None:
                        pass

                filters = {"sort": "#", "item": f"{MORTY.id}/{source.id}"}
                if case in ("open", "closed", "producer", "missing"):
                    filters.update(owner=OWNER.id, producer=MORTY.id, q="linked item")
                if case == "unread-words":
                    filters.update(unread="1", q="item")
                place = prefix + list_page.PAGE_PATH + "?show=working#" + urlencode(filters)
                url = prefix + "/console/" + console.place_query("list", place)
                with self.probe_server(prefix, Probe) as server:
                    try:
                        destination = f'http://127.0.0.1:{server.server_port}{url}&number={number}&case={case}'
                        if case == "proof" and proof:
                            self.browser_screenshot(destination, 1440, 1100, Path(proof), complete, dark=True)
                        else:
                            self.browser_results(destination, 1440, 1100, complete, dark=True)
                        self.assertEqual(results, [[]])
                    finally:
                        complete.set()

    def test_staff_owner_and_producer_names_have_role_colors_but_nate_stays_plain(self) -> None:
        _ = self.add("Staff names")
        _ = self.add("User name", owner=the_list.UnassignedOwner.NATE)
        body = list_page.render(self.root / "reviews").decode()
        self.assertRegex(body, r'data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(body, r'data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn('<td data-label="Owner"><span class="list-owner-name"><span>nate</span></span></td>', body)

    def test_added_log_shows_staff_colors_local_eta_and_field_labels(self) -> None:
        _ = self.add("Log labels")
        body = list_page.render(self.root / "reviews").decode()
        log = body.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]
        self.assertRegex(log, r'Owner</dt><dd><span[^>]*data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(log, r'Producer</dt><dd><span[^>]*data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn("ETA</dt><dd>14:00 EST – 15:00 EST</dd>", log)
        self.assertIn("State</dt><dd>Working</dd>", log)
        self.assertIn("Next step</dt><dd>", log)
        self.assertIn("Latest status</dt><dd>", log)
        shown = re.sub(r"<[^>]+>", "", log)
        for text in (OWNER.id, MORTY.id, "EtaRange(", "next_step", "latest_status"):
            self.assertNotIn(text, shown)

    def test_updated_log_shows_full_eta_and_shares_the_next_step_renderer(self) -> None:
        item = self.add("Log an update")
        _ = the_list.update(MORTY.id, item.id, {
            "eta": NOW + timedelta(hours=3), "next_step": "Read the **new** result <first>."}, source="morty")
        body = list_page.render(self.root / "reviews").decode()
        log = body.split('<ol class="list-log">', 1)[1].split("</li>", 1)[0]
        self.assertIn("Updated", log)
        eta = body.split('<span class="list-eta-text">', 1)[1].split("</span>", 1)[0]
        self.assertEqual(eta, "Nov 16 16:00 EST")
        self.assertIn("ETA</dt><dd>16:00 EST</dd>", log)
        next_step = body.split('<td data-label="Next step">', 1)[1].split("</td>", 1)[0]
        self.assertIn("Read the <strong>new</strong> result &lt;first&gt;.", next_step)
        self.assertIn(f"Next step</dt><dd>{next_step}</dd>", log)

    def test_updated_log_omits_values_that_match_the_previous_update(self) -> None:
        item = self.add("Show changed fields")
        initial_step = item.next_step
        _ = the_list.update(MORTY.id, item.id, {"next_step": "Read the new result."}, source="morty")
        _ = the_list.update(MORTY.id, item.id, {"next_step": "Read the new result.",
                                                "latest_status": "The result is ready."}, source="morty")
        _ = the_list.update(MORTY.id, item.id, {"next_step": initial_step, "latest_status": "The result is ready."},
                            source="morty")
        body = list_page.render(self.root / "reviews", include_counts=False).decode()
        log = body.split('<ol class="list-log">', 1)[1]
        first, second, third = (log.split(f'<li data-log-index="{index}">', 1)[1].split('</li>', 1)[0]
                                for index in (1, 2, 3))
        self.assertIn('<dt>Next step</dt>', first)
        self.assertIn('<dt>Latest status</dt>', second)
        self.assertNotIn('<dt>Next step</dt>', second)
        self.assertIn(f'<dt>Next step</dt><dd><p>{initial_step}</p></dd>', third)
        self.assertNotIn('<dt>Latest status</dt>', third)
        self.assertIn(f'<time datetime="{NOW.isoformat()}">13:00 EST</time>', second)
        self.assertIn('morty</small>', without_role_colors(second))

    def test_updated_log_keeps_the_time_and_source_when_no_fields_change(self) -> None:
        log = self.legacy_log(
            '- 2026-11-16 18:00:00+00:00 Added; state: "Working" — nate',
            '- 2026-11-16 18:10:00+00:00 Updated state: "Working" — morty')
        update = log.split('<li data-log-index="1">', 1)[1].split('</li>', 1)[0]
        self.assertIn('<time datetime="2026-11-16T18:10:00+00:00">13:10 EST</time>', update)
        self.assertIn('<strong>Updated <small>— morty</small></strong>', without_role_colors(update))
        self.assertNotIn('<dl>', update)

    def test_log_keeps_added_evidence_and_shows_only_new_evidence_in_closed_details(self) -> None:
        item = self.add("Show the Evidence history")
        added = item.evidence
        appended = "\nThe <check> passed."
        replacement = "A different result."
        _ = the_list.update(MORTY.id, item.id, {"evidence": added + appended}, source="morty")
        _ = the_list.update(MORTY.id, item.id, {"evidence": replacement}, source="morty")
        _ = the_list.update(MORTY.id, item.id, {"evidence": replacement, "next_step": "Read the result."}, source="morty")
        body = list_page.render(self.root / "reviews", include_counts=False).decode()
        log = body.split('<ol class="list-log">', 1)[1]
        added_html = "Read https://example.invalid/proof?x=1&amp;y=2."
        for index, expected in enumerate((added_html, "The &lt;check&gt; passed.", replacement)):
            with self.subTest(index=index):
                row = log.split(f'<li data-log-index="{index}">', 1)[1].split('</li>', 1)[0]
                self.assertIn('<dt>Evidence</dt><dd><details class="log-evidence">'
                              + f'<summary id="list-evidence-{MORTY.id}-{item.id}-{index}">Details</summary>'
                              + f'<div>{expected}</div></details></dd>', row)
        unchanged = log.split('<li data-log-index="3">', 1)[1].split('</li>', 1)[0]
        self.assertNotIn('<dt>Evidence</dt>', unchanged)

    def test_log_shows_when_an_update_clears_evidence(self) -> None:
        item = self.add("Clear the Evidence")
        _ = the_list.update(MORTY.id, item.id, {"evidence": ""}, source="morty")
        _ = the_list.update(MORTY.id, item.id, {"evidence": "", "next_step": "Check the log."}, source="morty")
        body = list_page.render(self.root / "reviews", include_counts=False).decode()
        log = body.split('<ol class="list-log">', 1)[1]
        cleared = log.split('<li data-log-index="1">', 1)[1].split('</li>', 1)[0]
        unchanged = log.split('<li data-log-index="2">', 1)[1].split('</li>', 1)[0]
        self.assertIn('<dt>Evidence</dt><dd><details class="log-evidence">', cleared)
        self.assertIn('<div>Not recorded</div></details>', cleared)
        self.assertNotIn('<dt>Evidence</dt>', unchanged)

    def test_log_marks_historical_question_events_with_bold_labels_and_the_question_class(self) -> None:
        item = self.add("Show historical questions")
        question = question_record.Question("Q2", NOW.isoformat(), "Which check failed?")
        item = replace(item, log=(*item.log,
            the_list.ItemChange(NOW, the_list.QuestionAnswered("Q1"), "morty"),
            the_list.ItemChange(NOW, the_list.QuestionClosed(question), "nate")))
        log = list_page.item_log_html(list_page.ListRecord(MORTY.id, item), NOW.astimezone(user_zone()),
                                      staff_names.read(), frozenset())
        self.assertIn('<li class="question" data-log-index="1">', log)
        self.assertIn('<div><strong>Answer</strong> to question Q1', log)
        self.assertIn('<li class="question" data-log-index="2">', log)
        self.assertIn('<div><strong>Question</strong> Q2 closed with the item', log)

    def test_list_and_review_emit_the_question_color_rule(self) -> None:
        body = list_page.render(self.root / "reviews", include_counts=False).decode()
        rule = '.question{--type-color:#b99aff}'
        self.assertIn(rule, review_page.STYLE)
        self.assertIn(rule, body)
        self.assertIn('.list-log>li.question{border-inline-start:3px solid var(--type-color);', body)

    def test_latest_status_shows_the_update_time_in_the_user_zone(self) -> None:
        item = self.add("Status time")
        updated = NOW + timedelta(minutes=20)
        with patch.object(the_list, "now", return_value=updated):
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "The report is ready."}, source="morty")
        for read_at, expected in ((updated, "13:20 EST"), (updated + timedelta(days=1), "2026-11-16 13:20 EST")):
            with self.subTest(read_at=read_at), patch.object(time, "time", return_value=read_at.timestamp()):
                body = list_page.render(self.root / "reviews", include_counts=False).decode()
                self.assertIn(f'<h3>Latest status · <time datetime="{updated.isoformat()}">{expected}</time></h3>', body)

        read_at = NOW + timedelta(hours=11, minutes=5)
        with (patch.object(the_list, "now", return_value=read_at),
              patch.object(time, "time", return_value=read_at.timestamp())):
            read = the_list.mark_read(MORTY.id, item.id, source="nate")
            self.assertEqual(read.updated, read_at)
            body = list_page.render(self.root / "reviews", include_counts=False).decode()
            self.assertIn(f'<h3>Latest status · <time datetime="{updated.isoformat()}">2026-11-16 13:20 EST</time></h3>', body)

    def test_log_shows_the_recorded_clock_time_once(self) -> None:
        log = self.legacy_log('- 2026-11-16 18:20:00+00:00 Updated state: "Working" — morty, 09:00 CST')
        self.assertIn('<time datetime="2026-11-16T18:20:00+00:00">13:20 EST</time>', log)
        self.assertIn("morty</small>", without_role_colors(log))
        self.assertEqual(log.count("13:20 EST"), 1)
        self.assertNotIn("09:00 CST", log)

    def test_legacy_log_text_renders_with_a_new_field_change(self) -> None:
        item = self.add("Keep the original log")
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        base, _, _ = path.read_text().partition("## Log\n\n")
        _ = path.write_text(base + "## Log\n\n- 2026-11-16 17:30:00+00:00 Recorded <old words> — nate\n")
        original = the_list.load(MORTY.id, item.id)
        _ = the_list.update(MORTY.id, item.id, {"eta": NOW + timedelta(hours=3),
            "evidence": 'Source checkins.json item:\n\n{"owner": "sdlc"}\n'}, source="morty")
        _ = the_list.clean_evidence(MORTY.id, source="morty")
        _ = the_list.link(MORTY.id, item.id, ("review<key>",), source="morty")
        _ = the_list.unlink(MORTY.id, item.id, ("review<key>",), source="morty")
        _ = the_list.close(MORTY.id, item.id, "Keep this record.", source="morty")
        _ = the_list.reopen(MORTY.id, item.id, source="morty")
        self.assertEqual(the_list.load(MORTY.id, item.id).log[0], original.log[0])
        body = list_page.render(self.root / "reviews").decode()
        log = body.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]
        self.assertIn("Recorded &lt;old words&gt;", log)
        self.assertIn("ETA</dt><dd>16:00 EST</dd>", log)
        self.assertIn("Linked reviews: review&lt;key&gt;", log)
        self.assertIn("Unlinked reviews: review&lt;key&gt;", log)
        self.assertIn("Reopened <small>", log)
        self.assertIn("Removed the migration copy of checkins.json from Evidence", log)

    def legacy_log(self, *lines: str) -> str:
        item = self.add("Keep the original log")
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        body, marker, _ = path.read_text().partition("## Log\n\n")
        _ = path.write_text(body + marker + "\n".join(lines) + "\n")
        rendered = list_page.render(self.root / "reviews").decode()
        return rendered.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]

    def test_old_added_log_uses_the_field_renderer(self) -> None:
        fields = {"title": "Keep the original log", "owner": OWNER.id, "producer": MORTY.id,
                  "state": "Working", "eta": repr(the_list.EtaRange(NOW + timedelta(hours=1), NOW + timedelta(hours=2))),
                  "next_step": 'Read "the result"; then check the page.\nKeep the note.'}
        note = "; ".join(f"{key}: {json.dumps(value)}" for key, value in fields.items())
        log = self.legacy_log("- 2026-11-16 18:00:00+00:00 Added; " + note + " — nate")
        self.assertRegex(log, r'Owner</dt><dd><span[^>]*data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(log, r'Producer</dt><dd><span[^>]*data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn("State</dt><dd>Working</dd>", log)
        self.assertIn("ETA</dt><dd>14:00 EST – 15:00 EST</dd>", log)
        self.assertIn("Next step</dt><dd>", log)
        self.assertIn('Read &quot;the result&quot;; then check the page.', log)
        self.assertNotIn("EtaRange", log)

    def test_old_updated_log_reads_descriptions_and_datetimes(self) -> None:
        log = self.legacy_log(
            "- 2026-11-16 18:10:00+00:00 Updated eta: "
            + json.dumps("EtaDescription(text='Pending (check 16:45 EST)')")
            + '; next_step: "Wait for the check." — morty',
            '- 2026-11-16 18:20:00+00:00 Updated eta: "2026-11-16 21:00:00+00:00" — morty',
            '- 2026-11-17 16:54:58.780107+00:00 Updated next_check: "2026-11-17 16:00:00-05:00" — morty, 11:54 EST',
            '- 2026-11-17 03:12:17.088559+00:00 Updated state: "Working" — builds, 22:12 EST')
        self.assertIn("ETA</dt><dd>Pending (check 16:45 EST)</dd>", log)
        self.assertIn("ETA</dt><dd>16:00 EST</dd>", log)
        self.assertIn("Next check</dt><dd>Nov 17 16:00 EST</dd>", log)
        self.assertIn("State</dt><dd>Working</dd>", log)
        self.assertNotIn("EtaDescription", log)

    def test_old_feedback_log_uses_the_feedback_renderer(self) -> None:
        rows = [
            json.dumps({"request": "feedback", "words": "Read the **result** <today>.",
                        "state": state, "alerted_to": alerted_to})
            for state, alerted_to in (("pending", ""), ("sent", ""), ("alerted", "sdlc"))
        ]
        log = self.legacy_log(*(
            "- 2026-11-16 18:10:00+00:00 Feedback to morty: " + row + " — nate via List" for row in rows))
        for label in ("Feedback for morty", "Feedback sent to morty", "Feedback could not reach morty"):
            self.assertIn(label, log)
        self.assertEqual(log.count("Read the <strong>result</strong> &lt;today&gt;."), 3)
        self.assertNotIn("&quot;request&quot;", log)

    def test_old_park_log_uses_the_local_date_renderer(self) -> None:
        log = self.legacy_log(
            "- 2026-11-16 18:10:00+00:00 Parked until 2026-11-17T05:00:00+00:00 — nate",
            "- 2026-11-16 18:20:00+00:00 Parked until not parked — nate")
        self.assertIn("Parked until</dt><dd>Nov 17 00:00 EST</dd>", log)
        self.assertIn("Parked until</dt><dd>Not parked</dd>", log)
        self.assertNotIn("2026-11-17T05:00:00+00:00", log)

    def test_park_date_matches_the_response_and_table(self) -> None:
        root = self.root / "reviews"
        item = self.add("Park this item")
        _ = the_list.park(MORTY.id, item.id, the_list.time_value("2026-11-18T02:00:00+00:00"), source="nate")
        with self.subTest(view="table"):
            body = list_page.render(root).decode()
            self.assertIn("Parked until Nov 17 21:00 EST</small>", body)
            self.assertIn("Parked until</dt><dd>Nov 17 21:00 EST</dd>", body)
        with self.subTest(view="response"):
            self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["parked_until"], "Nov 17 21:00 EST")

    def test_old_closed_log_uses_the_closing_words_renderer(self) -> None:
        log = self.legacy_log("- 2026-11-16 18:10:00+00:00 Closed: The **check** passed. — nate")
        self.assertIn("Closing words</dt><dd>", log)
        self.assertIn("The <strong>check</strong> passed.", log)

    def test_old_log_keeps_unparseable_notes_as_text(self) -> None:
        log = self.legacy_log(
            "- 2026-11-16 18:00:00+00:00 Added on sdlc's request <old words> — nate",
            '- 2026-11-16 18:10:00+00:00 Updated state: "Working"; next_step: invalid — nate',
            '- 2026-11-16 18:20:00+00:00 Updated state: "Done" — nate',
            '- 2026-11-16 18:30:00+00:00 Updated title: "" — nate',
            r'- 2026-11-16 18:40:00+00:00 Updated unit: "first\nsecond" — nate')
        self.assertIn("Added on sdlc&#x27;s request &lt;old words&gt;", log)
        self.assertIn("Updated title: &quot;&quot;", log)
        self.assertIn(r"Updated unit: &quot;first\nsecond&quot;", log)
        self.assertIn("next_step: invalid", log)
        self.assertEqual(log.count("<dl>"), 1)
        self.assertIn("State</dt><dd>Done</dd>", log)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_log_fits_the_left_column_and_keeps_each_scroll_position(self) -> None:
        for number in (1, 2):
            item = self.add(f"Log for item {number}", number=number)
            for update in range(18):
                _ = the_list.update(MORTY.id, item.id, {"next_step": f"Check result {update}."}, source="morty")
        _ = the_list.add(MORTY.id, "Short log", number=3, owner=OWNER.id, source="nate", changes={})
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = r'''<script>
const freshList=MARKUP;
let reads=0;
window.fetch=async()=>{reads++;return {ok:true,text:async()=>freshList};};
</script>'''.replace('MARKUP', json.dumps(markup).replace('</', '<\\/'))
        probe = r'''<script>
(async()=>{
const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
const panel=number=>document.querySelector('[data-number="'+number+'"] .list-panel');
const open=number=>document.querySelector('[data-number="'+number+'"] .list-toggle').click();
const scrollArea=right=>right.querySelector('.list-log-scroll') ?? right.querySelector('.list-log');
const height=node=>node.getBoundingClientRect().height;
try{
 open(1);
 const [left,right]=panel(1).children,log=scrollArea(right),heading=right.querySelector('h3');
 right.style.display='none';
 const naturalHeight=height(left);
 right.style.display='';
 check(height(right)===naturalHeight && height(left)===naturalHeight,'The left column sets the height of a long log.');
 const headingTop=heading.getBoundingClientRect().top;
 log.scrollTop=90;
 check(log.scrollTop===90 && log.scrollHeight>log.clientHeight &&
  heading.getBoundingClientRect().top===headingTop,'The long log scrolls below its fixed heading.');
 open(2);
 const secondLog=scrollArea(panel(2).lastElementChild);
 secondLog.scrollTop=45;
 open(3);
 const shortLog=scrollArea(panel(3).lastElementChild);
 check(shortLog.scrollHeight===shortLog.clientHeight && getComputedStyle(shortLog).overflowY==='auto',
  'The short log fits without a scroll bar.');
 const original=document.querySelector('[data-list]');
 const replaced=new Promise(resolve=>{
  const observer=new MutationObserver(()=>{if(!original.isConnected){observer.disconnect();resolve();}});
  observer.observe(original.parentNode,{childList:true});
 });
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await replaced;
 const thirdAtTop=scrollArea(panel(3).lastElementChild).scrollTop===0;
 open(1);
 const firstPosition=scrollArea(panel(1).lastElementChild).scrollTop;
 open(2);
 check(reads===1 && thirdAtTop && firstPosition===90 && scrollArea(panel(2).lastElementChild).scrollTop===45,
  'Refresh preserves each log position when its row opens.');
}catch(error){failures.push(String(error));}
const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
document.body.append(result);
})();</script>'''
        self.browser_actions(markup, setup, probe, window_size=(1440, 900))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_log_refresh_preserves_the_visible_entry_and_keeps_the_top_at_zero(self) -> None:
        self.browser_log_refresh('''
 const evidence=(number,index)=>log(number).querySelector('[data-log-index="'+index+'"] .log-evidence');
 open(2);
 check(log(2).scrollTop===0,'The second item starts at the top.');
 open(1);
 evidence(1,0).open=true;
 log(1).scrollTop=239;
 const before=firstVisible(log(1));
 check(before.offset<0 && log(1).scrollTop>0,'The first item starts partway through an entry.');
 evidence(1,0).querySelector('summary').focus({preventScroll:true});
 await refresh();
 check(evidence(1,0).open && !evidence(1,1).open && !evidence(2,0).open,
  'Evidence stays open only for the same item and log entry.');
 check(document.activeElement===evidence(1,0).querySelector('summary'),
  'Keyboard focus stays on the same Evidence summary.');
 const after=firstVisible(log(1));
 check(after.text===before.text && Math.abs(after.offset-before.offset)<=1,
  'The first item keeps the same entry and offset after the new entry arrives.');
 open(2);
 check(log(2).scrollTop===0 && firstVisible(log(2)).text.includes('New result for item 2.'),
  'The second item stays at the top and shows the new entry.');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_log_refresh_restores_the_position_when_a_filter_reveals_the_item(self) -> None:
        self.browser_log_refresh('''
 open(1);
 log(1).scrollTop=239;
 const before=firstVisible(log(1));
 const show=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]').click();
 show('blocked');
 check(row(1).hidden,'The filter hides the expanded row.');
 await refresh();
 await refresh();
 check(row(1).hidden,'The row stays hidden after both refreshes.');
 show('working');
 check(!row(1).hidden && !row(1).querySelector('[data-detail]').hidden,'The filter reveals the expanded row.');
 const after=firstVisible(log(1));
 check(after.text===before.text && Math.abs(after.offset-before.offset)<=1,
  'The revealed log restores its entry and offset after two hidden refreshes.');
''')

    def browser_log_refresh(self, steps: str) -> None:
        for number in (1, 2):
            item = self.add(f"Log for item {number}", number=number)
            _ = the_list.update(MORTY.id, item.id, {"next_step": "Check result 0.",
                                                    "evidence": "The first check passed."}, source="morty")
            for update in range(1, 18):
                _ = the_list.update(MORTY.id, item.id, {"next_step": f"Check result {update}."}, source="morty")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
            for number in (1, 2):
                _ = the_list.update(MORTY.id, f"L{number}", {"next_step": f"New result for item {number}."}, source="morty")
            fresh = list_page.render(self.root / "reviews").decode()
        setup = r'''<script>
const freshList=MARKUP;
window.fetch=async()=>({ok:true,text:async()=>freshList});
</script>'''.replace('MARKUP', json.dumps(fresh).replace('</', '<\\/'))
        probe = r'''<script>
(async()=>{
const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
const row=number=>document.querySelector('[data-number="'+number+'"]');
const open=number=>row(number).querySelector('.list-toggle').click();
const log=number=>row(number).querySelector('.list-log-scroll');
function firstVisible(log){
 const top=log.getBoundingClientRect().top;
 const entry=[...log.querySelector('.list-log').children].find(entry=>entry.getBoundingClientRect().bottom>top);
 return {text:entry.textContent,offset:entry.getBoundingClientRect().top-top};
}
function refresh(){
 const original=document.querySelector('[data-list]');
 return new Promise(resolve=>{
  const observer=new MutationObserver(()=>{if(!original.isConnected){observer.disconnect();resolve();}});
  observer.observe(original.parentNode,{childList:true});
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 });
}
try{
''' + steps + '''
}catch(error){failures.push(String(error));}
const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
document.body.append(result);
})();</script>'''
        self.browser_actions(markup, setup, probe, window_size=(1440, 900))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_log_uses_the_configured_height_at_narrow_width(self) -> None:
        item = self.add("Long log for a narrow view", number=1)
        for update in range(18):
            _ = the_list.update(MORTY.id, item.id, {"next_step": f"Check result {update}."}, source="morty")
        settings = self.root / "config/review_links.conf"
        original = settings.read_text()
        for limit in (8, 12):
            with self.subTest(limit=limit):
                _ = settings.write_text(re.sub(r"(?m)^list_log_max_height_rem=.*$", f"list_log_max_height_rem={limit}", original))
                with patch.object(list_page, "counts_strip", return_value=""):
                    markup = list_page.render(self.root / "reviews").decode()
                probe = r'''<script>
document.querySelector('.list-toggle').click();
const [left,right]=document.querySelector('.list-panel').children;
const log=right.querySelector('.list-log-scroll') ?? right.querySelector('.list-log');
const expected=LIMIT*parseFloat(getComputedStyle(document.documentElement).fontSize);
const fits=right.getBoundingClientRect().top>=left.getBoundingClientRect().bottom &&
 log.clientHeight===expected && log.scrollHeight>log.clientHeight;
const result=document.createElement('p');result.id='probe-results';
result.textContent=JSON.stringify(fits ? [] : ['The narrow log uses the configured height.']);
document.body.append(result);
</script>'''.replace('LIMIT', str(limit))
                self.browser_actions(markup, "", probe, window_size=(500, 900))

    def test_log_entries_share_one_label_column(self) -> None:
        self.assertIn(".list-log dl{display:grid;grid-template-columns:min(7rem,42%) minmax(0,1fr)", list_page.STYLE)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="list-page-test-")))
        config = self.root / "config"
        changes = self.root / "change-reviews"
        _ = shutil.copytree(self.browser_root / "config", config)
        _ = (config / "change_review.conf").write_text(
            f"lists_directory={changes}\nreview_links_directory={self.root / 'reviews'}\n")
        environment = {name: value for name, value in os.environ.items() if not name.startswith("GIT_")}
        environment.update({"CONF_DIR": str(config), "CHANGE_REVIEW_DIR": str(changes),
            "SHOWRUNNER_STATE_DIR": str(self.root / "state"), "STAFF_FILE": str(self.root / "staff.json"),
            "USER_ZONE": "America/New_York", "REVIEW_LINKS_ROOT": str(self.root / "reviews")})
        _ = cast(object, self.enterContext(patch.dict(os.environ, environment, clear=True)))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            row.to_json() for row in (MORTY, DWIGHT, OWNER, OTHER)]}))
        _ = self.enterContext(patch.object(the_list, "now", return_value=NOW))
        _ = self.enterContext(patch.object(time, "time", return_value=NOW.timestamp()))
        _ = self.enterContext(patch.object(rebuild_row, "read", return_value=rebuild_row.RebuildAnswer((), False)))
        _ = change_page.console_folder(self.root / "reviews")

    def add(self, title: str, state: the_list.ItemState = the_list.ItemState.WORKING, *,
            owner: the_list.ItemOwner = OWNER.id, number: int | the_list.NextNumber = the_list.NextNumber.AVAILABLE,
            next_check: the_list.NextCheck = the_list.NoTimestamp.UNSET) -> the_list.ListItem:
        return the_list.add(MORTY.id, title, owner=owner, number=number, source="nate", changes={
            "state": state, "next_check": next_check, "next_step": "Read the result and check the page.",
            "latest_status": "The page reads the saved records.", "done_when": "The checks pass.",
            "evidence": "Read https://example.invalid/proof?x=1&y=2.",
            "eta": the_list.EtaRange(NOW + timedelta(hours=1), NOW + timedelta(hours=2))})

    def fixture_items(self) -> list[the_list.ListItem]:
        items = [self.add("Remove the mesh code", the_list.ItemState.WAITING_FOR_NATE, number=58),
                 self.add("Console status", number=105, owner=OTHER.id, next_check=NOW - timedelta(minutes=10)),
                 self.add("The List in the console Straße", number=106, next_check=NOW + timedelta(hours=1)),
                 self.add("Shared documentation structure", the_list.ItemState.PAUSED, number=41),
                 self.add("Read the delivered page", the_list.ItemState.DONE, number=63),
                 self.add("Removed task", number=54)]
        _ = the_list.mark_read(MORTY.id, items[0].id, source="nate")
        _ = the_list.update(MORTY.id, items[1].id, {"producer": DWIGHT.id}, source="morty")
        _ = the_list.update(MORTY.id, items[3].id, {"producer": DWIGHT.id}, source="morty")
        _ = the_list.park(MORTY.id, items[3].id, NOW + timedelta(days=2), source="nate")
        _ = the_list.close(MORTY.id, items[5].id, "The user removed this task.", source="nate")
        return items

    def test_records_share_cli_order_and_each_render_reads_current_fields(self) -> None:
        items = self.fixture_items()
        _ = the_list.update(MORTY.id, items[0].id, {"related_to": (106,),
            "blocked_by": (the_list.ItemDependency(105, "The status page reaches main"),)}, source="morty")
        markup = list_page.render(self.root / "reviews").decode()
        body = without_role_colors(markup)
        self.assertEqual(body.count(page_frame.BUTTON_STATES), 1)
        self.assertEqual(body.count('<tbody data-list-item'), 5)
        for label in list_page.HEADERS:
            self.assertIn(label + '<span data-sort-mark', body)
        controls = body.split('data-record-filters>', 1)[1].split('<p data-missing-item', 1)[0]
        self.assertNotIn('<select', controls)
        self.assertEqual(controls.count('type="search"'), 1)
        self.assertEqual([part.split('"', 1)[0] for part in controls.split('data-filter-row="')[1:]],
                         ['owner', 'producer', 'show'])
        shows = controls.split('data-filter-row="show"', 1)[1].split('</div>', 1)[0]
        self.assertEqual(re.findall(r'data-filter-value="([^"]+)"[^>]*>([^<]+)<b>', shows),
                         [("all", "All"), ("needs-me", "Needs me"), ("morty-answering", "Morty answering"),
                          ("working", "Working"), ("blocked", "Blocked"), ("parked", "Parked")])
        self.assertIn('role="switch" id="list-unread" value="1">Unread only</label>', controls)
        owners = controls.split('data-filter-row="owner"', 1)[1].split('</div>', 1)[0]
        producers = controls.split('data-filter-row="producer"', 1)[1].split('</div>', 1)[0]
        chip = r'data-filter-value="([^"]+)"[^>]*>([^<]+)<b>(\d+)</b>'
        self.assertEqual(re.findall(chip, owners),
                         [('all', 'All', '5'), (f'staff:{OTHER.id}', 'builds', '1'), (f'staff:{OWNER.id}', 'sdlc', '4')])
        self.assertEqual(re.findall(chip, producers),
                         [('all', 'All', '5'), (f'staff:{DWIGHT.id}', 'dwight', '2'), (f'staff:{MORTY.id}', 'morty', '3')])
        self.assertEqual(body.count('<td data-label="Producer">dwight</td>'), 2)
        self.assertEqual(body.count('<td data-label="Producer">morty</td>'), 3)
        self.assertIn(f'data-producer="{DWIGHT.id}"', body)
        for text in ('Latest status', 'Done when', 'Log · newest first', 'Links',
                     'Check overdue', '14:00 EST', '15:00 EST'):
            self.assertIn(text, body)
        self.assertNotIn('<h3>Evidence</h3>', body)
        self.assertEqual(the_list.load(MORTY.id, items[0].id).evidence, items[0].evidence)
        self.assertEqual(body.count('<form method="post" data-feedback-form>'), 5)
        self.assertEqual(body.count('value="read">Mark read</button>'), 5)
        self.assertEqual([entry.item.number for entry in list_page.records()],
                         [item.number for item in sorted(the_list.records(MORTY.id), key=the_list.order)])
        parked = self.add("Check the delivery", the_list.ItemState.WAITING_FOR_NATE, number=59)
        _ = the_list.park(MORTY.id, parked.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")
        parked_markup = list_page.render(self.root / "reviews").decode()
        self.assertEqual(re.findall(r'<tbody data-list-item [^>]*data-number="(\d+)"', parked_markup),
                         ["58", "105", "106", "63", "59", "41"])
        self.assertNotIn(f'data-key="{MORTY.id}/L54"', body)
        self.assertNotIn('<h3>Closing words</h3>', body)
        _ = the_list.reopen(MORTY.id, "L54", source="nate")
        with patch.dict(list_page.FIELD_LABELS, {"closing_words": "Recorded closing words"}):
            renamed = list_page.render(self.root / "reviews").decode()
        self.assertIn("<dt>Recorded closing words</dt>", renamed)
        _ = the_list.update(MORTY.id, "L106", {"title": "New title <script>", "latest_status": "New status"}, source="morty")
        changed = list_page.render(self.root / "reviews").decode()
        self.assertIn('New title &lt;script&gt;', changed)
        self.assertNotIn('<script>New title', changed)
        self.assertIn('New status', changed)

    def test_date_eta_renders_and_sorts_after_a_time_on_the_same_day(self) -> None:
        day = date(2026, 11, 20)
        midnight = datetime(2026, 11, 20, tzinfo=user_zone())
        timed_eta = midnight.replace(hour=15)
        dated = the_list.add(MORTY.id, "Date ETA", owner=OWNER.id, source="nate", changes={"eta": midnight})
        timed = the_list.add(MORTY.id, "Time ETA", owner=OWNER.id, source="nate", changes={"eta": timed_eta})
        path = the_list.folder(MORTY.id) / f"{dated.id}.md"
        _ = path.write_text(path.read_text().replace(midnight.isoformat(), day.isoformat()))
        page = list_page.render(self.root / "reviews", include_counts=False).decode()
        dated_row, timed_row = (
            page.split(f'data-number="{item.number}"', 1)[1].split("</tbody>", 1)[0] for item in (dated, timed)
        )

        def eta_sort_key(row: str) -> list[float]:
            encoded = row.split('data-sort-values="', 1)[1].split('"', 1)[0]
            values = cast(list[object], json.loads(html.unescape(encoded)))
            return cast(list[float], values[6])

        with self.subTest(check="display"):
            self.assertIn('<span class="list-eta-text">Nov 20</span>', dated_row)
            self.assertIn('title="2026-11-20"', dated_row)
        with self.subTest(check="sort"):
            timed_key, dated_key = eta_sort_key(timed_row), eta_sort_key(dated_row)
            self.assertEqual((timed_key[0], dated_key[0]), (0, 0))
            self.assertLess(timed_key, dated_key)

    def test_linked_reviews_share_one_current_read_and_link_to_the_review_cards(self) -> None:
        def change(identity: str, state: review_items.ReviewState) -> review_items.ReviewItem:
            source = review_items.ChangeSource(self.root / "changes.md", {"#": identity},
                change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
                delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
            return review_items.ReviewItem("sdlc/" + identity, identity, review_items.ReviewType.CHANGE,
                OWNER.id, OWNER.id, "Check " + identity, state, review_items.ReviewTimes(NOW, NOW), (), (), source)

        ready = (change("C1", review_items.ReviewState.OPEN), change("C2", review_items.ReviewState.OPEN))
        waiting = change("C3", review_items.ReviewState.WAITING)
        done = change("C4", review_items.ReviewState.DONE)
        later = replace(change("C5", review_items.ReviewState.OPEN),
                        times=review_items.ReviewTimes(NOW, NOW, NOW + timedelta(days=1)))
        question = investigation.file(OWNER.id, "Find the cause", "Why did it stop?", waiting_why='The owner must complete the investigation.')
        _ = investigation.answer(OWNER.id, question.id, "The cause is known.")
        answer = review_items.investigation_items(staff.snapshot())[0]
        reviews = [*ready, waiting, done, later, answer]
        cases = (
            ((ready[0].key, ready[1].key), "2 changes for you"),
            ((ready[0].key, answer.key), "2 reviews for you"),
            ((waiting.key, done.key, "missing/C9"), "1 review waiting on others"),
            ((waiting.key, later.key), "2 reviews waiting on others"),
            ((answer.key,), "1 review for you"),
            ((done.key,), "Reviews done — decide: close, keep or park"),
            (("missing/C9",), list_page.REVIEW_UNAVAILABLE),
            ((), ""),
            ((later.key,), "1 review waiting on others"),
            ((ready[0].key, waiting.key, done.key), "1 change for you"),
        )
        items: list[the_list.ListItem] = []
        for index, (identities, _) in enumerate(cases):
            item = self.add(f"Review group {index}")
            items.append(the_list.link(MORTY.id, item.id, identities, source="morty"))
        paths = [the_list.folder(MORTY.id) / f"{item.id}.md" for item in items]
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in paths]
        root = self.root / "reviews"
        with patch.object(list_page, "read_linked_reviews", return_value=reviews) as read, patch.object(list_page, "counts_strip", return_value=""):
            body = list_page.render(root).decode()
        read.assert_called_once_with(root, members=staff.snapshot())
        for item, (identities, expected) in zip(items, cases):
            with self.subTest(item=item.id):
                row = body.split(f'data-key="{MORTY.id}/{item.id}"', 1)[1].split('</tbody>', 1)[0]
                state = row.split('<td data-label="State">', 1)[1].split('</td>', 1)[0]
                if not identities:
                    self.assertNotIn(review_page.PAGE_PATH, row)
                    continue
                self.assertIn(expected, state)
                self.assertEqual(row.count(expected), 2)
                found = [key for key in identities if any(review.key == key for review in reviews)]
                if len(found) != 1:
                    self.assertNotIn('<a ', state)
                else:
                    href = html.escape(review_page.card_url(found[0]), quote=True)
                    self.assertIn(f'href="{href}" data-list-review-card="" aria-haspopup="dialog">{expected}</a>', state)
                detail = row.split('<div class="list-panel"><div>', 1)[1]
                self.assertLess(detail.index(expected), detail.index('<form'))
                positions: list[int] = []
                for identity in identities:
                    matching = [review for review in reviews if review.key == identity]
                    href = html.escape(review_page.card_url(identity), quote=True)
                    anchor = f'<a href="{href}" data-list-review-card="" aria-haspopup="dialog">'
                    if matching:
                        card = f'{anchor}{html.escape(matching[0].id)}</a>'
                    else:
                        card = html.escape(identity)
                        self.assertNotIn(f'{anchor}{card}</a>', detail)
                    self.assertIn(card, detail)
                    positions.append(detail.index(card))
                self.assertEqual(positions, sorted(positions))
        with patch.object(review_items, "read", return_value=[
                replace(review, state=review_items.ReviewState.DONE) for review in reviews]) as read:
            refreshed = list_page.render(root).decode()
        read.assert_called_once()
        first = refreshed.split(f'data-key="{MORTY.id}/{items[0].id}"', 1)[1].split('</tbody>', 1)[0]
        self.assertIn("Reviews done — decide: close, keep or park", first)
        self.assertNotIn("2 changes for you", first)
        with (patch.object(list_page, "read_linked_reviews", return_value=reviews) as read,
              patch.object(list_page, "counts_strip", return_value="")):
            partial = list_page.render(root, review_records={}).decode()
        read.assert_called_once_with(root, members=staff.snapshot())
        first = partial.split(f'data-key="{MORTY.id}/{items[0].id}"', 1)[1].split('</tbody>', 1)[0]
        self.assertIn("2 changes for you", first)
        self.assertEqual([(path.read_bytes(), path.stat().st_mtime_ns) for path in paths], before)

    def test_live_change_and_delivered_state_return_parked_items_to_needs_me(self) -> None:
        root = self.root / "reviews"
        doc = self.root / "sdlc.md"
        listing = self.root / f"{change_review.PREFIX}sdlc.md"
        _ = doc.write_text(f'# Production\n- **Change review:** `{listing}`\n')
        _ = listing.write_text(change_review.BODY)
        members = staff.StaffSnapshot((MORTY, replace(OWNER, doc=staff.LiveDoc(local_machine(), doc))))
        parking = the_list.ParkUntilDelivered.DELIVERED
        first, second = self.add("Publish this change"), self.add("Record this delivery")
        first_key, second_key = "sdlc/C1", "sdlc/C2"
        _ = the_list.link(MORTY.id, first.id, (first_key,), source="morty")
        _ = the_list.link(MORTY.id, second.id, (second_key,), source="morty")
        first = the_list.park(MORTY.id, first.id, parking, source="nate")
        second = the_list.park(MORTY.id, second.id, parking, source="nate")
        with (patch.object(staff, "snapshot", return_value=members),
              patch.object(change_review, "lists_directory", return_value=self.root)):
            before = list_page.render(root, include_counts=False).decode()
            self.assertEqual(before.count('data-parked="delivered"'), 2)
            self.assertEqual(before.count('>Parked until delivered<'), 4)
            self.assertEqual(list_page.waiting_updates(console.NewsRequest(root)), frozenset())
            self.assertEqual(the_list.load(MORTY.id, first.id), first)
            self.assertEqual(the_list.load(MORTY.id, second.id), second)
            second = the_list.update(MORTY.id, second.id, {"state": the_list.ItemState.DELIVERED}, source="morty")
            self.assertIs(second.parked_until, the_list.NoTimestamp.UNSET)
            self.assertEqual(second.log[-1].what, the_list.UnparkedForDelivery(the_list.ItemDelivered.RECORDED))
            self.assertEqual(the_list.load(MORTY.id, first.id), first)
            rows = "| C1 | Publish this change | | Read C1 |\n| C2 | Record this delivery | | Read C2 |\n"
            _ = listing.write_text(change_review.BODY.replace("\n## Reviewed", rows + "\n## Reviewed"))
            manifest = root / change_page.CONSOLE_FOLDER / change_browser.MANIFEST
            manifest.unlink()
            with (patch.object(change_page, "recover_pending"), patch.object(list_page, "recover_pending"),
                  patch.object(record_store, "write_text_atomic", wraps=atomic_file.write_text_atomic) as save,
                  serve.ReviewLinksServer(("127.0.0.1", 0), root, initialize_review=True) as server):
                server.review_cache.synchronize()
                self.assertTrue(manifest.is_file())
                self.assertEqual(the_list.load(MORTY.id, first.id).log[-1].what,
                                 the_list.UnparkedForDelivery(the_list.LinkedChangeDelivered(first_key)))
                server.review_cache.invalidate()
                server.review_cache.synchronize()
                save.assert_called_once()
            after = list_page.render(root, include_counts=False).decode()
            first = the_list.load(MORTY.id, first.id)
            self.assertEqual(first.state, the_list.ItemState.DELIVERED)
            self.assertIs(first.parked_until, the_list.NoTimestamp.UNSET)
            self.assertEqual(first.log[-1].what, the_list.UnparkedForDelivery(the_list.LinkedChangeDelivered(first_key)))
            chip = 'data-filter-value="needs-me"'
            self.assertRegex(after, chip + r'[^>]*data-filter-count="2"')
            for item, key in ((first, first_key), (second, second_key)):
                self.assertEqual(item.unread_since, NOW)
                self.assertEqual(item.reviews, (key,))
                row = after.split(f'data-key="{MORTY.id}/{item.id}"', 1)[1].split('</tbody>', 1)[0]
                self.assertIn('data-state="Delivered" data-unread="true" data-parked="false"', row)
                link = html.escape(review_page.card_url(key), quote=True)
                self.assertIn(f'href="{link}"', row)
            self.assertEqual(len(list_page.waiting_updates(console.NewsRequest(root))), 2)
            self.assertEqual(the_list.load(MORTY.id, first.id), first)
            self.assertEqual(the_list.load(MORTY.id, second.id), second)

    def test_stale_ready_change_keeps_a_newer_park_and_a_change_that_is_no_longer_ready(self) -> None:
        root = self.root / "reviews"
        doc = self.root / "sdlc.md"
        listing = self.root / f"{change_review.PREFIX}sdlc.md"
        _ = doc.write_text(f'# Production\n- **Change review:** `{listing}`\n')
        rows = "| C1 | Publish this change | | Read C1 |\n"
        _ = listing.write_text(change_review.BODY.replace("\n## Reviewed", rows + "\n## Reviewed"))
        members = staff.StaffSnapshot((MORTY, replace(OWNER, doc=staff.LiveDoc(local_machine(), doc))))
        item = self.add("Keep the new park")
        key = "sdlc/C1"
        parking = the_list.ParkUntilDelivered.DELIVERED
        _ = the_list.link(MORTY.id, item.id, (key,), source="morty")
        first = the_list.park(MORTY.id, item.id, parking, source="nate")
        with (patch.object(staff, "snapshot", return_value=members),
              patch.object(change_review, "lists_directory", return_value=self.root)):
            snapshot = review_page.build_snapshot(root)
            self.assertTrue(snapshot.records[key].for_you(NOW))
            _ = the_list.unpark(MORTY.id, first.id, source="nate")
            second = the_list.park(MORTY.id, first.id, parking, source="nate")
            with patch.object(list_page, "records", return_value=[list_page.ListRecord(MORTY.id, first)]):
                serve.release_delivery_parks(root, snapshot)
            self.assertEqual(the_list.load(MORTY.id, first.id), second)
            _ = listing.write_text(change_review.BODY)
            serve.release_delivery_parks(root, snapshot)
            self.assertEqual(the_list.load(MORTY.id, first.id), second)

    def test_list_readers_leave_a_ready_park_and_a_fresh_root_unchanged(self) -> None:
        root = self.root / "fresh"
        item = self.add("Keep the park until the owner releases it")
        source = review_items.ChangeSource(self.root / "changes.md", {"#": "C1"},
            change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
            delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
        ready = review_items.ReviewItem("sdlc/C1", "C1", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Read the change", review_items.ReviewState.OPEN,
            review_items.ReviewTimes(NOW, NOW), (), (), source)
        _ = the_list.link(MORTY.id, item.id, (ready.key,), source="morty")
        parked = the_list.park(MORTY.id, item.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")
        with (patch.object(review_items, "read", return_value=[ready]) as read,
              patch.object(the_list, "_save", side_effect=OSError("The store is read-only.")) as save):
            before = list_page.render(root, include_counts=False).decode()
            self.assertIn("Parked until delivered", before)
            self.assertFalse(root.exists())
            self.assertEqual(list_page.waiting_updates(console.NewsRequest(root)), frozenset())
            read.assert_called_once()
            save.assert_not_called()
        self.assertEqual(the_list.load(MORTY.id, item.id), parked)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_waiting_item_shows_the_park_actions_and_unread_delivery_uses_needs_me(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Wait for an answer during delivery", number=1)
        source = review_items.ChangeSource(self.root / "changes.md", {"#": "C1"},
            change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
            delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
        review = review_items.ReviewItem("sdlc/C1", "C1", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Read the change", review_items.ReviewState.OPEN,
            review_items.ReviewTimes(NOW, NOW), (), (), source)
        _ = the_list.link(MORTY.id, item.id, (review.key,), source="morty")
        with patch.object(change_page, "send_question", return_value=question_outcome()):
            delivery = list_page.submit(root, urlencode(self.fields(item, "question", words="What changed?")), status)
            list_page.deliver(delivery)
        parking = the_list.ParkUntilDelivered.DELIVERED
        _ = the_list.park(MORTY.id, item.id, parking, source="morty")
        with patch.object(review_items, "read", return_value=[review]):
            before = list_page.render(root, include_counts=False, status=status).decode()
            pending = list_page.saved_action_state(MORTY.id, item.id, status=status).get("waiting_details_html", "")
            self.assertIn("Parked until delivered", pending)
            self.assertIn('value="unpark"', pending)
            self.assertNotIn(' hidden>Unpark', pending)
            self.assertIn('data-list-review-card="" aria-haspopup="dialog">C1</a>', pending)
            _ = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.DELIVERED}, source="morty")
            after = list_page.render(root, include_counts=False, status=status).decode()
            self.assertRegex(after, r'data-filter-value="needs-me"[^>]*data-filter-count="1"')
        setup = "<script>window.fetch=()=>Promise.reject(new Error('Fixture does not send requests'));</script>"
        self.browser_actions(before, setup, r"""<script>
const failures=[];
const row=document.querySelector('[data-number="1"]');
document.getElementById('filter-show-parked').click();
row.querySelector('.list-toggle').click();
const unpark=row.querySelector('[value="unpark"]');
if(row.hidden || !unpark?.getClientRects().length) failures.push('The waiting row shows Unpark in Parked');
if(!row.querySelector('[data-list-review-card]')) failures.push('The waiting row shows its change link');
const result=document.createElement('p');result.id='probe-results';
result.textContent=JSON.stringify(failures);document.body.append(result);
</script>""", reduced_motion=True)
        self.browser_actions(after, setup, r"""<script>
const failures=[];
const row=document.querySelector('[data-number="1"]');
document.getElementById('filter-show-needs-me').click();
if(row.hidden || !row.querySelector('.list-dot')) failures.push('Needs me shows the unread delivery');
row.querySelector('.list-toggle').click();
if(!row.querySelector('[value="read"]') || !row.querySelector('[data-list-review-card]'))
 failures.push('The waiting row shows Mark read and its change link');
document.getElementById('filter-show-morty-answering').click();
if(!row.hidden) failures.push('Morty answering excludes the unread delivery');
const result=document.createElement('p');result.id='probe-results';
result.textContent=JSON.stringify(failures);document.body.append(result);
</script>""", reduced_motion=True)

    def test_unlinked_rows_do_not_read_the_review_stores(self) -> None:
        _ = self.add("Work without reviews")
        with patch.object(review_items, "read", side_effect=AssertionError("No review read is needed")) as read:
            body = list_page.render(self.root / "reviews").decode()
        read.assert_not_called()
        self.assertIn('<strong>Work without reviews</strong>', body)
        self.assertNotIn("Reviews done — decide", body)

    def test_review_keys_keep_duplicate_ids_distinct_and_link_only_supported_cards(self) -> None:
        source = review_items.ChangeSource(self.root / "changes.md", {"#": "C1"},
            change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
            delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
        ready = review_items.ReviewItem("change/sdlc/C1", "C1", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Check C1", review_items.ReviewState.OPEN, review_items.ReviewTimes(NOW, NOW), (), (), source)
        done = replace(ready, key="change/builds/C1", state=review_items.ReviewState.DONE)
        proposal = replace(ready, key="proposal/sdlc/P1", id="P1", type=review_items.ReviewType.PROPOSAL,
                           source=review_items.ProposalLedgerSource(self.root / "proposals.md"))
        efficiency = replace(proposal, key="efficiency/sdlc/E1", id="sdlc/E1", type=review_items.ReviewType.EFFICIENCY,
                             source=review_items.EfficiencySource(self.root / "efficiencies.md", "Result"))
        for review, expected in ((ready, "1 change for you"), (done, "Reviews done — decide: close, keep or park"),
                                 (proposal, "1 review for you"), (efficiency, "1 review for you")):
            with self.subTest(key=review.key):
                item = self.add("Review " + review.key)
                _ = the_list.link(MORTY.id, item.id, (review.key,), source="morty")
                with patch.object(review_items, "read", return_value=[ready, done, proposal, efficiency]) as read, patch.object(
                        list_page, "counts_strip", return_value=""):
                    body = list_page.render(self.root / "reviews").decode()
                read.assert_called_once()
                row = body.split(f'data-key="{MORTY.id}/{item.id}"', 1)[1].split('</tbody>', 1)[0]
                self.assertIn(expected, row)
                card = review_page.card_url(review.key)
                if review.type in review_page.TYPES:
                    self.assertIn(f'<a href="{html.escape(card, quote=True)}" data-list-review-card="" '
                                  + f'aria-haspopup="dialog">{review.id}</a>', row)
                else:
                    self.assertNotIn(card, row)
                    self.assertIn('<p>' + review.id + '</p>', row)

    def test_unknown_owners_and_unassigned_owners_keep_their_labels(self) -> None:
        missing = staff.StaffId("owner-without-row")
        for owner in (missing, the_list.UnassignedOwner.NATE, the_list.UnassignedOwner.NOT_RECORDED):
            _ = self.add("Work for " + owner, owner=owner)
        body = list_page.render(self.root / "reviews").decode()
        owners = body.split('data-filter-row="owner"', 1)[1].split('</div>', 1)[0]
        for label in (str(missing), "nate", "Not recorded"):
            self.assertIn('>' + label + '<b>1</b></button>', owners)
        _ = (self.root / "staff.json").write_text('{"schema":1,"rows":[]}')
        self.assertEqual(len(list_page.records()), 3)

    def test_feedback_button_belongs_to_the_words_box_and_starts_disabled(self) -> None:
        item = self.add("Explain this item")
        form = list_page.feedback_form(list_page.ListRecord(MORTY.id, item), "fixture-secret")
        actions, words = form.split('</div>', 1)
        self.assertNotIn('value="tell"', actions)
        self.assertNotIn('<label for="words-', words)
        self.assertIn('placeholder="Your words for morty about this item"', words)
        self.assertIn('aria-label="Your words for morty about this item"', words)
        self.assertIn('</textarea><div class="list-tell"><span role="status" data-send-status></span>'
                      + '<button name="action" value="question" disabled>Ask question</button>'
                      + '<button name="action" value="tell" disabled>Tell morty</button></div>', words)
        self.assertIn("!words.value.trim()", list_page.SCRIPT)
        self.assertIn("words.addEventListener('input',retain)", list_page.SCRIPT)
        self.assertIn("setCardSubmitting(form,false,item);syncTell()", list_page.SCRIPT)
        self.assertIn('rows="4"', words)

    def test_record_fields_share_the_markdown_formatter(self) -> None:
        item = self.add("Formatted record")
        names = (("latest_status", "Status"), ("done_when", "Completion"), ("next_step", "Next"))

        def text(heading: str) -> str:
            return f"## {heading}\n\n**{heading} field:** Ready.\n- One\n- Two\n\n1. First\n2. Second"

        _ = the_list.update(MORTY.id, item.id, {"latest_status": text("Status"), "done_when": text("Completion"),
            "evidence": text("Evidence"), "next_step": text("Next")}, source="morty")
        body = list_page.render(self.root / "reviews").decode()
        next_step = body.split('<td data-label="Next step">', 1)[1].split('</td>', 1)[0]
        details = body.split('<div class="list-panel"><div>', 1)[1].split('<section class="list-log-panel"><h3>Log · newest first', 1)[0]
        self.assertNotIn('<h3>Evidence</h3>', details)
        self.assertNotIn('<h2>Evidence</h2>', details)
        self.assertEqual(the_list.load(MORTY.id, item.id).evidence, text("Evidence"))
        for _, heading in names:
            rendered = next_step if heading == "Next" else details
            self.assertIn(f'<h2>{heading}</h2>', rendered)
            self.assertIn(f'<strong>{heading} field:</strong>', rendered)
            self.assertNotIn('**', rendered)
            self.assertNotIn('## ', rendered)
        self.assertIn('<ul><li>One</li><li>Two</li></ul>', details)
        self.assertIn('<ol><li>First</li><li>Second</li></ol>', details)

    def test_bad_records_leave_valid_rows_and_nav_news(self) -> None:
        good = self.add("Valid item")
        bad = self.add("Invalid item")
        path = the_list.folder(MORTY.id) / f"{bad.id}.md"
        _ = path.write_text(path.read_text().replace('state: "Working"', 'state: "bad"'))
        errors = io.StringIO()
        with redirect_stderr(errors):
            body = list_page.render(self.root / "reviews").decode()
            news = list_page.waiting_updates(console.NewsRequest(self.root))
        self.assertIn('<strong>Valid item</strong>', body)
        self.assertNotIn('<strong>Invalid item</strong>', body)
        self.assertEqual(news, frozenset({f"{MORTY.id}/{good.id}/{NOW.isoformat()}"}))
        self.assertEqual(len(errors.getvalue().splitlines()), 2)

    def test_nav_marks_new_update_times_and_keeps_read_operations_out_of_the_page(self) -> None:
        pages = tuple(page for page in console.read_pages() if page.identity == "list")
        root = self.root / "reviews"
        self.assertEqual(list_page.page_url(), '/console/list.html')
        self.assertEqual(console.news(root, pages, "changes"), ())
        item = self.add("Unread item")
        self.assertEqual(console.news(root, pages, "changes"), ("list",))
        self.assertEqual(console.news(root, pages, "list"), ())
        self.assertEqual(console.news(root, pages, "changes"), ())
        with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=1)):
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "A new result arrived."}, source="morty")
        self.assertEqual(console.news(root, pages, "changes"), ("list",))
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        self.assertEqual(console.news(root, pages, "changes"), ())

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_unread_excludes_legacy_closed_records(self) -> None:
        open_item = self.add("Open with an unread update", number=1)
        closed = self.add("Closed with a stored unread time", number=2)
        _ = the_list.close(MORTY.id, closed.id, "The report is retired.", source="nate")
        _ = the_list.mark_read(MORTY.id, closed.id, source="nate")
        path = the_list.folder(MORTY.id) / f"{closed.id}.md"
        field = f'unread_since: {json.dumps(NOW.isoformat())}\n'
        stored = path.read_text().replace('unread_since: ""\n', field, 1)
        self.assertIn(field, stored)
        _ = path.write_text(stored)
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        probe = '''<script>
const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
const chip=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]');
const visible=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')];
const unread=document.getElementById('list-unread');
unread.click();
check(chip('all').querySelector('b').textContent==='1','All counts only the unread item when the switch is on.');
check(visible().map(row=>row.dataset.number).join() === '1','the Unread view shows only the open item');
check(document.querySelector('[data-list-count]').textContent.endsWith(' · 1 unread'),'the page total counts one unread item');
unread.click();
check(!chip('closed') && !document.querySelector('[data-number="2"]'),'Closed has no chip or row.');
const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
document.body.append(result);
</script>'''
        self.browser_actions(markup, "", probe)
        self.assertEqual(list_page.waiting_updates(console.NewsRequest(self.root)),
                         frozenset({f"{MORTY.id}/{open_item.id}/{NOW.isoformat()}"}))
        self.assertEqual(list_page.saved_action_state(MORTY.id, closed.id)["unread"], "false")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_status_counts_partition_all_items_with_and_without_unread(self) -> None:
        for offset in (0, 8):
            for number, state in enumerate(the_list.ItemState, start=1 + offset):
                closed = state is the_list.ItemState.CLOSED
                item = self.add(f"Check {state.value}", the_list.ItemState.WORKING if closed else state, number=number)
                if offset:
                    _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=1), source="nate")
                if closed:
                    _ = the_list.close(MORTY.id, item.id, "The work is complete.", source="nate")
                elif number in (2, 5, 7, 10, 12, 14):
                    _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        for number, state in ((17, the_list.ItemState.DELIVERED), (18, the_list.ItemState.WORKING),
                              (19, the_list.ItemState.WORKING), (20, the_list.ItemState.BLOCKED)):
            item = self.add("Check the question result", state, number=number)
            if number == 18:
                item = the_list.park(MORTY.id, item.id, NOW + timedelta(days=1), source="nate")
            outcome = question_outcome("refused", "Producer offline") if number == 19 else question_outcome()
            with (patch.object(change_page, "send_question", return_value=outcome),
                  patch.object(change_page, "alert_delivery_failures")):
                list_page.deliver(list_page.submit(root, urlencode(
                    self.fields(item, "question", words="Which check passed?")), status))
            if number in (18, 19):
                _ = the_list.update(MORTY.id, item.id, {"latest_status": "A new check passed."}, source="morty")
            if number == 20:
                with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1",
                                                    "--text", "The gate passed."]), 0)
        markup = list_page.render(root, status=status, include_counts=False).decode()
        controls = markup.split('data-filter-row="show"', 1)[1].split('</div>', 1)[0]
        self.assertEqual(re.findall(r'data-filter-count="(\d+)"', controls), ["18", "3", "2", "2", "2", "8"])
        probe = '''<script>
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
const chips=[...document.querySelectorAll('[data-filter-row="show"] button')];
const unread=document.getElementById('list-unread');
const total=document.querySelector('[data-list-count]');
const entries=[
 ['all',[1,2,3,4,5,6,7,9,10,11,12,13,14,15,17,18,19,20],[1,3,4,6,9,11,13,15,18,19,20]],
 ['needs-me',[4,5,20],[4,20]],['morty-answering',[17,19],[19]],['working',[1,2],[1]],['blocked',[3,7],[3]],
 ['parked',[9,10,11,12,13,14,15,18],[9,11,13,15,18]],
];
const count=button=>Number(button.dataset.filterCount);
const visible=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')]
 .map(row=>Number(row.dataset.number)).sort((a,b)=>a-b);
for(const onlyUnread of [false,true]){
 if(unread.checked!==onlyUnread) unread.click();
 check(count(chips[0])===1+chips.slice(1).reduce((sum,button)=>sum+count(button),0),
  'The status counts and the unparked Done item sum to All.');
 check(total.textContent.includes(' · '+count(chips[1])+' need you · '),'The total equals the Needs me count.');
 entries.forEach(([name,all,unreadItems],index)=>{
  const expected=onlyUnread ? unreadItems : all;
  const button=chips[index];
  button.click();
  check(button.dataset.filterValue===name && count(button)===expected.length && visible().join()===expected.join(),
   name+' counts and shows its items with Unread only '+(onlyUnread ? 'on' : 'off')+'.');
 });
}
const row=number=>document.querySelector('[data-number="'+number+'"]');
const expand=number=>row(number).querySelector('.list-toggle').click();
const detail=number=>row(number).querySelector('[data-detail]:not([hidden])');
unread.click();
chips[2].click();
expand(17);
check(!row(17).hidden && detail(17)?.querySelector('[data-waiting-note]'),
 'The unparked item shows the waiting note in Morty answering.');
expand(19);
check(!row(19).hidden && detail(19)?.querySelector('[data-question-delivery-failed]'),
 'The failed question stays in Morty answering with a failure notice.');
chips[5].click();expand(18);
const waitingPanel=detail(18);
check(!row(18).hidden && waitingPanel?.querySelector('[data-waiting-note]') &&
 waitingPanel.querySelector('[data-parked-detail]').getClientRects().length &&
 waitingPanel.querySelector('[value="unpark"]').getClientRects().length &&
 !row(18).querySelector('[data-parked-note]').getClientRects().length,
 'The parked item shows one parking note, the waiting note, and Unpark in its panel.');
chips[1].click();
expand(20);
const answer=detail(20)?.querySelector('.list-panel [data-question="Q1"]');
check(!row(20).hidden && row(20).querySelector('[aria-label="Unread update"]') &&
 answer?.textContent.includes('The gate passed.') && answer.querySelector('.question-unread') &&
 !detail(20).querySelector('[data-log-index="Q1-answered"]'),
 'The answered item shows in Needs me with its unread answer in the panel.');
const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
document.body.append(result);
</script>'''
        self.browser_actions(markup, "", probe)

    def test_count_strip_colors_its_showrunner_with_the_page_name_reading(self) -> None:
        members = staff.StaffSnapshot((replace(OWNER, doc=staff.LiveDoc(local_machine(), self.root / "sdlc.md")),))
        with (patch.object(staff, "snapshot", return_value=members),
              patch.object(list_page, "change_counts", return_value={"sdlc": 1}),
              patch.object(accounts, "role_colors",
                           wraps=accounts.role_colors) as colors,
              patch.object(counts_row, "question_counts", return_value=(0, ""))):
            body = list_page.render(self.root / "reviews").decode()
        colors.assert_called_once()
        chip = body.split('<span class="list-count-chip">', 1)[1].split('<a ', 1)[0]
        self.assertRegex(chip, r'data-color-text="role.showrunner"[^>]*>sdlc</span> · ')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_loads_deferred_counts_after_the_list(self) -> None:
        item = self.add("Show this row first")
        item = the_list.link(MORTY.id, item.id, ("sdlc/C1",), source="morty")
        entry = f"{MORTY.id}/{item.id}"
        template = (f'<template data-list-review-key="{entry}" data-list-review-version="{list_page.review_version(item)}">'
                    '<span data-list-review-summary>1 change for you</span>'
                    '<div data-list-review-links><a href="/console/?page=review">sdlc/C1</a></div></template>')
        counts = '<aside class="list-counts"><a href="/console/?page=changes">2 changes</a></aside>'
        change = review_items.ReviewItem("sdlc/C1", "sdlc/C1", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Check the change", review_items.ReviewState.OPEN,
            review_items.ReviewTimes(NOW, NOW), (), (), review_items.ProposalLedgerSource(self.root / "review.md"))
        published: list_page.ReviewRecords = {change.key: change}
        partial: list_page.ReviewRecords = {}
        for lookup, query, response, expected in (
            (published, list_page.COUNTS_QUERY, counts, ""),
            (partial, list_page.COUNTS_AND_LINKS_QUERY, counts + template, "1 change for you"),
            (list_page.LinkedReviewRead.AFTER_RENDER, list_page.COUNTS_AND_LINKS_QUERY,
             counts + '<template data-list-review-read-error></template>', list_page.REVIEW_UNAVAILABLE),
        ):
            with self.subTest(query=query, expected=expected), patch.object(list_page, "counts_strip",
                    side_effect=AssertionError("The List read the counts.")):
                markup = list_page.render(self.root / "reviews", defer_counts=True, review_records=lookup).decode()
                self.assertIn("Show this row first", markup)
                self.assertEqual("Checking linked reviews…" in markup, bool(expected))
                setup = """<script>
let countRequests=[];
window.fetch=async (url,options)=>{
 countRequests.push([url,options.cache]);
 return {ok:true,text:async()=>MARKUP};
};
</script>""".replace("MARKUP", json.dumps(response))
                probe = """
<script>
(async()=>{
 const footer=document.querySelector('[data-list-counts-url]');
 if(!footer.firstChild) await new Promise(resolve=>{
  const observer=new MutationObserver(()=>{observer.disconnect();resolve();});
  observer.observe(footer,{childList:true});
 });
 const failures=[];
 if(countRequests.length!==1 || countRequests[0].join()!==REQUEST)
  failures.push('The footer uses one independent request.');
 if(!document.querySelector('[data-list-item]') || footer.querySelector('a')?.textContent!=='2 changes')
  failures.push('The counts appear without replacing the List rows.');
 if(EXPECTED && document.querySelector('[data-list-review-summary]')?.textContent!==EXPECTED)
  failures.push('The same request settles the linked review status.');
 if(EXPECTED==='1 change for you' && document.querySelector('[data-list-review-links] a')?.textContent!=='sdlc/C1')
  failures.push('The same request fills the linked review fields.');
 const result=document.createElement('p');
 result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>""".replace("REQUEST", json.dumps(f"{list_page.PAGE_PATH}?{query},no-store")).replace(
                    "EXPECTED", json.dumps(expected))
                self.browser_actions(markup, setup, probe)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_refresh_keeps_the_follow_up_request_and_retries_after_timeout(self) -> None:
        settings = conf.path("review_links")
        _ = settings.write_text(settings.read_text().replace(
            "page_request_timeout_seconds=15\n", "page_request_timeout_seconds=4\n"))
        root = self.root / "reviews"
        changed = self.add("Keep the changed row usable")
        counts_only = list_page.render(root, defer_counts=True, review_records={}).decode()
        stable = self.add("Keep the unchanged row usable")
        changed = the_list.link(MORTY.id, changed.id, ("sdlc/C1",), source="morty")
        stable = the_list.link(MORTY.id, stable.id, ("sdlc/C1",), source="morty")
        markup = list_page.render(root, defer_counts=True,
                                  review_records=list_page.LinkedReviewRead.AFTER_RENDER).decode()
        renewed = the_list.update(MORTY.id, changed.id, {"title": "Show the new title"}, source="morty")
        refreshed = list_page.render(root, defer_counts=True,
                                     review_records=list_page.LinkedReviewRead.AFTER_RENDER).decode()

        def update(item: the_list.ListItem, label: str) -> str:
            return (f'<template data-list-review-key="{MORTY.id}/{item.id}"'
                    + f' data-list-review-version="{list_page.review_version(item)}">'
                    + f'<span data-list-review-summary>{label}</span></template>')

        first_updates = update(changed, "Old review status") + update(stable, "Unchanged review status")
        current_updates = update(renewed, "Current review status") + update(stable, "Unchanged review status")
        setup = """<script>
const requests=[],deadlines=new Map();
let page=INITIAL_PAGE;
AbortSignal.timeout=milliseconds=>{
 const timer=new AbortController();
 deadlines.set(timer.signal,{milliseconds,timer});
 return timer.signal;
};
window.fetch=(url,options)=>{
 if(!String(url).includes('?counts=')) return Promise.resolve(new Response(page));
 return new Promise((resolve,reject)=>{
  const request={url,signal:options.signal,finish:markup=>resolve({ok:true,text:()=>Promise.resolve(markup)})};
  requests.push(request);
  request.signal.addEventListener('abort',()=>reject(request.signal.reason));
 });
};
</script>""".replace("INITIAL_PAGE", json.dumps(markup).replace("</", "<\\/"))
        probe = """<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const summary=number=>document.querySelector('[data-number="'+number+'"] [data-list-review-summary]');
 const footer=()=>document.querySelector('[data-list-counts-url]');
 const refresh=()=>{
  const list=document.querySelector('[data-list]');
  const replaced=changed(list.parentElement);
  document.dispatchEvent(new Event('visibilitychange'));
  return replaced;
 };
 const changed=node=>new Promise(resolve=>{
  const observer=new MutationObserver(()=>{observer.disconnect();resolve();});
  observer.observe(node,{childList:true});
 });
 const first=requests[0];
 await refresh();
 page=UPDATED_PAGE;
 await refresh();
 check(requests.length===1 && !first.signal.aborted,'Refreshes keep the healthy request active.');
 const firstLoaded=changed(footer());
 first.finish('<aside>First counts</aside>'+FIRST_UPDATES);
 await firstLoaded;
 check(footer().textContent==='First counts' && summary(2).textContent==='Unchanged review status',
  'The first response fills the current footer and unchanged row.');
 check(summary(1).textContent==='Checking linked reviews…',
  'The response skips a changed row with the same review links.');
 check(requests.length===2,'The changed row starts one follow-up without another refresh.');
 const timedOut=changed(summary(1));
 deadlines.get(requests[1].signal).timer.abort();
 await timedOut;
 check(summary(1).textContent===UNAVAILABLE && document.querySelector('.list-toggle').matches(':enabled'),
  'A timeout leaves the row usable and identifies the unavailable status.');
 check(requests.length===2,'A timeout does not repeat the same request.');
 await refresh();
 check(footer().textContent==='First counts','The refresh keeps the last counts during the active request.');
 const loaded=changed(footer());
 requests[2].finish('<aside>New counts</aside>'+CURRENT_UPDATES);
 await loaded;
 check(footer().textContent==='New counts' && summary(1).textContent==='Current review status',
  'The next refresh loads counts and status after a timeout.');
 page=COUNTS_ONLY_PAGE;
 await refresh();
 const counts=requests[3];
 check(counts.url.endsWith('?counts=1') && !summary(2),'The counts-only request starts before the new row appears.');
 page=UPDATED_PAGE;
 await refresh();
 check(requests.length===4 && !counts.signal.aborted && summary(2).textContent==='Checking linked reviews…',
  'A refresh adds the waiting row and keeps the counts-only request active.');
 const countsLoaded=changed(footer());
 counts.finish('<aside>Counts before linked status</aside>');
 await countsLoaded;
 check(requests.length===5 && requests[4].url.endsWith('?counts=1&linked-reviews=1'),
  'The completed counts request starts one follow-up for the linked status.');
 const links=requests[4];
 if(links){
  const statusLoaded=changed(summary(2));
  links.finish('<aside>Final counts</aside>'+CURRENT_UPDATES);
  await statusLoaded;
  check(summary(2).textContent==='Unchanged review status',
   'The follow-up fills the new row without another refresh.');
 }
 check(requests.length===5 && [...deadlines.values()].every(deadline=>deadline.milliseconds===4000),
  'Each request uses the configured time limit.');
 const result=document.createElement('p');
 result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>""".replace("UPDATED_PAGE", json.dumps(refreshed).replace("</", "<\\/")).replace(
            "COUNTS_ONLY_PAGE", json.dumps(counts_only).replace("</", "<\\/")).replace(
            "FIRST_UPDATES", json.dumps(first_updates).replace("</", "<\\/")).replace(
            "CURRENT_UPDATES", json.dumps(current_updates).replace("</", "<\\/")).replace(
            "UNAVAILABLE", json.dumps(list_page.REVIEW_UNAVAILABLE))
        self.browser_actions(markup, setup, probe)

    def test_counts_use_status_line_readers(self) -> None:
        owners: list[staff.StaffRow] = []
        listings: list[Path] = []
        for owner, count in ((OWNER, 3), (OTHER, 1)):
            doc = self.root / f"{owner.name}.md"
            listing = self.root / f"{change_review.PREFIX}{owner.name}.md"
            _ = doc.write_text(f'# Production\n- **Change review:** `{listing}`\n')
            _ = listing.write_text('## To review\n\n| # | Change | How to check | Producer |\n'
                + '| --- | --- | --- | --- |\n'
                + ''.join(f'| C{number} | Work | Read it | {owner.id} |\n' for number in range(1, count + 1)))
            owners.append(replace(owner, doc=staff.LiveDoc(local_machine(), doc)))
            listings.append(listing)
        members = staff.StaffSnapshot(tuple(owners))
        with listings[0].open('a') as output:
            _ = output.write(f'| C4 | A change for builds | Read it | {OTHER.id} |\n')
        _ = self.add("A valid List row")
        with (patch.object(counts_row, "question_counts", return_value=(2, "")) as questions,
              patch.object(staff, "snapshot", return_value=members),
              patch.object(showrunners, "timer_docs", return_value={owner.name: owner.doc.path
                  for owner in owners if isinstance(owner.doc, staff.LiveDoc)}),
              patch.object(change_review, "lists_directory", return_value=self.root),
              patch.object(change_review, "review_snapshot", wraps=change_review.review_snapshot) as read,
              patch.object(change_routing, "StaffRouting", wraps=change_routing.StaffRouting) as routing,
              patch.object(session_staff, "member_sources", side_effect=ValueError("Invalid pane record")) as panes,
              patch.object(rebuild_row, "read", return_value=rebuild_row.RebuildAnswer(("mac",), False)) as rebuilds):
            body = list_page.counts_strip(members, staff_names.StaffNames(members))
            self.assertEqual(questions.call_count, 2)
            self.assertEqual(questions.call_args_list[0].args[0], owners[1])
            self.assertEqual(questions.call_args_list[1].args[0], owners[0])
            self.assertCountEqual([call.args[0] for call in read.call_args_list], listings)
            routing.assert_called_once()
            panes.assert_not_called()
            rebuilds.assert_called_once_with(conf.path("statusline"))
            root = self.root / "reviews"
            root.mkdir(exist_ok=True)
            status = change_page.DeliveryStatus(root)
            with patch.object(QuestionStaff, "for_record", return_value=QuestionStaff(members, session_staff.MemberSources(()))):
                for validation in (False, True):
                    with self.subTest(validation=validation):
                        if validation:
                            page = change_page.ReviewPage(listings[0], listings[0], ("C1",), "")
                            _ = status.path(page, "C1").write_text(json.dumps({"validation": {
                                "attempt": "recorded", "state": "pending", "feedback": "Check it"}}))
                        forms = BrowserForms()
                        forms.feed(change_browser.render(change_page.ReviewBrowser("fixture"), status).decode())
                        totals = list_page.change_counts()
                        self.assertEqual(totals, forms.counts)
                        self.assertEqual(totals, {"sdlc": 2 if validation else 3, "builds": 2})
                        chips = list_page.counts_strip(members, staff_names.StaffNames(members))
                        for name, count in totals.items():
                            chip = without_role_colors(chips).split(name + ' · ', 1)[1].split('</span>', 1)[0]
                            self.assertIn(f'<b>{count}</b> changes', chip)
            for reader, label in (("questions", "questions"), ("changes", "changes"), ("rebuilds", "rebuild")):
                target = (counts_row if reader == "questions" else list_page if reader == "changes" else rebuild_row)
                function = {"questions": "question_counts", "changes": "change_counts", "rebuilds": "read"}[reader]
                with (self.subTest(failed_count=reader), patch.object(target, function, side_effect=OSError("Unavailable")),
                      patch.object(staff, "snapshot", return_value=members)):
                    available = list_page.render(self.root / "reviews").decode()
                    self.assertIn('<strong>A valid List row</strong>', available)
                    self.assertNotIn(f'</b> {label}<', available)
                    self.assertNotIn(f'</b> {label}s<', available)
                    self.assertIn('</b> changes<' if reader == "questions" else '</b> questions<', available)
        for page, place in (("questions", '/console/changes.html?type=questions#showrunner=sdlc'),
                            ("changes", '/console/changes.html#showrunner=sdlc'),
                            ("rebuilds", '/console/rebuilds.html')):
            link = '/console/' + console.place_query(page, place)
            self.assertIn(f'href="{html.escape(link, quote=True)}" target="_top"', body)
        for text in ('<b>2</b> questions', '<b>3</b> changes', '<b>1</b> rebuild'):
            self.assertIn(text, body)
        self.assertNotIn("proposals", body)
        self.assertNotIn("efficiencies", body)

    def fields(self, item: the_list.ListItem, action: str, **changes: str) -> dict[str, str]:
        folder = change_page.console_folder(self.root / "reviews")
        secret = change_browser.browser_manifest(folder)[0]
        form = list_page.feedback_form(list_page.ListRecord(MORTY.id, item), secret)
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form))
        return {**fields, "action": action, "words": "", "until": "", **changes}

    def test_unpark_saves_before_delivery_and_sends_one_notice(self) -> None:
        root = self.root / "reviews"
        item = self.add("Unpark this item", number=106)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        fields = self.fields(item, "unpark")
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(fields))
            saved = the_list.load(MORTY.id, item.id)
            self.assertIs(saved.parked_until, the_list.NoTimestamp.UNSET)
            self.assertEqual(saved.state, item.state)
            self.assertEqual(saved.log[-2].what, the_list.Unparked())
            self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["parked_until"], "")
            send.assert_not_called()
            list_page.deliver(delivery)
            self.assertEqual(send.call_count, 1)
            self.assertEqual((send.call_args.args[0], send.call_args.args[2]),
                             (MORTY.id, "List page: nate unparked item 106."))
            delivered = the_list.load(MORTY.id, item.id)
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(the_list.load(MORTY.id, item.id), delivered)
            fields = self.fields(delivered, "unpark")
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            saved = the_list.load(MORTY.id, item.id)
            receipt = the_list.UnparkNoChangeReceipt(
                the_list.PageRequest(fields["request"], "unpark", "List page: nate unparked item 106."),
                the_list.UnparkNoChangeReason.NO_ACTIVE_PARKING)
            self.assertEqual(saved.log[-1].what, receipt)
            self.assertEqual(replace(saved, log=saved.log[:-1]), delivered)
            self.assertEqual(send.call_count, 1)

    def test_remove_closes_before_delivery_and_repeated_requests_do_nothing(self) -> None:
        item = self.add("Remove this item now")
        fields = self.fields(item, "remove")
        root = self.root / "reviews"
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(fields))
            closed = the_list.load(MORTY.id, item.id)
            self.assertEqual(closed.state, the_list.ItemState.CLOSED)
            self.assertEqual(sum(isinstance(entry.what, the_list.FieldChange) and entry.what.action == "Closed"
                                 for entry in closed.log), 1)
            send.assert_not_called()
            list_page.deliver(delivery)
            self.assertEqual(send.call_args.args[2], f"List page: nate removed item {item.number}.")
            saved = the_list.load(MORTY.id, item.id)
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
            self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_park_writes_the_date_before_delivery_and_repeated_requests_do_nothing(self) -> None:
        item = self.add("Park this item now")
        fields = self.fields(item, "park", until="2026-12-03")
        root = self.root / "reviews"
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(fields))
            parked = the_list.load(MORTY.id, item.id)
            self.assertEqual(parked.parked_until, the_list.time_value("2026-12-03"))
            self.assertEqual(parked.state, item.state)
            send.assert_not_called()
            list_page.deliver(delivery)
            self.assertEqual(send.call_args.args[2], f"List page: nate parked item {item.number} until 2026-12-03.")
            saved = the_list.load(MORTY.id, item.id)
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
            self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_park_until_delivered_reuses_the_receipt_after_delivery(self) -> None:
        root = self.root / "reviews"
        item = self.add("Keep this work parked until delivery")
        fields = self.fields(item, "park", until="delivered")
        _ = list_page.submit(root, urlencode(fields))
        parked = the_list.load(MORTY.id, item.id)
        self.assertIs(parked.parked_until, the_list.ParkUntilDelivered.DELIVERED)
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["parked_until"], "delivered")
        self.assertEqual(the_list.feedback_requests(parked)[fields["request"]].parked_until, parked.parked_until)
        with self.assertRaisesRegex(the_list.ListError, "different parking"):
            _ = list_page.submit(root, urlencode({**fields, "until": "2026-12-03"}))
        delivered = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.DELIVERED}, source="morty")
        _ = list_page.submit(root, urlencode(fields))
        self.assertEqual(the_list.load(MORTY.id, item.id), delivered)

    def test_refused_remove_and_notice_write_failure_keep_the_item(self) -> None:
        item = self.add("Keep this item when Remove fails")
        root = self.root / "reviews"
        with patch.object(the_list, "close", side_effect=the_list.ListError("The fixture refuses this close.")):
            with self.assertRaisesRegex(the_list.ListError, "refuses this close"):
                _ = list_page.submit(root, urlencode(self.fields(item, "remove")))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)
        with patch.object(the_list, "_save", side_effect=OSError("The fixture refuses this write.")):
            with self.assertRaises(OSError):
                _ = list_page.submit(root, urlencode(self.fields(item, "remove")))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_read_rejects_a_saved_park_request_and_its_retry_preserves_newer_updates(self) -> None:
        item = self.add("Keep each request with its action")
        root = self.root / "reviews"
        park = self.fields(item, "park", until="2026-12-03")
        _ = list_page.submit(root, urlencode(park))
        parked = the_list.load(MORTY.id, item.id)
        with self.assertRaisesRegex(the_list.ListError, "different action or words"):
            _ = list_page.submit(root, urlencode({**park, "action": "read"}))
        self.assertEqual(the_list.load(MORTY.id, item.id), parked)

        read = self.fields(parked, "read")
        _ = list_page.submit(root, urlencode(read))
        self.assertIs(the_list.load(MORTY.id, item.id).unread_since, the_list.NoTimestamp.UNSET)
        newer = the_list.update(MORTY.id, item.id, {"latest_status": "Keep this newer update unread."}, source="morty")
        _ = list_page.submit(root, urlencode(read))
        self.assertEqual(the_list.load(MORTY.id, item.id), newer)
        for changed in ({"action": "remove"}, {"words": "Different words."}):
            with self.subTest(changed=changed), self.assertRaisesRegex(the_list.ListError, "different action or words"):
                _ = list_page.submit(root, urlencode({**read, **changed}))
        self.assertEqual(the_list.load(MORTY.id, item.id), newer)

    def test_park_retry_succeeds_after_the_date_arrives(self) -> None:
        item = self.add("Retry this Park after midnight")
        root = self.root / "reviews"
        fields = self.fields(item, "park", until="2026-12-03")
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            saved = the_list.load(MORTY.id, item.id)
            with patch.object(the_list, "now", return_value=datetime(2026, 12, 3, 5, tzinfo=UTC)):
                list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_park_saves_the_picked_instant_in_the_user_zone(self) -> None:
        item = self.add("Park until a time")
        root = self.root / "reviews"
        until = "2026-12-04T00:30:00.000Z"
        _ = list_page.submit(root, urlencode(self.fields(item, "park", until=until)))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.parked_until, datetime(2026, 12, 4, 0, 30, tzinfo=UTC))
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["parked_until"], "Dec 3 19:30 EST")
        body = list_page.render(root).decode()
        self.assertIn("Parked until Dec 3 19:30 EST</small>", body)
        self.assertIn("Parked until</dt><dd>Dec 3 19:30 EST</dd>", body)

    def test_park_retry_compares_the_saved_instant_to_the_second(self) -> None:
        item = self.add("Retry the exact parking time")
        root = self.root / "reviews"
        fields = self.fields(item, "park", until="2026-12-04T00:30:05Z")
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            saved = the_list.load(MORTY.id, item.id)
            with self.assertRaisesRegex(the_list.ListError, "different parking time"):
                _ = list_page.submit(root, urlencode({**fields, "until": "2026-12-04T00:30:55Z"}))
            same_time = {**fields, "until": "2026-12-03T19:30:05-05:00"}
            list_page.deliver(list_page.submit(root, urlencode(same_time)))
            send.assert_called_once()
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_park_retry_accepts_a_legacy_date_only_record(self) -> None:
        item = self.add("Retry a Park saved by the old page")
        root = self.root / "reviews"
        fields = self.fields(item, "park", until="2026-12-03")
        request_fields = the_list._request_fields  # pyright: ignore[reportPrivateUsage]

        def legacy_fields(request: the_list.PageRequest) -> dict[str, str]:
            return {key: value for key, value in request_fields(request).items() if key != "parked_until"}

        with (patch.object(the_list, "_request_fields", side_effect=legacy_fields),
              patch.object(change_page, "send_to_staff") as send):
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            later = datetime(2026, 12, 6, 5, tzinfo=UTC)
            saved = the_list.park(MORTY.id, item.id, later, source="morty")
            with patch.object(the_list, "now", return_value=datetime(2026, 12, 3, 5, tzinfo=UTC)):
                list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_park_retry_refuses_an_earlier_park_when_its_own_event_is_missing(self) -> None:
        item = self.add("Retry a Park with missing evidence")
        root = self.root / "reviews"
        earlier = self.fields(item, "park", until="2026-12-03")
        original = {**earlier, "request": earlier["request"] + "." + "a" * 32, "until": "2026-12-04"}
        request_fields = the_list._request_fields  # pyright: ignore[reportPrivateUsage]

        def legacy_fields(request: the_list.PageRequest) -> dict[str, str]:
            return {key: value for key, value in request_fields(request).items() if key != "parked_until"}

        with patch.object(the_list, "_request_fields", side_effect=legacy_fields):
            _ = list_page.submit(root, urlencode(earlier))
            _ = list_page.submit(root, urlencode(original))
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        body, marker, log = path.read_text().partition("## Log\n\n")
        lines = log.splitlines()
        self.assertIn('"action": "Parked"', lines[-2])
        del lines[-2]
        _ = path.write_text(body + marker + "\n".join(lines) + "\n")
        saved = the_list.load(MORTY.id, item.id)
        with self.assertRaisesRegex(the_list.ListError, "different parking time"):
            _ = list_page.submit(root, urlencode({**original, "until": earlier["until"]}))
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_new_park_checks_the_date_at_the_locked_write(self) -> None:
        item = self.add("Refuse a Park that reaches the writer at midnight")
        root = self.root / "reviews"
        fields = self.fields(item, "park", until="2026-12-03")
        midnight = datetime(2026, 12, 3, 5, tzinfo=UTC)
        park = the_list.park

        def at_midnight(keeper: staff.StaffId, identity: str, until: the_list.Timestamp, *,
                        source: str, notice: the_list.PageItemNotice) -> the_list.ListItem:
            with patch.object(the_list, "now", return_value=midnight):
                return park(keeper, identity, until, source=source, notice=notice)

        with patch.object(the_list, "park", side_effect=at_midnight):
            with self.assertRaisesRegex(ValueError, "Choose a future parking date"):
                _ = list_page.submit(root, urlencode(fields))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_item_actions_unread_sync_and_words_fields(self) -> None:
        _ = self.fixture_items()
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
const requests=[];let finishPost;
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 requests.push(new URLSearchParams(options.body));
 const form=submittedForm;
 return new Promise(resolve=>finishPost=result=>respondToSave(form,()=>resolve({
  ok:result.ok!==false,status:result.ok===false ? 400 : 200,json:async()=>({unread:'false',...result})
 })));
};
</script>'''
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,name)=>{if(!ok) failures.push(name);};
 const row=number=>document.querySelector('[data-number="'+number+'"]');
 const visible=()=>[...document.querySelectorAll('[data-list-item]')].filter(item=>!item.hidden).map(item=>item.dataset.number).sort().join(',');
 const choose=(show,query)=>new Promise(done=>{
  document.querySelector('[data-filter-row="show"] [data-filter-value="'+show+'"]').click();
  const field=document.getElementById('list-search');
  field.addEventListener('filterchange',done,{once:true});field.value=query;field.dispatchEvent(new Event('input'));
 });
 const submit=(number,action)=>{const form=row(number).querySelector('form');form.requestSubmit(form.querySelector('[value="'+action+'"]'));return form;};
 try{
  for(const [query,expected] of [['105','105'],['L105','105'],['#105','105'],['BUILDS','105'],
    ['Straße','106'],['WORKING','105,106'],['Waiting','58'],['Parked','41'],['Closed','']]){
   await choose('all',query);check(visible()===expected,'words filter: '+query);
  }
  await choose('working','106');row('106').querySelector('.list-toggle').click();
  const removed=submit('106','remove');
  check(row('106').hidden,'Remove hides the row before the response');
  check(row('106').dataset.unread==='false' && !row('106').querySelector('.list-dot') &&
   !removed.querySelector('[value="read"]'),'Remove clears the unread controls before the response');
  check([...row('106').querySelectorAll('button')].every(button=>button.disabled),'Remove disables the row buttons');
  submit('106','remove');check(requests.length===1,'a second Remove press sends nothing');
  await finishPost({item_state:'Closed',parked:'false'});
  check(row('106').hidden,'Remove stays outside Working after success');
  await choose('all','106');check(visible()==='','Remove keeps the item out of All');
  check(row('106').querySelector('.list-state').textContent==='Closed','Remove updates the state text');
  await choose('working','105');row('105').querySelector('.list-toggle').click();
  const refused=submit('105','remove');check(row('105').hidden,'a refused Remove hides the row while pending');
  await finishPost({ok:false,reason:'The fixture refuses this close.'});
  check(!row('105').hidden && row('105').dataset.state==='Working','a refused Remove restores the row');
  check(row('105').dataset.unread==='true' && row('105').querySelector('.list-dot') &&
   refused.querySelector('[value="read"]'),'a refused Remove restores the unread controls');
  check(refused.querySelector('[data-send-status]').textContent==='Not saved: The fixture refuses this close.','a refused Remove shows its reason');
  const restore=document.querySelector('[data-record-motion]')?.getAnimations({subtree:true}) ?? [];
  for(const animation of restore) animation.finish();
  await Promise.allSettled(restore.map(animation=>animation.finished));
  check(!refused.querySelector('[value="remove"]').disabled,'a refused Remove enables another attempt');
  refused.elements.until.value='2026-12-03';const parked=submit('105','park');
  check(row('105').hidden,'Park hides the row before the response');
  check(document.querySelector('[data-record-motion]')?.getAnimations({subtree:true}).length,
   'Park animates the row when it leaves Working');
  submit('105','park');check(requests.length===3,'a second Park press sends nothing');
  await finishPost({item_state:'Working',parked:'true'});
  check(row('105').hidden,'Park stays outside Working after success');
  await choose('parked','105');check(visible()==='105','Park updates the Parked view');
  check(parked.querySelector('[data-send-status]').textContent==='Parked','Park shows its result');
  await choose('all','58');row('58').querySelector('.list-toggle').click();
  const form=row('58').querySelector('form');
  check(row('58').dataset.unread==='false' && !form.querySelector('[value="read"]'),'the unread sync case starts read');
  form.elements.words.value='Keep this result.';form.elements.words.dispatchEvent(new Event('input'));
  submit('58','tell');await finishPost({item_state:'Waiting for nate',parked:'false',unread:'true'});
  check(row('58').dataset.unread==='true' && row('58').querySelector('.list-dot'),'an unread response restores the dot');
  check(Boolean(form.querySelector('[value="read"]')),'an unread response restores Mark read');
  if(form.querySelector('[value="read"]')){
   document.getElementById('list-unread').click();
   submit('58','read');await finishPost({item_state:'Waiting for nate',parked:'false',unread:'false'});
   check(!row('58').querySelector('.list-dot') && !form.querySelector('[value="read"]'),'the restored Mark read button works');
   check(row('58').hidden && document.querySelector('[data-record-motion]')?.getAnimations({subtree:true}).length,
    'Mark read animates the row when it leaves Unread');
  }
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, setup, probe)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_opens_the_shared_picker_and_submits_the_chosen_time(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-12-02T13:00:00Z');
 const toggle=form.querySelector('[data-later-toggle]');
 check(!form.querySelector('input[type=date]'),'Park has no native date input');
 toggle.click();
 const sheet=form.querySelector('[data-later-custom]');
 check(!sheet.hidden && !sheet.closest('[data-later-menu]').hidden,'Park opens the shared sheet');
 check(document.activeElement.dataset.date==='2026-12-03','Focus starts on the selected date');
 sheet.querySelector('[data-date="2026-12-03"]').click();
 const field=sheet.querySelector('[data-later-time]');
 field.value='1930';field.dispatchEvent(new Event('input',{bubbles:true}));
 const summary=sheet.querySelector('[data-later-summary]').textContent;
 check(summary==='Parks until Dec 3 19:30 EST','The Park picker shows the short time in the user zone');
 check(!requests.length,'Changing the selection does not submit the form');
 sheet.querySelector('[value="park"]').click();
 check(sheet.hidden && requests.length===1,'Save closes the shared sheet and submits once');
 check(requests[0]?.get('action')==='park' && requests[0].get('until')==='2026-12-04T00:30:00.000Z',
  'Save sends the selected instant');
 await finishPost({item_state:'Working',parked:'true',parked_until:'Dec 3 19:30 EST'});
 check(row.querySelector('[data-parked-note]').textContent===summary.replace('Parks until','Parked until'),
  'The row shows the same time as the picker');
 check(form.querySelector('[data-parked-detail]').textContent==='Parked until Dec 3 19:30 EST',
  'The expanded form uses the same saved time.');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_parks_until_delivered_with_one_post(self) -> None:
        _ = self.add("Check the other item", number=2)
        self.browser_action_sequence(r"""
 const toggle=form.querySelector('[data-later-toggle]');
 const menu=form.querySelector('[data-later-menu]');
 const preset=menu.querySelector('[data-later-preset]');
 toggle.click();
 check(menu.firstElementChild===preset && preset.textContent==='Until delivered','The preset comes first');
 check(!menu.querySelector('[data-later-custom]').hidden,'The date controls stay open below the preset');
 preset.click();
 check(menu.hidden && requests.length===1,'One click closes the menu and posts once');
 check(requests[0]?.get('action')==='park' && requests[0].get('until')==='delivered','The POST uses the delivery value');
 await finishPost({item_state:'Working',parked:'true',parked_until:'delivered'});
 check(!row.hidden && row.nextElementSibling?.dataset.number==='2',
  'All keeps the server order until a refresh.');
 check(document.querySelector('#filter-show-all b').textContent==='2' &&
  document.querySelector('#filter-show-parked b').textContent==='1','All and Parked count the parked item.');
 check(form.querySelector('[data-send-status]').textContent==='Parked until delivered',
  'All confirms the parking action in the row.');
 show('working');
 check(row.hidden,'Working hides the item until delivery');
 show('parked');
 check(!row.hidden && !form.querySelector('[value="unpark"]').hidden,'Parked shows the item and Unpark');
 check(form.querySelector('[data-parked-detail]').textContent==='Parked until delivered','The panel shows the condition');
 check(row.querySelector('[data-parked-note]').textContent==='Parked until delivered','The state cell has the condition');
 submit('unpark');
 await finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'});
 check(row.hidden,'Unpark removes the item from Parked');
 show('working');
 check(!row.hidden,'Unpark returns the item to Working');
 show('all');
 check(row.nextElementSibling?.dataset.number==='2','All restores the default order after Unpark.');
""", reduced_motion=True)
        self.browser_waiting_parking_refresh(unpark=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_preset_skips_menu_navigation(self) -> None:
        self.browser_action_sequence(r"""
 const errors=[];
 addEventListener('error',event=>errors.push(event.message));
 const toggle=form.querySelector('[data-later-toggle]');
 toggle.click();
 const preset=form.querySelector('[data-later-preset]');
 preset.focus();
 for(const key of ['ArrowUp','ArrowDown','Home','End']){
  const event=new KeyboardEvent('keydown',{key,bubbles:true,cancelable:true});
  preset.dispatchEvent(event);
  check(!event.defaultPrevented && document.activeElement===preset,'The dialog skips menu navigation for '+key);
 }
 check(errors.length===0,'The keys cause no JavaScript errors');
 check(requests.length===0,'Navigation sends no POST');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_focus_moves_to_the_calendar_when_previous_month_disables(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-11-30T15:00:00Z');
 form.querySelector('[data-later-toggle]').click();
 const previous=form.querySelector('[data-later-month-step="-1"]');
 const grid=form.querySelector('[data-later-grid]');
 previous.focus();
 check(document.activeElement===previous,'Previous month has focus in December');
 previous.click();
 const day=grid.querySelector('button:not(:disabled)[tabindex="0"]');
 check(previous.disabled && day?.dataset.date==='2026-11-30','November has an enabled day');
 check(document.activeElement===day,'Focus moves from Previous month to the enabled day');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_retries_the_same_parking_time_after_it_passes(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-12-02T13:00:00Z');
 Date.now=()=>now;
 show('all');
 const toggle=form.querySelector('[data-later-toggle]');
 toggle.click();
 const time=form.querySelector('[data-later-time]'),save=form.querySelector('[value="park"]');
 time.value='1930';time.dispatchEvent(new Event('input',{bubbles:true}));
 save.click();
 const first=requests[0];
 check(first?.get('until')==='2026-12-04T00:30:00.000Z','The first save sends the chosen instant');
 await losePost();
 await finishRowMotion();
 now=Date.parse('2026-12-05T13:00:00Z');
 toggle.click();
 check(time.value==='19:30' && !save.disabled,'The picker retains and enables the expired retry');
 save.click();
 check(requests.length===2 && requests[1].get('request')===first.get('request') &&
  requests[1].get('until')===first.get('until'),'Save retries the same ID and instant');
 if(requests.length===2){await finishPost({parked:'false',parked_until:''});}
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_refusal_from_enter_restores_focus(self) -> None:
        for number in (1, 2):
            _ = self.add(f"Park item {number} with Enter", number=number)
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-12-02T13:00:00Z');
 show('working');
 for(const [number,afterBlur] of [[1,false],[2,true]]){
  const item=document.querySelector('[data-number="'+number+'"]');
  if(item.querySelector('[data-detail]').hidden) item.querySelector('.list-toggle').click();
  const toggle=item.querySelector('[data-later-toggle]'),time=item.querySelector('[data-later-time]');
  toggle.click();
  time.focus();
  let lostFocus=false;
  const blurred=new Promise(resolve=>time.addEventListener('blur',()=>{
   lostFocus=true;resolve();
  },{once:true}));
  time.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
  check(requests.length===number && item.hidden,'Enter submits Park and hides the row from Working');
  if(afterBlur) await blurred;
  check(lostFocus===afterBlur,'The refusal follows the requested order for row '+number);
  await finishPost({reason:'The fixture refuses Park'});
  await blurred;
  await finishRowMotion();
  check(!item.hidden && !toggle.disabled && document.activeElement===toggle,
   'The restored row returns focus to Park until for row '+number);
 }
""", add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_expired_park_retry_keeps_the_calendar_in_the_tab_order(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-12-02T13:00:00Z');
 Date.now=()=>now;
 show('all');
 const toggle=form.querySelector('[data-later-toggle]'),grid=form.querySelector('[data-later-grid]');
 const save=form.querySelector('[value="park"]');
 toggle.click();
 save.click();
 const first=requests[0];
 await losePost();
 await finishAnimations(document.getAnimations());
 now=Date.parse('2026-12-15T13:00:00Z');
 toggle.click();
 const entry=grid.querySelector('button:not(:disabled)[tabindex="0"]');
 check(entry?.dataset.date==='2026-12-15','The calendar has an enabled day in the Tab order');
 check(entry===document.activeElement && !save.disabled,'The calendar has focus and Save can retry');
 document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 now=Date.parse('2027-01-02T13:00:00Z');
 toggle.click();
 const nextMonth=grid.querySelector('button:not(:disabled)[tabindex="0"]');
 check(nextMonth?.dataset.date==='2027-01-02','The calendar opens on an enabled month');
 if(nextMonth){
  nextMonth.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));
  check(document.activeElement.dataset.date==='2027-01-03','ArrowRight selects a new date from the focused day');
  save.click();
  check(requests[1]?.get('request')!==first.get('request') && requests[1]?.get('until')?.startsWith('2027-01-03'),
   'The new date sends a new request');
  if(requests.length===2){await finishPost({parked:'true'});}
 }
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_refusal_keeps_focus_on_search(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-12-02T13:00:00Z');
 form.querySelector('[data-later-toggle]').click();
 const time=form.querySelector('[data-later-time]');
 show('working');
 time.focus();
 time.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
 check(requests.length===1 && row.hidden,'Enter submits Park and hides the row from Working');
 await finishPost({reason:'The fixture refuses Park'});
 const search=document.getElementById('list-search');
 search.focus();
 check(document.activeElement===search,'Search receives focus while the row returns');
 await finishAnimations(document.getAnimations());
 check(!row.hidden && document.activeElement===search,'A refused Park keeps focus on Search');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reserves_a_lost_park_id_and_updates_the_saved_date_label(self) -> None:
        item = self.add("Change the parking date")
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
const requests=[];let finishPost,losePost;
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 requests.push(new URLSearchParams(options.body));
 const form=submittedForm;
 return new Promise((resolve,reject)=>{
  losePost=()=>respondToSave(form,()=>reject(new Error('Fixture lost response')));
  finishPost=result=>respondToSave(form,()=>resolve({ok:!result.reason,status:result.reason ? 400 : 200,
   json:async()=>({unread:'true',...result})}));
 });
};
</script>'''
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,name)=>{if(!ok) failures.push(name);};
 const row=document.querySelector('[data-list-item]'),form=row.querySelector('form');
 const submit=action=>{
  form.requestSubmit(form.querySelector('[value="'+action+'"]'));
  return requests.at(-1).get('request');
 };
 const labels=()=>[...row.querySelectorAll('[data-label="State"] small')].map(note=>note.textContent);
 try{
  check(labels().join() === 'Parked until Nov 18 13:00 EST','The row starts with one saved time label');
  form.elements.until.value='2026-12-03';
  const park=submit('park');await losePost();
  check(submit('park')===park,'an exact Park retry keeps its request ID');
  await losePost();
  check(submit('remove')!==park,'Remove gets a different ID after a lost Park response');
  await finishPost({reason:'Fixture refuses Remove'});
  const returning=document.querySelector('[data-record-motion]')?.getAnimations({subtree:true}) ?? [];
  for(const animation of returning) animation.finish();
  await Promise.allSettled(returning.map(animation=>animation.finished));
  form.elements.until.value='2026-12-04';
  check(submit('park')!==park,'a different Park date gets a different ID');
  await finishPost({item_state:'Working',parked:'true',parked_until:'Dec 5 00:00 EST'});
  check(labels().length===1,'a successful Park keeps one date label');
  check(labels().join()==='Parked until Dec 5 00:00 EST','the label uses the saved date from the response');
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, setup, probe)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_unpark_controls_layout_and_focus(self) -> None:
        parked = self.add("Parked item", number=1, next_check=NOW + timedelta(hours=1))
        _ = self.add("Unparked item", number=2)
        _ = the_list.park(MORTY.id, parked.id, NOW + timedelta(days=2), source="nate")
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        with patch.object(the_list, "now", return_value=NOW + timedelta(hours=2)):
            _ = the_list.unpark(MORTY.id, parked.id, source="nate")
            saved = json.dumps({**list_page.saved_action_state(MORTY.id, parked.id), "unpark_outcome": "cleared"})
        self.browser_action_sequence(r"""
 show('all');
 const other=document.querySelector('[data-number="2"]');
 const rectangle=element=>element.getBoundingClientRect();
 const near=(a,b)=>Math.abs(a-b)<1;
 for(const item of [row,other]){
  show(item===row ? 'parked' : 'all');
  if(item===other) other.querySelector('.list-toggle').click();
  const pick=selector=>item.querySelector(selector);
  const box=rectangle(pick('textarea'));
  const remove=rectangle(pick('[value=remove]')),park=rectangle(pick('[data-later-toggle]'));
  check(pick('textarea').rows===4 && remove.bottom<box.top,'four lines below the controls');
  check(near(remove.left,box.left) && near(park.right,box.right) && near(remove.top,park.top),'one aligned control row');
  check(pick('[value=unpark]').hidden===(item===other),'Unpark only shows on the parked item');
  check(pick('[data-parked-detail]').hidden===(item===other),'parking line only shows on the parked item');
  if(item===row){
   check(near(park.left-rectangle(pick('[value=unpark]')).right,8),'parking controls are adjacent');
   check(getComputedStyle(pick('[data-parked-note]')).display==='none','the expanded row shows one parking line');
   pick('[data-later-toggle]').click();
   check(near(rectangle(pick('[data-later-menu]')).right,box.right),'picker aligns with the text box');
   pick('[data-later-toggle]').click();
  }
 }
 const unpark=form.querySelector('[value=unpark]');
 show('parked');
 row.querySelector('.list-toggle').click();
 check(!row.querySelector('[data-list-check]'),'The parked row omits the future check.');
 unpark.focus();
 const first=submit('unpark');
 await losePost();
 check(!row.hidden && !unpark.hidden,'a lost response leaves Unpark available');
 check(document.activeElement===unpark,'a lost response returns focus to Unpark');
 check(submit('unpark').id===first.id,'an exact Unpark retry keeps its request ID');
 await finishPost(SAVED_UNPARK);
 check(row.hidden && document.activeElement===document.querySelector('#filter-show-parked'),'focus moves to Parked');
 const status=document.querySelector('[data-hidden-action-status][role="status"]');
 check(!status.hidden && status.getClientRects().length>0 && status.textContent==='Item #1 unparked.',
       'Parked announces which item left');
 show('all');
 check(status.hidden,'All shows the confirmation in the row');
 const nextCheck=row.querySelector('[data-list-check]');
 check(nextCheck.getClientRects().length>0 &&
  nextCheck.querySelector('.warning')?.textContent==='Check overdue Nov 16 14:00 EST',
  'Unpark shows the current check with the warning style before a refresh.');
 form.elements.words.value='Keep these words.';
 form.elements.until.value='2026-12-03';
 submit('park');
 check(!nextCheck.getClientRects().length,'The row hides the check while Park is pending.');
 await finishPost({item_state:'Working',parked:'true',parked_until:'Dec 3 00:00 EST',check_html:''});
 show('parked');
 check(!row.querySelector('[data-list-check]'),'The Park response removes the check before a refresh.');
 document.querySelector('[data-sort="3"]').click();
 unpark.focus();
 submit('unpark');
 show('all');
 await finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'});
 check(!row.hidden && !row.querySelector('[data-detail]').hidden,'Unpark in All keeps the row open');
 check(document.querySelector('[data-list-item]')===row,'Unpark preserves the item order by title');
 check(document.activeElement===row.querySelector('.list-toggle'),'focus moves to the item number');
 check(unpark.hidden && form.querySelector('[data-parked-detail]').hidden,'the parking controls update');
 check(form.elements.until.value==='' && form.elements.words.value==='Keep these words.',
       'Unpark clears the parking draft and keeps the words');
 show('working');
 check(!row.hidden && !row.querySelector('[data-detail]').hidden,'Working shows the expanded unparked row');
""".replace("SAVED_UNPARK", saved), reduced_motion=True, add_item=False, window_size=(1440, 1120), markup=markup)
        self.browser_waiting_parking_refresh(unpark=True)

    def browser_waiting_parking_refresh(self, *, unpark: bool) -> None:
        item = self.add("Keep a draft while waiting", the_list.ItemState.WAITING_FOR_NATE, number=3)
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        if unpark:
            _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        before = list_page.render(root, include_counts=False, status=status).decode()
        with patch.object(change_page, "send_question", return_value=question_outcome()):
            list_page.deliver(list_page.submit(root, urlencode(
                self.fields(item, "question", words="Which check passed?")), status))
        waiting = list_page.render(root, include_counts=False, status=status).decode() if unpark else None
        action = "unpark" if unpark else "park"
        submission = list_page.submit(root, urlencode(self.fields(item, action, until="delivered")), status)
        outcome = list_page.saved_action_state(MORTY.id, item.id, status=status)
        response: dict[str, object] = dict(outcome)
        if isinstance(submission, list_page.ListUnparkSubmission):
            response["unpark_outcome"] = submission.response_outcome(outcome).value
        saved = list_page.render(root, include_counts=False, status=status).decode()
        with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
              redirect_stdout(io.StringIO())):
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The check passed."]), 0)
        answered = list_page.render(root, include_counts=False, status=status).decode()
        fixture = json.dumps({
            "waiting": waiting, "action": action, "response": response, "saved": saved, "answered": answered,
        }).replace("</", "<\\/")
        self.browser_action_sequence(r"""
 const fixture=FIXTURE;
 const parks=fixture.action==='park';
 const current=()=>document.querySelector('[data-number="3"]');
 const other=()=>document.querySelector('[data-number="2"]');
 const numbers=()=>[...document.querySelectorAll('[data-number="2"],[data-number="3"]')]
  .map(item=>item.dataset.number).join(',');
 const refresh=async markup=>{
  const replaced=waitForRefresh();
  refreshMarkup=markup;
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await replaced;
 };
 const input=current().querySelector('textarea');
 input.value='Keep the words for this item.';
 input.dispatchEvent(new Event('input'));
 if(fixture.waiting) await refresh(fixture.waiting);
 const prior=current(),beforeOrder=numbers(),otherRow=other();
 const draft=otherRow.querySelector('textarea');
 const editOther=()=>{
  otherRow.querySelector('.list-toggle').click();
  draft.value='Keep the words for the other item.';
  draft.dispatchEvent(new Event('input'));
  draft.focus();draft.setSelectionRange(5,14,'backward');
 };
 if(!parks){editOther();prior.querySelector('.list-toggle').click();}
 const methods=[];
 window.fetch=new Proxy(window.fetch,{apply(fetch,receiver,args){
  methods.push(args[1]?.method || 'GET');return Reflect.apply(fetch,receiver,args);
 }});
 refreshMarkup=fixture.saved;
 submit(parks ? 'park-until-delivered' : 'unpark',prior.querySelector('form'));
 if(parks) editOther();
 else prior.querySelector('.list-log-scroll').focus();
 const replaced=waitForRefresh();
 await finishPost(fixture.response);
 await replaced;
 check(methods.join()==='POST,GET','The save makes one List GET while the item waits.');
 const refreshed=current(),freshDraft=other().querySelector('textarea');
 check(refreshed!==prior && beforeOrder===(parks ? '3,2' : '2,3') &&
  numbers()===(parks ? '2,3' : '3,2'),'The server moves the waiting row to its new group.');
 check(freshDraft.value==='Keep the words for the other item.','Refresh keeps the other draft.');
 if(parks){
  check(refreshed.querySelector('[data-detail]').hidden,'Refresh keeps the row closed after the user opens another row.');
  check(document.activeElement===freshDraft && freshDraft.selectionStart===5 &&
   freshDraft.selectionEnd===14 && freshDraft.selectionDirection==='backward',
   'Park keeps focus and the selection in the other draft.');
  refreshed.querySelector('.list-toggle').click();
 }else check(document.activeElement===refreshed.querySelector('.list-log-scroll'),
  'Unpark keeps focus on the log in the waiting row.');
 check(!refreshed.querySelector('[data-detail]').hidden &&
  refreshed.querySelector('[data-waiting-questions]').textContent.includes('Which check passed?'),
  'The open row shows the waiting question.');
 await refresh(fixture.answered);
 check(current().querySelector('textarea').value==='Keep the words for this item.',
  'The item draft returns after the producer answers.');
""".replace("FIXTURE", fixture), reduced_motion=True, add_item=False, markup=before, open_number=3)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reports_expired_parking_and_keeps_the_parking_draft(self) -> None:
        item = self.add("Parking expires before Unpark", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 show('parked');
 form.elements.until.value='2026-12-03';
 submit('unpark');
 await finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'expired'});
 const status=document.querySelector('[data-hidden-action-status]');
 check(row.hidden && !status.hidden && status.textContent==='Item #1: Parking time already expired.',
       'Parked reports that the parking time already expired.');
 show('all');
 check(form.querySelector('[data-send-status]').textContent==='Parking time already expired',
       'All reports that the parking time already expired.');
 check(form.elements.until.value==='2026-12-03','An expired time leaves the parking draft intact.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reports_unpark_replay_with_newer_parking(self) -> None:
        item = self.add("Reparked before the retry", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 show('parked');
 const unpark=form.querySelector('[value=unpark]');
 form.elements.until.value='2026-12-03';
 unpark.focus();
 const first=submit('unpark');
 await losePost();
 check(submit('unpark').id===first.id,'The retry sends the original request ID.');
 await finishPost({item_state:'Working',parked:'true',parked_until:'Dec 4 00:00 EST',
  unpark_outcome:'cleared_newer_parking_active'});
 const message=form.querySelector('[data-send-status]').textContent;
 check(message==='The original Unpark cleared parking. A newer parking time is active',
       'The replay reports the original result and the newer parking time.');
 check(!row.hidden && !unpark.hidden && document.activeElement===unpark,
       'The reparked row stays in view and Unpark keeps focus.');
 check(form.querySelector('[data-parked-detail]').textContent==='Parked until Dec 4 00:00 EST',
       'The row shows the newer parking time.');
 check(form.elements.until.value==='2026-12-03','The replay preserves the parking draft.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reports_unpark_with_no_parking_time(self) -> None:
        item = self.add("Parking cleared by another tab", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 show('parked');
 form.elements.until.value='2026-12-03';
 submit('unpark');
 await finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'no_active_parking'});
 const status=document.querySelector('[data-hidden-action-status]');
 check(row.hidden && !status.hidden && status.textContent==='Item #1: No active parking time to clear.',
       'The result shows that there was no active parking time.');
 check(form.elements.until.value==='2026-12-03','The request preserves the parking draft.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_preserves_both_overlapping_unpark_confirmations(self) -> None:
        for number in (1, 2):
            item = self.add(f"Parked item {number}", number=number)
            _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 show('parked');
 const second=document.querySelector('[data-number="2"]');
 submit('unpark');
 const finishFirst=finishPost;
 second.querySelector('.list-toggle').click();
 submit('unpark',second.querySelector('form'));
 const result={item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'};
 const status=document.querySelector('[data-hidden-action-status]');
 await finishFirst(result);
 check(row.hidden && !second.hidden && !status.hidden && status.textContent==='Item #1 unparked.',
       'The first request shows its result while the second request runs.');
 await finishPost(result);
 check(second.hidden && !status.hidden && status.textContent==='Item #1 unparked. Item #2 unparked.',
       'Both results stay visible after both rows leave Parked.');
 show('all');
 check(!status.hidden && status.textContent==='Item #1 unparked.' &&
       !second.hidden && !second.querySelector('[data-detail]').hidden &&
       second.querySelector('[data-send-status]').textContent==='Unparked',
       'All shows the first result above the table and the second result in the visible details.');
 show('parked');
 check(!status.hidden && status.textContent==='Item #1 unparked. Item #2 unparked.',
       'Parked shows both results again.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_brings_parked_filter_and_confirmation_into_view(self) -> None:
        for number in range(1, 25):
            item = self.add(f"Parked item {number}", number=number)
            _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        steps = '''
 show('parked');
 const sort=document.querySelector('[data-sort="0"]');
 sort.click();sort.click();
 const filter=document.querySelector('#filter-show-parked');
 if(SHORT_VIEWPORT){
  const buttons=filter.closest('[data-filter-row="show"]').querySelectorAll('button');
  check(innerHeight<=260 && new Set([...buttons].map(button=>button.offsetTop)).size>1,
        'The Show filter wraps in the short viewport.');
 }
 const unpark=form.querySelector('[value=unpark]');
 unpark.scrollIntoView({block:'center',behavior:'instant'});
 unpark.focus({preventScroll:true});
 check(filter.getBoundingClientRect().bottom<0,'The focused row is below the viewport with the filters.');
 submit('unpark');
 await finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'});
 const withinViewport=element=>{
  const {top,bottom}=element.getBoundingClientRect();
  return !element.hidden && top>=0 && bottom<=innerHeight;
 };
 const status=document.querySelector('[data-hidden-action-status]');
 const bounds=filter.getBoundingClientRect();
 const hit=document.elementFromPoint(bounds.left+bounds.width/2,bounds.top+bounds.height/2);
 const screen=JSON.stringify({innerWidth,innerHeight,scrollY,
  focus:document.activeElement.id,top:bounds.top,statusBottom:status.getBoundingClientRect().bottom,
  hit:hit?.outerHTML.slice(0,150),pageStatus:document.querySelector('[data-page-status]').getBoundingClientRect().height});
 check(row.hidden && document.activeElement===filter && withinViewport(filter) && filter.contains(hit),
       'Unpark moves focus and brings the Parked filter into view. '+screen);
 check(withinViewport(status) && status.textContent==='Item #1 unparked.',
       'Unpark brings the confirmation into the same viewport. '+screen);
'''
        for window_size in ((1440, 900), (500, 260)):
            with self.subTest(window_size=window_size):
                viewport_steps = steps.replace("SHORT_VIEWPORT", "true" if window_size[1] <= 260 else "false")
                self.browser_action_sequence(viewport_steps, reduced_motion=True, add_item=False, window_size=window_size)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_hides_unpark_and_updates_the_state_sort_key(self) -> None:
        active = self.add("Active parked item", number=1)
        closed = self.add("Closed parked item", number=2)
        for item in (active, closed):
            _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        _ = the_list.close(MORTY.id, closed.id, "Removed from the List.", source="nate")
        self.browser_action_sequence('''
 show('all');
 check(!document.querySelector('[data-number="2"]'),'A Closed record with a parking time has no row.');
 show('parked');
 const unpark=form.querySelector('[value=unpark]');
 await finishRowMotion();
 submit('remove');
 check(unpark.hidden,'Remove hides Unpark immediately.');
 await finishPost({reason:'Fixture refuses Remove'});
 check(!unpark.hidden,'A refusal restores Unpark.');
 await finishRowMotion();
 submit('remove');
 await finishPost({item_state:'Closed',parked:'true',unread:'false'});
 check(unpark.hidden && row.dataset.parked==='true','A confirmed Remove leaves Unpark hidden.');
 const sortValues=JSON.parse(row.dataset.sortValues);
 check(sortValues[3]==='active parked item' && sortValues[4]==='closed',
       'Remove keeps the Item key and updates the State key.');
''', add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_preserves_unpark_focus_during_periodic_refresh(self) -> None:
        item = self.add("Parked item", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "The refreshed status."}, source="morty")
            fresh = list_page.render(self.root / "reviews").decode()
        markup = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="1"', markup, count=1)
        setup = r'''<script>
window.fetch=async()=>new Response(FRESH);
</script>'''.replace('FRESH', json.dumps(fresh).replace('</', '<\\/'))
        probe = r'''<script>
const original=document.querySelector('[data-list]');
original.querySelector('#filter-show-parked').click();
original.querySelector('.list-toggle').click();
const button=original.querySelector('[value=unpark]');
button.focus();
const focusedBefore=document.activeElement===button;
const observer=new MutationObserver(()=>{
 if(original.isConnected) return;
 observer.disconnect();
 const fresh=document.querySelector('[data-list]');
 const unpark=fresh.querySelector('[value=unpark]');
 const failures=[];
 if(!fresh.textContent.includes('The refreshed status.')) failures.push('The page renders the refreshed status.');
 if(!focusedBefore || document.activeElement!==unpark || !button.id || unpark.id!==button.id)
  failures.push('The refresh restores focus to Unpark with the same ID.');
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
 document.body.append(result);
});
observer.observe(original.parentNode,{childList:true});
</script>'''
        self.browser_actions(markup, setup, probe)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_retries_a_lost_tell_after_a_refused_park(self) -> None:
        self.browser_action_sequence('''
 form.elements.words.value='Tell morty once.';
 const tell=submit('tell');await losePost();
 form.elements.until.value='2026-12-03';
 const park=submit('park');
 check(park.id!==tell.id,'Park uses its own request ID');
 await finishPost({reason:'Fixture refuses Park'});
 await finishRowMotion();
 check(submit('park').id!==park.id,'a refused Park leaves no unconfirmed request');
 await finishPost({reason:'Fixture refuses Park again'});
 await finishRowMotion();
 form.elements.until.value='';
 const retry=submit('tell');
 check(retry.id===tell.id && retry.token===tell.token,'Tell retains its signed ID across refusals');
 await finishPost({item_state:'Working',parked:'false'});
 form.elements.words.value='Tell morty once.';
 check(submit('tell').id!==tell.id,'a confirmed Tell leaves no unconfirmed request');
 await finishPost({item_state:'Working',parked:'false'});
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_retries_two_lost_park_dates_after_a_confirmed_remove(self) -> None:
        self.browser_action_sequence('''
 form.elements.until.value='2026-12-03';
 const first=submit('park');await losePost();
 await finishRowMotion();
 form.elements.until.value='2026-12-04';
 const second=submit('park');await losePost();
 await finishRowMotion();
 check(first.id!==second.id,'each Park date gets its own request ID');
 const remove=submit('remove');
 check(remove.id!==second.id,'Remove uses its own request ID');
 await finishPost({item_state:'Closed',parked:'true',unread:'false'});
 await finishRowMotion();
 for(const [date,original] of [['2026-12-03',first],['2026-12-04',second]]){
  form.elements.until.value=date;
  const retry=submit('park');
  check(retry.id===original.id && retry.token===original.token,'Park retains the signed ID for '+date);
  await finishPost({item_state:'Closed',parked:'true',unread:'false'});
 }
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_shows_a_lost_remove_notice_when_its_details_are_closed(self) -> None:
        self.browser_action_sequence('''
 show('all');
 submit('remove');
 check(row.hidden,'the row leaves All during Remove');
 check(document.querySelector('[data-record-motion]'),'Remove starts its exit animation');
 await finishRowMotion();
 await losePost();
 await finishRowMotion();
 check(!row.hidden && row.dataset.unread==='true' && row.querySelector('.list-dot'),
  'a lost Remove restores the row and unread mark');
 row.querySelector('.list-row').click();
 check(row.querySelector('[data-detail]').hidden,'the user can close the details after a lost response');
 const hiddenStatus=document.querySelector('[data-hidden-action-status]');
 check(!hiddenStatus.hidden && hiddenStatus.textContent==='Item 1: Save not confirmed: Fixture lost response',
  'the table shows the lost response while the details are closed');
 row.querySelector('.list-toggle').click();
 check(hiddenStatus.hidden && form.querySelector('[data-send-status]').getClientRects().length>0,
  'opening the details shows the inline notice only');
 row.querySelector('.list-toggle').click();
 check(!hiddenStatus.hidden,'closing the details restores the notice above the table');
 submit('remove');await finishPost({reason:'Fixture refuses Remove'});
 check(row.dataset.state==='Working' && row.dataset.unread==='true' && row.querySelector('.list-dot'),
  'a refused Remove retry restores the last saved unread state');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_switching_rows_moves_a_lost_remove_notice(self) -> None:
        _ = self.add("Second List item", number=2)
        self.browser_action_sequence('''
 show('all');
 submit('remove');
 await finishRowMotion();
 await losePost();
 await finishRowMotion();
 const above=document.querySelector('[data-hidden-action-status]');
 const inline=form.querySelector('[data-send-status]');
 const message='Save not confirmed: Fixture lost response';
 const inlineOnly=()=>above.hidden && inline.textContent===message && inline.getClientRects().length>0;
 const aboveOnly=()=>!above.hidden && above.textContent==='Item 1: '+message && !inline.getClientRects().length;
 check(inlineOnly(),'The open row has the notice in its details.');
 document.querySelector('[data-number="2"] .list-toggle').click();
 check(aboveOnly(),'Opening another row moves the notice above the table.');
 row.querySelector('.list-toggle').click();
 check(inlineOnly(),'Opening the first row returns the notice to its details.');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_shows_only_the_latest_hidden_remove_notice(self) -> None:
        _ = self.add("Second List item", number=2)
        self.browser_action_sequence('''
 show('all');
 const second=document.querySelector('[data-number="2"]');
 for(const item of [row,second]){
  if(item.querySelector('[data-detail]').hidden) item.querySelector('.list-toggle').click();
  submit('remove',item.querySelector('form'));
  await losePost();
  item.querySelector('.list-row').click();
 }
 const status=document.querySelector('[data-hidden-action-status]');
 const message=number=>'Item '+number+': Save not confirmed: Fixture lost response';
 const shows=number=>!status.hidden && status.textContent.includes(message(number));
 check(!shows(1) && shows(2),'only the latest hidden notice appears above the table');
 row.querySelector('.list-toggle').click();
 check(!shows(1) && shows(2),'opening row one preserves the latest notice');
 check(form.querySelector('[data-send-status]').textContent==='Save not confirmed: Fixture lost response',
  'row one shows its notice inline');
 row.querySelector('.list-toggle').click();
 check(!shows(1) && shows(2),'Closing row one does not restore the older notice.');
 second.querySelector('.list-toggle').click();
 check(status.hidden,'opening the latest failed row hides the notice above the table');
''')

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_browser_clears_the_list_save_notice(self) -> None:
        for number in range(1, 9):
            _ = self.add(f"List item {number}", number=number)
        with patch.object(list_page, "counts_strip", return_value=""):
            page = list_page.render(self.root / "reviews")
        def load(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            with self.browser_page(arguments[-1], 1366, 768):
                return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch("scripts.review_links.test_accounts_page.subprocess", SimpleNamespace(run=load)):
            self.assertEqual(save_notice_failures(page, 'remove'), [[]])

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_keeps_the_question_request_after_a_service_failure(self) -> None:
        self.browser_action_sequence('''
 form.elements.words.value='Which check passed?';
 form.elements.words.dispatchEvent(new Event('input'));
 const attempt=submit('question');
 await finishPost({status:503,reason:'Fixture delivery status unavailable'});
 check(form.querySelector('[data-send-status]').textContent===
  'Save not confirmed: Fixture delivery status unavailable','a service error leaves the save unconfirmed');
 const retry=submit('question');
 check(retry.id===attempt.id && retry.token===attempt.token,'the retry uses the original signature');
 await finishPost({item_state:'Working',parked:'false'});
 check(form.elements.words.value==='','the confirmed reply clears the question');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_ask_uses_its_own_request_and_keeps_the_exact_retry(self) -> None:
        self.browser_action_sequence('''
 const words=form.elements.words,ask=form.querySelector('[value="question"]');
 check(ask?.disabled,'Ask question starts disabled');
 words.value=' ';words.dispatchEvent(new Event('input'));
 check(ask?.disabled,'spaces keep Ask question disabled');
 words.value='What changed?';words.dispatchEvent(new Event('input'));
 check(!ask?.disabled,'words enable Ask question');
 const question=submit('question');await losePost();
 const feedback=submit('tell');await losePost();
 check(question.id!==feedback.id,'Ask and Tell use different request IDs');
 const retry=submit('question');
 check(retry.id===question.id && retry.token===question.token,'an exact Ask retry keeps its signature');
 await finishPost({item_state:'Working',parked:'false'});
 check(words.value==='' && ask.disabled,'a saved question clears the box');
''')

    def test_ask_marks_the_seen_update_read_after_it_saves(self) -> None:
        item = self.add("Read the update before asking")
        root = self.root / "reviews"
        fields = self.fields(item, "question", words="Which check passed?")
        with patch.object(the_list, "mark_read", wraps=the_list.mark_read) as mark:
            for _ in range(2):
                _ = list_page.submit(root, urlencode(fields))
        mark.assert_called_once_with(MORTY.id, item.id, source=the_list.PAGE_SOURCE, update=the_list.unread_update(item))
        saved = the_list.load(MORTY.id, item.id)
        self.assertIs(saved.unread_since, the_list.NoTimestamp.UNSET)
        self.assertIsInstance(saved.log[-2].what, the_list.QuestionRequestReceipt)
        self.assertEqual(saved.log[-1].what, the_list.MarkedRead())

    def test_words_actions_keep_updates_that_arrive_after_render_unread(self) -> None:
        root = self.root / "reviews"
        for action in ("question", "tell"):
            with self.subTest(action=action):
                item = self.add("Keep the update that arrives after render")
                fields = self.fields(item, action, words="Which check passed?")
                newer = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
                _ = list_page.submit(root, urlencode(fields))
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(the_list.unread_update(saved), the_list.unread_update(newer))
                self.assertFalse(any(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log))
                _ = list_page.submit(root, urlencode(self.fields(saved, action, words="Which check is next?")))
                self.assertIs(the_list.load(MORTY.id, item.id).unread_since, the_list.NoTimestamp.UNSET)

    def test_saved_question_and_answer_appear_on_the_item_and_the_answer_is_unread(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Inspect the List", number=106)
        fields = self.fields(item, "question", words="What changed in <List>?")
        asked_at = NOW - timedelta(days=1)
        answered_at = NOW - timedelta(minutes=25)
        answer_logged_at = answered_at + timedelta(minutes=1)
        asked_time = f'<time datetime="{asked_at.isoformat()}">2026-11-15 13:00 EST</time>'
        answer_time = f'<time datetime="{answered_at.isoformat()}">12:35 EST</time>'
        question_clock = Mock(wraps=datetime)
        question_clock.configure_mock(**{"now.side_effect": (asked_at, answered_at)})
        with patch.object(page_question, "datetime", question_clock):
            delivery = list_page.submit(root, urlencode(fields), status)
            opened = list_page.render(root, status=status).decode().split('<script>', 1)[0]
            self.assertIn('data-waiting-for-answer="true"', opened)
            self.assertIn('<p data-waiting-note>The producer will answer.</p>', opened)
            self.assertIn('Question Q1 — waiting for an answer</h3>' + asked_time, opened)
            self.assertIn("What changed in &lt;List&gt;?", opened)
            self.assertNotIn('name="action" value="question"', opened)
            self.assertNotIn('aria-label="Unread update"', opened)
            with patch.object(change_page, "send_question", return_value=question_outcome()):
                list_page.deliver(delivery)
            with (patch.dict(os.environ, {"CODEX_THREAD_ID": "", "CLAUDE_CODE_SESSION_ID": MORTY.current_session}),
                  patch.object(the_list, "now", return_value=answer_logged_at), redirect_stdout(io.StringIO())):
                self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The <filter> changed."]), 0)
        _ = list_page.submit(root, urlencode(fields))
        answered = list_page.render(root).decode().split('<script>', 1)[0]
        self.assertIn('data-waiting-for-answer="false"', answered)
        self.assertIn('name="action" value="question"', answered)
        self.assertIn('aria-label="Unread update"', answered)
        self.assertIn('value="read">Mark read</button>', answered)
        panel, log = answered.split('<ol class="list-log">', 1)
        self.assertIn("Marked read <small>", log)
        self.assertNotIn("<strong>Answer</strong> to question Q1 <small>", log)
        self.assertNotIn('What changed in &lt;List&gt;?', log)
        self.assertNotIn('The &lt;filter&gt; changed.', log)
        self.assertIn('Question Q1</h3>' + asked_time + '<p>What changed in &lt;List&gt;?</p>', panel)
        self.assertIn('<h3>Answer <span class="question-unread">Unread</span></h3>'
                      + answer_time + '<p>The &lt;filter&gt; changed.</p>', panel)

        current = the_list.load(MORTY.id, item.id)
        _ = list_page.submit(root, urlencode(self.fields(current, "read")))
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "A newer update arrived."}, source="morty")
        read = list_page.render(root).decode().split('<script>', 1)[0]
        self.assertIn('aria-label="Unread update"', read)
        panel, log = read.split('<ol class="list-log">', 1)
        self.assertNotIn('data-question="Q1"', panel)
        self.assertNotIn('class="question-unread"', panel)
        log = re.sub(r'<span class="staff-name"[^>]*>(.*?)</span>', r'\1', log)
        question_row = ('<li class="question" data-log-index="Q1-asked">' + asked_time
                        + '<div><strong>Question</strong> Q1 <small>— nate</small>')
        logged_time = f'<time datetime="{answer_logged_at.isoformat()}">12:36 EST</time>'
        answer_row = ('<li class="question" data-log-index="Q1-answered">' + logged_time
                      + '<div><strong>Answer</strong> <small>— morty</small>')
        self.assertIn(question_row + '<p>What changed in &lt;List&gt;?</p></div></li>', log)
        self.assertIn(answer_row + '<p>The &lt;filter&gt; changed.</p></div></li>', log)
        self.assertEqual(log.count('12:36 EST'), 1)
        self.assertNotIn('12:35 EST', log)
        self.assertLess(log.index(answer_row), log.index(question_row))

        saved = the_list.load(MORTY.id, item.id)
        legacy = replace(saved, log=tuple(entry for entry in saved.log
                                         if entry.what != the_list.QuestionAnswered("Q1")))
        legacy_log = list_page.item_log_html(list_page.ListRecord(MORTY.id, legacy), NOW.astimezone(user_zone()),
                                             staff_names.read(), frozenset())
        self.assertIn('<li class="question" data-log-index="Q1-answered">' + answer_time, legacy_log)
        self.assertEqual(legacy_log.count('12:35 EST'), 1)

    def test_stale_mark_read_keeps_the_newer_answer_in_the_panel(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Read the answer shown by the form")
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        already_read = self.fields(the_list.load(MORTY.id, item.id), "read")
        with (patch.object(change_page, "send_question", return_value=question_outcome()),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO())):
            first = self.fields(the_list.load(MORTY.id, item.id), "question", words="Which check passed?")
            list_page.deliver(list_page.submit(root, urlencode(first), status))
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The first check passed."]), 0)
            current = the_list.load(MORTY.id, item.id)
            stale = self.fields(current, "read")
            second = self.fields(item, "question", words="Did the next check pass?")
            list_page.deliver(list_page.submit(root, urlencode(second), status))
            waiting = list_page.saved_action_state(MORTY.id, item.id, status=status).get("waiting_details_html", "")
            self.assertIn('data-question="Q1"', waiting)
            credential = re.search(r'data-open-read-request="([^"]+)" data-open-read-token="([^"]+)"', waiting)
            assert credential is not None
            request, token = credential.groups()
            _ = list_page.submit(root, urlencode({**stale, "request": request, "token": token}), status)
            self.assertIn('data-log-index="Q1-answered"',
                          list_page.saved_action_state(MORTY.id, item.id, status=status).get("waiting_details_html", ""))
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q2", "--text", "The next check passed."]), 0)

        current = the_list.load(MORTY.id, item.id)
        for fields in (stale, already_read):
            with self.subTest(request=fields["request"]):
                _ = list_page.submit(root, urlencode(fields), status)
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(the_list.unread_update(saved), the_list.unread_update(current))
                markup = list_page.render(root, include_counts=False, status=status).decode().split('<script>', 1)[0]
                panel, log = markup.split('<ol class="list-log">', 1)
                self.assertIn('data-question="Q2"', panel)
                self.assertIn('class="question-unread">Unread</span>', panel)
                self.assertIn('The next check passed.', panel)
                self.assertIn('data-log-index="Q1-answered"', log)
                self.assertNotIn('data-log-index="Q2-answered"', log)

    def test_answer_returns_a_parked_item_with_its_current_parking(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        until = NOW + timedelta(days=7)
        for unpark in (False, True):
            with self.subTest(unpark=unpark):
                item = self.add("Check the parked item")
                parked = the_list.park(MORTY.id, item.id, until, source="morty")
                earlier_unpark = self.fields(parked, "unpark")
                with patch.object(change_page, "send_question", return_value=question_outcome()):
                    list_page.deliver(list_page.submit(root, urlencode(
                        self.fields(parked, "question", words="Which check passed?")), status))
                waiting = list_page.saved_action_state(MORTY.id, item.id, status=status)
                self.assertEqual((waiting["waiting_for_answer"], waiting["parked"]), ("true", "true"))
                self.assertEqual(the_list.load(MORTY.id, item.id).parked_until, until)

                if unpark:
                    submission = list_page.submit(root, urlencode(earlier_unpark), status)
                    self.assertIsInstance(submission, list_page.ListUnparkSubmission)
                    assert isinstance(submission, list_page.ListUnparkSubmission)
                    current = list_page.saved_action_state(MORTY.id, item.id, status=status)
                    self.assertIs(submission.response_outcome(current), list_page.UnparkOutcome.CLEARED)
                    self.assertEqual((current["waiting_for_answer"], current["parked"]), ("true", "false"))

                with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1",
                                                    "--text", "The gate passed."]), 0)
                answered = list_page.saved_action_state(MORTY.id, item.id, status=status)
                self.assertEqual(answered["waiting_for_answer"], "false")
                self.assertEqual(answered["unread"], "true")
                self.assertEqual(answered["parked"], "false" if unpark else "true")

    def test_another_question_keeps_the_first_failure_visible(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Keep the first question available")

        def ask(words: str) -> list_page.ListQuestionDelivery:
            current = the_list.load(MORTY.id, item.id)
            delivery = list_page.submit(root, urlencode(self.fields(current, "question", words=words)), status)
            assert isinstance(delivery, list_page.ListQuestionDelivery)
            return delivery

        first = ask("Which check passed?")
        with (patch.object(change_page, "send_question", return_value=question_outcome("refused", "Producer offline")),
              patch.object(change_page, "alert_delivery_failures")):
            list_page.deliver(first)
        second = ask("Which check is next?")
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            for pending in (True, False):
                with self.subTest(second_pending=pending):
                    if not pending:
                        list_page.deliver(second)
                        send.assert_called_once()
                    self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=status)["waiting_for_answer"], "false")
                    markup = list_page.render(root, include_counts=False, status=status).decode().split('<script>', 1)[0]
                    self.assertRegex(markup, r'<tbody data-list-item[^>]*data-waiting-for-answer="false"[^>]*">')
                    self.assertEqual(markup.count("Question not delivered."), 1)
                    self.assertNotIn('value="resend-', markup)

    def test_recorded_question_failure_takes_priority_when_the_intent_clear_fails(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Keep the failed question available")
        asked = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check passed?")), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        _ = delivery.job.release()
        failure = change_page.FailedDelivery(delivery.attempt, delivery.page.review_list, delivery.row, "Producer offline")
        saved: change_page.StoredReviewMessage = {"saved": True, "source": str(delivery.page.review_list),
            "owner": MORTY.id, "message": delivery.message, "row": delivery.row}
        intent = change_page.DeliveryIntent(delivery.attempt, "verdict", recipients=(MORTY.id,))
        page = change_page.ReviewMessagePage(delivery.page.review_list, delivery.page.production,
                                             delivery.page.rows, "", saved)
        with status.locked():
            status.write_slots(page, delivery.row, change_page.DeliverySlots(change_page.NoDelivery(), intent))
            status.record_result_locked(delivery, failure)
        reopened = change_page.DeliveryStatus(root)
        self.assertEqual(reopened.slots(page, delivery.row).verdict, intent)
        self.assertEqual(reopened.failed_delivery(page, delivery.row), failure)
        markup = list_page.render(root, include_counts=False).decode().split('<script>', 1)[0]
        self.assertRegex(markup, r'<tbody data-list-item[^>]*data-waiting-for-answer="false"[^>]*">')
        self.assertIn("Question not delivered.", markup)
        self.assertNotIn('value="resend-', markup)

    def test_startup_reuses_the_list_question_key_after_a_result_write_failure(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Check the repair")
        asked = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check passed?")), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        path = status.path(delivery.page, delivery.row)
        transport = FakeQuestionTransport()
        publish = change_page.publish_question_attempt

        def lose_result(path: Path, record: delivery_state.QuestionAttempt) -> None:
            if record.state is delivery_state.QuestionSendState.SENT:
                raise OSError("Cannot save the result.")
            publish(path, record)

        with patch.object(change_page, "send_question", side_effect=transport.send):
            with (patch.object(change_page, "publish_question_attempt", side_effect=lose_result),
                  self.assertRaisesRegex(OSError, "Cannot save the result")):
                list_page.deliver(asked)
            sending = change_page.read_question_attempt(path)
            self.assertEqual(sending.state, delivery_state.QuestionSendState.SENDING)
            for _ in range(2):
                change_page.recover_review_messages(change_page.DeliveryStatus(root))
        self.assertEqual(transport.calls, [sending.message_id] * 2)
        self.assertEqual(len(transport.receipts), 1)
        saved = change_page.read_question_attempt(path)
        self.assertEqual(saved.state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(saved.attempt_id, sending.attempt_id)

    def test_ask_sends_to_the_keeper_when_another_producer_has_the_item(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Check a producer's work")
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        words = "Which check passed?"
        submission = list_page.submit(root, urlencode(self.fields(item, "question", words=words)), status)
        markup = list_page.render(root, include_counts=False, status=status).decode().split("<script>", 1)[0]
        self.assertIn(words, markup)
        for text in ("Resend question", "ready to send", "may not have reached", "Question not delivered."):
            self.assertNotIn(text, markup)
        assert isinstance(submission, list_page.ListQuestionDelivery)
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            list_page.deliver(submission)
        path = status.path(submission.delivery.page, submission.delivery.row)
        record = change_page.read_question_attempt(path)
        self.assertIs(record.state, delivery_state.QuestionSendState.SENT)
        send.assert_called_once_with(MORTY.id, record.message, record.message_id)

    def test_question_start_error_records_a_failure_and_logs_one_line(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Keep the failed question")
        submission = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check passed?")), status)
        assert isinstance(submission, list_page.ListQuestionDelivery)
        with (patch.object(change_page, "_authorize_question_send", side_effect=RuntimeError("Cannot verify\nsource")),
              patch.object(change_page, "send_question") as send, redirect_stderr(io.StringIO()) as output):
            list_page.deliver(submission)
        send.assert_not_called()
        path = status.path(submission.delivery.page, submission.delivery.row)
        record = change_page.read_question_attempt(path)
        self.assertIs(record.state, delivery_state.QuestionSendState.FAILED)
        self.assertIsInstance(record.owner, delivery_state.NoQuestionSendOwner)
        self.assertEqual(output.getvalue().splitlines(), [f"review-links question {submission.delivery.row}: Cannot verify source"])

    def test_a_failed_list_question_alerts_the_item_showrunner(self) -> None:
        root = self.root / "reviews"
        item = self.add("Ask about the repair", owner=OTHER.id)
        asked = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check passed?")))
        refused = question_outcome("refused", "List keeper is offline.")
        with (patch.object(change_page, "send_question", return_value=refused) as send,
              patch.object(change_page, "send_to_staff") as alert,
              patch.object(change_page, "review_alert_showrunners") as fallback,
              patch.object(time, "sleep")):
            list_page.deliver(asked)
        self.assertEqual(send.call_count, conf.read("review_links").whole("delivery_tries"))
        self.assertTrue(all(call.args[0] == MORTY.id for call in send.call_args_list))
        alert.assert_called_once()
        self.assertEqual(alert.call_args.args[0], OTHER.id)
        self.assertIn("Which check passed?", cast(str, alert.call_args.args[2]))
        fallback.assert_not_called()

    def test_question_result_write_failure_keeps_the_saved_question(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        first_jobs: dict[str, the_list.ListItem] = {}
        locks: dict[str, Path] = {}
        for number in (1, 2):
            item = self.add(f"Recovery question {number}", number=number)
            asked = list_page.submit(root, urlencode(self.fields(item, "question", words=f"Which check {number}?")), status)
            assert isinstance(asked, list_page.ListQuestionDelivery)
            delivery = asked.delivery
            assert isinstance(delivery, change_page.PageQuestionSend)
            assert isinstance(delivery.job, change_page.QuestionJob)
            self.assertEqual(item.id, delivery.job.record.source.item_id)
            first_jobs[delivery.job.record.message_id] = item
            locks[delivery.job.record.message_id] = Path(cast(str, delivery.job.lock.name))
            _ = delivery.job.release()
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        recovering = change_page.DeliveryStatus(root)
        transport = FakeQuestionTransport()
        publish = change_page.publish_question_attempt
        lost: list[str] = []

        def fail_first_result(path: Path, record: delivery_state.QuestionAttempt) -> None:
            if record.state is delivery_state.QuestionSendState.SENT and not lost:
                lost.append(record.message_id)
                raise OSError("Cannot save the result")
            publish(path, record)

        def send(recipient: staff.StaffId, message: str, message_id: str
                 ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            if lost:
                first = first_jobs[lost[0]]
                self.assertEqual(list_page.saved_action_state(MORTY.id, first.id, status=recovering)["waiting_for_answer"], "false")
                second = first_jobs[message_id]
                self.assertEqual(list_page.saved_action_state(MORTY.id, second.id, status=recovering)["waiting_for_answer"], "true")
                markup = list_page.render(root, include_counts=False, status=recovering).decode().split('<script>', 1)[0]
                self.assertIn('Question Q1 — waiting for an answer', markup)
                self.assertNotIn("Question not delivered.", markup)
                self.assertEqual(len(list_page.waiting_updates(console.NewsRequest(root))), 2)
            return transport.send(recipient, message, message_id)

        with (patch.object(change_page, "publish_question_attempt", side_effect=fail_first_result),
              patch.object(change_page, "send_question", side_effect=send)):
            change_page.recover_review_messages(recovering)
            with file_lock.exclusive(locks[lost[0]]):
                change_page.recover_review_messages(change_page.DeliveryStatus(root))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(len(transport.receipts), 2)

    def test_accepted_question_sends_once_when_job_cleanup_fails(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("The accepted question")
        fields = self.fields(item, "question", words="Which check passed?")
        delivery = list_page.submit(root, urlencode(fields), status)
        transport = FakeQuestionTransport()

        with patch.object(change_page, "send_question", side_effect=transport.send):
            with (patch.object(status.question_jobs, "discard", side_effect=OSError("Cannot clear the job.")),
                  self.assertRaisesRegex(OSError, "Cannot clear the job.")):
                list_page.deliver(delivery)
            restarted = change_page.DeliveryStatus(root)
            change_page.recover_review_messages(restarted)
            list_page.deliver(list_page.submit(root, urlencode(fields), restarted))
        markup = list_page.render(root, include_counts=False).decode().split('<script>', 1)[0]
        self.assertIn('data-waiting-for-answer="true"', markup)
        self.assertNotIn('value="resend-Q1"', markup)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.receipts), 1)

    def test_a_title_edit_preserves_ready_sent_and_queued_questions(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        state = delivery_state.QuestionSendState
        for number, previous_state in enumerate((state.READY, state.SENT, state.QUEUED), 1):
            with self.subTest(state=previous_state):
                item = self.add("Original title", number=number)
                words = "Which check passed?\nWhat did you find?"
                asked = list_page.submit(root, urlencode(self.fields(item, "question", words=words)), status)
                assert isinstance(asked, list_page.ListQuestionDelivery)
                delivery = asked.delivery
                assert isinstance(delivery, change_page.PageQuestionSend)
                assert isinstance(delivery.job, change_page.QuestionJob)
                claimed = delivery.job.record
                path = status.path(delivery.page, delivery.row)
                if previous_state is state.READY:
                    with status.locked():
                        change_page.advance_question_attempt(path, claimed, replace(
                            claimed, state=state.READY, owner=delivery_state.NoQuestionSendOwner()))
                    _ = delivery.job.release()
                else:
                    outcome = "queued" if previous_state is state.QUEUED else "sent"
                    with patch.object(change_page, "send_question", return_value=question_outcome(outcome)):
                        list_page.deliver(asked)

                edited = the_list.update(MORTY.id, item.id, {"title": "New title"}, source="morty")
                target = change_page.list_question_target(Path(claimed.source.path), edited.id, edited.number,
                                                          edited.title, MORTY.id)
                with status.locked(), self.subTest(check="state after edit"):
                    view = change_page.question_delivery_view(status, target.saved(edited.questions[0]))
                    self.assertEqual(view.state, previous_state)

                with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
                    change_page.recover_review_messages(status)
                    if previous_state is state.READY:
                        send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
                    else:
                        send.assert_not_called()
                final_state = state.SENT if previous_state is state.READY else previous_state
                self.assertEqual(change_page.read_question_attempt(path).state, final_state)
                self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=status)["waiting_for_answer"], "true")
                body = list_page.render(root, include_counts=False, status=status).decode().split("<script>", 1)[0]
                self.assertNotIn('value="resend-Q1"', body)

    def test_recovery_and_list_reject_a_changed_question_message(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        state = delivery_state.QuestionSendState
        cases = ((state.READY, False), (state.SENT, False), (state.QUEUED, False), (state.READY, True))
        for number, (previous_state, changed_command) in enumerate(cases, 1):
            with self.subTest(state=previous_state, changed_command=changed_command):
                item = self.add("Check the saved question", number=number)
                words = "Which check finished?"
                asked = list_page.submit(root, urlencode(self.fields(item, "question", words=words)), status)
                assert isinstance(asked, list_page.ListQuestionDelivery)
                delivery = asked.delivery
                assert isinstance(delivery, change_page.PageQuestionSend)
                assert isinstance(delivery.job, change_page.QuestionJob)
                claimed = delivery.job.record
                path = status.path(delivery.page, delivery.row)
                receipt: delivery_state.NoQuestionSendReceipt | delivery_state.QuestionSendReceipt
                if previous_state is state.READY:
                    receipt = delivery_state.NoQuestionSendReceipt()
                else:
                    receipt = delivery_state.QuestionSendReceipt("queued" if previous_state is state.QUEUED else "sent", "")
                wrong_message = (claimed.message.replace("--question Q1", "--question Q99") if changed_command
                                 else claimed.message.replace(words, "A different question."))
                with status.locked():
                    change_page.advance_question_attempt(path, claimed, replace(
                        claimed, state=previous_state, message=wrong_message,
                        owner=delivery_state.NoQuestionSendOwner(), receipt=receipt))
                _ = delivery.job.release()

                with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
                    change_page.recover_review_messages(status)
                    send.assert_not_called()
                    self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=status)["waiting_for_answer"], "false")
                    body = list_page.render(root, include_counts=False, status=status).decode().split("<script>", 1)[0]
                    self.assertNotIn('value="resend-', body)
                    current = the_list.load(MORTY.id, item.id)
                    fields = urlencode(self.fields(current, "question", words=words))
                    list_page.deliver(list_page.submit(root, fields, status))
                    send.assert_not_called()
                self.assertEqual(the_list.load(MORTY.id, item.id).questions, current.questions)

    def test_mixed_question_results_keep_counts_and_unread_in_agreement(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("The item with four questions")
        cases = (("Already answered?", question_outcome()), ("Sent to the owner?", question_outcome()),
                 ("The owner refused?", question_outcome("refused", "The owner is offline.")),
                 ("What happened?", (send_result.UnconfirmedSendOutcome(), "The response is missing.")))
        for words, outcome in cases:
            current = the_list.load(MORTY.id, item.id)
            delivery = list_page.submit(root, urlencode(self.fields(current, "question", words=words)), status)
            with (patch.object(change_page, "send_question", return_value=outcome),
                  patch.object(change_page, "alert_delivery_failures")):
                list_page.deliver(delivery)
        with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
              redirect_stdout(io.StringIO())):
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The check passed."]), 0)
        reopened = change_page.DeliveryStatus(root)
        body = list_page.render(root, include_counts=False, status=reopened).decode().split("<script>", 1)[0]
        self.assertIn('data-waiting-for-answer="false"', body)
        self.assertIn('aria-label="Unread update"', body)
        self.assertEqual(body.count("Question not delivered."), 1)
        self.assertNotIn('value="resend-', body)
        self.assertIn('data-unread-answer="true"', body)
        self.assertRegex(body, r'data-filter-value="needs-me"[^>]*data-filter-count="1"')
        self.assertEqual(len(list_page.waiting_updates(console.NewsRequest(root))), 1)
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=reopened)["unread"], "true")
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        read = list_page.render(root, include_counts=False, status=reopened).decode().split("<script>", 1)[0]
        self.assertRegex(read, r'data-filter-value="needs-me"[^>]*data-filter-count="0"')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_moves_a_saved_question_to_morty_answering(self) -> None:
        _ = self.add("Only this row matches the search", the_list.ItemState.WAITING_FOR_NATE, number=1)
        _ = self.add("Other item", number=2, owner=OTHER.id)
        self.browser_action_sequence(r"""
 const input=document.getElementById('list-search');
 const count=document.querySelector('[data-list-count]');
 const changes=[];
 window.fetch=new Proxy(window.fetch,{apply(fetch,receiver,args){
  changes.push(args[1]?.method || 'GET');return Reflect.apply(fetch,receiver,args);
 }});
 show('all');
 form.elements.words.value='Which check passed?';
 submit('question');
 check(!row.hidden,'The row stays in All until the save succeeds');
 await finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});
 check(!row.hidden,'The saved row stays in All.');
 check(row.querySelector('[data-open-question-note]').getClientRects().length &&
  row.querySelector('.list-state').textContent==='Waiting for nate','The note appears below the saved state.');
 check(changes.join()==='POST','The chip changes without a refresh.');
 check(count.textContent==='2 of 2 items shown · 0 need you · 1 unread','All counts the saved row.');
 const needsMe=document.getElementById('filter-show-needs-me');
 const staffChip=(name,id)=>document.querySelector('[data-filter-row="'+name+'"] [data-filter-value="staff:'+id+'"]');
 check(needsMe.dataset.filterCount==='0' && staffChip('owner',row.dataset.owner).dataset.filterCount==='1',
  'Needs me excludes the saved row and Owner includes it.');
 check(staffChip('producer',row.dataset.producer).dataset.filterCount==='2','Producer counts the saved row.');
 for(const view of ['needs-me','blocked','parked','working']){
  show(view);check(row.hidden,view+' hides the saved row');
 }
 await navigate({show:'working',item:row.dataset.key});
 check(!row.hidden && row.closest('[data-linked-item]:not([hidden])') && !row.querySelector('[data-detail]').hidden &&
  row.querySelector('[data-waiting-note]').textContent==='The producer will answer.' &&
  !row.querySelector('[value="question"]'),'A direct link opens only the waiting row without Ask');
 check(count.textContent.startsWith('1 of 2'),'The linked row counts in All and stays outside Working.');
 show('morty-answering');
 const filtered=new Promise(resolve=>input.addEventListener('filterchange',resolve,{once:true}));
 input.value='Only this row';input.dispatchEvent(new Event('input'));
 await filtered;
 check(!row.hidden && count.textContent.startsWith('1 of 2'),'Search finds the waiting row in Morty answering.');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_unpark_replay_keeps_the_current_parking(self) -> None:
        item = self.add("Check the parked item", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=7), source="morty")
        with self.subTest(outcome="newer parking remains active"):
            self.browser_action_sequence('''
 show('parked');
 const chip=document.querySelector('[data-filter-row="show"] [data-filter-value="parked"]');
 submit('unpark');
 await finishPost({parked:'true',parked_until:'Nov 26 00:00 EST',waiting_for_answer:'true',
  open_question:'true',unpark_outcome:'cleared_newer_parking_active'});
 check(!row.hidden && chip.dataset.filterCount==='1','The newer parking keeps the waiting item in Parked.');
 check(row.querySelector('[data-waiting-note]') && row.dataset.waitingForAnswer==='true',
  'The parked row shows that the producer will answer.');
 check(row.querySelector('[data-parked-note]').textContent==='Parked until Nov 26 00:00 EST',
  'The parked row shows the newer time.');
 show('all');
 check(!row.hidden,'All shows the waiting item with the newer park.');
''', reduced_motion=True, add_item=False)
        self.browser_action_sequence('''
 show('parked');
 const chip=document.querySelector('[data-filter-row="show"] [data-filter-value="parked"]');
 const status=document.querySelector('[data-hidden-action-status]');
 const unpark=form.querySelector('[value="unpark"]');
 check(!row.hidden && chip.dataset.filterCount==='1','Parked includes the item before the question.');
 unpark.focus();
 submit('unpark');
 await finishPost({parked:'false',parked_until:'',waiting_for_answer:'true',open_question:'true',unpark_outcome:'cleared'});
 check(row.hidden && chip.dataset.filterCount==='0','The waiting item leaves Parked and its count.');
 check(document.activeElement===chip,'The Parked filter receives focus.');
 check(!status.hidden && status.textContent==='Item #1 unparked.',
  'Show keeps the Unpark confirmation for the waiting item.');
 await navigate({show:'parked',item:row.dataset.key});
 check(!row.hidden && !row.querySelector('[data-detail]').hidden &&
  row.querySelector('[data-waiting-note]').textContent==='The producer will answer.' &&
  !row.querySelector('form'),'The direct link opens the item with only the waiting line.');
 check(chip.dataset.filterCount==='0' && !status.hidden,
  'The direct link leaves the item out of the count and keeps the Unpark confirmation.');
 show('morty-answering');
 check(!row.hidden && !status.hidden,'Morty answering keeps the Unpark confirmation.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_open_row_and_shows_the_refusal_after_a_lost_remove(self) -> None:
        self.browser_action_sequence('''
 show('working');
 const remove=submit('remove');
 check(row.hidden,'Remove immediately hides the row in Working');
 await losePost();
 check(!row.hidden && row.dataset.state==='Working','the lost response restores the Working row');
 await finishRowMotion();
 check(submit('remove').id===remove.id,'the exact Remove retry uses the same ID');
 show('blocked');
 await finishPost({reason:'Fixture refuses Remove'});
 const hiddenStatus=document.querySelector('[data-hidden-action-status]');
 check(row.hidden && hiddenStatus.getClientRects().length>0 &&
  hiddenStatus.textContent==='Item 1: Not saved: Fixture refuses Remove',
  'the refusal is visible when the Blocked filter hides the restored row');
 show('working');
 check(!row.hidden && row.dataset.state==='Working','the refusal restores the confirmed row to Working');
 const notice=form.querySelector('[data-send-status]');
 check(notice.getClientRects().length>0 && notice.textContent==='Not saved: Fixture refuses Remove',
  'the refusal is visible on the restored row');
 check(hiddenStatus.hidden,'the page hides the extra notice when the restored row is visible');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reads_the_saved_item_after_a_lost_park_or_remove_response(self) -> None:
        self.browser_action_sequence('''
 show('working');
 const loseAndRead=async()=>{
  let readStarted;
  const started=new Promise(resolve=>{readStarted=resolve;});
  readback=key=>new Promise(resolve=>readStarted({key,reply:resolve}));
  const saved=losePost();
  return {...await started,saved};
 };
 form.elements.until.value='2026-12-03';
 const park=submit('park');
 const parkRead=await loseAndRead();
 check(row.hidden && parkRead.key===row.dataset.key,'Park waits for the current item before restoring the row.');
 parkRead.reply({item_state:'Working',unread:'true',parked:'true',parked_until:'Dec 3 00:00 EST',park_request_saved:true});
 await parkRead.saved;
 check(row.hidden,'The saved Park stays out of Working.');
 show('parked');
 check(!row.hidden && row.querySelector('[data-parked-note]').textContent==='Parked until Dec 3 00:00 EST',
  'Parked shows the saved parking time.');
 check(submit('park').id===park.id,'The exact Park retry uses the original request ID.');
 await finishPost({item_state:'Working',parked:'true',parked_until:'Dec 3 00:00 EST'});
 submit('remove');
 const removeRead=await loseAndRead();
 removeRead.reply({item_state:'Closed',unread:'false',parked:'true',parked_until:'Dec 3 00:00 EST'});
 await removeRead.saved;
 show('all');
 check(row.hidden && row.dataset.state==='Closed','The saved Remove stays closed after readback.');
''', reduced_motion=True)
        for action in ("park", "park-until-delivered"):
            with self.subTest(action=action, failed_readback=True):
                self.browser_action_sequence('''
 show('working');
 refreshMarkup=document.documentElement.outerHTML;
 form.elements.until.value='2026-12-03';
 let readRequest;
 readback=(key,request)=>{
  readRequest=request;
  throw new Error('The item read failed.');
 };
 const park=submit(PARK_ACTION);
 check(row.hidden,'The pending park hides the row from Working.');
 const replaced=waitForRefresh();
 await losePost();
 await replaced;
 check(readRequest===park.id,'The item read uses the original request ID.');
 const refreshed=document.querySelector('[data-number="1"]');
 check(refreshed!==row && !refreshed.hidden && refreshed.dataset.parked==='false',
  'The immediate refresh restores the unparked row to Working.');
'''.replace('PARK_ACTION', json.dumps(action)), reduced_motion=True, add_item=False)
        item = the_list.load(MORTY.id, "L1")
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=1), source="morty")
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        for action in ("park", "park-until-delivered"):
            for saved in (False, True):
                with self.subTest(action=action, park_request_saved=saved):
                    self.browser_action_sequence('''
 await navigate({show:'parked',unread:'1',item:row.dataset.key});
 const linked=document.querySelector('[data-linked-item]');
 const isLinked=()=>!linked.hidden && linked.contains(row) && !row.querySelector('[data-detail]').hidden;
 check(isLinked(),'The read item starts linked under Parked with Unread only on.');
 const saved=PARK_RECEIPT;
 const action=PARK_ACTION;
 const previous='Nov 17 13:00 EST';
 const until=saved && action==='park-until-delivered' ? 'delivered' : previous;
 readback=(key,request)=>{
  check(key===row.dataset.key && request===park.id,'The read uses the exact Park request ID.');
  return {item_state:'Working',unread:'false',parked:'true',parked_until:until,
   park_request_saved:saved};
 };
 form.elements.until.value='2026-12-03';
 const park=submit(action);
 check(!isLinked(),'Park releases the link at submission.');
 await losePost();
 check(isLinked()===!saved,'Only a missing receipt restores the link.');
 if(!saved){
  check(row.querySelector('[data-parked-note]').textContent==='Parked until '+previous,
   'The restored row shows the previous saved parking time.');
  check(submit(action).id===park.id,'The restored row keeps the exact Park request ID.');
  await finishPost({reason:'Fixture refuses Park'});
 }
'''.replace('PARK_RECEIPT', json.dumps(saved)).replace('PARK_ACTION', json.dumps(action)),
                                                 reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_updates_the_unread_sort_before_refresh(self) -> None:
        with patch.object(the_list, "now", return_value=NOW - timedelta(minutes=1)):
            _ = self.add("An older unread update", number=2)
        self.browser_action_sequence("""
 show('all');
 const sort=document.querySelector('[data-sort="7"]');sort.click();sort.click();
 const first=()=>document.querySelector('[data-list-item]').dataset.number;
 const updateKey=()=>JSON.parse(row.dataset.sortValues)[7];
 const original=updateKey();
 check(original>0 && first()==='1','the newest unread item sorts first');
 submit('remove');
 check(updateKey()===0 && first()==='2','a pending Remove sorts as read');
 await finishPost({reason:'Fixture refuses Remove'});
 check(updateKey()===original && first()==='1','a refusal restores the exact Update sort key');
 submit('remove');
 await finishPost({item_state:'Closed',parked:'false',unread:'false'});
 check(updateKey()===0 && first()==='2','a confirmed Remove sorts as read without a refresh');
""", reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_refusal_restores_update_order_during_motion(self) -> None:
        with patch.object(the_list, "now", return_value=NOW - timedelta(minutes=1)):
            _ = self.add("An older unread update", number=2)
        self.browser_action_sequence(r"""
 show('working');
 const sort=document.querySelector('[data-sort="7"]');sort.click();sort.click();
 const first=()=>document.querySelector('[data-list-item]:not([hidden])').dataset.number;
 const animation=()=>document.querySelector('[data-record-motion]')?.getAnimations({subtree:true})[0];
 const updateKey=()=>JSON.parse(row.dataset.sortValues)[7];
 const original=updateKey();
 check(original>0 && first()==='1','the newest unread item starts first');
 submit('remove');
 const exit=animation();
 if(!exit) throw new Error('Remove must animate the exit.');
 exit.pause();exit.currentTime=exit.effect.getTiming().duration/2;
 check(row.hidden && updateKey()===0 && first()==='2','Remove clears the Update key during the exit');
 await finishPost({reason:'Fixture refuses Remove'});
 const restore=animation();
 check(!row.hidden && restore && restore!==exit,'the refusal reverses the exit animation');
 check(updateKey()===original && first()==='1','the restored Update key puts the row first');
 if(restore) await finishAnimations([restore]);
 check(!animation() && first()==='1','the correct order remains after the animation');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_closed_response_clears_unread_when_the_server_reports_read(self) -> None:
        self.browser_action_sequence(r"""
 show('all');
 const updateKey=()=>JSON.parse(row.dataset.sortValues)[7];
 check(updateKey()>0 && row.querySelector('.list-dot'),'the item starts unread');
 submit('remove');
 await finishPost({item_state:'Closed',unread:'false'});
 check(row.dataset.state==='Closed' && row.dataset.unread==='false','the Closed item is read');
 check(updateKey()===0,'the Closed item has a zero Update key');
 check(!row.querySelector('.list-dot') && !form.querySelector('[value="read"]'),
  'the Closed item has no unread controls');
 onlyUnread();
 check(row.hidden,'the Closed item stays out of Unread');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_excludes_a_legacy_closed_item_with_a_waiting_question(self) -> None:
        _ = self.add("Open item", number=1)
        item = self.add("Closed with a waiting question", number=2, owner=OTHER.id)
        closed = the_list.close(MORTY.id, item.id, "The report is retired.", source="nate")
        path = the_list.folder(MORTY.id) / f"{item.id}.md"
        question = question_record.Question("Q1", NOW.isoformat(), "Is this working?")
        stored = path.read_text().replace("\n---\n", "\nquestions: "
                                         + json.dumps(json.dumps([question_record.stored(question)])) + "\n---\n", 1)
        _ = path.write_text(stored)
        self.assertEqual(the_list.load(MORTY.id, item.id), replace(closed, questions=(question,)))
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        self.assertNotIn("Closed with a waiting question", markup)
        self.assertNotIn(f'data-filter-value="staff:{OTHER.id}"', markup)
        self.browser_action_sequence(r'''
 const chip=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]');
 const selected=()=>document.querySelector('[data-filter-row="show"] [aria-pressed="true"]');
 const count=()=>document.querySelector('[data-list-count]').textContent;
 check(!chip('closed'),'Show has no Closed chip.');
 check(chip('morty-answering').dataset.filterCount==='0','The closed question does not count.');
 check(count()==='1 of 1 items shown · 0 need you · 1 unread','The totals count the open item.');
 for(const params of [{show:'closed'},{state:'Closed'}]){
  await navigate(params);
  check(selected().dataset.filterValue==='all' && count().startsWith('1 of 1'),'A legacy address selects All.');
 }
 await navigate({item:row.dataset.key.replace(/L1$/,'L2')});
 const notice=document.querySelector('[data-missing-item]');
 check(!notice.hidden && notice.textContent==='Item #2 is closed','A closed item link shows one line.');
 check(count().startsWith('1 of 1'),'The closed link does not change the count.');
''', add_item=False)
        self.assertEqual(path.read_text(), stored)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_refusal_restores_unread_after_a_lost_remove(self) -> None:
        self.browser_action_sequence("""
 const original=JSON.parse(row.dataset.sortValues)[7];
 submit('remove');await finishRowMotion();
 await losePost();await finishRowMotion();
 check(!row.hidden && row.dataset.unread==='true','a lost Remove restores the unread update.');
 form.elements.until.value='2026-12-03';
 submit('park');
 await finishPost({reason:'Fixture refuses Park'});
 show('all');onlyUnread();
 check(!row.hidden && row.dataset.state==='Working','the refusal returns the Working row to Unread');
 check(row.querySelector('.list-dot') && form.querySelector('[value="read"]'),
  'the refusal restores the unread controls');
 check(JSON.parse(row.dataset.sortValues)[7]===original,'the refusal restores the Update sort key');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_collapses_and_refusal_restores_the_row(self) -> None:
        _ = self.add("Next item", number=2)
        self.browser_action_sequence(r"""
 document.querySelector('[data-sort="0"]').click();
 const next=document.querySelector('[data-number="2"]');
 show('working');
 const top=element=>element.getBoundingClientRect().top;
 const start=top(next),first=top(row);
 const motion=()=>document.querySelector('[data-record-motion]');
 const halfway=animation=>{animation.pause();animation.currentTime=animation.effect.getTiming().duration/2;};
 submit('remove');
 check(row.hidden && requests.length===1,'Remove starts the POST and hides the record in the same turn');
 const leaving=motion();
 check(leaving?.querySelector('[data-detail]:not([hidden])'),'the open details leave with the row');
 const exit=leaving?.getAnimations({subtree:true})[0];
 check(exit,'Remove starts the shared animation');
 if(!exit) throw new Error('No exit animation');
 halfway(exit);
 const opacity=Number(getComputedStyle(exit.effect.target).opacity);
 check(opacity>0 && opacity<1,'the row fades before it disappears');
 check(top(next)<start && top(next)>first,'the next row moves up during the fade');
 await finishAnimations([exit]);
 check(!motion() && Math.abs(top(next)-first)<2,'the next row ends in the removed row position');
 await finishPost({reason:'Fixture refuses Remove'});
 const restored=motion()?.getAnimations({subtree:true})[0];
 check(!row.hidden && restored,'the refusal restores the row with an animation');
 if(!restored) throw new Error('No restore animation');
 halfway(restored);
 check(top(next)<start && top(next)>first,'the next row moves down during restore');
 form.requestSubmit(form.querySelector('[value="remove"]'));
 check(!row.hidden && requests.length===1,'A second Remove during the fade sends no POST.');
 await finishAnimations([restored]);
 submit('remove');
 await finishRowMotion();
 check(!motion() && Math.abs(top(next)-first)<2,'Remove works after the fade ends.');
 await finishPost({item_state:'Closed',unread:'false'});
 show('all');
 check(row.hidden && !motion(),'a filter change keeps the Closed item hidden');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_respects_reduced_motion(self) -> None:
        _ = self.add("Check the motion preference", number=1)
        for reduced_motion in (False, True):
            with self.subTest(reduced_motion=reduced_motion):
                self.browser_action_sequence(r"""
 show('working');
 const reduced=matchMedia('(prefers-reduced-motion: reduce)').matches;
 check(reduced===EXPECTED_REDUCTION,'the browser uses the selected motion preference');
 submit('remove');
 check(row.hidden,'Remove hides the item before the response');
 check(Boolean(document.querySelector('[data-record-motion]'))===!reduced,
  'only the full motion setting animates the exit');
 await finishPost({reason:'Fixture refuses Remove'});
 check(!row.hidden,'the refusal restores the row');
 check(Boolean(document.querySelector('[data-record-motion]'))===!reduced,
  'only the full motion setting animates the restoration');
""".replace('EXPECTED_REDUCTION', json.dumps(reduced_motion)), reduced_motion=reduced_motion, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_mark_read_fades_at_click_and_restores_after_a_lost_response(self) -> None:
        item = self.add("Check the unread update", number=1)
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "An update is ready."}, source="morty")
        self.browser_action_sequence(r"""
 onlyUnread();
 const motion=()=>document.querySelector('[data-record-motion]')?.getAnimations({subtree:true})[0];
 const read=submit('read'),firstExit=motion();
 check(row.hidden && firstExit,'Mark read starts the fade before the response');
 await losePost();
 const restored=motion();
 check(!row.hidden && restored && restored!==firstExit,'a lost Mark read response slides the row back');
 check(form.querySelector('[data-send-status]').textContent==='Save not confirmed: Fixture lost response',
  'the restored item shows the failure reason');
 await finishRowMotion();
 check(!form.querySelector('[value="read"]').disabled,'the restored item can retry Mark read');
 check(submit('read').id===read.id,'the exact Mark read retry uses the same ID');
 await finishPost({unread:'true'});
 check(!row.hidden && row.dataset.unread==='true','a newer unread update returns the row to Unread');
 await finishRowMotion();
 form.elements.words.value='Send the update.';
 form.elements.words.dispatchEvent(new Event('input'));
 submit('tell');
 check(row.hidden && motion(),'Tell morty starts the fade before the response in Unread');
 await finishPost({unread:'false'});
 check(row.hidden,'Tell morty keeps the row outside Unread after confirmation');
""", add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_explicit_actions_release_open_rows_from_unread_only(self) -> None:
        _ = self.add("Check the current result", number=1)
        _ = self.add("Keep the next result in view", number=2)
        for actions in (("park", "read"), ("read", "park"), ("read",)):
            with self.subTest(actions=actions):
                self.browser_action_sequence("const actions=" + json.dumps(actions) + r""";
 const linked=document.querySelector('[data-linked-item]');
 const follow=()=>navigate({show:'all',unread:'1',item:row.dataset.key});
 onlyUnread();
 await follow();
 for(const action of actions){
  if(action==='read' && row.dataset.parked==='true') show('parked');
  if(action==='park' && row.dataset.unread==='false'){
   await follow();
   check(!linked.hidden && linked.contains(row),'The read item opens through its link outside Unread only.');
  }
  check(!row.hidden && !row.querySelector('[data-detail]').hidden,'The action starts with open details.');
  const section=row.closest('[data-linked-item]');
  const staysVisible=action==='park' && row.dataset.unread==='true';
  form.elements.until.value='2026-12-03';
  submit(action);
  if(staysVisible) check(!row.hidden,'All keeps the unread row visible during Park.');
  else check(row.hidden && (section || document).querySelector('[data-record-motion]'),
   action+' starts the fade in the same table before the response.');
  if(section) check(!section.hidden,'The linked section stays visible during its fade.');
  const parked=action==='park' || row.dataset.parked==='true';
  await finishPost({unread:action==='read' ? 'false' : 'true',parked:String(parked),
   parked_until:parked ? 'Dec 3 00:00 EST' : ''});
  await finishAnimations(document.getAnimations());
  check(row.hidden===(action==='read') && linked.hidden,
   action+' uses the saved unread state to show or hide the row in the main table.');
  check(!history.state.recordTable.linkedItem,'The action releases the link.');
 }
 if(actions.includes('park')){
  onlyUnread(false);
  show('all');
  check(!row.hidden && !linked.contains(row) && linked.hidden,'All shows the parked row in the main table.');
  for(const view of ['needs-me','morty-answering','working','blocked']){
   show(view);
   check(row.hidden,'The parked row is absent from '+view+'.');
  }
  show('parked');
  const note=row.querySelector('[data-detail]').hidden ?
   row.querySelector('[data-parked-note]') : form.querySelector('[data-parked-detail]');
  check(!row.hidden && note.textContent==='Parked until Dec 3 00:00 EST' && note.getClientRects().length>0,
   'Parked shows the row and its parking note.');
 }
""", add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_ask_leaves_unread_and_clears_the_dot_after_saving(self) -> None:
        self.browser_action_sequence(r"""
 onlyUnread();
 check(!row.hidden && row.querySelector('.list-dot'),'the item starts unread');
 form.elements.words.value='Which check passed?';
 form.elements.words.dispatchEvent(new Event('input'));
 submit('question');
 check(row.hidden,'Ask question starts the fade before the response in Unread');
 await finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});
 check(row.hidden && row.dataset.unread==='false','the saved question keeps the row outside Unread');
 onlyUnread(false);show('working');
 check(row.hidden && row.dataset.unread==='false' && !row.querySelector('.list-dot') && !form.querySelector('[value="read"]'),
  'The saved question also keeps the row outside Working with unread cleared.');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_moves_question_focus_only_when_the_submitted_row_had_focus(self) -> None:
        _ = self.add("The question item", number=1)
        _ = self.add("The next item", number=2, owner=OTHER.id)
        for destination in ("next row", "empty view", "user selection", "same row"):
            with self.subTest(destination=destination):
                self.browser_action_sequence("const destination=" + json.dumps(destination) + r""";
 const search=document.getElementById('list-search');
 const next=document.querySelector('[data-number="2"] .list-toggle');
 const all=document.querySelector('[data-filter-row="show"] [data-filter-value="all"]');
 if(destination!=='same row') show('working');
 if(destination==='empty view'){
  search.value='The question item';
  search.dispatchEvent(new CustomEvent('filterchange',{detail:search.value}));
 }
 const input=form.elements.words;
 input.value='Which check passed?';input.dispatchEvent(new Event('input'));
 form.querySelector('[value="question"]').focus();
 submit('question');
 if(destination==='user selection') all.focus();
 await finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});
 const expected=destination==='same row' ? row.querySelector('.list-toggle') :
  destination==='next row' ? next : destination==='empty view' ? search : all;
 check(document.activeElement===expected,'The browser keeps focus at '+destination);
""", add_item=False, reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_related_link_reveals_waiting_item_without_changing_filters(self) -> None:
        first = self.add("Check the current result", number=1)
        target = self.add("Check the later result", number=2)
        _ = the_list.update(MORTY.id, first.id, {"related_to": (target.number,)}, source="nate")
        status = change_page.DeliveryStatus(self.root / "reviews")
        delivery = the_list.ask_question(MORTY.id, target.id, the_list.PageRequest("ask", "question", "What passed?"), status)
        with patch.object(change_page, "send_question", return_value=question_outcome()):
            change_page.deliver(delivery, status)
        self.browser_action_sequence(r"""
 const search=document.getElementById('list-search');
 const target=document.querySelector('[data-number="2"]');
 show('working');
 const staffChip=(name,id)=>document.querySelector('[data-filter-row="'+name+'"] [data-filter-value="staff:'+id+'"]');
 staffChip('owner',row.dataset.owner).click();
 staffChip('producer',row.dataset.producer).click();
 search.value='check';search.dispatchEvent(new CustomEvent('filterchange',{detail:search.value}));
 check(target.hidden,'The waiting item stays out of Working.');
 row.querySelector('a[data-record-jump]').click();
 const place=new URLSearchParams(location.hash.slice(1));
 check(!target.hidden && target.closest('[data-linked-item]:not([hidden])') &&
  !target.querySelector('[data-detail]').hidden,'The related link opens the waiting item outside the results.');
 check(place.get('show')==='working' && place.get('owner')===row.dataset.owner && search.value==='check',
  'The related link keeps Show, Owner and the search');
 staffChip('producer',row.dataset.producer).click();
 check(!target.hidden && !target.querySelector('[data-detail]').hidden &&
  target.closest('[data-linked-item]:not([hidden])'),'Clicking the selected Producer keeps the linked item open.');
 check(location.hash==='#'+place,'Clicking the selected Producer keeps the address.');
 const results=document.querySelector('table[aria-label="The List"]');
 check(results.querySelectorAll('[data-list-item]:not([hidden])').length===1 &&
  document.querySelector('[data-list-count]').textContent.startsWith('1 of 2'),
  'The linked waiting row counts in All and stays outside Working.');
""", add_item=False, reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_direct_link_keeps_a_read_item_outside_unread_results(self) -> None:
        _ = self.add("The unread item", number=1)
        target = self.add("The read item", number=2)
        _ = the_list.mark_read(MORTY.id, target.id, source="nate")
        self.browser_action_sequence(r"""
 const target=document.querySelector('[data-number="2"]');
 const results=document.querySelector('table[aria-label="The List"]');
 const section=document.querySelector('[data-linked-item]');
 const count=document.querySelector('[data-list-count]');
 const unread=document.getElementById('list-unread');
 await navigate({show:'all',unread:'1',item:target.dataset.key});
 check(unread.checked && results.querySelectorAll('[data-list-item]:not([hidden])').length===1 &&
  count.textContent.startsWith('1 of 2'),'Unread only keeps one row in the results.');
 check(!section.hidden && section.contains(target) && !target.hidden && !target.querySelector('[data-detail]').hidden,
  'The direct link opens the read item outside the results.');
 onlyUnread(false);
 check(section.hidden && results.contains(target) && !target.hidden && count.textContent.startsWith('2 of 2'),
  'The read item rejoins the results when Unread only is off.');
""", add_item=False, reduced_motion=True)

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_browser_refresh_keeps_a_linked_delivered_item_after_its_answer(self) -> None:
        root = self.root / "reviews"
        source = self.add("Follow the delivered item", number=1)
        target = self.add("Check the delivered item", the_list.ItemState.DELIVERED, number=2)
        _ = the_list.update(MORTY.id, source.id, {"related_to": (2,)}, source="morty")
        _ = the_list.update(MORTY.id, target.id, {
            "latest_status": "\n\n".join(f"Check {n} passed." for n in range(55))}, source="morty")
        with patch.object(change_page, "send_question", return_value=question_outcome()):
            list_page.deliver(list_page.submit(root, urlencode(self.fields(
                target, "question", words="Which check passed?"))))
        with patch.object(list_page, "counts_strip", return_value=""):
            before = list_page.render(root).decode()
            with (
                patch.object(staff, "caller_identity", return_value=MORTY.current_session),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(the_list.main(["answer", target.id, "--question", "Q1", "--text", "The gate passed."]), 0)
            answered = list_page.render(root).decode()
        setup = r"""<script>
if(!history.state?.recordTable && !location.hash) history.replaceState(null,'','#show=working');
const snapshot=SNAPSHOT;
window.fetch=async(_url,options)=>{
 if(options?.method==='POST') throw new Error('The answer is below the viewport.');
 return new Response(snapshot);
};
</script>""".replace('SNAPSHOT', json.dumps(answered).replace("<", r"\u003c"))
        probe = """<script>
(async()=>{
 const stateKey='list-link-reload';
 const {stage,failures=[]}=JSON.parse(sessionStorage.getItem(stateKey) ?? '{}');
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 const consoleReload=RELOAD_CONSOLE;
 try{
  const source=document.querySelector('[data-number="1"]');
  const row=()=>document.querySelector('[data-number="2"]');
  const section=()=>document.querySelector('[data-linked-item]');
  const chip=name=>document.getElementById('filter-show-'+name);
  const expanded=item=>!item.querySelector('[data-detail]').hidden;
  const linked=item=>!section().hidden && section().contains(item) && !item.hidden && expanded(item);
  const framePlace=()=>location.pathname+location.search+location.hash;
  const consolePlace=()=>new URL(parent.location.href).searchParams.get('at.list');
  const reload=async next=>{
   if(consoleReload){
    if(consolePlace()!==framePlace()) await new Promise(resolve=>{
     parent.addEventListener('message',function recorded(event){
      if(event.source!==window || consolePlace()!==framePlace()) return;
      parent.removeEventListener('message',recorded);resolve();
     });
    });
    const savedPlace=new URL(consolePlace(),location.href);
    const reveal=new URLSearchParams(savedPlace.hash.slice(1)).get('reveal');
    check(reveal===(next==='answered' ? '1' : null),'The console address tracks the revealed item.');
   }
   sessionStorage.setItem(stateKey,JSON.stringify({stage:next,failures}));
   (consoleReload ? parent.location : location).reload();
  };
  if(stage==='dismissed'){
   check(section().hidden && !expanded(row()),'The reload keeps the dismissed link closed.');
  }else if(stage==='answered'){
   check(linked(row()) && chip('working').getAttribute('aria-pressed')==='true',
    'The reload keeps the answered item linked under Working.');
   chip('all').click();
   check(section().hidden && !expanded(row()),'Changing the filter dismisses the link after reload.');
   await reload('dismissed');
   return;
  }else{
  source.querySelector('.list-toggle').click();
  source.querySelector('a[data-record-jump]').click();
  const waiting=row();
  check(linked(waiting) && chip('morty-answering').dataset.filterCount==='1',
   'The link opens the waiting item outside Working and counts it in Morty answering.');
  const replaced=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{
    if(waiting.isConnected) return;
    observer.disconnect();resolve();
   });
   observer.observe(document.body,{childList:true,subtree:true});
  });
  document.dispatchEvent(new Event('visibilitychange'));
  await replaced;
  const answered=row();
  check(linked(answered) && answered.querySelector('[data-question="Q1"]').textContent.includes('The gate passed.'),
   'Refresh keeps the linked details open and shows the answer.');
  check(chip('working').getAttribute('aria-pressed')==='true' &&
   chip('needs-me').dataset.filterCount==='1' && chip('morty-answering').dataset.filterCount==='0' &&
   document.querySelector('[data-list-count]').textContent.startsWith('1 of 2'),
   'The item counts in Needs me and stays outside the Working results.');
  chip('working').click();
  check(linked(answered),'The selected filter keeps the link open.');
  await reload('answered');
  return;
  }
 }catch(error){failures.push(String(error));}
 await parent.fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        for reload_console in (False, True):
            with self.subTest(reload_console=reload_console):
                self.browser_answer_visibility(before, setup, probe.replace("RELOAD_CONSOLE", json.dumps(reload_console)),
                                               reload_markup=answered, reload_console=reload_console)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_ask_focus_after_a_failed_post(self) -> None:
        self.browser_action_sequence(r"""
 onlyUnread();
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check passed?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 check(row.hidden,'The row leaves Unread during the request');
 ask.blur();
 check(document.activeElement===document.body,'The browser has no focused control while Ask is disabled');
 await losePost();
 check(!row.hidden && !ask.disabled && document.activeElement===ask,'The restored Ask button has focus');
 submit('question');
 const search=document.getElementById('list-search');
 search.focus();
 await finishPost({reason:'The POST was refused'});
 check(document.activeElement===search,'A failed request preserves the focus the user chose');
""", reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_ask_focus_after_the_unread_row_finishes_its_animation(self) -> None:
        _ = self.add("Restore the unread row", number=1)
        for user_selects_search in (False, True):
            with self.subTest(user_selects_search=user_selects_search):
                self.browser_action_sequence(r"""
 onlyUnread();
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check passed?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 ask.blur();
 await losePost();
 const spacer=document.querySelector('[data-record-motion]');
 const animation=spacer?.getAnimations({subtree:true})[0];
 check(animation && getComputedStyle(row).display==='none','The row is still hidden by the animation');
 if(!animation) throw new Error('The restored row must animate.');
 const search=document.getElementById('list-search');
 if(USER_SELECTS_SEARCH) search.focus();
 await finishAnimations([animation]);
 check(!row.hidden && !ask.disabled,'The row and Ask button are available');
 check(document.activeElement===(USER_SELECTS_SEARCH ? search : ask),'Focus returns only if the user did not move it');
""".replace('USER_SELECTS_SEARCH', json.dumps(user_selects_search)), add_item=False)

    def add_item_with_failed_question(self) -> the_list.ListItem:
        root = self.root / "reviews"
        item = self.add("Keep the row with a failed question", number=1)
        delivery = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check failed?")))
        with (patch.object(change_page, "send_question", return_value=question_outcome("refused", "Producer offline")),
              patch.object(change_page, "alert_delivery_failures")):
            list_page.deliver(delivery)
        return the_list.load(MORTY.id, item.id)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_keeps_row_focus_through_refresh_when_an_older_question_failed(self) -> None:
        _ = self.add_item_with_failed_question()
        self.browser_action_sequence(r"""
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 check(Boolean(row.querySelector('[data-question-delivery-failed]')),'The older question has a delivery failure');
 refreshMarkup=document.documentElement.outerHTML;
 input.value='Which check is next?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 ask.blur();
 const replaced=waitForRefresh();
 await finishPost({unread:'false',waiting_for_answer:'false'});
 await replaced;
 const refreshed=document.querySelector('[data-number="1"]');
 check(refreshed!==row && !refreshed.hidden,'The refresh keeps the row visible');
 check(document.activeElement===refreshed.querySelector('.list-toggle'),'The row toggle keeps focus through refresh');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_moves_focus_when_a_mixed_question_save_removes_the_row_from_unread(self) -> None:
        item = self.add_item_with_failed_question()
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        _ = self.add("The next unread item", number=2)
        self.browser_action_sequence(r"""
 onlyUnread();
 check(!row.hidden && row.querySelector('[data-question-delivery-failed]'),'Unread shows the item with a failed question');
 const snapshot=document.documentElement.cloneNode(true);
 snapshot.querySelector('[data-number="1"]').dataset.unread='false';
 refreshMarkup=snapshot.outerHTML;
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check is next?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 ask.blur();
 const replaced=waitForRefresh();
 await finishPost({unread:'false',waiting_for_answer:'false'});
 await replaced;
 const refreshed=document.querySelector('[data-number="1"]');
 check(refreshed!==row && refreshed.hidden,'The item leaves Unread and the List refreshes');
 check(document.activeElement===document.querySelector('[data-number="2"] .list-toggle'),
  'Focus moves to the next unread item and stays there through refresh');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_the_selected_action_on_another_item_after_refresh(self) -> None:
        _ = self.add_item_with_failed_question()
        _ = self.add("The next open item", number=2)
        self.browser_action_sequence(r"""
 const other=document.querySelector('[data-number="2"]');
 const remove=other.querySelector('button[value="remove"]');
 refreshMarkup=document.documentElement.outerHTML;
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check is next?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 other.querySelector('.list-toggle').click();
 remove.focus();
 check(document.activeElement===remove,'The user selects Remove on the other item during the request');
 const replaced=waitForRefresh();
 await finishPost({unread:'false',waiting_for_answer:'false'});
 await replaced;
 const refreshed=document.querySelector('[data-number="2"]');
 check(refreshed!==other && !refreshed.hidden,'The List refreshes the other item');
 check(document.activeElement===refreshed.querySelector('button[value="remove"]'),
  'Remove on the same item keeps focus through refresh');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_the_column_sort_focus_after_refresh(self) -> None:
        _ = self.add_item_with_failed_question()
        self.browser_action_sequence(r"""
 const sort=document.querySelector('[data-sort="3"]');
 refreshMarkup=document.documentElement.outerHTML;
 form.elements.words.value='Which check is next?';form.elements.words.dispatchEvent(new Event('input'));
 form.querySelector('[value="question"]').focus();
 submit('question');
 sort.focus();
 const replaced=waitForRefresh();
 await finishPost({unread:'false',waiting_for_answer:'false'});
 await replaced;
 const renewed=document.querySelector('[data-sort="3"]');
 check(renewed!==sort,'The List replaces the sort button during refresh');
 check(document.activeElement===renewed,'The selected column sort keeps focus');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_skips_a_row_that_is_still_in_its_return_animation(self) -> None:
        _ = self.add("The first unread item", number=1)
        _ = self.add("The second unread item", number=2)
        self.browser_action_sequence(r"""
 onlyUnread();
 const second=document.querySelector('[data-number="2"]');
 function ask(target){
  target.elements.words.value='Which check passed?';target.elements.words.dispatchEvent(new Event('input'));
  const button=target.querySelector('[value="question"]');
  button.focus();
  check(button.getClientRects().length && document.activeElement===button,'Ask is visible and has focus.');
  submit('question',target);
 }
 ask(form);
 const failFirst=finishPost;
 second.querySelector('.list-toggle').click();
 ask(second.querySelector('form'));
 await failFirst({reason:'The save failed'});
 const returning=row.nextElementSibling.getAnimations({subtree:true})[0];
 check(!row.hidden && !row.querySelector('.list-toggle').getClientRects().length,
  'The first row returns but its animation still hides its toggle');
 returning.pause();
 await finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});
 check(document.activeElement===document.getElementById('list-search'),
  'Search receives focus because neither row has an available toggle');
 returning.finish();
""", add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_question_response_preserves_focus_in_the_parent_console(self) -> None:
        _ = self.add("The question item", number=1)
        _ = self.add("The next item", number=2, owner=OTHER.id)
        self.browser_action_sequence(r"""
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 const nav=parent.document.getElementById('console-nav');
 input.value='Which check passed?';input.dispatchEvent(new Event('input'));
 ask.focus();
 check(document.hasFocus() && document.activeElement===ask,'Ask starts with focus in the List frame');
 submit('question');
 nav.focus();
 check(!document.hasFocus() && parent.document.activeElement===nav,'The user focuses the console during the request');
 await finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});
 check(!row.hidden,'The saved question stays in All.');
 check(parent.document.activeElement===nav,'The response preserves the console focus');
""", add_item=False, reduced_motion=True, in_frame=True)

    def render_unread_answer(self, *, latest_status: str = "",
                             questions_and_answers: tuple[tuple[str, str], ...] = (
                                 ("Which check passed?", "The gate passed."),)) -> tuple[the_list.ListItem, str]:
        root = self.root / "reviews"
        item = self.add("Read the answer after opening the tab")
        if latest_status:
            _ = the_list.update(MORTY.id, item.id, {"latest_status": latest_status}, source="morty")
        with (patch.object(change_page, "send_question", return_value=question_outcome()),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO())):
            for question, _answer in questions_and_answers:
                current = the_list.load(MORTY.id, item.id)
                fields = self.fields(current, "question", words=question)
                list_page.deliver(list_page.submit(root, urlencode(fields)))
            for number, (_question, answer) in enumerate(questions_and_answers, start=1):
                self.assertEqual(the_list.main(["answer", item.id, "--question", f"Q{number}", "--text", answer]), 0)
        markup = list_page.render(root, include_counts=False).decode()
        markup = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', markup, count=1)
        return item, markup

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_browser_direct_answer_link_waits_for_visibility(self) -> None:
        item, markup = self.render_unread_answer()
        fragment = urlencode({"show": "unread", "item": f"{MORTY.id}/{item.id}"})
        setup = """<script>
let visibility='hidden';
Object.defineProperty(document,'visibilityState',{get:()=>visibility});
window.frameElement.hidden=true;
history.replaceState(null,'','#FRAGMENT');
const reads=[];
window.fetch=async(_url,options)=>{
 if(options?.method!=='POST') return new Response('',{status:503});
 reads.push(new URLSearchParams(options.body));
 return new Response(JSON.stringify({unread:'true'}));
};
</script>""".replace('FRAGMENT', fragment)
        probe = """<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const row=document.querySelector('[data-list-item]'),detail=row.querySelector('[data-detail]');
 const panel=detail.querySelector('.list-panel');
 const unread=()=>row.dataset.unread==='true' && panel.querySelector('.question-unread');
 const showTab=()=>{visibility='visible';document.dispatchEvent(new Event('visibilitychange'));};
 const showFrame=()=>{
  window.frameElement.hidden=false;
  window.dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 };
 const paint=()=>new Promise(resolve=>parent.requestAnimationFrame(()=>parent.requestAnimationFrame(resolve)));
 try{
  check(!row.hidden && !detail.hidden,'The direct link opens the unread row');
  check(!reads.length && unread(),'A hidden tab keeps the answer unread');
  if(CLOSE_ROW) row.querySelector('.list-toggle').click();
  REVEAL_FIRST();
  await paint();
  check(!reads.length && unread(),'The answer stays unread while the tab or frame is hidden');
  REVEAL_SECOND();
  await paint();
  if(CLOSE_ROW) check(!reads.length && unread(),'Closing the row cancels its deferred read');
  else check(reads.length===1 && reads[0].get('action')==='read' &&
   reads[0].get('request')===panel.dataset.openReadRequest && reads[0].get('token')===panel.dataset.openReadToken,
   'The visible row sends its signed read once');
 }catch(error){failures.push(String(error));}
 await parent.fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        for first, second, close_row in (('showTab', 'showFrame', False), ('showFrame', 'showTab', False),
                                         ('showTab', 'showFrame', True)):
            with self.subTest(first=first, close_row=close_row):
                case = (probe.replace('REVEAL_FIRST', first).replace('REVEAL_SECOND', second)
                        .replace('CLOSE_ROW', str(close_row).lower()))
                self.browser_answer_visibility(markup, setup, case)

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_browser_direct_answer_link_waits_until_the_answer_scrolls_into_view(self) -> None:
        item, markup = self.render_unread_answer(latest_status="\n\n".join(f"Check {n} passed." for n in range(55)))
        fragment = urlencode({"item": f"{MORTY.id}/{item.id}"})
        setup = """<script>
history.replaceState(null,'','#FRAGMENT');
const reads=[];
let readStarted;
const readRequested=new Promise(resolve=>{readStarted=resolve;});
window.fetch=async(_url,options)=>{
 if(options?.method!=='POST') return new Response('',{status:503});
 reads.push(new URLSearchParams(options.body));
 readStarted();
 return new Response(JSON.stringify({unread:'true'}));
};
</script>""".replace('FRAGMENT', fragment)
        probe = """<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 try{
  const row=document.querySelector('[data-list-item]'),detail=row.querySelector('[data-detail]');
  const panel=detail.querySelector('.list-panel');
  const answer=[...panel.querySelectorAll('[data-question="Q1"] p')].find(p=>p.textContent==='The gate passed.');
  const sample=()=>new Promise(resolve=>{
   const observer=new IntersectionObserver(([entry])=>{observer.disconnect();resolve(entry);});
   observer.observe(answer);
  });
  check(!row.hidden && !detail.hidden && row.querySelector('.list-row').getBoundingClientRect().bottom<innerHeight,
   'The direct link opens a row within the viewport');
  check(!(await sample()).isIntersecting && answer.getBoundingClientRect().top>innerHeight,
   'The answer starts below the viewport');
  check(!reads.length && panel.querySelector('.question-unread'),'The answer below the viewport stays unread');
  answer.scrollIntoView({block:'center',behavior:'instant'});
  check((await sample()).isIntersecting,'Scrolling brings the answer into the viewport');
  await readRequested;
  check(reads.length===1 && reads[0].get('action')==='read' &&
   reads[0].get('request')===panel.dataset.openReadRequest && reads[0].get('token')===panel.dataset.openReadToken,
   'The answer sends one signed read when it enters the viewport');
 }catch(error){failures.push(String(error));}
 await parent.fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        self.browser_answer_visibility(markup, setup, probe)

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_browser_direct_answer_link_reads_once_in_a_720_pixel_viewport(self) -> None:
        with patch.object(list_page, "counts_strip", return_value=""):
            item, markup = self.render_unread_answer()
        markup = markup.replace('<section class="page-question" data-question="Q1">',
                                '<div id="answer-spacer" style="height:1800px"></div>'
                                + '<section class="page-question" data-question="Q1">', 1)
        fragment = urlencode({"item": f"{MORTY.id}/{item.id}"})
        setup = """<script>
history.replaceState(null,'','#FRAGMENT');
const reads=[];
let readStarted;
const readRequested=new Promise(resolve=>{readStarted=resolve;});
window.fetch=async(_url,options)=>{
 if(options?.method!=='POST') return new Response('',{status:503});
 reads.push(new URLSearchParams(options.body));
 readStarted();
 return new Response(JSON.stringify({unread:'false'}));
};
</script>""".replace('FRAGMENT', fragment)
        probe = """<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 try{
  const panel=document.querySelector('[data-detail]:not([hidden]) .list-panel');
  const answer=panel.querySelector('[data-question="Q1"] p:last-child');
  const spacer=document.getElementById('answer-spacer');
  spacer.style.height=(spacer.offsetHeight+1800-answer.getBoundingClientRect().top)+'px';
  const sample=()=>new Promise(resolve=>{
   const observer=new IntersectionObserver(([entry])=>{observer.disconnect();resolve(entry);});
   observer.observe(answer);
  });
  check(innerHeight===720 && Math.abs(answer.getBoundingClientRect().top-1800)<=1,
   'Expected a 720 pixel viewport and answer at 1,800 pixels. Got '+innerHeight+' and '+answer.getBoundingClientRect().top+'.');
  check(!(await sample()).isIntersecting && reads.length===0,'The offscreen answer sends no read.');
  answer.scrollIntoView({block:'center',behavior:'instant'});
  const visible=await sample();
  await readRequested;
  check(visible.intersectionRect.height>0 && reads.length===1 &&
   reads[0].get('action')==='read' && reads[0].get('token')===panel.dataset.openReadToken,
   'Scrolling to the answer sends one signed read.');
  scrollTo({top:0,behavior:'instant'});
  await sample();
  answer.scrollIntoView({block:'center',behavior:'instant'});
  check((await sample()).intersectionRect.height>0 && reads.length===1,
   'Scrolling to the answer again sends no second read.');
 }catch(error){failures.push(String(error));}
 await parent.fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        self.browser_answer_visibility(markup, setup, probe, viewport_height=720)

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_browser_item_link_waits_for_every_unread_answer(self) -> None:
        item, markup = self.render_unread_answer(questions_and_answers=(
            ("Which checks passed?", "\n".join(f"Check {number} passed." for number in range(65))),
            ("Did the final check pass?", "The final check passed.")))
        fragment = urlencode({"item": f"{MORTY.id}/{item.id}"})
        setup = """<script>
const fragment='#FRAGMENT',reads=[];
const seenEntries=[],NativeIntersectionObserver=window.IntersectionObserver;
window.IntersectionObserver=class extends NativeIntersectionObserver{
 constructor(callback,options){
  super((entries,observer)=>{
   if(options?.threshold) for(const entry of entries) seenEntries.push([
    entry.target.parentElement.dataset.question,entry.isIntersecting,entry.intersectionRatio,document.visibilityState
   ]);
   callback(entries,observer);
  },options);
 }
};
let receivedRead,receivedFirstAnswer;
const postedRead=new Promise(resolve=>{receivedRead=resolve;});
const firstAnswerSeen=new Promise(resolve=>{receivedFirstAnswer=resolve;});
const unobserveAnswer=IntersectionObserver.prototype.unobserve;
IntersectionObserver.prototype.unobserve=function(answer){
 unobserveAnswer.call(this,answer);
 if(answer.matches('[data-question="Q1"] > p:last-child')) receivedFirstAnswer();
};
window.fetch=async(_url,options)=>{
 if(options?.method!=='POST') return new Response('',{status:503});
 reads.push(new URLSearchParams(options.body));
 receivedRead();
 return new Response(JSON.stringify({unread:'true'}));
};
</script>""".replace('FRAGMENT', fragment)
        probe = """<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 try{
  const row=document.querySelector('[data-list-item]'),detail=row.querySelector('[data-detail]');
  if(!LINK_ON_LOAD){
   check(detail.hidden && !reads.length,'The loaded List starts with a closed and unread row');
   const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
   const link=document.createElement('a');
   link.href=fragment;document.body.append(link);link.click();
   await changed;
  }
  await new Promise(resolve=>parent.requestAnimationFrame(()=>parent.requestAnimationFrame(resolve)));
  const panel=detail.querySelector('.list-panel');
  const first=panel.querySelector('[data-question="Q1"] p:last-child');
  const second=panel.querySelector('[data-question="Q2"] p:last-child');
  const sample=target=>new Promise(resolve=>{
   const observer=new IntersectionObserver(([entry])=>{observer.disconnect();resolve(entry);});
   observer.observe(target);
  });
  const paint=()=>new Promise(resolve=>parent.requestAnimationFrame(()=>parent.requestAnimationFrame(resolve)));
  const showAnswer=async answer=>{
   scrollTo({top:scrollY+answer.getBoundingClientRect().top-16,behavior:'instant'});
   await paint();
  };
  const requireEvent=(event,message)=>{
   let timer;
   const deadline=new Promise((_,reject)=>{
    timer=setTimeout(()=>reject(new Error(message+' '+JSON.stringify({count:reads.length,seenEntries}))),20000);
   });
   return Promise.race([event,deadline]).finally(()=>clearTimeout(timer));
  };
  check(!row.hidden && !detail.hidden,'The item link opens the row');
  document.body.style.paddingBottom='100vh';
  await paint();
  await showAnswer(first);
  check((await sample(first)).intersectionRect.height>0,'The first answer enters the viewport');
  await requireEvent(firstAnswerSeen,'The List did not record the first visible answer within 20 seconds.');
  check(!(await sample(second)).isIntersecting && second.getBoundingClientRect().top>innerHeight,
   'The second answer starts below the viewport');
  check(!reads.length && panel.querySelectorAll('.question-unread').length===2,
   'The first answer alone leaves both answers unread');
  await showAnswer(second);
  check((await sample(second)).intersectionRect.height>0 && first.getBoundingClientRect().bottom<0,
   'The second answer enters the viewport after the first leaves it');
  await requireEvent(postedRead,'The List did not send the signed read within 20 seconds.');
  check(reads.length===1 && reads[0].get('action')==='read' &&
   reads[0].get('request')===panel.dataset.openReadRequest && reads[0].get('token')===panel.dataset.openReadToken,
   'The last answer to enter the viewport sends one signed read. '+JSON.stringify({count:reads.length,seenEntries}));
 }catch(error){failures.push(String(error));}
 await parent.fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        for link_on_load in (True, False):
            with self.subTest(link_on_load=link_on_load):
                enabled = str(link_on_load).lower()
                self.browser_answer_visibility(markup, setup, probe.replace('LINK_ON_LOAD', enabled),
                                               viewport_height=1120,
                                               console_fragment="#" + fragment if link_on_load else "")

    def browser_answer_visibility(self, markup: str, setup: str, probe: str, *,
                                  viewport_height: int | None = None, reload_markup: str = "",
                                  reload_console: bool = False, console_fragment: str = "",
                                  viewport_width: int | None = None) -> None:
        if not CHROME and viewport_width is not None and viewport_width < FIREFOX_MIN_SHARED_WIDTH:
            probe = '<img hidden src="/complete">' + probe
        page = markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>').encode()
        reloaded = reload_markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>').encode()
        loaded = threading.Event()
        height = f"{viewport_height}px" if viewport_height is not None else "calc(100vh - 24px)"
        frame_path = "/page"
        frame_url = frame_path
        console_shell = b""
        if reload_console or console_fragment:
            frame_path = list_page.PAGE_PATH
            listing = console.ConsolePage("list", "List", "list-checks", "The List", console.AvailablePage(frame_path))
            console_shell = console.render((listing,), console.UnavailablePage())
            place = frame_path + (console_fragment or "#show=working")
            frame_url = "/console/?" + urlencode({"page": "list", "at.list": place})
        host_page = (f'<!doctype html><style>iframe{{border:0;width:100%;height:{height}}}'
                     + 'iframe[hidden]{display:block;visibility:hidden}</style>'
                     + f'<iframe data-src="{html.escape(frame_url)}"></iframe>'
                     + """<script>
// Set the frame size before the List starts its observers.
const frame=document.querySelector('iframe');
requestAnimationFrame(()=>requestAnimationFrame(()=>{frame.src=frame.dataset.src;}));
</script>""").encode()
        results: list[list[str]] = []
        complete = threading.Event()

        class Probe(serve.ReviewLinksHandler):
            @override
            def send_head(self) -> BinaryIO | None:
                if self.path == frame_path:
                    self.html_response(200, reloaded if reloaded and loaded.is_set() else page)
                    loaded.set()
                elif console_shell and urlsplit(self.path).path == "/console/":
                    self.html_response(200, console_shell)
                elif self.path == "/":
                    self.html_response(200, host_page)
                elif self.path == "/complete":
                    _ = complete.wait(TEST_HANG_GUARD_SECS)
                    self.json_response(200, {})
                else:
                    self.send_error(404)
                return None

            @override
            def do_POST(self) -> None:
                if self.path == "/console/news":
                    self.json_response(200, {"new": []})
                    return
                if self.path != "/results":
                    self.send_error(404)
                    return
                results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                self.json_response(200, {})
                complete.set()

        with (patch.object(serve, "ReviewLinksHandler", Probe),
              patch.object(review_page.ReviewSnapshotCache, "start"),
              patch.object(change_page, "recover_pending"),
              patch.object(list_page, "recover_pending"),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              serve.ReviewLinksServer(("127.0.0.1", 0), self.root / "reviews") as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                window_height = max(900, (viewport_height or 0) + 32)
                destination = frame_url if console_fragment or viewport_width is not None else "/"
                self.browser_results(f"http://127.0.0.1:{server.server_port}{destination}",
                                     viewport_width or 1280, window_height, complete)
                self.assertEqual(results, [[]])
            finally:
                complete.set()
                server.shutdown()
                thread.join()

    @unittest.skipUnless(LIST_BROWSER, "Chrome or Firefox is required for browser checks")
    def test_browser_ask_saves_and_shows_the_newer_update_and_answer(self) -> None:
        item = self.add("Check the current result")
        _ = self.add("Check the other unread result", owner=OTHER.id)
        root = self.root / "reviews"
        results: list[list[str]] = []
        complete = threading.Event()
        probe = """<!doctype html><meta charset="utf-8">
<style>iframe{width:100%;height:calc(100vh - 24px);border:0}</style>
<iframe></iframe><img hidden src="/complete"><script>
const frame=document.querySelector('iframe'), errors=[];
const check=(ok,message)=>{if(!ok) errors.push(message);};
const open=(show,key='',unread='')=>new Promise(resolve=>{
 frame.onload=resolve;
 frame.src='LIST_PATH?fixture='+show+unread+'#'+new URLSearchParams({show,unread,...(key ? {item:key} : {})});
});
const row=()=>frame.contentDocument.querySelector('[data-number="1"]');
const hasDot=()=>Boolean(row().querySelector('.list-dot'));
const act=async (action,words='')=>{
 const win=frame.contentWindow, current=row();
 const form=current.querySelector('form'), input=form.elements.words;
 const fetch=win.fetch.bind(win);
 let post, observer;
 const settled=new Promise(resolve=>{
  observer=new win.MutationObserver(()=>{if(!form.dataset.sending) resolve();});
  observer.observe(form,{attributeFilter:['data-sending']});
 });
 win.fetch=(url,options)=>options?.method==='POST'
  ? (post=fetch(url,options))
  : Promise.resolve(new win.Response('',{status:503}));
 try{
  if(current.querySelector('[data-detail]').hidden) current.querySelector('.list-toggle').click();
  input.value=words;input.dispatchEvent(new win.Event('input'));
  form.querySelector('[value="'+action+'"]').click();
  if(!post || !(await post).ok) throw new Error('The List POST must succeed.');
  await settled;
  return form.querySelector('[data-send-status]').textContent;
 }finally{observer.disconnect();win.fetch=fetch;}
};
const change=async path=>{
 const response=await fetch(path,{method:'POST'});
 if(!response.ok) throw new Error('Fixture update failed: '+path);
 return response.json();
};
(async()=>{
 try{
  await open('all','','1');
  check(!row().hidden && hasDot(),'the item starts in Unread');
  check(await act('question','Which check passed?')==='Question saved','the first question saves');
  check(row().hidden && !hasDot(),'the saved question leaves Unread and clears the dot');
  check(row().querySelector('[data-waiting-questions] time[datetime]'),
   'the save response supplies the asked time for the waiting row');
  const key=row().dataset.key;
  await open('working');
  check(row().hidden && !row().querySelector('[data-open-question-note]').hidden,
   'The saved question has its note and stays out of Working.');
  await open('all',key);
  check(!row().hidden && !row().querySelector('[data-detail]').hidden &&
   row().querySelector('[data-waiting-note]')?.textContent==='The producer will answer.' &&
   !row().querySelector('[value="question"]'),'a direct link shows the waiting line without Ask');
  const waitingQuestion=row().querySelector('[data-waiting-questions] [data-question="Q1"]');
  check(/(?:EST|EDT)$/.test(waitingQuestion?.querySelector('time[datetime]')?.textContent),
   'the waiting question shows the asked time in the user zone');
  await change('/answer');
  await open('needs-me','','1');
  const unreadQuestion=row().querySelector('.list-panel [data-question="Q1"]');
  check(!row().hidden && hasDot() && unreadQuestion?.textContent.includes('Which check passed?') &&
   unreadQuestion.textContent.includes('The gate passed.') && !row().querySelector('[data-open-question-note]'),
   'Needs me with Unread only shows the answer in the panel and clears the question note.');
  check(unreadQuestion?.querySelectorAll('time[datetime]').length===2 &&
   unreadQuestion.querySelector('.question-unread')?.textContent==='Unread',
   'the unread answer shows both times and the Unread mark');
  check(!row().querySelector('.list-log').textContent.includes('The gate passed.'),
   'the log has no unread answer');
  check(await act('read')==='Marked read','Mark read saves the existing read action');
  await open('working',key);
  const log=row().querySelector('.list-log');
  check(!hasDot() && !row().querySelector('[data-question="Q1"]'),
   'the read question leaves the panel');
  check(log.querySelector('[data-log-index="Q1-answered"]').textContent.includes('Answer — morty') &&
   log.querySelector('[data-log-index="Q1-asked"]').textContent.includes('Question Q1 — nate'),
   'the log shows both parts of the read question with their authors');
  await change('/update');
  await open('working','','1');
  await change('/newer');
  check(await act('question','Which check is next?')==='Question saved; update remains unread',
   'the notice says the newer update remains unread');
  check(hasDot() && row().hidden,'The unread item leaves Working after the question saves.');
  frame.contentDocument.getElementById('filter-show-morty-answering').click();
  check(hasDot() && !row().hidden,'Morty answering shows the unread item.');
  const nextQuestion=(current)=>current.querySelector('[data-waiting-questions] [data-question="Q2"]');
  const firstAnswer=(current)=>current.querySelector('[data-log-index="Q1-answered"]');
  check(nextQuestion(row()) && firstAnswer(row())?.textContent.includes('The gate passed.'),
   'the Ask response keeps the first answer in the log beside the new waiting question');
  await open('all',key);
  check(!row().querySelector('[data-detail]').hidden && nextQuestion(row()) && firstAnswer(row()),
   'a direct link shows both the waiting question and the first answer in the log');
  const {loggedAt}=await change('/second-answer');
  await open('all','','1');
  const original=row(), doc=frame.contentDocument;
  const panelQuestion=original.querySelector('[data-question="Q2"]');
  const askedAt=Date.parse(panelQuestion.querySelector('time').dateTime);
  check(hasDot() && original.querySelector('[data-question="Q2"] .question-unread'),
   'the second answer stays unread before the row opens');
  const refreshed=new Promise(resolve=>{
   const observer=new frame.contentWindow.MutationObserver(()=>{
    if(row()!==original && row().querySelector('[data-log-index="Q2-answered"]')){
     observer.disconnect();resolve();
    }
   });
   observer.observe(doc.body,{childList:true,subtree:true});
  });
  original.querySelector('.list-toggle').click();
  await refreshed;
  check(!hasDot() && !row().hidden && !row().querySelector('[data-detail]').hidden,
   'opening the row marks the answer read and keeps the row open in Unread');
  check(row().querySelector('[data-log-index="Q2-answered"]').textContent.includes('The next check passed.'),
   'opening the row moves the answer to the log');
  const asked=row().querySelector('[data-log-index="Q2-asked"]');
  const answered=row().querySelector('[data-log-index="Q2-answered"]');
  check(asked?.textContent.includes('Which check is next?') && Date.parse(asked.querySelector('time').dateTime)===askedAt &&
   Date.parse(answered.querySelector('time').dateTime)===Date.parse(loggedAt) && !row().querySelector('[data-question="Q2"]'),
   'The log shows the question and answer with their recorded times after the panel clears.');
  const chip=(filter,value)=>doc.querySelector('[data-filter-row="'+filter+'"] [data-filter-value="'+value+'"]');
  const count=(filter,value)=>Number(chip(filter,value).dataset.filterCount);
  const shown=()=>doc.querySelector('table').querySelectorAll('[data-list-item]:not([hidden])').length;
  check(shown()===2 && count('owner','all')===2 && count('producer','all')===2 && count('show','all')===2,
   'The Owner, Producer, and Show All counts include the row kept open after the read.');
  check(doc.querySelector('[data-list-count]').textContent.startsWith('2 of 2 items shown'),
   'The total includes the row kept open after the read.');
  check(count('show','working')===1,'The Working count excludes the read row when Show changes.');
  chip('owner','staff:'+doc.querySelector('[data-number="2"]').dataset.owner).click();
  const allOwners=count('owner','all');
  chip('owner','all').click();
  check(allOwners===2 && allOwners===shown(),'The Owner All count matches the rows after its click.');
  row().querySelector('.list-toggle').click();
  check(row().hidden && shown()===1 && count('owner','all')===1,
   'Closing the read row removes it from Unread only and the Owner All count.');
 }catch(error){errors.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(errors)});
})();
</script>""".replace('LIST_PATH', list_page.PAGE_PATH).encode()

        class Probe(serve.ReviewLinksHandler):
            @override
            def send_head(self) -> BinaryIO | None:
                if self.path == '/proof':
                    self.html_response(200, probe)
                elif self.path == '/complete':
                    _ = complete.wait(TEST_HANG_GUARD_SECS)
                    self.json_response(200, {})
                else:
                    return super().send_head()
                return None

            @override
            def do_POST(self) -> None:
                if self.path in ('/update', '/newer'):
                    _ = the_list.update(MORTY.id, item.id, {"latest_status": f"New result from {self.path}."}, source="morty")
                    self.json_response(200, {})
                elif self.path in ('/answer', '/second-answer'):
                    question_id, words = (("Q1", "The gate passed.") if self.path == '/answer'
                                          else ("Q2", "The next check passed."))
                    result = the_list.main(["answer", item.id, "--question", question_id, "--text", words])
                    if result:
                        self.json_response(500, {})
                        return
                    logged_at = next(entry.time for entry in the_list.load(MORTY.id, item.id).log
                                     if entry.what == the_list.QuestionAnswered(question_id))
                    self.json_response(200, {"loggedAt": logged_at.isoformat()})
                elif self.path == '/results':
                    results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                    self.json_response(200, {})
                    complete.set()
                else:
                    super().do_POST()

        with (patch.object(serve, "ReviewLinksHandler", Probe),
              patch.object(review_page.ReviewSnapshotCache, "start"),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(list_page, "counts_strip", return_value=""),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session),
              patch.object(change_page, "send_question", return_value=question_outcome()),
              serve.ReviewLinksServer(("127.0.0.1", 0), root) as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                self.browser_results(f'http://127.0.0.1:{server.server_port}/proof', 1440, 900, complete)
                self.assertEqual(results, [[]])
            finally:
                complete.set()
                server.shutdown()
                thread.join()

    def browser_action_sequence(self, steps: str, *, reduced_motion: bool = False, add_item: bool = True,
                                window_size: tuple[int, int] | None = None, in_frame: bool = False,
                                markup: str | None = None, open_number: int = 1) -> None:
        if add_item:
            _ = self.add("Test List action retries", number=1)
        if markup is None:
            with patch.object(list_page, "counts_strip", return_value=""):
                markup = list_page.render(self.root / "reviews").decode()
        markup = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', markup, count=1)
        setup = '''<script>
const requests=[],itemSnapshots=new Map();let finishPost,losePost,refreshMarkup,readback;
window.fetch=async(path,options={})=>{
 if(options.method!=='POST'){
  const query=new URL(path,location.href).searchParams;
  if(query.has('item-state')){
   const key=query.get('keeper')+'/'+query.get('item-state');
   return {ok:true,json:async()=>readback ? readback(key,query.get('park-request')) : {
    ...itemSnapshots.get(key),park_request_saved:false}};
  }
  if(refreshMarkup===undefined) throw new Error('Fixture refresh refused');
  return {ok:true,text:async()=>refreshMarkup};
 }
 requests.push(new URLSearchParams(options.body));
 const form=submittedForm;
 return new Promise((resolve,reject)=>{
  losePost=()=>respondToSave(form,()=>reject(new Error('Fixture lost response')));
  finishPost=result=>respondToSave(form,()=>resolve({
   ok:!result.reason,status:result.status ?? (result.reason ? 400 : 200),json:async()=>({unread:'true',...result})
  }));
 });
};
</script>'''
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,name)=>{if(!ok) failures.push(name);};
 const row=document.querySelector('[data-number="OPEN_NUMBER"]'),form=row.querySelector('form');
 const submit=(action,target=form)=>{
  const count=requests.length;
  target.requestSubmit(target.querySelector('[value="'+action+'"]'));
  if(requests.length!==count+1) throw new Error('The test submission did not start.');
  const request=requests.at(-1);
  return {id:request.get('request'),token:request.get('token')};
 };
 const show=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]').click();
 const onlyUnread=(on=true)=>{const control=document.getElementById('list-unread');if(control.checked!==on) control.click();};
 const navigate=params=>new Promise(resolve=>{
  addEventListener('hashchange',resolve,{once:true});
  location.hash=new URLSearchParams(params);
 });
 const finishRowMotion=()=>finishAnimations(
  [...document.querySelectorAll('[data-record-motion]')].flatMap(motion=>motion.getAnimations({subtree:true})));
 for(const item of document.querySelectorAll('[data-list-item]')) itemSnapshots.set(item.dataset.key,{
  item_state:item.dataset.state,parked:item.dataset.parked,unread:item.dataset.unread,
  unread_sort:JSON.parse(item.dataset.sortValues)[7],open_question:item.dataset.openQuestion,
  waiting_for_answer:item.dataset.waitingForAnswer,
 });
 try{
  document.querySelector('OPEN_CONTROL').click();
''' + steps + '''
 }catch(error){failures.push(String(error));}
 const result=parent.document.createElement('p');result.id='probe-results';
 result.textContent=JSON.stringify(failures);parent.document.body.append(result);
})();
</script>'''
        open_control = f'[data-number="{open_number}"] .list-toggle'
        probe = probe.replace('OPEN_CONTROL', open_control).replace('OPEN_NUMBER', str(open_number))
        self.browser_actions(markup, setup, probe, reduced_motion=reduced_motion,
                             window_size=window_size, in_frame=in_frame)

    def browser_actions(self, markup: str, setup: str, probe: str, *,
                        reduced_motion: bool = False,
                        window_size: tuple[int, int] | None = None, in_frame: bool = False) -> None:
        page = self.root / 'actions.html'
        _ = page.write_text(markup.replace('<head>', '<head>' + WATCH_ACTIONS + setup, 1).replace('</body>', probe + '</body>'))
        if in_frame:
            page = self.root / 'actions-console.html'
            _ = page.write_text('<!doctype html><button id="console-nav">Console navigation</button>'
                                + '<iframe title="The List" src="actions.html"></iframe>')
        result = self.chrome().probe(page, "probe-results", reduced_motion=reduced_motion, window_size=window_size)
        self.assertTrue(result, "The browser must complete the item action checks.")
        failures = cast(list[str], json.loads(result))
        self.assertEqual(failures, [])

    def test_delivery_notices_color_staff_names_and_keep_the_recorded_words(self) -> None:
        _ = self.add("Delivery notice")
        cases: tuple[tuple[the_list.PageFeedback, str, tuple[str, ...]], ...] = (
            (the_list.PageFeedback("request", "Words"), "sent to morty", ("morty",)),
            (the_list.PageFeedback("request", "Words", "sent"), "sent to morty", ("morty",)),
            (the_list.PageFeedback("request", "Words", "alerted", "sdlc"), "not sent; sdlc told", ("sdlc",)),
            (the_list.PageFeedback("request", "Words", "alerted", "<unknown>"), "not sent; <unknown> told", ()),
            (the_list.PageFeedback("request", "Words", "alerted"), "not sent; no showrunner for alert", ()),
        )
        for feedback, text, colored in cases:
            with (self.subTest(state=feedback.state, recipient=feedback.alerted_to),
                  patch.object(the_list, "sent_to_morty", return_value=NOW),
                  patch.object(the_list, "feedback_requests", return_value={feedback.request: feedback})):
                body = list_page.render(self.root / "reviews").decode()
                cell = body.split('<td data-label="Update">', 1)[1].split('</td>', 1)[0]
                notice = ElementTree.fromstring('<root>' + cell + '</root>').find('small')
                assert notice is not None
                self.assertEqual(''.join(notice.itertext()), text + ", 13:00 EST")
                self.assertEqual(tuple(span.text for span in notice.findall('span')), colored)

    def test_signed_feedback_logs_words_and_uses_the_shared_sender(self) -> None:
        root = self.root / "reviews"
        item = self.add("The feedback row", number=106)
        initial = item
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(self.fields(item, "read")))
            list_page.deliver(delivery)
            send.assert_not_called()
            item = the_list.load(MORTY.id, item.id)
            self.assertIs(item.unread_since, the_list.NoTimestamp.UNSET)
            self.assertEqual(item.log[-1].what, the_list.MarkedRead())
            for action, extra, words in (
                    ("park", {"until": "2026-12-03"}, "List page: nate parked item 106 until 2026-12-03."),
                    ("tell", {"words": "Explain this simply.\nKeep these words."}, "Explain this simply.\nKeep these words."),
                    ("remove", {}, "List page: nate removed item 106.")):
                with self.subTest(action=action):
                    fields = self.fields(item, action, **extra)
                    delivery = list_page.submit(root, urlencode(fields))
                    item = the_list.load(MORTY.id, item.id)
                    feedback = the_list.feedback_requests(item)[fields["request"]]
                    self.assertEqual(feedback.words, words)
                    if action == "tell":
                        self.assertEqual(item.log[-1].what, the_list.MarkedRead())
                    self.assertEqual(item.log[-2 if action == "tell" else -1].what, feedback)
                    self.assertEqual(the_list.sent_to_morty(item), NOW)
                    body = list_page.render(root).decode()
                    if action == "remove":
                        self.assertNotIn(f'data-key="{MORTY.id}/{item.id}"', body)
                    else:
                        self.assertIn("sent to morty, 13:00 EST", without_role_colors(body))
                        self.assertIn('data-feedback-form', body)
                        log = body.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]
                        self.assertIn("Feedback for morty", log)
                        self.assertIn(html.escape(words.splitlines()[0]), log)
                        self.assertNotIn(fields["request"], log)
                    list_page.deliver(delivery)
                    self.assertEqual(send.call_args.args[0], MORTY.id)
                    expected = "List page: nate's words for item 106\n" + words if action == "tell" else words
                    self.assertEqual(send.call_args.args[2], expected)
                    item = the_list.load(MORTY.id, item.id)
                    before = send.call_count, item.log
                    list_page.deliver(list_page.submit(root, urlencode(fields)))
                    self.assertEqual((send.call_count, the_list.load(MORTY.id, item.id).log), before)
            self.assertEqual(item.state, the_list.ItemState.CLOSED)
            self.assertEqual(item.parked_until, the_list.time_value("2026-12-03"))
            self.assertEqual((item.title, item.owner, item.next_step), (initial.title, initial.owner, initial.next_step))
            _ = the_list.reopen(MORTY.id, item.id, source="morty")
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "Morty acts."}, source="morty")
            self.assertNotIn("<small>sent to morty,", without_role_colors(list_page.render(root).decode()))
            good = self.fields(item, "tell", words="Keep it.")
            for body in (urlencode({**good, "token": "bad"}), urlencode(good) + '&id=L2',
                         urlencode({**good, "words": ""}), urlencode({**good, "words": " \t\n "}),
                         urlencode({**good, "action": "unknown"}),
                         urlencode({**good, "action": "park", "until": "2026-02-30"})):
                with self.subTest(body=body), self.assertRaises(ValueError):
                    _ = list_page.submit(root, body)

    def test_tell_marks_an_unread_update_read_once_and_saves_feedback(self) -> None:
        item = self.add("Read the update before sending feedback")
        fields = self.fields(item, "tell", words="Keep this result.")
        with patch.object(the_list, "mark_read", wraps=the_list.mark_read) as mark:
            for _ in range(2):
                _ = list_page.submit(self.root / "reviews", urlencode(fields))
        mark.assert_called_once_with(MORTY.id, item.id, source=the_list.PAGE_SOURCE, update=the_list.unread_update(item))
        saved = the_list.load(MORTY.id, item.id)
        self.assertIs(saved.unread_since, the_list.NoTimestamp.UNSET)
        self.assertEqual([(entry.what, entry.source) for entry in saved.log if isinstance(entry.what, the_list.MarkedRead)],
                         [(the_list.MarkedRead(), the_list.PAGE_SOURCE)])
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")

    def test_tell_on_a_read_item_adds_no_read_log_entry(self) -> None:
        item = self.add("An update already read")
        item = the_list.mark_read(MORTY.id, item.id, source="nate")
        before = tuple(entry for entry in item.log if isinstance(entry.what, the_list.MarkedRead))
        fields = self.fields(item, "tell", words="Keep this result.")
        with patch.object(the_list, "mark_read", wraps=the_list.mark_read) as mark:
            _ = list_page.submit(self.root / "reviews", urlencode(fields))
        mark.assert_not_called()
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(tuple(entry for entry in saved.log if isinstance(entry.what, the_list.MarkedRead)), before)
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")

    def test_refused_tell_keeps_the_unread_update_and_log(self) -> None:
        item = self.add("Keep the unread update after a refusal")
        for words in ("", " \t\n "):
            with self.subTest(words=words), self.assertRaises(question_record.AnswerRefused):
                _ = list_page.submit(self.root / "reviews", urlencode(self.fields(item, "tell", words=words)))
            self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_tell_storage_failure_keeps_the_unread_update_and_log(self) -> None:
        item = self.add("Keep the update when feedback storage fails")
        fields = self.fields(item, "tell", words="Keep this result.")
        with patch.object(the_list, "save_feedback", side_effect=OSError("Fixture storage failure")):
            with self.assertRaises(OSError):
                _ = list_page.submit(self.root / "reviews", urlencode(fields))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    def test_tell_read_failure_keeps_feedback_and_reports_the_unread_update(self) -> None:
        item = self.add("Keep feedback when the read write fails")
        root = self.root / "reviews"
        fields = self.fields(item, "tell", words="Keep this result.")
        with patch.object(the_list, "mark_read", side_effect=OSError("Fixture read failure")):
            delivery = list_page.submit(root, urlencode(fields))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.unread_since, item.unread_since)
        self.assertFalse(any(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log))
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id)["unread"], "true")
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(delivery)
        send.assert_called_once()

    def test_repeated_tell_keeps_a_newer_update_unread(self) -> None:
        item = self.add("Read only the original update")
        fields = self.fields(item, "tell", words="Keep this result.")
        root = self.root / "reviews"
        _ = list_page.submit(root, urlencode(fields))
        newer = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        _ = list_page.submit(root, urlencode(fields))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved, newer)
        self.assertEqual(len(the_list.feedback_requests(saved)), 1)
        self.assertEqual(sum(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log), 1)

    def test_legacy_tell_retry_uses_only_its_saved_read_target(self) -> None:
        root = self.root / "reviews"
        for case in ("saved", "newer", "first_send"):
            with self.subTest(case=case):
                item = self.add("Keep the legacy read target")
                request = uuid4().hex
                words = "Keep this result."
                token = list_page.request_token(root, MORTY.id, item.id, request)
                body = urlencode(self.fields(item, "tell", request=request, token=token, words=words))
                if case != "first_send":
                    feedback = the_list.PageFeedback(request, words, read_update=the_list.unread_update(item))
                    _ = the_list.save_feedback(MORTY.id, item.id, feedback)
                if case == "newer":
                    item = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
                _ = list_page.submit(root, body)
                saved = the_list.load(MORTY.id, item.id)
                expected = the_list.NoTimestamp.UNSET if case == "saved" else item.unread_since
                self.assertEqual(saved.unread_since, expected)
                self.assertEqual(len(the_list.feedback_requests(saved)), 1)
                self.assertEqual(sum(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log), int(case == "saved"))

    def test_conflicting_tell_keeps_a_newer_update_unread(self) -> None:
        item = self.add("Refuse new words under the saved request")
        fields = self.fields(item, "tell", words="Keep this result.")
        root = self.root / "reviews"
        _ = list_page.submit(root, urlencode(fields))
        newer = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        with self.assertRaises(the_list.ListError):
            _ = list_page.submit(root, urlencode({**fields, "words": "Different words."}))
        self.assertEqual(the_list.load(MORTY.id, item.id), newer)

    def test_tell_keeps_an_update_that_arrives_after_feedback_storage_unread(self) -> None:
        item = self.add("Keep the update that arrives during Tell")
        fields = self.fields(item, "tell", words="Keep this result.")
        save_feedback = the_list.save_feedback

        def save(keeper: staff.StaffId, identity: str, feedback: the_list.PageFeedback) -> the_list.PageFeedback:
            saved = save_feedback(keeper, identity, feedback)
            _ = the_list.update(keeper, identity, {"latest_status": "A newer result arrived."}, source="morty")
            return saved

        with patch.object(the_list, "save_feedback", side_effect=save):
            _ = list_page.submit(self.root / "reviews", urlencode(fields))
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.unread_since, item.unread_since)
        self.assertEqual(saved.latest_status, "A newer result arrived.")
        self.assertFalse(any(isinstance(entry.what, the_list.MarkedRead) for entry in saved.log))
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].words, "Keep this result.")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_read_and_tell_clear_the_unread_controls_without_a_refresh(self) -> None:
        _ = self.add("Check the update")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 return {ok:true,json:async()=>({unread:'UNREAD'})};
};
</script>'''
        probe = '''<script>
(async()=>{
 const row=document.querySelector('[data-list-item]'),form=row.querySelector('form');
 document.getElementById('list-unread').click();
 const before=row.dataset.unread==='true' && row.querySelector('.list-dot') && form.querySelector('[value="read"]') && !row.hidden;
 form.elements.words.value='Keep this result.';form.elements.words.dispatchEvent(new Event('input'));
 form.requestSubmit(form.querySelector('[value="ACTION"]'));
 await waitForSave(form);
 const cleared=row.dataset.unread==='false' && !row.querySelector('.list-dot') && !form.querySelector('[value="read"]') && row.hidden;
 const retained=row.dataset.unread==='true' && row.querySelector('.list-dot') && form.querySelector('[value="read"]') && !row.hidden;
 const after=EXPECTED_UNREAD ? retained : cleared;
 const notice=form.querySelector('[data-send-status]').textContent;
 const result=document.createElement('p');result.id='probe-result';
 result.textContent=before && after && notice==='NOTICE' ? 'PASS' : 'FAIL';document.body.append(result);
})();
</script>'''
        for action, unread, notice in (("read", "false", "Marked read"), ("read", "true", "Update remains unread"),
                                      ("tell", "false", "sent to morty"),
                                      ("tell", "true", "sent to morty; update remains unread")):
            with self.subTest(action=action, unread=unread):
                page = self.root / f"{action}-{unread}.html"
                _ = page.write_text(markup.replace('<head>', '<head>' + WATCH_ACTIONS + setup.replace('UNREAD', unread), 1).replace('</body>',
                    probe.replace('ACTION', action).replace('NOTICE', notice).replace('EXPECTED_UNREAD', unread) + '</body>'))
                result = self.chrome().probe(page, "probe-result")
                self.assertEqual(result, "PASS",
                                 f"The {action} action must show the saved unread state and apply the Unread filter.")

    def test_feedback_recovers_after_restart_and_alerts_after_failed_tries(self) -> None:
        root = self.root / "reviews"
        item = self.add("Recover the message")
        _ = list_page.submit(root, urlencode(self.fields(item, "tell", words="Ask sdlc for an ETA.")))
        saved = the_list.load(MORTY.id, item.id)
        with (patch.object(change_page, "send_to_staff") as send,
              patch.object(change_review, "lists_directory", return_value=self.root / "lists")):
            list_page.recover_pending()
            send.assert_called_once()
            self.assertIn("Ask sdlc for an ETA.", cast(str, send.call_args.args[2]))
            list_page.recover_pending()
            send.assert_called_once()
        self.assertEqual(the_list.feedback_requests(the_list.load(MORTY.id, item.id)),
                         {key: replace(value, state="sent") for key, value in the_list.feedback_requests(saved).items()})
        delivery = list_page.submit(root, urlencode(self.fields(item, "remove")))
        def fail_message(recipient: staff.StaffId, _sender: str, _message: str, *, key: str) -> None:
            self.assertTrue(key.startswith("review:"))
            if recipient == MORTY.id:
                raise RuntimeError("fixture offline")
        with (patch.object(change_page, "send_to_staff", side_effect=fail_message) as send,
              patch.object(time, "sleep"), redirect_stderr(io.StringIO())):
            list_page.deliver(delivery)
            tries = conf.read("review_links").whole("delivery_tries")
            self.assertEqual(send.call_count, tries + 1)
            self.assertIn("List feedback needs attention", cast(str, send.call_args.args[2]))
            self.assertIn("nate removed item 1", cast(str, send.call_args.args[2]))
            self.assertEqual(send.call_args.args[0], OWNER.id)
            list_page.recover_pending()
            list_page.deliver(delivery)
            self.assertEqual(send.call_count, tries + 1)
            alerted = the_list.load(MORTY.id, item.id)
            latest = tuple(the_list.feedback_requests(alerted).values())[-1]
            self.assertEqual((latest.state, latest.alerted_to), ("alerted", "sdlc"))
            self.assertNotIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
            self.assertEqual(the_list.mark_read(MORTY.id, item.id, source="morty"), alerted)
            self.assertNotIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
        self.assertEqual(the_list.load(MORTY.id, item.id).state, the_list.ItemState.CLOSED)

    def test_failed_alert_keeps_feedback_pending_for_post_and_startup_retries(self) -> None:
        root = self.root / "reviews"
        item = self.add("Retry when the alert fails")
        fields = self.fields(item, "tell", words="Keep trying until someone receives this.")
        delivery = list_page.submit(root, urlencode(fields))
        pending = the_list.load(MORTY.id, item.id)
        with (patch.object(change_page, "send_to_staff", side_effect=RuntimeError("all sends fail")) as send,
              patch.object(time, "sleep"), redirect_stderr(io.StringIO())):
            list_page.deliver(delivery)
            tries = conf.read("review_links").whole("delivery_tries")
            self.assertEqual(send.call_count, tries + 1)
            self.assertEqual(the_list.feedback_requests(the_list.load(MORTY.id, item.id))[fields["request"]].state, "pending")
            self.assertNotIn("not sent; sdlc told", without_role_colors(list_page.render(root).decode()))
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(send.call_count, 2 * (tries + 1))
            list_page.recover_pending()
            self.assertEqual(send.call_count, 3 * (tries + 1))
            self.assertEqual(the_list.load(MORTY.id, item.id), pending)
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
            list_page.recover_pending()
            send.assert_called_once()
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].state, "sent")

    def test_feedback_save_failures_leave_recoverable_requests_only(self) -> None:
        root = self.root / "reviews"
        item = self.add("Save before sending")
        for published in (False, True):
            with self.subTest(published=published):
                fields = self.fields(item, "tell", words="Keep these words.")
                original = the_list.save_feedback
                def failed_save(keeper: staff.StaffId, identity: str, feedback: the_list.PageFeedback) -> the_list.PageFeedback:
                    if published:
                        _ = original(keeper, identity, feedback)
                    raise OSError("fixture save failed")
                with patch.object(the_list, "save_feedback", side_effect=failed_save):
                    with self.assertRaisesRegex(OSError, "fixture save failed"):
                        _ = list_page.submit(root, urlencode(fields))
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(isinstance(the_list.sent_to_morty(saved), datetime), published)
                self.assertEqual(fields["request"] in the_list.feedback_requests(saved), published)
                with patch.object(change_page, "send_to_staff") as send:
                    if published:
                        list_page.deliver(list_page.submit(root, urlencode(fields)))
                        send.assert_called_once()
                    list_page.recover_pending()
                    list_page.recover_pending()
                    self.assertEqual(send.call_count, int(published))
                    if published:
                        list_page.deliver(list_page.submit(root, urlencode(fields)))
                        self.assertEqual(send.call_count, 1)
                if published:
                    item = the_list.update(MORTY.id, item.id, {"latest_status": "Recorded"}, source="morty")

    def test_feedback_claim_releases_the_list_lock_during_delivery(self) -> None:
        root = self.root / "reviews"
        item = self.add("Send without blocking edits")
        other = self.add("Mark this item read")
        delivery = list_page.submit(root, urlencode(self.fields(item, "tell", words="Keep these words.")))
        entered, release = threading.Event(), threading.Event()
        def send(_recipient: staff.StaffId, _sender: str, _message: str, *, key: str) -> None:
            self.assertTrue(key.startswith("review:"))
            entered.set()
            if not release.wait(TEST_HANG_GUARD_SECS):
                raise RuntimeError("the test did not release delivery")
        def edit_items() -> None:
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "Morty edited during delivery"}, source="morty")
            _ = the_list.mark_read(MORTY.id, other.id, source=the_list.PAGE_SOURCE)
        with patch.object(change_page, "send_to_staff", side_effect=send) as sends, ThreadPoolExecutor(max_workers=2) as pool:
            sending = pool.submit(list_page.deliver, delivery)
            try:
                self.assertTrue(entered.wait(TEST_HANG_GUARD_SECS))
                list_page.deliver(delivery)
                edited = pool.submit(edit_items)
                edited.result(TEST_HANG_GUARD_SECS)
                self.assertIs(the_list.load(MORTY.id, other.id).unread_since, the_list.NoTimestamp.UNSET)
                self.assertEqual(sends.call_count, 1)
            finally:
                release.set()
            sending.result(TEST_HANG_GUARD_SECS)
        current = the_list.load(MORTY.id, item.id)
        self.assertEqual(current.latest_status, "Morty edited during delivery")
        self.assertEqual(tuple(the_list.feedback_requests(current).values())[-1].state, "sent")
        self.assertIs(the_list.sent_to_morty(current), the_list.NoTimestamp.UNSET)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the shared status layout check")
    def test_shared_page_status_uses_the_real_list_loader(self) -> None:
        _ = self.fixture_items()

        def screenshot(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            path = Path(arguments[arguments.index("--screenshot") + 1])
            width, height = map(int, arguments[arguments.index("--window-size") + 1].split(","))
            with self.browser_page(arguments[-1], width, height, firefox_only=True) as browser:
                browser.screenshot(path)
            return subprocess.CompletedProcess(arguments, 0, "", "")

        with patch("scripts.review_links.test_page_frame.subprocess", SimpleNamespace(run=screenshot)):
            assert_status_states(self, list_page.render(self.root / "reviews").decode(), 15)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for the viewport layout check")
    def test_browser_sort_filters_hash_refresh_open_rows_and_phone_cards(self) -> None:
        _ = self.fixture_items()
        self.browser()

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_staff_chip_rows_wrap_and_fit(self) -> None:
        _ = self.add("Item for cargo liner", owner=staff.StaffId("cargo-liner-style-modules"), number=1)
        _ = self.add("Item for a long owner",
                     owner=staff.StaffId("cargo-liner-style-modules-review-links-and-delivery"), number=2)
        _ = self.add("Item for an owner without hyphens", owner=staff.StaffId("longownername" * 6), number=3)
        for number in range(4, 13):
            _ = self.add(f"Item {number}", owner=staff.StaffId(f"delivery-team-{number:02d}"), number=number)
        all_staff = staff.StaffId("all")
        item = self.add("Item for all", owner=all_staff, number=13)
        _ = the_list.update(MORTY.id, item.id, {"producer": all_staff}, source="morty")
        _ = self.add("Item for builds", owner=OTHER.id, number=14)
        _ = self.add("Item for builds support", owner=staff.StaffId("builds-support"), number=15)
        spacetime = staff.StaffId("10e35465-1777-435a-8bbf-5709e6ab0491")
        _ = self.add("Item for spacetime", owner=spacetime, number=16)
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            member.to_json() for member in (MORTY, DWIGHT, OWNER, replace(OTHER, name="builds-unit"),
                replace(OTHER, id=spacetime, name="spacetimebuilds", current_session=spacetime))]}))
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        probe = """<script>
(async()=>{
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
try{
const rows=[...document.querySelectorAll('[data-record-filters] [data-filter-row]')];
const left=row=>row.querySelector('button').getBoundingClientRect().left;
for(const row of rows){
 const bounds=row.getBoundingClientRect();
 for(const chip of row.querySelectorAll('button')){
  const box=chip.getBoundingClientRect();
  check(box.left>=bounds.left && box.right<=bounds.right+1,'The chip fits inside its row.');
 }
}
const ownerChips=[...rows[0].querySelectorAll('button')];
check(new Set(ownerChips.map(button=>button.getBoundingClientRect().top)).size>1,
 'The owner chips wrap onto more than one line.');
check(rows.every(row=>Math.abs(left(row)-left(rows[0]))<1),'The three rows align.');
check(document.documentElement.scrollWidth<=innerWidth,'The filter rows fit without horizontal scrolling.');
const ownerName=number=>document.querySelector('[data-number="'+number+'"] .list-owner-name');
const fits=name=>name.scrollWidth<=name.clientWidth;
const spacetime=ownerName(16),name=document.createRange();
name.selectNodeContents(document.createTreeWalker(spacetime,NodeFilter.SHOW_TEXT).nextNode());
check(spacetime.textContent==='spacetimebuilds' && fits(spacetime) && name.getClientRects().length===1,
 'The owner name without hyphens fits on one line.');
const parts=name=>[...name.querySelectorAll(':scope > span')];
const lineCount=name=>new Set(parts(name).map(part=>part.getBoundingClientRect().top)).size;
const cargo=ownerName(1),long=ownerName(2);
check(cargo.textContent==='cargo-liner-style-modules' && fits(cargo) && lineCount(cargo)===1,
 'The full cargo-liner-style-modules name fits on one line.');
const longParts=parts(long),segment=document.createRange();
check(fits(long) && lineCount(long)>1 &&
 longParts.slice(0,-1).every(part=>part.textContent.endsWith('-')) &&
 longParts.every(part=>{
  segment.selectNodeContents(document.createTreeWalker(part,NodeFilter.SHOW_TEXT).nextNode());
  return segment.getClientRects().length===1;
 }),
 'The long owner name wraps at hyphens and each segment stays on one line.');
const unbroken=ownerName(3);
segment.selectNodeContents(parts(unbroken)[0]);
check(fits(unbroken) && segment.getClientRects().length>1,
 'The oversized segment wraps within the available width.');
if(innerWidth>960){
 const width=label=>document.querySelector('[data-number="1"] td[data-label="'+label+'"]').getBoundingClientRect().width;
 check(Math.abs(width('Next step')-width('Item')*23/17)<1,
  'Item and Next step share the available width in their original proportion.');
}
const producerBox=rows[1].getBoundingClientRect(),words=document.getElementById('list-search').getBoundingClientRect();
check(innerWidth>620 ? words.top<producerBox.bottom && words.left>=producerBox.right :
 words.top>=producerBox.bottom,'The words box sits beside Producer on desktop and wraps below it on a phone.');
const allChips=rows.slice(0,2).map(row=>row.querySelector('[data-filter-value="all"]'));
const staffChips=rows.slice(0,2).map(row=>row.querySelector('[data-filter-value="staff:all"]'));
check(allChips.every(chip=>chip.textContent==='All16') && staffChips.every(chip=>chip?.textContent==='all1'),
 'Each staff row has distinct All and all chips with separate counts.');
check(ownerChips[2].textContent==='builds1' && ownerChips[3].textContent==='builds-support1',
 'The owner chips omit the suffix and sort by the displayed name.');
const matchesAllStaff=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')].map(row=>row.dataset.number).join()==='13';
staffChips.forEach(chip=>chip.click());
const selectedPlace=new URLSearchParams(location.hash.slice(1));
check(matchesAllStaff() && selectedPlace.get('owner')==='all' && selectedPlace.get('producer')==='all',
 'Selecting the staff ID all filters the items and stores the raw IDs.');
allChips.forEach(chip=>chip.click());
const restored=new Promise(done=>addEventListener('hashchange',done,{once:true}));
location.hash='show=all&owner=all&producer=all';
await restored;
check(matchesAllStaff() && staffChips.every(chip=>chip.getAttribute('aria-pressed')==='true'),
 'An existing address with the staff ID all selects both staff chips.');
}catch(error){failures.push(String(error));}
const result=document.createElement('p');result.id='probe-results';
result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>"""
        for width in (1700, 1280, 1024, 390):
            with self.subTest(width=width):
                self.browser_actions(markup, "", probe, window_size=(width, 900))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_staff_chips_strip_repeated_suffixes_and_keep_empty_name_ids(self) -> None:
        for number, identity in enumerate((OTHER.id, staff.StaffId("-unit")), start=1):
            item = self.add(f"Item {number}", owner=identity, number=number)
            _ = the_list.update(MORTY.id, item.id, {"producer": identity}, source="morty")
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            member.to_json() for member in (MORTY, OWNER, replace(OTHER, name="builds-unit-unit"))]}))
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        probe = """<script>
const failures=[];
for(const [number,label] of [['1','builds'],['2','-unit']]){
 const row=document.querySelector('[data-number="'+number+'"]'),id=row.dataset.owner;
 for(const name of ['owner','producer']){
  const chip=document.querySelector('[data-filter-row="'+name+'"] [data-filter-value="staff:'+id+'"]');
  if(chip.textContent!==label+'1') failures.push('The '+name+' chip shows '+label+' and its count.');
  chip.click();
 }
 const place=new URLSearchParams(location.hash.slice(1));
 if(row.hidden || place.get('owner')!==id || place.get('producer')!==id)
  failures.push('Both filters use the original staff ID '+id+'.');
 for(const name of ['owner','producer']) document.getElementById('filter-'+name+'-all').click();
}
const result=document.createElement('p');result.id='probe-results';
result.textContent=JSON.stringify(failures);document.body.append(result);
</script>"""
        self.browser_actions(markup, "", probe)

    def browser(self) -> None:
        setup = r'''<script>
const nativeFetch=fetch, errors=[], reads=[],posts=[];
let original=MARKUP,holdPost=false,finishPost,failRefresh=false,realFeedback=false,loseResponse=false;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.fetch=async(_path,options={})=>{
 if(options.method==='POST'){
  const fields=new URLSearchParams(options.body);posts.push(fields);
  if(realFeedback){
   const result=await nativeFetch('CASE_PATH/feedback',options);
   if(loseResponse){loseResponse=false;throw new Error('fixture lost response');}
   return result;
  }
  if(fields.get('action')==='read'){
   const page=new DOMParser().parseFromString(original,'text/html');
   const row=[...page.querySelectorAll('[data-list-item]')].find(row=>row.dataset.number==='106');
   row.dataset.unread='false';row.querySelector('.list-dot')?.remove();row.querySelector('[value="read"]')?.remove();
   original=page.documentElement.outerHTML;
  }
  const result={ok:true,status:200,json:async()=>({state:'sent',unread:'false',
   ...(fields.get('action')==='remove' ? {item_state:'Closed'} : {})})};
  if(holdPost) return await new Promise(resolve=>finishPost=(reply=result)=>resolve(reply));
  return result;
 }
 reads.push(1);if(failRefresh) throw new Error('fixture refresh failed');
 if(realFeedback) return await nativeFetch('CASE_PATH/fresh');
 return {ok:true,status:200,text:async()=>original};
};
addEventListener('error',event=>errors.push(event.message));
addEventListener('unhandledrejection',event=>errors.push(String(event.reason)));
</script>'''
        probe = r'''<script>
(async()=>{
const failed=[],check=(ok,text)=>{if(!ok) failed.push(text);};
const changed=(element,ready)=>new Promise(resolve=>{
 const observer=new MutationObserver(complete);
 function complete(){if(ready()){observer.disconnect();resolve();}}
 observer.observe(element,{attributes:true,childList:true,subtree:true});
 complete();
});
const navigate=async params=>{
 const completed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
 location.hash=new URLSearchParams(params);
 await completed;
};
const submitted=(form,button)=>new Promise((resolve,reject)=>{
 if(form.dataset.sending){reject(new Error('The test submission did not start.'));return;}
 const observer=new MutationObserver(()=>{
  if(!form.dataset.sending){observer.disconnect();resolve();}
 });
 observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
 form.requestSubmit(button);
 if(!form.dataset.sending){observer.disconnect();reject(new Error('The test submission did not start.'));}
});
const rows=()=>[...document.querySelectorAll('[data-list-item]')];
const visible=()=>rows().filter(row=>!row.hidden);
const shownNumbers=()=>visible().map(row=>row.dataset.number).join(',');
const refreshOrder=async label=>{
 const first=visible()[0],order=shownNumbers();
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await changed(document.body,()=>!first.isConnected);
 check(shownNumbers()===order,label);
};
const filterRow=name=>document.querySelector('[data-filter-row="'+name+'"]');
const staffChip=(name,value)=>filterRow(name).querySelector('[data-filter-value="'+
 (name==='show' || value==='all' ? value : 'staff:'+value)+'"]');
const chip=value=>staffChip('show',value);
const show=value=>chip(value).click();
const pressed=name=>filterRow(name).querySelector('[aria-pressed="true"]');
const selectedFilter=name=>pressed(name).dataset.filterValue;
const selectedShow=()=>selectedFilter('show');
const select=(name,value)=>staffChip(name,value).click();
const count=value=>Number(chip(value).querySelector('b').textContent);
const staffCount=(name,value)=>Number(staffChip(name,value).dataset.filterCount);
const unread=()=>document.getElementById('list-unread');
const searchField=()=>document.getElementById('list-search');
const clearFilter=()=>document.querySelector('[data-clear-filter-words]');
const border=()=>getComputedStyle(searchField()).borderTopColor;
const query=()=>new URLSearchParams(location.hash.slice(1)).get('q');
const search=value=>new Promise(done=>{
 const field=searchField();
 field.addEventListener('filterchange',done,{once:true});field.value=value;field.dispatchEvent(new Event('input'));
});
try{
 if(document.readyState==='loading') await new Promise(resolve=>document.addEventListener('DOMContentLoaded',resolve,{once:true}));
 const reloadKey='list-filter-clear-reload',saved=sessionStorage.getItem(reloadKey);
 if(saved===null){
  const emptyBorder=border();
  check(searchField().autocomplete==='off' && !clearFilter().getClientRects().length,
   'The empty filter hides Clear filter and disables autocomplete.');
  check(getComputedStyle(searchField()).paddingInlineEnd==='10px','The empty filter leaves room for its placeholder.');
  await search('console');
  searchField().blur();
  check(getComputedStyle(searchField()).paddingInlineEnd==='44px','The active filter leaves room for Clear filter.');
  check(border()!==emptyBorder && border()===getComputedStyle(chip('all')).backgroundColor &&
   clearFilter().getClientRects().length===1 && !clearFilter().matches(':hover,:focus') &&
   document.activeElement!==searchField(),'The field with words uses the chip accent and shows Clear filter without focus or hover.');
  select('owner',OWNER);
  check(shownNumbers()==='106' && query()==='console','The Owner chip keeps the words.');
  select('producer',MORTY);
  check(shownNumbers()==='106' && query()==='console','The Producer chip keeps the words.');
  show('working');
  check(shownNumbers()==='106' && query()==='console','The Show chip keeps the words.');
  select('owner','all');select('producer','all');show('all');
  sessionStorage.setItem(reloadKey,JSON.stringify({emptyBorder,failures:[...failed,...errors]}));
  location.reload();
  return;
 }
 sessionStorage.removeItem(reloadKey);
 const {emptyBorder,failures}=JSON.parse(saved);
 failed.push(...failures);
 check(searchField().value==='console' && query()==='console' && visible().length===2 &&
  border()!==emptyBorder && clearFilter().getClientRects().length===1,'Reload restores the filter from the address.');
 const restoredWords=new Promise(resolve=>{
  const setWords=filterControl.setWords;
  filterControl.setWords=function(field,value){
   setWords.call(this,field,value);
   if(field===searchField()){this.setWords=setWords;resolve();}
  };
 });
 searchField().value='auto';
 dispatchEvent(new Event('pageshow'));await restoredWords;
 check(searchField().value==='console' && query()==='console','Silent browser restoration cannot change the address filter.');
 clearFilter().click();
 check(!searchField().value && !query() && visible().length===5 && document.activeElement===searchField() &&
  border()===emptyBorder && !clearFilter().getClientRects().length,'Clear filter removes the words and returns all five rows.');
 await search('console');
 searchField().dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',cancelable:true,bubbles:true}));
 check(!searchField().value && !query() && visible().length===5 && border()===emptyBorder &&
  !clearFilter().getClientRects().length,'Escape removes the words and returns all five rows.');
 const countLine=document.querySelector('[data-list-count]'),table=countLine.nextElementSibling;
 const bounds=element=>element.getBoundingClientRect();
 const near=(a,b)=>Math.abs(a-b)<1;
 const shown=bounds(countLine);
 check(table.matches('.list-table') && shown.bottom<=bounds(table).top &&
       near(shown.right,bounds(table).right) && getComputedStyle(countLine).textAlign==='right',
       'The count is right aligned above the table.');
 check(!document.querySelector('[data-list-expansion], [aria-label="Expand all"], [aria-label="Collapse all"]'),
       'The List has no bulk expansion controls.');
 check(countLine.textContent.startsWith('5 of 5 items shown') && countLine.getAttribute('role')==='status',
       'The count in the row reports the visible items.');
 check(shown.left>=0 && shown.right<=innerWidth && document.documentElement.scrollWidth<=innerWidth,
       'The count fits without horizontal overflow.');
 if(innerWidth>=960){
  check(bounds(document.querySelector('main')).width===Math.min(1124,document.documentElement.clientWidth),
        'The desktop uses the available width up to the shared limit.');
  for(const header of document.querySelectorAll('[data-sort]')){
   const label=document.createRange();label.selectNodeContents(header.firstChild);
   check(label.getClientRects().length===1,'the desktop header fits on one line: '+header.textContent);
  }
 }
 check(shownNumbers()==='58,105,106,63,41','Parked rows come last in the initial default order.');
 await refreshOrder('Parked rows stay last in the default order after a refresh.');
 check(visible().at(-1).querySelector('[data-parked-note]')?.textContent==='Parked until Nov 18 13:00 EST',
       'The parked row shows its park end time.');
 check(['owner','producer','show'].every(name=>staffCount(name,'all')===5),
       'All three All chips count the parked row with no filters.');
 document.querySelector('[data-sort="0"]').click();
 check(visible().map(row=>row.dataset.number).join(',')==='41,58,63,105,106','The number sort includes the parked row.');
 document.querySelector('[data-sort="0"]').click();
 check(visible().map(row=>row.dataset.number).join(',')==='106,105,63,58,41','A second click reverses the sort.');
 for(let column=1;column<8;column++){
  document.querySelector('[data-sort="'+column+'"]').click();
  check(document.querySelector('[data-sort="'+column+'"]').parentElement.getAttribute('aria-sort')==='ascending','sort column '+column);
  if(column===1) await refreshOrder('Rows with equal Owner values keep their order after a refresh.');
 }
 show('needs-me');check(visible().length===1 && visible()[0].dataset.number==='58','Needs me');
 show('all');unread().click();
 check(visible().map(row=>row.dataset.number).sort().join() === '105,106,41,63','Unread only includes unread parked items in All.');
 check(['owner','producer','show'].every(name=>staffCount(name,'all')===4),
       'All three All chips count the unread parked row with Unread only.');
 unread().click();
 show('parked');check(visible().length===1 && visible()[0].dataset.number==='41','Parked');
 check(!chip('closed'),'Show has no Closed chip.');
 show('all');check(visible().length===5 && count('all')===5,'All shows and counts every open row.');
 show('working');
 const showRow=chip('working').closest('[data-filter-row]');
 const showValues=filterControl.buttons(showRow).map(button=>button.dataset.filterValue);
 filterControl.setVisible(showRow,showValues.filter(value=>value!=='working'));
 dispatchEvent(new PopStateEvent('popstate'));
 check(selectedShow()==='all' && visible().length===5,'Hiding Working selects All and shows five items.');
 check(new URLSearchParams(location.hash.slice(1)).get('show')==='all','the address records the fallback to All');
 filterControl.setVisible(showRow,showValues);
 const theme=document.documentElement.dataset.theme;
 const pixel=document.createElement('canvas').getContext('2d',{willReadFrequently:true});
 const luminance=color=>{
  pixel.fillStyle=color;pixel.fillRect(0,0,1,1);
  const [r,g,b]=[...pixel.getImageData(0,0,1,1).data].slice(0,3).map(byte=>{
   const value=byte/255;
   return value<=.04045 ? value/12.92 : ((value+.055)/1.055)**2.4;
  });
  return .2126*r+.7152*g+.0722*b;
 };
 show('working');select('owner',OWNER);select('producer',MORTY);
 check(pressed('owner').textContent==='sdlc1' && pressed('producer').textContent==='morty1',
  'The selected staff chips show the names and counts.');
 for(const themeName of ['light','dark']){
  document.documentElement.dataset.theme=themeName;
  for(const view of ['working','blocked']){
   show(view);
   for(const control of [chip(view),pressed('owner'),pressed('producer')]){
    const background=luminance(getComputedStyle(control).backgroundColor);
    for(const element of [control,...control.querySelectorAll('b')]){
     const ink=luminance(getComputedStyle(element).color);
     const contrast=(Math.max(ink,background)+.05)/(Math.min(ink,background)+.05);
     check(getComputedStyle(control).opacity==='1' && contrast>=4.5,
      themeName+' contrast for '+control.textContent+': '+contrast);
    }
   }
  }
 }
 if(theme===undefined) delete document.documentElement.dataset.theme;
 else document.documentElement.dataset.theme=theme;
 select('producer','all');select('owner','all');show('all');
 select('owner',OWNER);
 check(visible().length===4 && staffCount('owner',OWNER)===visible().length &&
       visible().some(row=>row.dataset.number==='41'),'The Owner count includes the parked row in All.');
 check(selectedFilter('owner')==='staff:'+OWNER && new URLSearchParams(location.hash.slice(1)).get('owner')===OWNER,
  'The address keeps the selected owner.');
 select('owner','all');
 select('producer',DWIGHT);
 check(visible().map(row=>row.dataset.number).join(',')==='105,41' &&
       staffCount('producer',DWIGHT)===visible().length,'The Producer count includes the parked row in All.');
 check(staffCount('owner','all')===2 && staffCount('owner',OWNER)===1 && staffCount('owner',OTHER)===1 &&
       count('all')===2 && count('parked')===1,'The Producer filter sets the Owner and Show counts.');
 check(new URLSearchParams(location.hash.slice(1)).get('producer')===DWIGHT,'the address keeps the producer');
 select('owner',OWNER);await search('Parked');
 check(visible().map(row=>row.dataset.number).join()==='41' && count('all')===1 &&
       staffCount('owner',OWNER)===1 && staffCount('producer',DWIGHT)===1,
       'All combines Owner, Producer, and search for the parked row.');
 show('parked');check(visible().map(row=>row.dataset.number).join()==='41' && count('parked')===1,
       'Parked combines Owner, Producer, and search.');
 await search('');select('owner','all');show('all');
 select('producer','all');
 check(visible().length===5 && !new URLSearchParams(location.hash.slice(1)).has('producer'),'All producers');
 check(['show','owner','producer'].every(name=>filterRow(name).querySelector('button').dataset.filterValue==='all'),
  'Each filter row starts with All.');
 show('working');
 check(visible().length===2 && count('working')===2 && count('blocked')===0,
  'Working shows both matching records. Blocked counts zero.');
 check(staffCount('owner',OWNER)===1 && staffCount('producer',DWIGHT)===1,
  'The staff chips count the records in Working.');
 const zero=chip('blocked'),zeroPosition=zero.getBoundingClientRect();
 check(!zero.hidden && !zero.disabled && Number(getComputedStyle(zero).opacity)<1 && zeroPosition.width>0,
  'a zero chip stays in the row, dimmed and enabled');
 show('blocked');
 check(visible().length===0 && count('working')===2,'Selecting a zero chip keeps the other status counts.');
 const zeroStaff=[staffChip('owner',OWNER),staffChip('producer',DWIGHT)];
 check(zeroStaff.every(button=>button.dataset.filterCount==='0' && !button.hidden && !button.disabled &&
  Number(getComputedStyle(button).opacity)<1 && button.getBoundingClientRect().width>0),
  'The zero staff chips stay visible, enabled and dimmed.');

 show('working');select('owner',OWNER);select('producer',DWIGHT);
 check(visible().length===0 && staffCount('owner','all')===1 && staffCount('producer','all')===1 && count('all')===1,
  'Each count ignores only its own filter.');
 select('producer','all');await search('console');
 check(visible().map(row=>row.dataset.number).join(',')==='106' && count('working')===1 &&
  staffCount('producer',DWIGHT)===0 && staffCount('owner','all')===2,'The search and chip rows combine.');
 check(zero.getBoundingClientRect().left===zeroPosition.left,'a zero chip retains its place');
 await search('');show('all');select('owner','all');
 await search('#106');
 check(visible().length===1 && visible()[0].dataset.number==='106','number search');
 await search('console');check(visible().length===2,'title search');
 check(new URLSearchParams(location.hash.slice(1)).get('q')==='console','the address keeps the query');
 for(const title of ['Straße','STRAẞE']){
  await search(title);
  check(visible().length===1 && visible()[0].dataset.number==='106','Unicode title search: '+title);
 }
 await search('console');
 const selected=visible().find(row=>row.dataset.number==='106');selected.querySelector('.list-toggle').click();
 check(!selected.querySelector('[data-detail]').hidden,'a row opens');
 const words=selected.querySelector('textarea'),tell=selected.querySelector('[value="tell"]');
 check(tell.textContent==='Tell morty','the button names its recipient');
 check(words.placeholder==='Your words for morty about this item' && words.getAttribute('aria-label')===words.placeholder,'the words box has its accessible name');
 check(!selected.querySelector('label[for="'+words.id+'"]'),'the words box has no repeated visible label');
 check(!tell.closest('.list-actions') && tell.parentElement.querySelector('[data-send-status]'),'the status stays beside the separate primary button');
 const wordsRect=words.getBoundingClientRect(),tellRect=tell.getBoundingClientRect();
 check(tellRect.top>=wordsRect.bottom && Math.abs(tellRect.right-wordsRect.right)<1,'the button sits below the box at its right edge');
 check(tell.disabled,'empty words disable Tell morty');
 words.value=' \t\n ';words.dispatchEvent(new Event('input'));
 check(tell.disabled,'spaces disable Tell morty');
 const emptyPosts=posts.length;tell.click();check(posts.length===emptyPosts,'empty words cannot send');
 words.value='x';words.dispatchEvent(new Event('input'));check(!tell.disabled,'typed text enables Tell morty immediately');
 words.value='';words.dispatchEvent(new Event('input'));check(tell.disabled,'cleared words disable Tell morty immediately');
 words.value='Keep my draft';words.dispatchEvent(new Event('input'));
 const field=document.querySelector('#list-search');field.focus();field.setSelectionRange(1,3);
 document.body.style.minHeight='2400px';scrollTo(0,100);
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await changed(document.body,()=>!selected.isConnected);
 const refreshed=rows().find(row=>row.dataset.number==='106');
 check(reads.length===3,'Each shared status event requests a fresh page.');
 check(document.querySelector('#list-search').value==='console','refresh keeps the query');
 check(document.activeElement.id==='list-search','refresh keeps focus');
 check(document.activeElement.selectionStart===1 && document.activeElement.selectionEnd===3,'refresh keeps the selection');
 check(!refreshed.querySelector('[data-detail]').hidden,'Refresh keeps the open row.');
 check(refreshed.querySelector('textarea').value==='Keep my draft','refresh keeps feedback words');
 check(!refreshed.querySelector('[value="tell"]').disabled,'a restored draft enables Tell morty');
 check(scrollY===100,'refresh keeps scroll');
 check(refreshed!==selected,'refresh replaces the row');
 const refusedForm=refreshed.querySelector('form');
 holdPost=true;
 const refused=submitted(refusedForm,refusedForm.querySelector('[value="tell"]'));
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 check(reads.length===3,'The shared status event makes no List request during a row save.');
 finishPost({ok:false,status:409,json:async()=>({reason:'The row save was refused.'})});
 holdPost=false;finishPost=undefined;
 await refused;
 check(reads.length===3 && refreshed.isConnected,'A refused row save makes no List request after the event.');
 check(refreshed.dataset.unread==='true' && refusedForm.elements.words.value==='Keep my draft' &&
       refusedForm.querySelector('[data-send-status]').textContent==='Not saved: The row save was refused.',
       'A refusal restores the unread state and shows the reason beside the draft.');
 realFeedback=true;loseResponse=true;
 const tellRow=rows().find(row=>row.dataset.number==='105');tellRow.querySelector('.list-toggle').click();
 check(visible().length===2 && refreshed.querySelector('[data-detail]').hidden &&
  !tellRow.querySelector('[data-detail]').hidden,'Opening the second List row closes the first row.');
 check(refreshed.querySelector('textarea').value==='Keep my draft','Closing the first row keeps its draft.');
 check(tellRow.dataset.unread==='true' && tellRow.querySelector('[value="read"]'),'Tell starts with a fresh unread row');
 let retryForm=tellRow.querySelector('form');
 retryForm.elements.words.value='Deliver this feedback once';retryForm.elements.words.dispatchEvent(new Event('input'));
 const retryId=retryForm.elements.request.value,retryToken=retryForm.elements.token.value,startPosts=posts.length;
 await submitted(retryForm,retryForm.querySelector('[value="tell"]'));
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await changed(document.body,()=>!retryForm.isConnected);
 retryForm=rows().find(row=>row.dataset.number==='105').querySelector('form');
 check(retryForm.elements.request.value===retryId && retryForm.elements.token.value===retryToken,'refresh keeps the failed request identity');
 await submitted(retryForm,retryForm.querySelector('[value="tell"]'));
 check(posts.length===startPosts+2,'the lost response has one retry');
 check(posts.at(-1).get('request')===retryId && posts.at(-1).get('token')===retryToken,'the retry uses its original signature');
 retryForm=rows().find(row=>row.dataset.number==='105').querySelector('form');
 check(retryForm.elements.request.value!==retryId,'success clears the saved retry identity');
 check(retryForm.elements.words.value==='' && retryForm.querySelector('[value="tell"]').disabled,'a successful send clears the box and disables Tell morty');
 realFeedback=false;failRefresh=true;
 rows().find(row=>row.dataset.number==='106').querySelector('.list-toggle').click();
 const requestIds=[];
 for(const action of ['read','park','tell','remove']){
  const row=rows().find(row=>row.dataset.number==='106'),form=row.querySelector('form');
  form.elements.until.value='2026-12-03';form.elements.words.value='Explain this simply';
  requestIds.push(form.elements.request.value);
  holdPost=action==='tell';
  form.requestSubmit(form.querySelector('[value="'+action+'"]'));
  if(holdPost){
   if(typeof finishPost!=='function') throw new Error('The held response did not start.');
   form.elements.words.value='New draft during send';form.elements.words.dispatchEvent(new Event('input'));
   check(form.querySelector('[value="tell"]').disabled,'editing words during delivery keeps Tell morty disabled');finishPost();}
  check(form.querySelector('[data-send-status]').textContent===(action==='read' ? 'Saving…' : 'Sending…'),'immediate response '+action);
  await changed(form,()=>!form.dataset.sending);
  check(posts.at(-1).get('action')===action && posts.at(-1).get('id')==='L106','signed feedback '+action);
  check(posts.at(-1).get('token').length===64,'the form carries its token');
  check(form.elements.request.value!==requestIds.at(-1),'success renews the request without refresh');
  const motions=[...document.querySelectorAll('[data-record-motion]')].flatMap(node=>node.getAnimations({subtree:true}));
  await Promise.allSettled(motions.map(animation=>animation.finished));
  if(action==='tell'){
   check(form.elements.words.value==='New draft during send','a newer draft survives the send');
   check(!form.querySelector('[value="tell"]').disabled,'the newer draft enables Tell morty after delivery');
  }
  if(action==='remove') check(row.hidden,'Remove hides the item after the save');
  else check(!row.querySelector('[data-detail]').hidden &&
   rows().find(row=>row.dataset.number==='105').querySelector('[data-detail]').hidden,
   'Feedback keeps item 106 open and item 105 closed.');
 }
 check(new Set(requestIds).size===4,'each new action uses a fresh request');
 realFeedback=true;failRefresh=false;
 const beforeRefresh=document.querySelector('[data-list]');
 const replaced=new Promise(resolve=>{
  const observer=new MutationObserver(()=>{
   if(beforeRefresh.isConnected) return;
   observer.disconnect();resolve();
  });
  observer.observe(beforeRefresh.parentElement,{childList:true});
 });
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await replaced;
 realFeedback=false;failRefresh=true;
 await navigate({show:'closed',sort:'#',dir:'desc'});
 check(visible().map(row=>row.dataset.number).join(',')==='106,105,63,58,41' && selectedShow()==='all',
  'An old Closed address opens All in the requested sort order.');
 await navigate({show:'done'});
 check(selectedShow()==='all' && visible().map(row=>row.dataset.number).sort().join()==='105,106,41,58,63',
  'An old Done address opens All.');
 await navigate({show:'all',producer:DWIGHT});
 check(selectedFilter('producer')==='staff:'+DWIGHT && visible().map(row=>row.dataset.number).join(',')==='105,41',
   'an address restores the producer filter');
 await navigate({show:'all',producer:'missing'});
 check(selectedFilter('producer')==='all' && visible().length===5,'An unknown producer clears the Producer filter.');
 const restoredOldAddress=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
 location.hash=new URLSearchParams({show:'unread',state:'Working',owner:OWNER,q:'106'});
 await restoredOldAddress;
 check(selectedShow()==='working' && selectedFilter('owner')==='staff:'+OWNER && unread().checked &&
  visible().map(row=>row.dataset.number).join(',')==='106','An old address restores the Show and Owner chips, switch and search.');

 for(const target of rows().filter(row=>row.dataset.state==='Done' || row.dataset.parked==='true')){
  await navigate({item:target.dataset.key});
  check(!target.hidden && !target.querySelector('[data-detail]').hidden,'an item link shows '+target.dataset.state);
  check(selectedShow()==='all' &&
    selectedFilter('owner')==='all' && !unread().checked && selectedFilter('producer')==='all' &&
    document.getElementById('list-search').value==='','An item link selects Show and clears the other filters.');
 }
 const retained=rows().find(row=>row.dataset.number==='106');
 await navigate({item:retained.dataset.key,owner:retained.dataset.owner,show:'working',q:'106',sort:'#',dir:'desc'});
 check(selectedFilter('owner')==='staff:'+retained.dataset.owner && selectedShow()==='working' &&
   document.getElementById('list-search').value==='106','an address with an open item keeps its filters');
 check(!retained.hidden && !retained.querySelector('[data-detail]').hidden,'an address restores its open item');
 check(document.querySelectorAll('[data-feedback-form]').length===5,'each row has feedback controls');
 check(document.documentElement.scrollWidth<=innerWidth,'the page has no horizontal overflow');
 check((innerWidth<960)===(FIXTURE_WIDTH<960),'the browser uses the requested layout');
 if(FIXTURE_WIDTH===390) check(innerWidth===390,'The phone cards use the 390 px viewport.');
 if(FIXTURE_WIDTH<960){
  check(getComputedStyle(visible()[0]).display==='block','a phone row is a card');
  const card=number=>rows().find(row=>row.dataset.number===number);
  const cell=(number,label,part=null)=>getComputedStyle(card(number).querySelector('td[data-label="'+label+'"]'),part);
  check(cell('105','Item').fontSize==='17px' && cell('105','Producer').fontSize!=='17px','a phone card shows the title largest');
  check(cell('105','Producer','::before').content.includes('Producer'),'a phone card labels another producer');
  check(cell('106','Producer').display==='none','a phone card leaves out the keeper as producer');
 }
}catch(error){failed.push(String(error));}
await nativeFetch('CASE_PATH/results',{method:'POST',body:JSON.stringify([...failed,...errors])});
})();</script>'''
        for name, identity in (("OWNER", OWNER.id), ("DWIGHT", DWIGHT.id), ("MORTY", MORTY.id), ("OTHER", OTHER.id)):
            probe = probe.replace(name, json.dumps(identity))
        for width in (1440, 961, 390):
            with self.subTest(width=width):
                prefix = "/" + uuid4().hex
                for identity in ("L105", "L106"):
                    _ = the_list.update(MORTY.id, identity,
                        {"latest_status": f"The fixture uses a width of {width} pixels."}, source="morty")
                markup = list_page.render(self.root / "reviews").decode()
                setup_for_width = setup.replace('CASE_PATH', prefix).replace('MARKUP', json.dumps(markup).replace('</', '<\\/'))
                probe_for_width = probe.replace('CASE_PATH', prefix).replace('FIXTURE_WIDTH', str(width))
                page = markup.replace('<head>', '<head>' + setup_for_width, 1).replace('</body>', probe_for_width + '</body>').encode()
                root = self.root / "reviews"
                results: list[list[str]] = []
                complete = threading.Event()
                host = (f'<iframe style="border:0;width:100vw;height:100vh" src="{prefix}/page"></iframe>'
                        + f'<img hidden src="{prefix}/complete">').encode()

                class Probe(BaseHTTPRequestHandler):
                    result_rows: ClassVar[list[list[str]]] = results
                    result_event: ClassVar[threading.Event] = complete
                    probe_page: ClassVar[bytes] = page
                    host_page: ClassVar[bytes] = host

                    def do_GET(self) -> None:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        if self.path == '/fresh':
                            _ = self.wfile.write(list_page.render(root).decode().encode())
                        elif self.path == '/complete':
                            _ = self.result_event.wait(40)
                        else:
                            _ = self.wfile.write(self.probe_page if self.path == '/page' else self.host_page)

                    def do_POST(self) -> None:
                        if self.path == '/feedback':
                            body = self.rfile.read(int(self.headers['Content-Length'])).decode()
                            delivery = list_page.submit(root, body)
                            list_page.deliver(delivery)
                            fields = parse_qs(body)
                            result = {"state": "sent", **list_page.saved_action_state(fields["keeper"][0], fields["id"][0])}
                            self.send_response(200)
                            self.send_header('Content-Type', 'application/json')
                            self.end_headers()
                            _ = self.wfile.write(json.dumps(result).encode())
                            return
                        if self.path != '/results':
                            self.send_error(404)
                            return
                        self.result_rows.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                        self.result_event.set()
                        self.send_response(204)
                        self.end_headers()

                    @override
                    def log_message(self, format: str, *args: object) -> None:
                        pass

                with self.probe_server(prefix, Probe) as server:
                    try:
                        with patch.object(change_page, "send_to_staff") as sends:
                            self.browser_results(f'http://127.0.0.1:{server.server_port}{prefix}/',
                                                 width, 900, complete, firefox_only=True)
                            self.assertEqual(results, [[]])
                            sends.assert_called_once()
                    finally:
                        complete.set()


if __name__ == '__main__':
    _ = unittest.main()

"""Test The List page with private records and a real browser."""
from __future__ import annotations

import base64
import html
import io
import json
import os
import re
import shlex
import select
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Generator, Mapping
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import BinaryIO, ClassVar, Literal, cast, final, override
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit
from uuid import uuid4
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

from scripts.console_text.test_support import accepting_console_text
from scripts.accounts import accounts
from scripts.agents.remote import local_machine
from scripts.herdr.place import HerdrPlace
from scripts.lib import conf, file_lock
from scripts.lib.process_group import run_process_group, stop_process_group
from scripts.lib.test_hang_guard import TEST_HANG_GUARD_SECS
from scripts.lib.user_zone import user_zone
from scripts.lint.memory_test_support import write_config_fixture
from scripts.message import send_result, sessions
from scripts.review_links.test_page_question import FakeQuestionTransport, question_outcome
from scripts.production import (change_review, change_routing, delivery_state, investigation, list_exchange, record_store, repeat_rule,
                                session_pane, session_staff, showrunners, staff, the_list, unrouted_alert, update_registration)
from scripts.production.update_registration import QuestionStaff
from scripts.review_links import (browser_test_support, console, go_ledger, list_page, page_frame, serve, staff_names,
                                  studio_page)
from scripts.review_links import answer_sources, change_browser, change_page, images, new_item_dialog, page_question, question_record
from scripts.review_links import review_items, review_link, review_page, review_submissions, search_fold, send_hold
from scripts.review_links.browser_test_support import UNDO_ACTION_SCRIPT, assert_status_states, firefox_probe_results
from scripts.review_links.server_test_support import started_review_server
from scripts.review_links.test_change_page import BrowserForms
from scripts.review_links.test_record_table import save_notice_failures
from scripts.review_links.test_serve import new_item_notice_log, read_new_item_notices
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


def without_scripts(markup: str) -> str:
    return re.sub(r'<script\b[^>]*>.*?</script>', '', markup, flags=re.DOTALL)


REVIEW_CARD_CONFLICT_CHECK = r"""
async function reviewCardPosition(card){
 const win=card.defaultView;
 const paint=window=>new Promise(resolve=>window.requestAnimationFrame(()=>window.requestAnimationFrame(resolve)));
 await paint(win);await paint(win.top);
 const text=card.querySelector('[data-review-body] p');
 const range=card.createRange();range.selectNodeContents(text);
 const line=range.getClientRects()[0];
 let top=line.top,left=line.left,view=win;
 while(view.frameElement){
  const frame=view.frameElement,box=frame.getBoundingClientRect();
  top+=box.top+frame.clientTop;left+=box.left+frame.clientLeft;view=frame.ownerDocument.defaultView;
 }
 const main=card.querySelector('main').getBoundingClientRect().height;
 const frame=win.frameElement;
 const height=frame?.getBoundingClientRect().height ?? win.innerHeight;
 const requested=frame && Number.parseFloat(frame.style.getPropertyValue('--review-card-height'));
 const dialog=frame?.closest('dialog'),fullscreen=dialog?.hasAttribute('data-fullscreen');
 // The item heading takes space above the frame. The desktop keeps 16 px below the window.
 const start=frame?.getBoundingClientRect().top ?? 0;
 const border=dialog ? parseFloat(win.parent.getComputedStyle(dialog).borderBottomWidth) : 0;
 const cap=win.parent.innerHeight-start-(fullscreen ? 0 : 16+border);
 return {top,left,main,height,requested,cap,
  resized:!frame || Math.abs(height-(fullscreen ? cap : Math.min(requested,cap)))<1,
  row:card.querySelector('[data-review-card-updates]').getBoundingClientRect().height,
  dark:win.matchMedia('(prefers-color-scheme:dark)').matches};
}
async function checkReviewCardConflict(card,layout,check,advance){
 const win=card.defaultView,request=win.fetch;
 const form=()=>card.querySelector('[data-review-form]');
 const show=card.querySelector('[data-show-record-updates]'),notice=card.querySelector('[data-review-card-notice]');
 const wait=ready=>new Promise((resolve,reject)=>{
  const observer=new win.MutationObserver(finish);
  const timer=setTimeout(()=>{observer.disconnect();reject(new Error('The Review card did not change.'));},8000);
  function finish(){
   if(ready()){clearTimeout(timer);observer.disconnect();resolve();}
  }
  observer.observe(card.body,{attributes:true,childList:true,subtree:true});finish();
 });
 const submit=()=>form().requestSubmit(form().querySelector('[value="works"]'));
 const revision=form().elements.revision.value,original=form();
 const nextBody='The source changed while the card was open.';
 const reason='This item changed. '+('Check the current revision before saving. '.repeat(20)).trim();
 let posts=0,status;
 win.fetch=async(url,options)=>{
  const savesVerdict=options?.method==='POST' && url===win.location.pathname;
  if(savesVerdict) posts++;
  const response=await request.call(win,url,options);
  if(!savesVerdict) return response;
  status=response.status;
  return status===409 ? new win.Response(JSON.stringify({reason}),{status}) : response;
 };
 try{
  const before=await reviewCardPosition(card);
  await advance();
  submit();
  await wait(()=>!show.hidden && !original.dataset.sending);
  const after=await reviewCardPosition(card);
  const marker=notice.textContent;
  check(before.dark && after.dark,'The Review card uses dark mode.');
  check(before.resized && after.resized,'The frame takes the height the card requests, up to the cap.');
  check(before.row===52 && after.row===52,'The status row keeps the same 52 pixel height.');
  check(Math.abs(after.top-before.top)<0.5 && Math.abs(after.left-before.left)<0.5 &&
   before.main===after.main && before.height===after.height && before.requested===after.requested,'A 409 keeps the text and card in place. '+JSON.stringify({before,after}));
  if(layout==='short') check(before.requested<before.cap,'The short card fits below the iframe height cap.');
  if(layout==='cap') check(before.requested>before.height && Math.abs(before.height-before.cap)<1,
   'The tall card reaches the iframe height cap.');
  if(layout==='phone') check(win.top.innerWidth===390 && win.frameElement.closest('dialog').hasAttribute('data-fullscreen'),
   'The phone card fills the List dialog at 390 pixels.');
  if(layout==='standalone') check(win===win.top,'The standalone card runs outside an iframe.');
  check(status===409 && marker==='Not saved: '+reason && notice.title===marker && notice.scrollWidth>notice.clientWidth,
   'The fixed row shows the refusal and keeps the full reason in its title.');
  check(form()===original && form().elements.revision.value===revision &&
   !card.querySelector('[data-review-body]').textContent.includes(nextBody),
   'A 409 keeps the old body and signed form.');
  submit();
  check(posts===1,'The old form cannot submit while Show updates is available.');
  const edited='Keep the edit made while Show updates is available.';
  form().elements.feedback.value=edited;
  form().elements.feedback.dispatchEvent(new win.Event('input',{bubbles:true}));
  show.click();
  await wait(()=>show.hidden);
  check(form().elements.revision.value!==revision && notice.textContent===marker &&
   form().elements.feedback.value===edited && card.querySelector('[data-review-body]').textContent.includes(nextBody),
   'Show updates applies the new revision and keeps the edit and failure reason.');
  const words=card.querySelectorAll('textarea');
  check(words.length===1 && words[0]===form().elements.feedback && !words[0].readOnly,
   'Show updates keeps one editable draft.');
  return {before,after};
 }finally{
  win.fetch=request;
 }
}
"""


def _private_firefox_command(profile: Path, width: int, height: int, *, dark: bool) -> tuple[list[str], dict[str, str]]:
    profile.mkdir()
    if dark:
        _ = (profile / "user.js").write_text('user_pref("ui.systemUsesDarkTheme", 1);\n')
    command = ["firefox", "--headless", "--no-remote", "--profile", str(profile), "--window-size", f"{width},{height}"]
    return command, {**os.environ, "MOZ_HEADLESS_WIDTH": str(width), "MOZ_HEADLESS_HEIGHT": str(height)}


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


class _Chrome(_Browser):
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
            window_size: tuple[int, int] | None, keep_storage: bool = False) -> subprocess.CompletedProcess[str]:
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
            clear_storage = "" if keep_storage else (
                "try { sessionStorage.clear(); localStorage.clear(); }"
                "catch (error) { if (error.name !== 'SecurityError') throw error; }"
            )
            script = clear_storage + (
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


class _Firefox(_Browser):
    """Keep Firefox open and control its private profile through Marionette."""

    def __init__(self, root: Path) -> None:
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
            deadline = time.monotonic() + 30
            while not port_file.exists():
                if process.poll() is not None or time.monotonic() >= deadline:
                    raise RuntimeError((root / "firefox.log").read_text(errors="replace"))
                time.sleep(0.02)
            address = ("127.0.0.1", int(port_file.read_text()))
            self.connection: socket.socket = self.resources.enter_context(socket.create_connection(address, timeout=45))
            self.stream: BinaryIO = self.resources.enter_context(self.connection.makefile("rb"))
            _ = self.read()
            self.sequence: int = 0
            _ = self.command("WebDriver:NewSession", {"capabilities": {"alwaysMatch": {"pageLoadStrategy": "none"}}})
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


class ListPageTests(unittest.TestCase):
    root: Path = Path()
    browser_root: ClassVar[Path]
    _chrome: ClassVar[_Chrome | None] = None
    _test_chrome: _Chrome | None = None
    _firefox: ClassVar[_Firefox | None] = None

    @classmethod
    @override
    def setUpClass(cls) -> None:
        cls.enterClassContext(accepting_console_text())
        cls.browser_root = Path(cls.enterClassContext(tempfile.TemporaryDirectory(prefix="list-page-class-")))
        cls._chrome = None
        cls._firefox = None
        config = cls.browser_root / "config"
        config.mkdir()
        _ = (config / "the_list.conf").write_text("# whole: 1..\nseries_run_limit=730\n")
        _ = (config / "production.conf").write_text("# whole: 20..\nquestion_title_max_chars=100\n")
        _ = (config / "statusline.conf").write_text(
            "role.showrunner=#a379fd\nrole.unit-director=#ffaf02\n"
            + "role.producer=#90fe42\nrole.worker=#ff0404\n")
        _ = (config / "review_links.conf").write_text(
            "# whole: 1..20\ndelivery_tries=3\n# range: 0..300\ndelivery_pause_seconds=0\n"
            + "# whole: 1..20\nfeedback_rows=5\n# whole: 1..20\nreview_words_rows=4\n"
            + "# whole: 1..1440\nlater_soon_minutes=60\n"
            + "later_morning=09:00\n"
            + "later_week_day=Monday\nlater_week_time=09:00\n"
            + "# whole: 1..60\npark_time_step_minutes=15\n"
            + "# range: 5..300\npage_refresh_seconds=15\n# whole: 1..80\nlist_log_max_height_rem=16\n"
            + "# whole: 1..300\npage_request_timeout_seconds=15\n"
            + "# whole: 1..5000\ncard_fade_out_ms=450\n# whole: 1..5000\ncard_fade_in_ms=600\n"
            # Undo is off, so each send saves at once; test_send_hold and test_serve check the wait.
            + "# whole: 0..60\nundo_seconds=0\n"
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
            "page.studio.label=Studio\npage.studio.icon=clapperboard\npage.studio.title=The Studio\n"
            + "page.studio.url=python:scripts.review_links.studio_page:page_url\n"
            + "page.studio.items=python:scripts.review_links.list_page:waiting_updates\n"
            + "# range: 0.001..86400\nstatus.round_trip_limit_seconds=1\n"
            + "# range: 0.001..86400\nstatus.render_limit_seconds=1\n"
            + "# range: 0.001..86400\nstatus.reading_delay_seconds=1\n"
            + "# range: 0.001..86400\nstatus.list.age_limit_seconds=45\n"
            + "# range: 0.001..86400\nstatus.review.age_limit_seconds=45\n")

    def chrome(self) -> _Chrome:
        cls = type(self)
        browser = cls._chrome
        if browser is None or browser.closed or browser.process.poll() is not None:
            if browser is not None:
                browser.close()
            root = Path(tempfile.mkdtemp(dir=cls.browser_root, prefix="chrome-"))
            browser = _Chrome(root)
            cls._chrome = browser
            cls.addClassCleanup(browser.close)
        if self._test_chrome is not browser:
            self._test_chrome = browser
            self.addCleanup(browser.close_test_pages)
        return browser

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

    def firefox_view(self, destination: str, path: Path | None, width: int, height: int,
                     complete: threading.Event | None = None, *, dark: bool = False
                     ) -> subprocess.CompletedProcess[str]:
        if width < FIREFOX_MIN_SHARED_WIDTH:
            if self._chrome is not None:
                self._chrome.close()
            with tempfile.TemporaryDirectory(dir=self.root, prefix="firefox-view-") as temporary:
                root = Path(temporary)
                command, environment = _private_firefox_command(root / "profile", width, height, dark=dark)
                screenshot = path if path is not None else root / "page.png"
                return run_process_group([*command, "--screenshot", str(screenshot), destination],
                                         timeout=45, env=environment)
        try:
            browser = self.firefox()
            with browser.page(destination, width, height, dark=dark):
                if complete is None:
                    browser.wait_for_load(destination)
                elif not complete.wait(45):
                    raise TimeoutError("Firefox did not report the page result")
                if path is not None:
                    browser.screenshot(path)
        except (OSError, RuntimeError) as error:
            raise RuntimeError(f"Firefox could not show {destination}") from error
        return subprocess.CompletedProcess(destination, 0, "", "")

    def firefox_probe(self, page: bytes, *, window_size: tuple[int, int] | None = None) -> list[list[str]]:
        width, height = window_size or (1366, 768)

        def load(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            return self.firefox_view(arguments[-1], None, width, height)

        with patch.object(browser_test_support, "run_process_group", side_effect=load):
            return firefox_probe_results(page, window_size=(width, height))

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
    def test_window_send_files_one_linked_row_before_the_notice(self) -> None:
        self.check_live_window_send(lose_reply=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_exact_window_retry_returns_the_first_item_and_closes(self) -> None:
        self.check_live_window_send(lose_reply=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_window_bug_receipt_shows_the_saved_number_owner_and_producer(self) -> None:
        self.check_live_window_send(lose_reply=False, kind="bug")

    def check_live_window_send(self, *, lose_reply: bool, kind: str = "investigation") -> None:
        root = self.root / "reviews"
        _ = write_config_fixture(conf.path("console"), f"page.list.owner={MORTY.id}\npage.studio.owner={DWIGHT.id}\n")
        _ = write_config_fixture(conf.path("producer_map"))
        words = "Keep the wait time next to the queue name."
        saved: list[tuple[int, Mapping[str, object]]] = []
        probe = r"""<script>
(async()=>{
 const result=document.createElement('p');
 result.id='probe-results';
 const until=(element,ready)=>new Promise((resolve,reject)=>{
  const observer=new MutationObserver(finish);
  const timer=setTimeout(()=>{
   observer.disconnect();
   reject(new Error(document.querySelector('.new-item-error').textContent || 'The window did not settle.'));
  },5000);
  function finish(){
   if(ready()){clearTimeout(timer);observer.disconnect();resolve();}
  }
  observer.observe(element,{attributes:true,subtree:true,childList:true});finish();
 });
 try {
  const dialog=document.querySelector('#review-new-item');
  const send=dialog.querySelector('[type="submit"]');
  const words=dialog.querySelector('textarea');
  const error=dialog.querySelector('.new-item-error');
  const button=document.querySelector('[data-page-owner="studio"] [data-owner-new]');
  if(!button) throw new Error('The Studio owner button is missing.');
  button.click();
  const includePage=dialog.querySelector('[name="include_page"]');
  if(includePage.checked) includePage.click();
  const producer=id=>[...dialog.querySelectorAll('[name="recipient_id"]')].find(input=>input.value===id);
  await until(dialog,()=>producer(__STUDIO_PRODUCER__) && producer(__SAVED_PRODUCER__));
  if(!sessionStorage.getItem('probe-producer-selected')){
   if(!producer(__STUDIO_PRODUCER__).checked) throw new Error('New item did not select the Studio producer.');
   dialog.querySelector('[name="kind"][value="investigation"]').click();
   producer(__SAVED_PRODUCER__).click();
   sessionStorage.setItem('probe-producer-selected','yes');
   result.textContent=JSON.stringify([]);
   document.body.append(result);
   return;
  }
  if(!producer(__SAVED_PRODUCER__).checked) throw new Error('New item lost the selected producer on reload.');
  dialog.querySelector('[name="kind"][value="'+__KIND__+'"]').click();
  words.value=__WORDS__;
  words.dispatchEvent(new Event('input',{bubbles:true}));
  if(send.disabled) throw new Error('Send is disabled with the Studio producer selected.');
  let filed=false;
  document.addEventListener('review-item-created',()=>{filed=true;},{once:true});
  const settled=()=>filed || dialog.open && !error.hidden && !send.disabled;
  send.click();
  if(dialog.open) throw new Error('Send did not close the window.');
  await until(document.body,settled);
  if(__RETRY__){
   if(!dialog.open) throw new Error('The lost reply did not reopen the window.');
   send.click();
   await until(document.body,settled);
  }
  if(!filed || dialog.open) throw new Error(error.textContent || 'The List did not confirm the item.');
  const receipt = document.querySelector('[data-report-receipt]')?.textContent;
  if(__KIND__==='bug' && receipt !== 'List item 1. Owner morty. Producer morty.')
   throw new Error('The report receipt shows: ' + receipt);
  result.textContent=JSON.stringify([]);
 } catch(error){
  result.textContent=JSON.stringify([String(error)]);
 }
 document.body.append(result);
})();
</script>"""
        probe = probe.replace("__WORDS__", json.dumps(words)).replace("__RETRY__", json.dumps(lose_reply))
        probe = probe.replace("__KIND__", json.dumps(kind)).replace("__STUDIO_PRODUCER__", json.dumps(DWIGHT.id))
        probe = probe.replace("__SAVED_PRODUCER__", json.dumps(MORTY.id))

        @final
        class Probe(serve.ReviewLinksHandler):
            @override
            def html_response(self, status: int, content: bytes, *, delivery_pending: bool | None = None,
                              builds_version: str = "", content_security_policy: str = "",
                              cache_gzip: bool = False) -> None:
                if self.path == studio_page.PAGE_PATH:
                    content = content.replace(b"</body>", probe.encode() + b"</body>", 1)
                super().html_response(status, content, delivery_pending=delivery_pending, builds_version=builds_version,
                                      content_security_policy=content_security_policy, cache_gzip=cache_gzip)

            @override
            def json_response(self, status: int, fields: Mapping[str, object], *,
                              hold: send_hold.HoldHeader | None = None,
                              hold_outcome: Literal["undone", "missing"] | None = None) -> None:
                if self.command == "POST" and self.path == review_page.NEW_ITEM_PATH and "id" in fields:
                    saved.append((status, fields))
                    if lose_reply and len(saved) <= 2 and status == 200:
                        self.close_connection = True
                        return
                super().json_response(status, fields, hold=hold, hold_outcome=hold_outcome)

        with (patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "key-state"),
                                     "REBUILD_NEEDS_DIR": str(self.root / "rebuild-needs")}),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(serve, "loaded_module_stamps", return_value={}),
              new_item_notice_log(self.root, MORTY.id) as log,
              serve.ReviewLinksServer(("127.0.0.1", 0), root) as server):
            server.RequestHandlerClass = Probe
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                url = f"http://127.0.0.1:{server.server_port}{studio_page.PAGE_PATH}"
                browser = self.chrome()
                first_page = browser.dom(url, "probe-results", reduced_motion=True, window_size=(1280, 900))
                result = browser.dom(url, "probe-results", reduced_motion=True, window_size=(1280, 900),
                                     keep_storage=True)
            finally:
                server.shutdown()
                thread.join()
            marker = '<p id="probe-results">'
            for page in (first_page, result):
                self.assertIn(marker, page.stdout, page.stderr)
                failures = cast(list[str], json.loads(html.unescape(page.stdout.split(marker, 1)[1].split("</p>", 1)[0])))
                self.assertEqual(failures, [])
            record, = investigation.records(MORTY.id, include_read=True)
            row, = the_list.records(MORTY.id, include_closed=True)
            notice, = read_new_item_notices(log)
            self.assertIn(words, notice["words"])
            self.assertEqual(notice["rows"], [row.id])
            first_status, first = saved[0]
            self.assertEqual(first_status, 200)
            self.assertEqual((first["id"], first["list_number"]), (record.id, row.number))
            assert isinstance(row.report, the_list.ListReport)
            report_kind = the_list.ReportKind.BUG if kind == "bug" else the_list.ReportKind.INVESTIGATION
            self.assertEqual((row.report.kind, row.report.evidence_key), (report_kind, first["key"]))
            if lose_reply:
                _, fallback, retry = saved
                for status, receipt in (fallback, retry):
                    self.assertEqual(status, 200)
                    self.assertEqual(receipt, {**first, "version": receipt["version"]})
            else:
                self.assertEqual(len(saved), 1)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_new_item_refreshes_without_opening_a_row(self) -> None:
        root = self.root / "reviews"
        _ = self.add("An existing List item")
        initial = page_frame.with_owner(list_page.render(root, include_counts=False), "list", MORTY,
                                        staff.StaffSnapshot((MORTY,)), new_item_dialog.render).decode()
        item = self.add("A new List item")
        fresh = list_page.render(root, include_counts=False).decode()
        setup = r"""<script>
const created=__KEY__,owner=__OWNER__,fresh=__FRESH__,requests=[];
let finishRowSave,reads=0;
window.setInterval=()=>0;
window.fetch=async (path,options={})=>{
 if(options.method==='POST' && path===document.querySelector('[data-list]').dataset.listPath)
  return new Promise(resolve=>{finishRowSave=resolve;});
 if(path===location.pathname){
  reads++;
  return new Response(fresh);
 }
 const request=new URL(path,location.href);
 requests.push([request.pathname+request.search,options.body && JSON.parse(options.body)]);
 if(request.search==='?page=list') return Response.json({token:'fixture',page_owner:owner,recipients:[{id:owner,name:'morty'}]});
 if(options.body && JSON.parse(options.body).page_capture) return Response.json({});
 return Response.json({id:'Q1',key:'fixture-review-key'});
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
 const buttons=document.querySelectorAll('.page-heading-actions button'),button=buttons[0];
 check(buttons.length===1 && button.hasAttribute('data-owner-new'),'The owner button is the only title action.');
 const dialog=document.querySelector('#review-new-item');
 check(document.querySelectorAll('#review-new-item').length===1,'The List mounts one shared dialog.');
 const existing=document.querySelector('[data-list-item]');
 existing.querySelector('.list-toggle').click();
 const tell=recordDetails(existing).querySelector('button[value="tell"]'),form=tell.form;
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
 const createdEvent=new Promise(resolve=>document.addEventListener('review-item-created',resolve,{once:true}));
 dialog.querySelector('[type="submit"]').click();
 await createdEvent;
 check(reads===0,'The List waits for the row save before it refreshes.');
 const reply=__ROW_REPLY__;
 finishRowSave(Response.json(reply.body,{status:reply.status}));
 const row=()=>document.querySelector('[data-list-item][data-key="'+CSS.escape(created)+'"]');
 await until(document.body,row);
 check(row().hidden && !recordIsOpen(row()),'Send preserves the search and keeps the new row closed.');
 check(!new URLSearchParams(location.hash.slice(1)).has('jump'),'Send adds no jump to the address.');
 check(reads===1,'The New item save refreshes the List once after the row save ends.');
 check(!dialog.open,'Send closes the dialog.');
 const saved=requests.find(([,body])=>body?.words);
 check(saved?.[0]==='/console/review/new-item' && saved[1].recipient_id===owner && saved[1].page==='list',
       'Send uses the shared endpoint and the selected List owner.');
 const searchField=document.querySelector('#list-search');
 await new Promise(done=>{
  searchField.addEventListener('filterchange',done,{once:true});
  searchField.value='';searchField.dispatchEvent(new Event('input',{bubbles:true}));
 });
 row().querySelector('.list-toggle').click();
 check(recordIsOpen(row()),'A click opens the row.');
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
 document.body.append(result);
})();
</script>"""
        for status, body in ((200, {"item_state": "Working"}), (409, {"reason": "The row save failed."})):
            with self.subTest(status=status):
                self.browser_actions(initial, setup, probe.replace("__ROW_REPLY__", json.dumps({
                    "status": status, "body": body})))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_new_item_question_keeps_the_filters_and_the_scroll(self) -> None:
        # List item 237: a question sent from the New item dialog cleared the filters and jumped to the end.
        root = self.root / "reviews"
        for number in range(1, 25):
            item = self.add(f"Needs me item {number}", number=number)
            _ = the_list.park(MORTY.id, item.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")
            _ = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.DELIVERED}, source="morty")
        for number in range(25, 31):
            _ = self.add(f"Working item {number}", number=number)
        markup = list_page.render(root, include_counts=False)
        self.assertRegex(markup.decode(), r'data-filter-value="needs-me"[^>]*data-filter-count="24"')
        initial = page_frame.with_owner(markup, "list", MORTY, staff.StaffSnapshot((MORTY,)),
                                        new_item_dialog.render).decode()
        _ = self.add("A Working item after the send", number=31)
        fresh = list_page.render(root, include_counts=False).decode()
        setup = r"""<script>
const owner=__OWNER__,fresh=__FRESH__;
let reads=0;
window.setInterval=()=>0;
window.fetch=async (path,options={})=>{
 if(path===location.pathname){
  if(options.method==='POST') return Response.json({});
  reads++;
  return new Response(fresh);
 }
 const request=new URL(path,location.href);
 if(request.search==='?page=list') return Response.json({token:'fixture',page_owner:owner,recipients:[{id:owner,name:'morty'}]});
 if(options.body && JSON.parse(options.body).page_capture) return Response.json({});
 return Response.json({id:'Q1',key:'fixture-review-key'});
};
</script>""".replace("__OWNER__", json.dumps(MORTY.id)).replace(
            "__FRESH__", json.dumps(fresh).replace("<", r"\u003c"))
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const until=(target,test)=>new Promise(resolve=>{
  const observer=new MutationObserver(()=>{if(test()){observer.disconnect();resolve();}});
  observer.observe(target,{subtree:true,childList:true,attributes:true});
  if(test()){observer.disconnect();resolve();}
 });
 const frames=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const needsMe=()=>document.querySelector('[data-filter-value="needs-me"]');
 needsMe().click();
 const row=()=>document.querySelector('[data-list-item][data-number="18"]');
 row().querySelector('.list-toggle').click();
 const summaryTop=()=>row().querySelector('.list-row').getBoundingClientRect().top;
 scrollBy({top:summaryTop()-150,behavior:'instant'});
 await frames();
 const hash=location.hash,top=summaryTop(),scroll=scrollY;
 const params=new URLSearchParams(hash.slice(1));
 check(needsMe().getAttribute('aria-pressed')==='true' && params.get('show')==='needs-me' &&
  params.get('item')===row().dataset.key && recordIsOpen(row()) && scroll>0,
  'Needs me is on and the page is scrolled to the open row. Found '+hash+' at '+scroll+' px.');
 const dialog=document.querySelector('#review-new-item');
 document.querySelector('.page-heading-actions [data-owner-new]').click();
 const recipient=()=>dialog.querySelector('[name="recipient_id"]:checked');
 await until(dialog,recipient);
 dialog.querySelector('[name="kind"][value="question"]').click();
 dialog.querySelector('[name="include_page"]').click();
 const words=dialog.querySelector('textarea');
 words.value='Why did the check fail?';
 words.dispatchEvent(new Event('input',{bubbles:true}));
 dialog.querySelector('[type="submit"]').click();
 await until(dialog,()=>!dialog.open);
 await until(document.body,()=>document.querySelector('[data-list-item][data-number="31"]'));
 await frames();
 check(reads===1,'The question refreshes the List once. Found '+reads+' reads.');
 check(needsMe().getAttribute('aria-pressed')==='true','The Needs me chip stays on.');
 check(location.hash===hash,'The address keeps its filters. Expected '+hash+'. Found '+location.hash+'.');
 check(scrollY===scroll,'The page keeps its scroll. Expected '+scroll+' px. Found '+scrollY+' px.');
 check(recordIsOpen(row()) && summaryTop()===top,
  'The open row keeps its top. Expected '+top+' px. Found '+summaryTop()+' px.');
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
 document.body.append(result);
})();
</script>"""
        self.browser_actions(initial, setup, probe, reduced_motion=True, window_size=(1280, 720))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_new_item_retry_refreshes_without_revealing_the_saved_row(self) -> None:
        root = self.root / "reviews"
        for number in range(1, 25):
            _ = self.add(f"List item {number}", number=number)
        initial = page_frame.with_owner(list_page.render(root, include_counts=False), "list", MORTY,
                                        staff.StaffSnapshot((MORTY,)), new_item_dialog.render).decode()
        item = self.add("The first Send")
        fresh = list_page.render(root, include_counts=False).decode()
        setup = r"""<script>
const savedKey=__KEY__,owner=__OWNER__,fresh=__FRESH__,sends=[];
let pendingCapture=false;
window.setInterval=()=>0;
window.fetch=async (path,options={})=>{
 if(path===location.pathname) return new Response(fresh);
 if(new URL(path,location.href).search==='?page=list')
  return Response.json({token:'fixture',page_owner:owner,recipients:[{id:owner,name:'morty'}]});
 const payload=JSON.parse(options.body);
 if(payload.page_capture){
  pendingCapture=true;
  return new Promise(()=>{});
 }
 sends.push(payload);
 if(sends.length<=2) throw new TypeError('The response was lost.');
 return Response.json({id:'Q1',key:'fixture-review-key'});
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
 const frame=()=>new Promise(resolve=>requestAnimationFrame(resolve));
 try{
  const launcher=document.querySelector('[data-owner-new]');
  const dialog=document.querySelector('#review-new-item');
  const words=dialog.querySelector('textarea'),send=dialog.querySelector('[type="submit"]');
  const type=value=>{
   words.value=value;
   words.dispatchEvent(new Event('input',{bubbles:true}));
  };
  scrollTo({top:480,behavior:'instant'});
  await frame();
  launcher.click();
  await until(dialog,()=>dialog.querySelector('[name="recipient_id"]:checked'));
  type('The first Send');
  check(pendingCapture && !send.disabled,'Send is available while the screenshot is pending.');
  send.click();
  await until(dialog,()=>!dialog.querySelector('.new-item-error').hidden && !send.disabled);
  dialog.querySelector('[data-later-draft]').click();
  check(!words.readOnly && document.activeElement===words,'Edit a separate draft opens the words for editing.');
  type('The words for a new Send');
  dialog.querySelector('[data-later-draft]').click();
  check(words.readOnly && words.value==='The first Send','Return to Retry restores the locked words.');
  const address=location.href,top=scrollY;
  check(top>0,'The List has scrolled before the retry.');
  send.click();
  const row=()=>document.querySelector('[data-list-item][data-key="'+CSS.escape(savedKey)+'"]');
  await until(document.body,()=>row() && !row().hidden);
  await frame();
  check(sends.length===3 && sends.every(send=>JSON.stringify(send)===JSON.stringify(sends[0])),
        'Both retries use the original draft and capture key.');
  check(location.href===address && scrollY===top,'The refresh keeps the List address and scroll position.');
  check(dialog.open && words.value==='The words for a new Send' && document.activeElement===send,
        'The dialog keeps the edited words and focus.');
  dialog.querySelector('[data-close]').click();
  await frame();
  check(!dialog.open && location.href===address && scrollY===top,
        'Close keeps the List address and scroll position.');
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
 document.body.append(result);
})();
</script>"""
        self.browser_actions(initial, setup, probe, window_size=(1440, 900))

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_list_owner_button_fits_at_320_pixels(self) -> None:
        owner = replace(MORTY, name="cargo-liner")
        markup = page_frame.with_owner(list_page.render(self.root / "reviews", include_counts=False),
                                       "list", owner, staff.StaffSnapshot((owner,)), new_item_dialog.render)
        setup = f"<script>Date.now=()=>{int(NOW.timestamp() * 1000)};</script>"
        probe = """<script>
(async()=>{
 const failures=[];
 try{
  await document.fonts.ready;
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  const title=document.querySelector('.page-heading h1'), button=document.querySelector('[data-owner-new]');
  const heading=title.parentElement.getBoundingClientRect(), bounds=title.getBoundingClientRect();
  const line=parseFloat(getComputedStyle(title).lineHeight);
  if(innerWidth!==320 || Math.abs(bounds.height-line)>1){
   failures.push('The List title fits on one line at 320 px.');
  }
  const controls=[button,document.querySelector('.page-owner-name')];
  if(controls.some(control=>{
   const box=control.getBoundingClientRect();
   return box.width<=0 || box.left<bounds.right || box.right>heading.right+1 ||
          box.top<heading.top || box.bottom>heading.bottom;
  }) || bounds.height>heading.height || heading.height>button.offsetHeight+1){
   failures.push('The List controls fit beside the title at 320 px.');
  }
  if(title.parentElement.querySelector('button') ||
     document.querySelector('[data-page-actions]').querySelectorAll('button').length!==1 ||
     button.getAttribute('aria-label')!=='New item for cargo-liner'){
   failures.push('The page owner button is the only action beside the title.');
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        for in_console in (False, True):
            with self.subTest(in_console=in_console):
                self.browser_answer_visibility(markup.decode(), setup, probe, reload_console=in_console,
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
  check(button.getAttribute('aria-controls')===detail.id && !button.hasAttribute('aria-haspopup') && isExpanded(row,false),
   'The button points to the collapsed detail row.');
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
    def test_browser_closes_an_item_that_a_filter_hides(self) -> None:
        _ = self.add("Deploy alpha")
        _ = self.add("Deploy beta")
        _ = self.add("Deploy gamma", owner=OTHER.id)
        self.browser_row_expansion(r'''
 const other=rows.find(row=>row.dataset.owner!==OWNER);
 toggle(other).click();
 selectOwner(OWNER);
 const matches=rows.filter(row=>!row.hidden);
 check(matches.length===2 && other.hidden && isExpanded(other,false),'The filter closes the hidden item.');
 selectOwner('');
 check(!other.hidden && isExpanded(other,false),'Clearing the filter reveals the closed item.');
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

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_empty_results_name_the_filters_and_offer_their_actions(self) -> None:
        read = self.add("Deploy alpha")
        _ = the_list.mark_read(MORTY.id, read.id, source="nate")
        _ = self.add("Deploy beta", owner=OTHER.id)
        for search, filter_on, message, actions in (
            ("xa", False, 'No item contains "xa".', ["Clear search"]),
            ("", True, "No item matches Owner: sdlc and Unread only.", ["Show all items"]),
            ("xa", True, 'No item contains "xa" and matches Owner: sdlc and Unread only.',
             ["Clear search", "Show all items"]),
        ):
            with self.subTest(search=search, filter_on=filter_on):
                params = json.dumps({"search": search, "filterOn": filter_on, "message": message,
                                     "actions": actions, "owner": OWNER.id})
                self.browser_row_expansion("const example=" + params + ";\n" + r'''
const words=document.querySelector('#list-search'),unread=document.querySelector('#list-unread');
if(example.filterOn){selectOwner(example.owner);unread.click();}
if(example.search){
 const filtered=new Promise(resolve=>words.addEventListener('filterchange',resolve,{once:true}));
 words.value=example.search;words.dispatchEvent(new Event('input'));await filtered;
}
const empty=()=>document.querySelector('.list-table table > [data-empty-result]');
const message=()=>empty()?.querySelector('[role="status"]').textContent;
const clear=()=>empty()?.querySelector('[data-clear-empty-search]');
const reset=()=>empty()?.querySelector('[data-reset-empty-filters]');
check(message()===example.message,'The empty row names the active filters.');
check(JSON.stringify([...empty().querySelectorAll('button:not([hidden])')].map(button=>button.textContent))
 ===JSON.stringify(example.actions),'The empty row offers the correct actions.');
if(example.search){
 clear().click();
 check(!words.value && document.activeElement===words,'Clear search empties and focuses the search box.');
 check(unread.checked===example.filterOn,'Clear search preserves Unread only.');
 if(example.filterOn){
  check(chip('owner','staff:'+example.owner).getAttribute('aria-pressed')==='true'
   && message()==='No item matches Owner: sdlc and Unread only.','Clear search preserves Owner and updates the message.');
  const filtered=new Promise(resolve=>words.addEventListener('filterchange',resolve,{once:true}));
  words.value=example.search;words.dispatchEvent(new Event('input'));await filtered;
 }else check(!empty() && rows.every(row=>!row.hidden),'Clear search shows the items.');
}
if(example.filterOn){
 reset().click();
 const all=chip('owner','all');
 check(!words.value && !unread.checked && all.getAttribute('aria-pressed')==='true'
  && document.activeElement===all,'Show all items resets the filters and focuses the first filter.');
 check(!empty() && rows.every(row=>!row.hidden),'Show all items reveals every row.');
}
''')

    def studio_avatars(self, *producers: staff.StaffId) -> Path:
        """Point the Studio at a temporary avatar root with one picture for All and for each producer."""
        root = self.root / "avatars"
        for folder in ("all", *producers):
            (root / folder).mkdir(parents=True)
            for size in studio_page.SIZES:
                _ = (root / folder / f"01-{size}.webp").write_bytes(b"RIFF")
        return root

    def test_studio_shows_list_rows_and_one_set_of_producer_tabs(self) -> None:
        items = self.fixture_items()
        _ = self.studio_avatars(DWIGHT.id)
        studio = list_page.render(self.root / "reviews").decode()
        row = '<div class="record-filter-row" role="group" aria-label="Producer" data-filter-row="producer">'
        self.assertEqual(studio.count(row), 1)
        self.assertIn('<div class="studio-tab-row"><div class="studio-tabs">' + row, studio)
        controls = ('<div data-page-top-controls>', '<div class="studio-tabs">', '<div class="record-filter-words">',
                    '<div data-record-filters>', '<div class="list-top-bands">')
        positions = [studio.index(control) for control in controls]
        self.assertEqual(positions, sorted(positions))
        self.assertIn('<h1 class="page-title">The Studio</h1>', studio)
        self.assertIn('<title>The Studio</title>', studio)
        self.assertEqual(studio.count('<tbody data-list-item'), len(items) - 1)
        tab = studio.split(f'data-filter-value="staff:{DWIGHT.id}"', 1)[1].split('</button>', 1)[0]
        self.assertIn(f'src="{studio_page.AVATAR_PATH}/{DWIGHT.id}/01-128.webp"', tab)
        self.assertIn('<span class="studio-name">dwight</span> <b>2</b>', tab)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the frozen Studio filters.")
    def test_browser_studio_freezes_only_the_picture_tabs_and_search(self) -> None:
        for number in range(1, 25):
            _ = self.add(f"Check Studio item {number}", number=number)
        markup = list_page.render(self.root / "reviews", include_counts=False)
        page = page_frame.with_owner(markup, "studio", MORTY, staff.StaffSnapshot((MORTY,)), new_item_dialog.render)
        probe = b'''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const paint=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const find=selector=>document.querySelector(selector);
 const box=element=>element.getBoundingClientRect();
 const hit=element=>{
  const {left,top,width,height}=box(element);
  return element.contains(document.elementFromPoint(left+width/2,top+height/2));
 };
 try{
  frameElement.style.height='800px';
  const top=find('.page-top'), title=find('.page-heading');
  const heading=find('.list-table thead'), row=find('[data-number="20"] .list-row');
  const controls=['.studio-tabs button','#list-search'].map(find);
  const scrolling=['#filter-owner-all','#filter-show-all','#list-unread'].map(find);
  check(controls.every(control=>top.contains(control)) && scrolling.every(control=>!top.contains(control)),
   'The frozen top contains the picture tabs and Search.');
  for(const width of [1440,390]){
   frameElement.style.width=width+'px';
   scrollTo({top:0,behavior:'instant'});
   await paint();
   const first=box(row).top,headerStart=box(heading).top;
   for(const scroll of [700,980]){
    scrollTo({top:scroll,behavior:'instant'});
    await paint();
    check(innerWidth===width && scrollY===scroll && Math.abs(box(row).top-first+scroll)<1,
     width+' px: the table rows scroll under the frame.');
    check(Math.abs(box(heading).top-headerStart+scroll)<1 && scrolling.every(control=>box(control).bottom<0),
     width+' px: the Owner chips, Show chips, Unread only switch, and table header scroll with the rows.');
    check(width===1440 ? controls.every(hit) : controls.every(control=>box(control).bottom<0) && hit(title),
     width+' px: the desktop filters stay visible and the phone keeps only the title.');
   }
   row.scrollIntoView({block:'start',behavior:'instant'});
   await paint();
   check(box(row).top>=box(width===1440 ? top : title).bottom-1 && hit(row.querySelector('.list-toggle')),
    width+' px: scrolling a row into view clears the frozen top.');
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>'''
        self.assertEqual(firefox_probe_results(page.replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required to check the short Studio layout.")
    def test_browser_short_studio_keeps_rows_and_filters_reachable(self) -> None:
        producers = tuple(staff.StaffRow(identity, "producer", name, staff.StaffRoot(), current_session=identity)
                          for identity, name in ((OWNER.id, "first"), (OTHER.id, "second")))
        members = staff.StaffSnapshot((MORTY, DWIGHT, *producers))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            member.to_json() for member in members.rows]}))
        for number in range(1, 9):
            _ = self.add(f"Check Studio item {number}")
        markup = list_page.render(self.root / "reviews", include_counts=False)
        page = page_frame.with_owner(markup, "studio", MORTY, members, new_item_dialog.render)
        probe = b'''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const paint=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const box=element=>element.getBoundingClientRect();
 const hit=element=>{
  const {left,top,width,height}=box(element);
  return element.contains(document.elementFromPoint(left+width/2,top+height/2));
 };
 try{
  frameElement.style.width='800px';frameElement.style.height='320px';
  await paint();
  const filters=document.querySelector('[data-page-top-controls]');
  const tabs=filters.querySelectorAll('.studio-tabs button');
  const search=filters.querySelector('#list-search');
  const first=document.querySelector('.list-table .list-row');
  check(innerWidth===800 && innerHeight===320 && tabs.length===5 && box(search).top>=box(tabs[0]).bottom,
   'Five Producer tabs put Search on its own line at 800 x 320 px.');
  check(filters.scrollHeight>filters.clientHeight,'The filters have their own scroll area.');
  first.scrollIntoView({block:'start',behavior:'instant'});await paint();
  check(box(first).top>=box(filters.closest('.page-top')).bottom-1 && hit(first.querySelector('.list-toggle')),
   'The first row is reachable below the frozen top.');
  const pageScroll=scrollY;
  search.focus();await paint();
  check(document.activeElement===search && hit(search) && filters.scrollTop>0 && scrollY===pageScroll,
   'Focus scrolls Search within the frozen top.');
  for(const selector of ['#filter-owner-all','#filter-show-all','#list-unread']){
   const control=document.querySelector(selector);
   control.scrollIntoView({block:'start',behavior:'instant'});
   control.focus();await paint();
   check(document.activeElement===control && hit(control),'The filter takes focus and stays visible: '+control.id);
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>'''
        self.assertEqual(firefox_probe_results(page.replace(b'</body>', probe + b'</body>')), [[]])

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required to check refresh position.")
    def test_browser_studio_refresh_keeps_open_row_below_the_grown_header(self) -> None:
        root = self.root / "reviews"
        for number in range(1, 13):
            _ = self.add(f"Check Studio item {number}")
        initial = list_page.render(root, include_counts=False).decode()
        identities = [staff.StaffId(str(uuid4())) for _ in range(13)]
        producers = [staff.StaffRow(identity, "producer", f"Team {number}", staff.StaffRoot(), current_session=identity)
                     for number, identity in enumerate(identities, start=1)]
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            member.to_json() for member in (MORTY, DWIGHT, OWNER, OTHER, *producers)]}))
        fresh = list_page.render(root, include_counts=False).decode()
        for offset in (2, -8):
            with self.subTest(offset=offset):
                self.assert_studio_refresh_row_position(initial, fresh, offset, r'''
 check(later.picturesHeight>earlier.picturesHeight,'The fresh picture tabs add a line.');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required to check refresh position.")
    def test_browser_studio_refresh_keeps_open_row_below_the_wrapped_status(self) -> None:
        for number in range(1, 13):
            _ = self.add(f"Check Studio item {number}")
        page = list_page.render(self.root / "reviews", include_counts=False).decode()
        initial = page.replace("<head>", f"<head><script>Date.now=()=>{NOW.timestamp() * 1000};</script>", 1)
        reason = "The reader cannot confirm the data time. " * 14
        fresh = page.replace("data-page-facts ", f'data-page-facts data-read-error="{reason}" ', 1)
        self.assert_studio_refresh_row_position(initial, fresh, 2, r'''
 check(later.statusHeight>earlier.statusHeight && later.statusState==='Failed',
  'The fresh status shows the failure on more lines.');
 check(later.picturesHeight===earlier.picturesHeight,'The picture tabs keep their height.');
''')

    def assert_studio_refresh_row_position(self, initial: str, fresh: str, offset: int, verify_growth: str) -> None:
        steps = r'''
 const paint=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const summary=()=>document.querySelector('[data-number="6"] .list-row');
 const header=()=>document.querySelector('.page-top').getBoundingClientRect();
 const measure=()=>{
  const status=document.querySelector('[data-page-status]');
  return {headerBottom:header().bottom,row:summary().getBoundingClientRect(),statusState:status.dataset.state,
   statusHeight:status.getBoundingClientRect().height,
   picturesHeight:document.querySelector('.studio-tabs').getBoundingClientRect().height};
 };
 await paint();
 summary().scrollIntoView({block:'start',behavior:'instant'});
 await paint();
 scrollBy({top:Math.floor(summary().getBoundingClientRect().top-header().bottom-(ROW_OFFSET)),behavior:'instant'});
 await paint();
 const earlier=measure(),offset=earlier.row.top-earlier.headerBottom;
 check(earlier.row.bottom>earlier.headerBottom && Math.abs(offset-(ROW_OFFSET))<2,
  'The open row starts at the expected offset from the header: '+offset);
 refreshMarkup=FRESH_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();await paint();await paint();
 const later=measure();
 VERIFY_GROWTH
 check(later.headerBottom>earlier.headerBottom,'The refresh grows the header.');
 check(recordIsOpen(summary().parentElement) && later.row.top>=later.headerBottom-0.5,
  'The open row clears the new header: '+later.row.top+' vs '+later.headerBottom);
'''.replace('ROW_OFFSET', str(offset)).replace('VERIFY_GROWTH', verify_growth)
        steps = steps.replace('FRESH_PAGE', json.dumps(fresh).replace('</', '<\\/'))
        self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial,
                                     open_number=6, window_size=(1280, 800))

    def test_studio_lists_linked_items_with_states_and_missing_records(self) -> None:
        closed = self.add("Close this check", number=210)
        missing = self.add("Remove this fixture record", number=406)
        _ = self.add("Check <this> & that", number=413)
        _ = self.add("Check the dependency", the_list.ItemState.BLOCKED, number=414)
        _ = self.add("Ask for a decision", the_list.ItemState.WAITING_FOR_NATE, number=464)
        _ = self.add("Publish the result", the_list.ItemState.DELIVERED, number=467)
        umbrella = self.add("The workflow is consistent and simple", number=494)
        _ = the_list.update(MORTY.id, umbrella.id, {"related_to": (467, 413, 406, 210, 464, 414)}, source="morty")
        _ = the_list.close(MORTY.id, closed.id, "The check passed.", source="morty")
        (the_list.folder(MORTY.id) / f"{missing.id}.md").unlink()
        root = self.root / "reviews"
        with patch.object(list_page, "records", wraps=list_page.records) as read_records:
            markup = list_page.render(root, include_counts=False).decode()
        read_records.assert_called_once()
        self.assertTrue(read_records.call_args.kwargs["include_closed"])
        self.assertEqual(markup.count(' data-linked-items>'), 1)
        panel = markup.split('data-number="494"', 1)[1].split('</tbody>', 1)[0]
        section = panel.split('<section class="item-links" data-linked-items>', 1)[1].split('</section>', 1)[0]
        parsed = ElementTree.fromstring('<section>' + section.replace(' data-record-jump ', ' data-record-jump="" ')
                                        + '</section>')
        self.assertEqual(parsed.findtext('h3'), 'Linked items · 1 of 6 closed')
        rows = parsed.findall('ul/li')
        self.assertEqual([(row.findtext('a') or row.text, row.findtext('span')) for row in rows], [
            ('#210 Close this check', 'Closed'), ('#406 not found', None), ('#413 Check <this> & that', 'Working'),
            ('#414 Check the dependency', 'Blocked'), ('#464 Ask for a decision', 'Waiting for nate'),
            ('#467 Publish the result', 'Delivered'),
        ])
        self.assertIsNone(rows[1].find('a'))
        anchors = parsed.findall('ul/li/a')
        numbers = (210, 413, 414, 464, 467)
        self.assertEqual([anchor.attrib['href'] for anchor in anchors], [
            list_page.item_link(MORTY.id, number) + ('&reveal=1' if number == 210 else '') for number in numbers
        ])
        for number, badge in zip(numbers, parsed.findall('ul/li/span'), strict=True):
            row = markup.split(f'data-number="{number}"', 1)[1].split('</tr>', 1)[0]
            self.assertIn(ElementTree.tostring(badge, encoding='unicode'), row)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_studio_resolves_saved_alias_keys_in_filters_and_across_refresh(self) -> None:
        first = self.add("Open the first item", number=1)
        filtered = self.add("Ask for a decision", the_list.ItemState.WAITING_FOR_NATE, number=2)
        closed = self.add("Close this check", number=3)
        link = self.add("Check the links", number=4)
        _ = the_list.update(MORTY.id, link.id, {"related_to": (1, 2, 3)}, source="morty")
        _ = the_list.close(MORTY.id, closed.id, "The check passed.", source="morty")
        original = the_list.folder(MORTY.id)
        alias = staff.StaffId("000-list-alias")
        distinct = staff.StaffId("001-separate-list")
        separate = the_list.folder(distinct)
        separate.mkdir(parents=True)
        _ = (separate / "L1.md").write_text((original / "L1.md").read_text().replace(first.title, "A separate item"))

        root = self.root / "reviews"
        initial = list_page.render(root, include_counts=False).decode()
        alias_folder = the_list.folder(alias)
        alias_folder.parent.mkdir(parents=True)
        alias_folder.symlink_to(original, target_is_directory=True)
        fresh = list_page.render(root, include_counts=False).decode()

        def selected_rows(keeper: staff.StaffId) -> list[tuple[str, bool]]:
            query = urlencode([("item", f"{keeper}/{filtered.id}"), ("key", f"{keeper}/{first.id}"),
                               ("show", "working"), ("reveal", "1")])
            markup = list_page.render(root, include_counts=False, query=query).decode()
            rows = r'<tbody data-list-item data-key="([^"]+)"([^>]+)>'
            return [(row[1], row[2].endswith(" hidden")) for row in re.finditer(rows, markup)]

        selected = selected_rows(alias)
        self.assertEqual(selected_rows(MORTY.id), selected)
        self.assertIn((f"{alias}/{filtered.id}", False), selected)
        self.assertIn((f"{distinct}/L1", True), selected)
        self.assertEqual(sum(key.endswith("/L1") for key, _ in selected), 2)
        closed_body = list_page.saved_action_state(alias, closed.id, include_detail=True)
        steps = r'''
 const [real,alias,distinct]=KEEPERS;
 const item=(keeper,number)=>document.querySelector('[data-key="'+keeper+'/L'+number+'"]');
 const target=number=>item(alias,number);
 const clickLink=(keeper,number,reveal=false)=>{
  const link=document.createElement('a');
  link.dataset.recordJump='';
  link.href='#item='+encodeURIComponent(keeper+'/L'+number)+(reveal ? '&reveal=1' : '');
  const group=document.createElement('div');
  group.dataset.linkedItems='';
  group.append(link);
  document.querySelector('[data-list]').append(group);
  link.click();
  group.remove();
 };
 check(recordIsOpen(item(real,4)),'The item is open before the alias becomes the displayed key.');
 refreshMarkup=FRESH;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();await flush();
 check(recordIsOpen(target(4)),'Refresh keeps the open item when its displayed keeper changes.');
 clickLink(real,1);
 check(recordIsOpen(target(1)) && !recordIsOpen(item(distinct,1)),'The saved alias opens only its physical row.');
 show('working');
 check(target(2).hidden,'The Working filter hides the target.');
 clickLink(alias,2);
 check(recordIsOpen(target(2)) && !target(2).hidden,'The displayed alias opens its filtered target.');
 show('working');
 check(target(2).hidden,'The Working filter hides the target.');
 clickLink(real,2);
 check(recordIsOpen(target(2)) && !target(2).hidden,'The saved alias opens its filtered target.');
 itemSnapshots.set(target(3).dataset.key,CLOSED);
 for(const keeper of [real,alias]){
  clickLink(keeper,3,true);
  await flush();
  check(recordIsOpen(target(3)) && !target(3).hidden,'The closed link opens through '+keeper+'.');
 }
'''
        steps = (steps.replace("KEEPERS", json.dumps([MORTY.id, alias, distinct]))
                 .replace("FRESH", json.dumps(fresh).replace("</", "<\\/"))
                 .replace("CLOSED", json.dumps(closed_body).replace("</", "<\\/")))
        self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial, open_number=4)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_studio_linked_items_open_and_refresh_with_the_page(self) -> None:
        closed = self.add("Close a linked item after the page shows the full title across several lines "
                          + "and keeps its state in the same column as every other linked item", number=210)
        open_item = self.add("Keep a linked item open", number=413)
        waiting = self.add("Ask for a decision", the_list.ItemState.WAITING_FOR_NATE, number=464)
        umbrella = self.add("The workflow is consistent and simple", number=494)
        _ = the_list.update(MORTY.id, umbrella.id, {"related_to": (open_item.number, closed.number, waiting.number)},
                            source="morty")
        root = self.root / "reviews"
        initial = list_page.render(root, include_counts=False).decode()
        _ = the_list.close(MORTY.id, closed.id, "The check passed.", source="morty")
        fresh = list_page.render(root, include_counts=False).decode()
        closed_detail = list_page.saved_action_state(MORTY.id, closed.id, include_detail=True)
        steps = r'''
 const item=number=>document.querySelector('[data-number="'+number+'"]');
 const links=()=>recordDetails(item(494)).querySelector('[data-linked-items]');
 const states=()=>[...links().querySelectorAll('.list-state')].map(badge=>badge.textContent).join(',');
 const heading=()=>links().querySelector('h3').textContent;
 const back=async()=>{
  const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  history.back();await changed;
 };
 check(states()==='Working,Working,Waiting for nate','The section shows the states from the first page.');
 check(heading()==='Linked items · 0 of 3 closed','The heading counts all links and no Closed items.');
 const [longTitle,shortTitle]=links().querySelectorAll('a');
 check(longTitle.offsetHeight>shortTitle.offsetHeight,'The long title wraps.');
 const titleStyle=getComputedStyle(longTitle);
 const rowStyle=getComputedStyle(item(494).querySelector('.list-row [data-label="Item"] strong'));
 check(titleStyle.color===rowStyle.color && titleStyle.fontWeight==='400' && titleStyle.textDecorationLine==='none',
  'The link uses the row text color and regular weight without an underline at rest.');
 refreshMarkup=FRESH_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 check(states()==='Closed,Working,Waiting for nate' && pageReads===1,'One page refresh updates the state badges.');
 check(heading()==='Linked items · 1 of 3 closed','The heading counts the Closed item after refresh.');
 const sectionRight=links().getBoundingClientRect().right;
 const badges=[...links().querySelectorAll('.list-state')].map(badge=>badge.getBoundingClientRect());
 check(badges.every(badge=>badge.left===badges[0].left),
  'Badges with different widths start in the same column.');
 check(badges.every(badge=>sectionRight-badge.right>=8),'Each badge has at least 8 px to the section edge.');
 check([...links().querySelectorAll('li')].every(row=>row.getBoundingClientRect().right===sectionRight),
  'The row dividers reach the section edge.');
 const count=document.querySelector('[data-list-count]').textContent;
 itemSnapshots.set(item(210).dataset.key,CLOSED_DETAIL);
 links().querySelectorAll('a')[0].click();
 const closed=item(210);
 check(recordIsOpen(closed) && !closed.hidden && closed.closest('[data-linked-item]'),
  'The closed item link opens its detail outside the results.');
 await flush();
 check(!recordDetails(closed).querySelector('form') &&
  recordDetails(closed).textContent.includes('The check passed.'),'The closed detail shows the log and no actions.');
 check(document.querySelector('[data-list-count]').textContent===count,'The closed item does not change the counts.');
 await back();
 check(recordIsOpen(item(494)) && closed.hidden,'Back restores the umbrella and hides the closed item.');
 links().querySelectorAll('a')[1].click();
 const target=item(OPEN_NUMBER);
 check(recordIsOpen(target) && !target.hidden && !recordIsOpen(item(494)) &&
  new URLSearchParams(location.hash.slice(1)).get('item')===target.dataset.key,'The open item link opens its item.');
'''
        steps = (steps.replace('FRESH_PAGE', json.dumps(fresh).replace('</', '<\\/'))
                 .replace('CLOSED_DETAIL', json.dumps(closed_detail).replace('</', '<\\/'))
                 .replace('OPEN_NUMBER', str(open_item.number)))
        for width in (1440, 390):
            with self.subTest(width=width):
                self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial,
                                             open_number=494, window_size=(width, 900))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_snapshot_removes_the_last_link_after_an_action(self) -> None:
        target = self.add("Check the linked item", number=1)
        source = self.add("Check the links", number=2)
        source = the_list.update(MORTY.id, source.id, {"related_to": (target.number,)}, source="morty")
        root = self.root / "reviews"
        initial = list_page.render(root, include_counts=False).decode()
        source = the_list.update(MORTY.id, source.id, {"related_to": ()}, source="morty")
        _, action = list_page.save_action(root, urlencode(self.fields(source, "read")),
                                          include_detail=True, top_priority=list_page.TopPriority.SHOWN)
        assert action is not None
        fresh = list_page.render(root, include_counts=False).decode()
        steps = r'''
 const linked=()=>recordDetails(row).querySelector('[data-linked-items]');
 check(linked()?.textContent.includes('#1 Check the linked item'),'The first snapshot shows the link.');
 row.closest('[data-list]').dataset.undoEnabled='false';
 const fetchAction=window.fetch;
 let releasePage,requestPage;
 const requested=new Promise(resolve=>{requestPage=resolve;});
 window.fetch=(path,options)=>options.method==='POST' ? fetchAction(path,options) : new Promise(resolve=>{
  releasePage=()=>resolve(new Response(FRESH_PAGE));requestPage();
 });
 submit('read');finishPost(ACTION_RESULT);
 await requested;
 check(row.dataset.unread==='false' && linked(),'The action keeps the previous link until the full snapshot.');
 const before=row.closest('[data-list]');
 const refreshed=new Promise(resolve=>{
  const observer=new MutationObserver(()=>{if(!before.isConnected){observer.disconnect();resolve();}});
  observer.observe(before.parentElement,{childList:true});
 });
 releasePage();await refreshed;
 check(recordIsOpen(row) && !linked(),'The full snapshot removes the last related link.');
'''.replace('FRESH_PAGE', json.dumps(fresh).replace('</', '<\\/'))
        self.browser_action_sequence(steps.replace('ACTION_RESULT', json.dumps(action).replace('</', '<\\/')),
                                     reduced_motion=True, add_item=False, markup=initial, open_number=2)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_closed_link_opens_its_closed_target_on_demand(self) -> None:
        leaf = self.add("Check the last result", number=1)
        middle = self.add("Check the middle result", number=2)
        source = self.add("Check the first result", number=3)
        _ = the_list.update(MORTY.id, middle.id, {"related_to": (leaf.number,)}, source="morty")
        _ = the_list.update(MORTY.id, source.id, {"related_to": (middle.number,)}, source="morty")
        _ = the_list.close(MORTY.id, leaf.id, "The leaf log is complete.", source="morty")
        _ = the_list.close(MORTY.id, middle.id, "The middle log is complete.", source="morty")
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        bodies = [list_page.saved_action_state(MORTY.id, item.id, include_detail=True) for item in (leaf, middle)]
        steps = r'''
 const item=number=>document.querySelector('[data-number="'+number+'"]');
 const closedReads=[];
 const fetchPage=window.fetch;
 window.fetch=(path,options)=>{
  const id=new URL(path,location.href).searchParams.get('item-state');
  if(id) closedReads.push(id);
  return fetchPage(path,options);
 };
 const [leaf,middle]=[item(1),item(2)];
 const bodies=CLOSED_BODIES;
 itemSnapshots.set(leaf.dataset.key,bodies[0]);itemSnapshots.set(middle.dataset.key,bodies[1]);
 check(!closedReads.length && !recordDetails(middle).querySelector('.list-log') &&
  !recordDetails(leaf).querySelector('.list-log'),'The snapshot keeps the Closed summaries without either log.');
 recordDetails(row).querySelector('[data-linked-items] a').click();
 await flush();
 const middleDetail=recordDetails(middle),link=middleDetail.querySelector('[data-linked-items] a');
 check(closedReads.join()==='L2' && middleDetail.textContent.includes('The middle log is complete.') &&
  !middleDetail.querySelector('form'),'Opening the middle item reads only its read-only detail.');
 check(link?.textContent==='#1 Check the last result' &&
  link.nextElementSibling?.textContent==='Closed','The middle item shows the Closed target and its title.');
 link.click();await flush();
 check(recordIsOpen(leaf) && !leaf.hidden && recordDetails(leaf).textContent.includes('The leaf log is complete.') &&
  !recordDetails(leaf).querySelector('form') && closedReads.join()==='L2,L1','The target opens and reads its own log.');
'''.replace('CLOSED_BODIES', json.dumps(bodies).replace('</', '<\\/'))
        self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=markup, open_number=3)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_studio_tab_filters_the_rows_to_its_producer(self) -> None:
        _ = self.fixture_items()
        _ = self.studio_avatars(DWIGHT.id)
        self.browser_row_expansion(r'''
 const tab=chip('producer','staff:'+DWIGHT);
 check(tab.closest('.studio-tabs') && tab.querySelector('img[data-keep-src]'),'The producer filter is a picture tab.');
 const others=rows.filter(row=>!row.hidden && row.dataset.producer!==DWIGHT);
 tab.click();
 const shown=rows.filter(row=>!row.hidden);
 check(shown.length && shown.every(row=>row.dataset.producer===DWIGHT),'The tab shows only its producer rows.');
 check(new URLSearchParams(location.hash.slice(1)).get('producer')===DWIGHT,'The fragment names the producer.');
 chip('producer','all').click();
 check(others.length && others.every(row=>!row.hidden),'All shows the other producer rows again.');
 const initial=chip('producer','staff:'+MORTY);
 check(initial.querySelector('.studio-initial[aria-hidden="true"]'),'A producer without a picture shows the initial.');
 initial.click();
 check(JSON.stringify(activeChipLabels(document,'[data-filter-row="producer"] [aria-pressed="true"]'))
  ==='["Producer: morty"]','The filter label omits the initial.');
'''.replace('DWIGHT', json.dumps(DWIGHT.id)).replace('MORTY', json.dumps(MORTY.id)))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_studio_refresh_keeps_the_pictures_and_updates_the_tab_counts(self) -> None:
        _ = self.add("Test List action retries", number=1)
        avatars = self.studio_avatars(DWIGHT.id)
        for folder in ("all", DWIGHT.id):
            for size in studio_page.SIZES:
                _ = (avatars / folder / f"02-{size}.webp").write_bytes(b"RIFF")

        def page(render_seconds: str) -> str:
            return re.sub(r'data-render-seconds="[^"]+"', f'data-render-seconds="{render_seconds}"',
                          list_page.render(self.root / "reviews", include_counts=False).decode(), count=1)

        with patch.object(studio_page, "choice", side_effect=min):
            initial = page("0.20").replace("<head>", f"<head><script>Date.now=()=>{NOW.timestamp() * 1000};</script>", 1)
        added = self.add("A new item for dwight", number=2)
        _ = the_list.update(MORTY.id, added.id, {"producer": DWIGHT.id}, source="morty")
        with patch.object(studio_page, "choice", side_effect=max):
            fresh = page("0.76")
        self.assertIn(f'src="{studio_page.AVATAR_PATH}/{DWIGHT.id}/02-128.webp"', fresh)
        steps = r'''
 const tab=value=>document.querySelector('[data-filter-row="producer"] [data-filter-value="'+value+'"]');
 const values=['all','staff:'+DWIGHT];
 const pictures=()=>JSON.stringify(values.map(value=>{
  const picture=tab(value).querySelector('img');
  return [picture.getAttribute('src'),picture.getAttribute('srcset')];
 }));
 const shown=pictures();
 const frozen=document.querySelector('.page-top');
 const status=frozen.querySelector('[data-page-status]');
 const renderTime=status.querySelector('[data-status-render]');
 check(!renderTime.hidden && renderTime.textContent==='render 0.20 s','The first status shows its render time.');
 check(shown.includes('/01-128.webp') && !shown.includes('/02-'),'The first render shows picture 01.');
 check(tab('staff:'+DWIGHT).dataset.filterCount==='0' && tab('all').dataset.filterCount==='1',
  'The first render counts one item.');
 refreshMarkup=FRESH_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(()=>requestAnimationFrame(resolve))));
 check(document.querySelector('[data-number="2"]'),'The refresh shows the new row.');
 check(frozen===document.querySelector('.page-top') && frozen.contains(status) && !renderTime.hidden &&
  renderTime.textContent==='render 0.76 s','The retained status displays the fresh render time.');
 check(pictures()===shown,'The refresh keeps each picture src and srcset on screen.');
 check(tab('staff:'+DWIGHT).dataset.filterCount==='1' && tab('staff:'+DWIGHT).querySelector('b').textContent==='1'
  && tab('all').dataset.filterCount==='2','The refresh updates the tab counts.');
'''.replace('DWIGHT', json.dumps(DWIGHT.id)).replace('FRESH_PAGE', json.dumps(fresh).replace('</', '<\\/'))
        self.browser_action_sequence(steps, add_item=False, markup=initial)

    def test_studio_marks_the_open_top_item_of_each_producer(self) -> None:
        items = self.fixture_items()
        _ = self.studio_avatars(DWIGHT.id)
        status, latest, removed = items[1], items[2], items[5]
        _ = staff.set_producer_top_item(DWIGHT.id, staff.TopItem(status.number))
        # A Closed item reads as no top item.
        _ = staff.set_producer_top_item(MORTY.id, staff.TopItem(removed.number))
        studio = list_page.render(self.root / "reviews").decode()
        rows: dict[int, str] = {}
        for part in studio.split('<tbody data-list-item')[1:]:
            row = part.split('</tbody>', 1)[0]
            number = re.search(r'data-number="(\d+)"', row)
            assert number is not None
            rows[int(number[1])] = row
        self.assertEqual([number for number, row in rows.items() if 'class="list-top-mark"' in row], [status.number])
        self.assertIn('<span class="list-top-mark" role="img" aria-label="Top priority" title="Top priority"><svg',
                      rows[status.number])
        self.assertNotIn('value="top"', rows[status.number])
        self.assertIn('<button name="action" value="top">Make top priority</button><div class="list-parking-actions">',
                      rows[latest.number])
        self.assertLess(studio.index('<div class="studio-tabs">'), studio.index('<div class="list-top-bands">'))
        bands = studio.split('<div class="list-top-bands">', 1)[1].split('<div data-record-filters>', 1)[0]
        dwight = bands.split(f'<section class="list-top-band" data-top-band="staff:{DWIGHT.id}" aria-label="Top priority">',
                             1)[1].split('</section>', 1)[0]
        top_link = html.escape(list_page.item_link(MORTY.id, status.number))
        self.assertIn(f'<a class="list-top-link" data-record-jump href="{top_link}">#{status.number} {status.title}</a>',
                      dwight)
        self.assertIn("Read the result and check the page.", dwight)
        self.assertIn('<span class="list-state accent">Working</span><small>ETA ', dwight)
        self.assertNotIn(f'data-top-band="staff:{MORTY.id}"', bands)
        self.assertIn('<section class="list-top-band" data-top-band="none" aria-label="Top priority">', bands)
        self.assertIn('<b>No top priority item</b><div class="list-top-next">Open an item and click "Make top priority".',
                      bands)
        self.assertIn(f':not([data-filter-value="all"],[data-filter-value="staff:{DWIGHT.id}"])) '
                      + '[data-top-band="none"]{display:flex}', bands)
        # A retired producer has no top item.
        _ = staff.set_producer_retirement(DWIGHT.id, staff.RetiredProducer("claude:sonnet:high"))
        retired = list_page.render(self.root / "reviews").decode()
        self.assertNotIn('class="list-top-mark"', retired)

    def test_studio_waiting_item_has_the_make_top_priority_button(self) -> None:
        _ = self.studio_avatars(MORTY.id)
        item = self.add("Wait for an answer", number=1)
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        with patch.object(change_page, "send_question", return_value=question_outcome()):
            list_page.deliver(list_page.submit(root, urlencode(
                self.fields(item, "question", words="Which check passed?")), status))

        def waiting_row() -> str:
            studio = list_page.render(root, include_counts=False, status=status).decode()
            return studio.split(f'data-number="{item.number}"', 1)[1].split('</tbody>', 1)[0]

        self.assertIn('<form method="post" data-feedback-form data-waiting-actions>', waiting_row())
        self.assertIn('<div class="list-actions"><button name="action" value="top">Make top priority</button>'
                      + '<div class="list-parking-actions">', waiting_row())
        _ = staff.set_producer_top_item(MORTY.id, staff.TopItem(item.number))
        top = waiting_row()
        self.assertIn('class="list-top-mark"', top)
        self.assertNotIn('value="top"', top)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_studio_band_follows_the_tab_and_make_top_priority_moves_the_flag(self) -> None:
        _ = self.studio_avatars(MORTY.id, DWIGHT.id)
        first = self.add("First item", number=1)
        second = self.add("Second item", number=2)

        def page() -> str:
            return list_page.render(self.root / "reviews", include_counts=False).decode()

        _ = staff.set_producer_top_item(MORTY.id, staff.TopItem(second.number))
        initial = page()
        _ = staff.set_producer_top_item(MORTY.id, staff.TopItem(first.number))
        fresh = page()
        steps = r'''
 const flagged=()=>JSON.stringify([...document.querySelectorAll('[data-list-item]')]
  .filter(item=>item.querySelector('.list-top-mark')).map(item=>item.dataset.number));
 const shown=()=>[...document.querySelectorAll('[data-top-band]')].filter(band=>getComputedStyle(band).display!=='none');
 const bands=()=>JSON.stringify(shown().map(band=>band.dataset.topBand));
 const tab=value=>document.querySelector('[data-filter-row="producer"] [data-filter-value="'+value+'"]');
 check(flagged()==='["2"]' && bands()==='[]','The flag marks item 2 on the All tab, and All shows no band.');
 check(form.querySelector('[value="top"]')?.textContent==='Make top priority','Item 1 has the Make top priority button.');
 const listFetch=window.fetch;
 window.fetch=(path,options={})=>{
  if(options.method==='POST') check(path===STUDIO_PATH,'Studio posts to its own path.');
  return listFetch(path,options);
 };
 refreshMarkup=FRESH_PAGE;
 submit('top');
 check(requests.at(-1).get('action')==='top','The button posts the top action.');
 finishPost({state:'saved',unread:'false'});
 for(let tries=0;tries<200 && flagged()!=='["1"]';tries++) await new Promise(resolve=>setTimeout(resolve,10));
 check(flagged()==='["1"]','The refresh moves the flag to item 1.');
 tab('staff:'+MORTY).click();
 check(bands()===JSON.stringify(['staff:'+MORTY]) && shown()[0].textContent.includes('#1 First item'),
  'The producer tab shows its top item band.');
 const topLink=()=>shown()[0]?.querySelector('.list-top-link');
 const refreshTop=async markup=>{
  const old=document.querySelector('[data-list]');
  refreshMarkup=markup;
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  for(let tries=0;tries<200 && old.isConnected;tries++) await flush();
  check(!old.isConnected,'Studio applied the refreshed band.');
 };
 const focused=topLink();
 focused.focus();
 await refreshTop(FRESH_PAGE);
 check(focused!==topLink() && document.activeElement===topLink(),'Refresh restores focus to the same top item.');
 await refreshTop(INITIAL_PAGE);
 check(document.activeElement.id==='list-search','A different top item sends focus to Search.');
 tab('staff:'+DWIGHT).click();
 check(bands()==='["none"]' && shown()[0].textContent.includes('No top priority item'),
  'A producer without a top item shows the empty band.');
 tab('all').click();
 check(bands()==='[]','The All tab hides the band.');
'''.replace('MORTY', json.dumps(MORTY.id)).replace('DWIGHT', json.dumps(DWIGHT.id))
        steps = steps.replace('STUDIO_PATH', json.dumps(studio_page.PAGE_PATH))
        steps = steps.replace('FRESH_PAGE', json.dumps(fresh).replace('</', '<\\/'))
        steps = steps.replace('INITIAL_PAGE', json.dumps(initial).replace('</', '<\\/'))
        self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_studio_top_priority_link_opens_the_item(self) -> None:
        item = self.add("Check the top item", number=264)
        _ = self.add("Check the other item", owner=OTHER.id, number=265)
        _ = staff.set_producer_top_item(MORTY.id, staff.TopItem(item.number))
        page = self.root / "studio.html"
        _ = page.write_bytes(list_page.render(self.root / "reviews", include_counts=False))
        browser = self.chrome()
        session = browser.session
        _ = browser.command("Emulation.setEmulatedMedia",
                            {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]}, session)
        browser.navigate(page.as_uri())

        def evaluate(expression: str) -> object:
            response = browser.command("Runtime.evaluate",
                                       {"expression": expression, "awaitPromise": True, "returnByValue": True}, session)
            self.assertNotIn("exceptionDetails", response, response)
            return cast(dict[str, object], response["result"]).get("value")

        link = "document.querySelector('.list-top-link')"
        row = f"document.querySelector('[data-number=\"{item.number}\"]')"
        opened = f"recordIsOpen({row}) && !{row}.hidden"
        tab = f'[data-filter-row="producer"] [data-filter-value="staff:{MORTY.id}"]'
        _ = evaluate(f"document.querySelector({json.dumps(tab)}).click()")
        _ = evaluate(f"""(() => {{
 window.addEventListener('click',event=>{{
  window.followed=event.defaultPrevented;
  event.preventDefault();
 }},{{once:true}});
 {link}.dispatchEvent(new MouseEvent('click',{{bubbles:true,cancelable:true,ctrlKey:true}}));
}})()""")
        self.assertIs(evaluate("window.followed"), False)
        self.assertIs(evaluate(opened), False)
        _ = evaluate(f"{link}.click()")
        self.assertIs(evaluate(opened), True)
        back = "new Promise(resolve=>{addEventListener('popstate',resolve,{once:true});history.back();})"
        _ = evaluate(back)
        self.assertIs(evaluate(opened), False)
        _ = evaluate("document.getElementById('list-unread').focus()")
        for key, code in (("Tab", 9), ("Enter", 13)):
            if key == "Enter":
                self.assertIs(evaluate(f"document.activeElement==={link}"), True)
            fields = {"key": key, "code": key, "windowsVirtualKeyCode": code}
            _ = browser.command("Input.dispatchKeyEvent", {**fields, "type": "rawKeyDown"}, session)
            _ = browser.command("Input.dispatchKeyEvent", {**fields, "type": "keyUp"}, session)
        self.assertIs(evaluate(opened), True)
        _ = evaluate(back)
        owner = f'[data-filter-row="owner"] [data-filter-value="staff:{OTHER.id}"]'
        _ = evaluate(f"document.querySelector({json.dumps(owner)}).click()")
        self.assertIs(evaluate(f"{row}.hidden"), True)
        _ = evaluate(f"{link}.click()")
        self.assertIs(evaluate(opened), True)
        self.assertEqual(evaluate("document.getElementById('filter-owner-all').getAttribute('aria-pressed')"), "true")
        self.assertEqual(evaluate("new URLSearchParams(location.hash.slice(1)).get('item')"), f"{MORTY.id}/{item.id}")
        place = cast(str, evaluate("location.href"))
        browser.navigate("about:blank")
        browser.navigate(place)
        self.assertIs(evaluate(opened), True)

    def browser_row_expansion(self, steps: str) -> None:
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        probe = r'''<script>
(async()=>{
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
const rows=[...document.querySelectorAll('[data-list-item]')];
const chip=(group,value)=>document.querySelector('[data-filter-row="'+group+'"] [data-filter-value="'+value+'"]');
const selectOwner=value=>chip('owner',value ? 'staff:'+value : 'all').click();
const toggle=row=>row.querySelector('.list-toggle');
const isExpanded=(row,expanded)=>recordIsOpen(row)===expanded && row.querySelector('[data-detail]').hidden===!expanded;
try{
''' + steps + '''
}catch(error){failures.push(String(error));}
const result=document.createElement('p');
result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, "", probe)

    def test_new_item_opens_above_the_review_box_and_its_screenshot_shows_the_box(self) -> None:
        from scripts.review_links.test_page_frame import WINDOW_BROWSERS, WINDOW_PROBE, window_probe_results

        if not WINDOW_BROWSERS:
            self.skipTest("Chrome or Firefox is required for the window rule.")
        source = review_items.ChangeSource(self.root / "changes.md", {"#": "C1"},
            change_routing.ChangeAssignment(change_routing.ShowrunnerOwnChange(OWNER.id, OWNER.name), self.root / "sdlc.md"),
            delivery_state.DeliverySlots(delivery_state.NoDelivery(), delivery_state.NoDelivery()))
        change = review_items.ReviewItem("sdlc/C1", "C1", review_items.ReviewType.CHANGE, OWNER.id, OWNER.id,
            "Check C1", review_items.ReviewState.OPEN, review_items.ReviewTimes(NOW, NOW), (), (), source)
        item = self.add("Check the linked change", number=7)
        _ = the_list.link(MORTY.id, item.id, (change.key,), source="morty")
        with (patch.object(list_page, "read_linked_reviews", return_value=[change]),
              patch.object(list_page, "counts_strip", return_value="")):
            markup = list_page.render(self.root / "reviews")
        page = page_frame.with_owner(markup, "studio", MORTY, staff.StaffSnapshot((MORTY,)), new_item_dialog.render)
        probe = r'''
(async()=>{
 if(nested) return;
 try{
  await painted();
  const dialog=document.querySelector('[data-list-review-dialog]');
  const link=document.querySelector('a[data-list-review-card]');
  link.click();
  await until(()=>dialog.open && dialog.querySelector('iframe'),'the review box');
  await painted();
  check(onTop(dialog.querySelector('.list-review-close')),'The action area does not cover the review box Close button.');
  check(dialog.getBoundingClientRect().top>=actionsBottom(),'The review box starts below the action area.');
  check(pressNewItem('over the review box')!==null,'The press reaches New item.');
  const capture=await newItemOver(dialog,'the review box');
  const address=location.pathname+location.search+location.hash;
  check(capture.place===address+(location.hash ? '&' : '#')+'console-window='+encodeURIComponent(link.getAttribute('href')),
   'The screenshot address names the open review box: '+capture.place);
  await closeNewItem(dialog,'the review box');
  await opensAgain(capture.place,doc=>doc.querySelector('[data-list-review-dialog]')?.open===true
   && doc.querySelector('[data-list-review-dialog] iframe')!==null,'the review box');
  dialog.querySelector('.list-review-close').click();
  await until(()=>!dialog.open,'the review box to close');
  frameElement.style.width='390px';
  const spacer=document.createElement('div');
  spacer.style.height='1000px';
  document.querySelector('.page-top').after(spacer);
  scrollTo({top:800,behavior:'instant'});
  await painted();
  const original=document.querySelector('[data-page-actions] [data-owner-new]');
  check(innerWidth===390 && scrollY>=500 && original.getBoundingClientRect().bottom<0,
   'At 390 px the page scrolls past the original New item button.');
  link.click();
  await until(()=>dialog.open && dialog.hasAttribute('data-fullscreen'),'the phone review box');
  await painted();
  const copy=dialog.querySelector('[data-page-window-actions] [data-owner-new]');
  const box=copy.getBoundingClientRect();
  check(copy.closest('[data-page-owner]').dataset.pageOwner==='studio','The copy uses the Studio owner.');
  check(box.top>=0 && box.bottom<=innerHeight && onTop(copy),'The New item copy is clickable on the phone.');
  check(dialog.getBoundingClientRect().top>=box.bottom-1,'The phone review box starts below the New item copy.');
  copy.click();
  await until(()=>newItem().matches(':modal'),'New item to open over the phone review box');
  check(dialog.matches(':modal') && onTop(newItem()),'New item opens above the phone review box.');
 }catch(error){failures.push(String(error));}
 await report();
})();'''
        page = page.replace(b"</body>", b"<script>" + (WINDOW_PROBE + probe).encode() + b"</script></body>", 1)
        card = b'<!doctype html><html data-theme="dark"><body><p>Review card fixture</p></body></html>'
        # The probe starts a private browser. Close the idle shared browsers first.
        for cached in (self._chrome, self._firefox):
            if cached is not None:
                cached.close()
        for browser in WINDOW_BROWSERS:
            with self.subTest(browser=browser):
                self.assertEqual(window_probe_results({studio_page.PAGE_PATH: page, review_page.PAGE_PATH: card},
                                                      studio_page.PAGE_PATH, browser), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_linked_review_send_blocks_a_second_form(self) -> None:
        from scripts.review_links.test_page_frame import window_probe_results

        root = self.root / "reviews"
        doc = self.root / "sdlc.md"
        listing = self.root / f"{change_review.PREFIX}sdlc.md"
        _ = doc.write_text(f'# Production\n- **Showrunner checkout:** `{self.root}`\n- **Change review:** `{listing}`\n')
        _ = listing.write_text(change_review.BODY.replace("\n## Reviewed",
            f"| C1 | Check the change | {NOW.date().isoformat()} | Read the result. |\n\n## Reviewed"))
        key = "sdlc/C1"
        item = self.add("Check the linked review")
        _ = the_list.link(MORTY.id, item.id, (key,), source="morty")
        members = staff.StaffSnapshot((MORTY, replace(OWNER, doc=staff.LiveDoc(local_machine(), doc))))
        with (patch.object(staff, "snapshot", return_value=members),
              patch.object(change_review, "lists_directory", return_value=self.root)):
            snapshot = review_page.build_snapshot(root)
            page = list_page.render(root, include_counts=False, review_records=snapshot.records)
            card = snapshot.body(key, view=review_page.ReviewBodyView.CARD)
        probe = br'''<script>
(async()=>{
 const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
 const dialog=document.querySelector('[data-list-review-dialog]');
 const row=document.querySelector('[data-list-item]');
 const link=row.querySelector('a[data-list-review-card]');
 const until=(node,ready)=>new Promise((resolve,reject)=>{
  const observer=new MutationObserver(finish);
  const timer=setTimeout(()=>{observer.disconnect();reject(new Error('The card or toast did not appear.'));},5000);
  function finish(){
   if(ready()){observer.disconnect();clearTimeout(timer);resolve();}
  }
  observer.observe(node,{attributes:true,childList:true,subtree:true});finish();
 });
 try{
  link.click();
  const frame=dialog.querySelector('iframe');
  await new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
  const view=frame.contentWindow,card=frame.contentDocument,form=card.querySelector('[data-review-form]');
  const toast=()=>top.document.querySelector('[data-send-undo]');
  const hold='a'.repeat(24);
  view.fetch=async(url)=>{
   if(url.endsWith('/undo')) return view.Response.json({outcome:'undone'});
   if(url.endsWith('/'+hold)) return view.Response.json({reason:'The test save failed.'},{status:503});
   return view.Response.json({hold,seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
  };
  const words='Keep these words.';
  form.elements.feedback.value=words;
  for(const undo of [true,false]){
   form.requestSubmit(form.querySelector('[value="feedback"]'));
   await until(top.document.body,toast);
   link.click();
   if(dialog.open || dialog.querySelectorAll('iframe').length!==1)
    throw new Error('The pending link must leave the original form closed.');
   if(undo) toast().querySelector('button').click();
   else toast().getAnimations()[0].finish();
   await until(dialog,()=>dialog.open && !frame.hasAttribute('data-saving'));
   check(dialog.querySelector('iframe:not([hidden])')===frame && frame.contentDocument===card,
    'The original form returns.');
   check(form.elements.feedback.value===words,'The original words remain.');
   const message=undo ? 'Undone. Edit and send again.' : 'Not saved: The test save failed.';
   check(card.querySelector('[data-review-card-notice]').textContent===message,'The form shows the result.');
  }
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>'''
        page = page.replace(b"</body>", probe + b"</body>", 1)
        if self._chrome is not None:
            self._chrome.close()
        self.assertEqual(window_probe_results(
            {studio_page.PAGE_PATH: page, review_page.PAGE_PATH: card}, studio_page.PAGE_PATH, "firefox"), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_review_links_open_one_card_without_leaving_the_list(self) -> None:
        if self._chrome is not None:
            self._chrome.close()
        root = self.root / "reviews"
        doc = self.root / "sdlc.md"
        listing = self.root / f"{change_review.PREFIX}sdlc.md"
        _ = doc.write_text(f'# Production\n- **Showrunner checkout:** `{self.root}`\n- **Change review:** `{listing}`\n')
        now = datetime.now(UTC)
        title = "Check the complete title of a linked review that opens in the List with its own header and controls"
        instructions = "Open the linked change." + " Read each field before you answer." * 36
        tall_title = [f"Title line {line}" for line in range(1, 13)]
        short_title = "Check the `status` field."
        check_url = "https://example.com/check"
        original = (change_review.BODY.replace("\n## Reviewed",
            f"| C1 | {title} | {now.date().isoformat()} | Open the linked change. |\n"
            + f"| C3 | {'<br>'.join(tall_title)} | {now.date().isoformat()} | Open the card. |\n"
            + f"| C4 | {short_title} | {now.date().isoformat()} | {short_title} |\n"
            + f"| C5 | {check_url} | {now.date().isoformat()} | {check_url} |\n\n## Reviewed")
            + "| C2 | A completed change | works | 2026-11-15 |\n")
        members = staff.StaffSnapshot((MORTY, DWIGHT, replace(OWNER, doc=staff.LiveDoc(local_machine(), doc)), OTHER))
        item = self.add("Check the linked reviews", number=111)
        _ = the_list.link(MORTY.id, item.id, ("sdlc/C1", "sdlc/C2"), source="morty")
        extra = self.add("Check the linked reviews at short heights", number=112)
        _ = the_list.link(MORTY.id, extra.id, ("sdlc/C3", "sdlc/C4", "sdlc/C5"), source="morty")
        waiting = investigation.file(OWNER.id, "Check the waiting review", "Check the waiting review",
                                     waiting_why="The owner checks the error.")
        waiting_key = f"investigation/{waiting.owner}/{waiting.id}"
        waiting_item = self.add("Check the linked reviews that are waiting", number=113)
        _ = the_list.link(MORTY.id, waiting_item.id, (waiting_key,), source="morty")
        settings = self.root / "config/review_links.conf"
        overlay_settings = settings.read_text()
        for key, value in (("page_request_timeout_seconds", 7), ("card_fade_out_ms", 180), ("card_fade_in_ms", 260)):
            overlay_settings = re.sub(rf"(?m)^{key}=.*$", f"{key}={value}", overlay_settings)
        _ = settings.write_text(overlay_settings)
        pages = (console.ConsolePage("studio", "Studio", "clapperboard", "The Studio", console.AvailablePage(studio_page.PAGE_PATH)),
                 console.ConsolePage("review", "Review", "inbox", "Review", console.AvailablePage(review_page.PAGE_PATH)))
        shell = console.render(pages, console.UnavailablePage()).decode()
        list_place = studio_page.PAGE_PATH + '#' + urlencode({"show": "working", "item": f"{MORTY.id}/{item.id}"})
        review_place = review_page.PAGE_PATH + '#' + urlencode({"show": "Done", "type": "question", "q": "keep"})
        destination = "/proof" + console.place_query("studio", list_place) + "&" + urlencode({"at.review": review_place})
        probe = r'''<script>
const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
const params=new URLSearchParams(location.search);
const shot=params.has('shot');
const waitingKey=WAITING_KEY;
const expectedTitle=REVIEW_TITLE,tallTitle=TALL_TITLE;
CONFLICT_CHECK
const advance=async()=>{
 if(!(await fetch('/advance-overlay',{method:'POST'})).ok) throw new Error('The source change could not be updated.');
};
const report=async()=>{await fetch('/results',{method:'POST',body:JSON.stringify(failures)});};
const listFrame=document.querySelector('iframe:not([hidden])');
const direct=!listFrame;
const short=params.has('short') || (direct && innerWidth===500);
const animationFrames=win=>new Promise(resolve=>win.requestAnimationFrame(()=>win.requestAnimationFrame(resolve)));
async function inspect(){
 const doc=listFrame?.contentDocument || document,win=doc.defaultView;
 const dialog=doc.querySelector('[data-list-review-dialog]');
 const row=()=>doc.querySelector('[data-number="111"]');
 const link=(key,item=row())=>[...win.recordDetails(item).querySelectorAll('a[data-list-review-card]')].find(link=>
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
 async function inspectLater(cardDoc){
  const cardWin=cardDoc.defaultView,box=cardDoc.querySelector('.review-box');
  const foot=box.querySelector('.review-box-footer'),height=foot.getBoundingClientRect().height;
  const later=foot.querySelector('[data-later-toggle]'),menu=foot.querySelector('[data-later-menu]');
  const menuFits=()=>{
   const bounds=menu.getBoundingClientRect();
   return bounds.top>=box.getBoundingClientRect().top-box.scrollTop &&
    bounds.bottom<=foot.getBoundingClientRect().top && menu.scrollHeight<=menu.clientHeight;
  };
  later.click();
  await animationFrames(cardWin);
  check(menuFits() && !menu.hidden,'The Later menu fits above the footer without scrolling.');
  menu.querySelector('[data-later-choose]').click();
  await animationFrames(cardWin);
  check(menuFits() && menu.hasAttribute('data-choosing'),'The calendar fits above the footer without scrolling.');
  check(foot.getBoundingClientRect().height===height,'The calendar keeps the footer height.');
  later.click();
  await animationFrames(cardWin);
 }
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
 const waitingRow=doc.querySelector('[data-number="113"]');
 const waitingLink=waitingRow.querySelector('[data-label="State"] a[data-list-review-card]');
 const heading=()=>doc.getElementById(dialog.getAttribute('aria-labelledby')).textContent;
 check(!win.recordIsOpen(waitingRow),'The waiting item starts closed.');
 waitingLink.scrollIntoView({block:'center',behavior:'instant'});
 const waitingCard=await readCard(waitingLink);
 const waitingTop=waitingRow.getBoundingClientRect().top;
 check(dialog.open && waitingCard.querySelector('[data-key="'+waitingKey+'"][data-state="Waiting"]'),
  'The summary opens the linked Waiting card.');
 check(heading()==='Item 113 · Check the linked reviews that are waiting',
  'The window heading names the item number and title.');
 check(win.recordIsOpen(waitingRow),'The summary opens its item behind the window.');
 check(waitingCard.querySelector('.review-box-meta').textContent.includes('The owner checks the error.'),
  'The metadata shows the Waiting reason.');
 check(!waitingCard.querySelector('[data-review-body] section').getClientRects().length,
  'The body hides the section that repeats the title.');
 dialog.querySelector('button').click();
 check(!dialog.open && win.recordIsOpen(waitingRow) && doc.activeElement===waitingLink &&
  Math.abs(waitingRow.getBoundingClientRect().top-waitingTop)<1,
  'Close keeps the item open at its scroll position and focuses its link.');
 const unlinked=waitingLink.cloneNode();
 doc.body.append(unlinked);
 win.addEventListener('click',event=>{
  check(!event.defaultPrevented && !dialog.open,'A review link outside an item keeps its destination.');
  event.preventDefault();
 },{once:true});
 unlinked.click();
 unlinked.remove();
 row().querySelector('.list-toggle').click();
 const doneLink=link('sdlc/C2');
 doneLink.scrollIntoView({block:'nearest',behavior:'instant'});
 const doneScroll=win.scrollY,done=await readCard(doneLink);
 check(dialog.open && done.querySelector('[data-state="Done"]'),'Done opens in the dialog.');
 check(!done.querySelector('[data-review-form]'),'The Done card has no action form.');
 const shortBody=done.querySelector('.review-box-scroll');
 check(shortBody.scrollHeight<=shortBody.clientHeight,'The short card fits without scrolling.');
 check(!done.querySelector('[data-review-body] section').getClientRects().length,'The empty check section is hidden.');
 check(!done.querySelector('[data-review-index]'),'The dialog loads one card.');
 check(heading()==='Item 111 · Check the linked reviews','Each review ID opens under the same item heading.');
 check(win.recordIsOpen(row()),'The Review dialog leaves the List row expanded.');
 const close=dialog.querySelector('button');
 done.body.tabIndex=-1;done.body.focus();
 done.body.dispatchEvent(new done.defaultView.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
 check(!dialog.open && doc.activeElement===doneLink && win.recordIsOpen(row()) && win.scrollY===doneScroll,
  'Escape closes the dialog, keeps the item open at its scroll position, and focuses its link.');
 let readyLink=link('sdlc/C1');
 const ready=await readCard(readyLink);
 await animationFrames(win);
 check(ready.querySelector('[data-review-body]').textContent.includes('Open the linked change.'),'The open card shows the Review body.');
 const form=ready.querySelector('[data-review-form]');
 check(!!form.querySelector('[value="works"]') && !!form.querySelector('[value="feedback"]') &&
  !!form.querySelector('[data-later-toggle]'),'The card shows the Review actions.');
 const head=ready.querySelector('.review-box-header'),body=ready.querySelector('.review-box-scroll');
 const foot=form.closest('footer'),panel=body.querySelector('.review-panel');
 check(head.querySelector('h1').textContent===expectedTitle && !ready.querySelector('.list-row'),
  'The header shows the full title without a table row.');
 const box=ready.querySelector('.review-box'),frame=ready.defaultView.frameElement;
 if(box.hasAttribute('data-whole-scroll')){
  check(!direct && (short || params.has('cap')),'Only a short window scrolls the whole card.');
  box.scrollTop=box.scrollHeight;
  const bounds=foot.getBoundingClientRect();
  check(box.scrollTop>0 && body.scrollTop===0 && bounds.top>=0 && bounds.bottom<=frame.clientHeight,
   'Scrolling the card brings the entire footer into view.');
  box.scrollTop=0;
 }else{
  const top=head.getBoundingClientRect().top,bottom=foot.getBoundingClientRect().bottom;
  const needsScroll=body.scrollHeight>body.clientHeight;
  body.scrollTop=body.scrollHeight;
  check((body.scrollTop>0)===needsScroll && box.scrollTop===0 && ready.scrollingElement.scrollTop===0,
   'Only the card body scrolls when its content exceeds its height.');
  check(head.getBoundingClientRect().top===top && foot.getBoundingClientRect().bottom===bottom,
   'The header and feedback controls stay fixed.');
  check(bottom<=frame.clientHeight && body.clientHeight>=96,
   'The frame contains the entire footer and at least 96 pixels of the body.');
 }
 await inspectLater(ready);
 const columns=ready.defaultView.getComputedStyle(panel).gridTemplateColumns.split(' ').length;
 check(columns===(ready.defaultView.innerWidth<800 ? 1 : 2),'The card changes columns at 800 pixels.');
 const phone=direct || document.querySelector('.mobile-header').getClientRects().length>0;
 check(phone===(direct || new URLSearchParams(location.search).has('phone')),'The List uses the expected layout.');
 const area=dialog.querySelector('[data-page-window-actions]');
 const clear=area ? Math.max(area.getBoundingClientRect().top,
  ...[...area.children].map(control=>control.getBoundingClientRect().bottom)) : 0;
 if(phone){
  const box=dialog.getBoundingClientRect();
  const top=listFrame?.getBoundingClientRect().top ?? 0;
  check(Math.abs(box.width-innerWidth)<=1 && Math.abs(box.top-clear)<1 && Math.abs(box.bottom-innerHeight)<=1
   && top===0,
   'The phone dialog fills the screen below the action area. '
   +JSON.stringify({width:innerWidth,height:innerHeight,box,top,clear}));
  check(ready.documentElement.scrollWidth<=ready.defaultView.innerWidth,'The card fits the phone width.');
 }else{
  const box=dialog.getBoundingClientRect();
  const frozenBottom=doc.querySelector('.page-top').getBoundingClientRect().bottom;
  const top=Math.max(clear,frozenBottom)+16;
  check(box.width<=1060 && Math.abs(box.left+box.right-win.innerWidth)<=20 && Math.abs(box.top-top)<1,
   'The desktop dialog is centered horizontally and starts 16 pixels below the frozen top. '
   +JSON.stringify({box,top}));
  check(ready.documentElement.scrollWidth<=ready.defaultView.innerWidth,'The card fits the desktop width.');
 }
 if(short && !direct){
  close.click();
  const extra=doc.querySelector('[data-number="112"]');
  extra.querySelector('.list-toggle').click();
  const tall=await readCard(link('sdlc/C3',extra));
  await animationFrames(tall.defaultView);
  const box=tall.querySelector('.review-box'),scroll=tall.querySelector('.review-box-scroll');
  const tallFoot=tall.querySelector('.review-box-footer'),title=box.querySelector('h1');
  check(title.textContent===tallTitle && box.scrollHeight>box.clientHeight,
   'The box scrolls with the full 12-line title.');
  box.scrollTop=box.scrollHeight;
  const bounds=tallFoot.getBoundingClientRect(),viewport=box.getBoundingClientRect();
  check(box.scrollTop>0 && scroll.scrollTop===0 && bounds.top>=viewport.top && bounds.bottom<=viewport.bottom,
   'Scrolling the box brings the whole footer into view.');
  close.click();
  const compact=await readCard(link('sdlc/C4',extra));
  await animationFrames(compact.defaultView);
  check(!compact.querySelector('[data-review-body] section').getClientRects().length,
   'The body hides text that matches an inline code title.');
  await inspectLater(compact);
  close.click();
  const repeated=await readCard(link('sdlc/C5',extra));
  const checkLink=repeated.querySelector('[data-review-body] a[href="https://example.com/check"]');
  check(checkLink?.getClientRects().length,'The check link stays visible when it matches the title.');
 }
 if(!short && (direct || params.has('cap'))){
  await checkReviewCardConflict(ready,direct ? 'phone' : 'cap',check,advance);
  await report();return;
 }
 if(short){await report();return;}
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
  document.querySelector('.nav-entry[data-page="studio"]').click();
  await animationFrames(win);
  check(win.recordIsOpen(row()),'The List keeps the item open on return.');
  check(!listFrame.hidden && !dialog.open && !dialog.querySelector('iframe'),'Returning to List shows no old card.');
  savedReviewPlace=reviewPlace();
  await changed(doc.body,()=>link('sdlc/C1'),'the refreshed List loads the linked card');
  readyLink=link('sdlc/C1');
  const draft=await readCard(readyLink);
  draft.querySelector('[name=feedback]').value='Keep this draft when I switch tabs.';
  for(const visibility of ['hidden','visible']){
   Object.defineProperty(doc,'visibilityState',{configurable:true,value:visibility});
   doc.dispatchEvent(new win.Event('visibilitychange'));
   await animationFrames(win);
   check(dialog.open && dialog.querySelector('iframe')?.contentDocument===draft &&
     draft.querySelector('[name=feedback]').value==='Keep this draft when I switch tabs.',
    'A browser tab switch keeps the open card and its draft.');
  }
  delete doc.visibilityState;
  check(win.recordIsOpen(row()) && link('sdlc/C1'),'The visible List keeps the item open and retains its link.');
  document.querySelector('.nav-entry[data-page="review"]').click();
  await animationFrames(window);
  check(listFrame.hidden && dialog.open,'Changing console pages keeps a card that has a draft.');
  const beforeReturn=row(),listBeforeReturn=doc.querySelector('[data-list]'),fetchBeforeReturn=win.fetch.bind(win);
  let releaseCounts,countsStarted;
  const heldCounts=new Promise(resolve=>{releaseCounts=resolve;});
  const readingCounts=new Promise(resolve=>{countsStarted=resolve;});
  win.fetch=async(url,options)=>{
   if(String(url).includes('?counts=')){countsStarted();await heldCounts;}
   return fetchBeforeReturn(url,options);
  };
  document.querySelector('.nav-entry[data-page="studio"]').click();
  await readingCounts;
  check(doc.querySelector('[data-list]')!==listBeforeReturn && row()===beforeReturn && win.recordIsOpen(row()),
   'The List refreshes and keeps the open row node while it reads links.');
  check(draft.querySelector('[name=feedback]').value==='Keep this draft when I switch tabs.',
   'The draft is still available on the List.');
  const retainedLink=link('sdlc/C1');
  close.click();
  check(!dialog.open && retainedLink && doc.activeElement===retainedLink,
   'Close focuses the retained link while the List loads its links.');
  releaseCounts();
  await animationFrames(win);
  check(doc.activeElement===link('sdlc/C1'),'Focus stays on the link after loading.');
  win.fetch=fetchBeforeReturn;
  readyLink=link('sdlc/C1');
 }
 const fetchList=win.fetch.bind(win);
 if(!phone){
  const timeout=win.AbortSignal.timeout,deadlines=[];
  win.AbortSignal.timeout=milliseconds=>{deadlines.push(milliseconds);return timeout(milliseconds);};
  const saved=await readCard(link('sdlc/C1')),savedWindow=saved.defaultView,savedFrame=savedWindow.frameElement;
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
  savedWindow.fetch=()=>Promise.resolve(new savedWindow.Response('Invalid JSON'));
  savedForm.requestSubmit(savedForm.querySelector('[value=works]'));
  await reading;
  check(!dialog.open && doc.activeElement===link('sdlc/C1'),
   'The card stays closed and returns focus during the List refresh.');
  const notice=doc.querySelector('[data-review-refresh-error]');
  check(!notice.hidden && notice.textContent==='Saved; the item could not refresh.',
   'The List reports the unreadable response from a confirmed save.');
  const currentSearch=doc.querySelector('#list-search');
  currentSearch.focus();
  check(doc.activeElement===currentSearch,'Search stays available during the List refresh.');
  const pageStatus=doc.querySelector('[data-page-status]');
  const readFailed=changed(pageStatus,()=>pageStatus.dataset.state==='Failed','the held refresh fails');
  finishRead(new win.Response(null,{status:503}));
  await readFailed;
  await changed(dialog,()=>!savedFrame.isConnected,'the saved frame is removed');
  check(!notice.hidden,'A failed refresh keeps the error visible.');
  check(!dialog.open && doc.activeElement===currentSearch,'A failed refresh leaves the card closed and preserves focus.');
  check(deadlines.length>0 && deadlines.every(value=>value===7000),
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
   if(url===win.location.pathname || url===doc.querySelector('[data-list]').dataset.listPath){
    if(options?.method==='POST') return listSave;
    if(delayRefresh){listReads++;await listRefresh;}
   }
   return fetchList(url,options);
  };
  const listWords=win.recordDetails(row()).querySelector('textarea[name="words"]');
  listWords.value='A List action is pending.';listWords.dispatchEvent(new win.Event('input',{bubbles:true}));
  const listForm=listWords.form;
  listForm.querySelector('[value="tell"]').click();
  check(!!listForm.dataset.sending,'The List action starts before the Review save.');
 }
 const card=await readCard(readyLink),cardWin=card.defaultView,sendingFrame=cardWin.frameElement;
 const formInCard=()=>card.querySelector('[data-review-form]');
 const status=()=>card.querySelector('[data-review-card-notice]').textContent;
 const button=outcome=>formInCard().querySelector('[value="'+outcome+'"]');
 const submit=outcome=>formInCard().requestSubmit(button(outcome));
 let reviewPosts=0;
 const fades=[];
 if(!phone){
  for(const realm of [win,cardWin]){
   const animate=realm.Element.prototype.animate;
   realm.Element.prototype.animate=function(keyframes,options){
    if(this.matches('.review-box, [data-list-review-dialog] iframe'))
     fades.push([this.tagName,options.duration]);
    return animate.call(this,keyframes,options);
   };
  }
  const fetchCard=cardWin.fetch.bind(cardWin);
  let undoRequests=0,sendReads=0;
  const hold='/send-hold/'+'a'.repeat(24);
  cardWin.fetch=async(url,options)=>{
   if(url===hold+'/undo'){
    undoRequests++;
    return cardWin.Response.json({outcome:'undone'});
   }
   if(url===hold){
    sendReads++;
    return cardWin.Response.json({reason:'The test save failed.'},{status:503});
   }
   if(url===cardWin.location.pathname && options?.method==='POST' && ++reviewPosts<=2){
    return cardWin.Response.json({hold:'a'.repeat(24),seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
   }
   return fetchCard(url,options);
  };
  const words='Keep these words through the failure and conflict.';
  formInCard().elements.feedback.value=words;
  submit('feedback');
  check(!dialog.open && sendingFrame.isConnected,'Send closes the modal and keeps the card frame.');
  const toast=()=>document.querySelector('[data-send-undo]');
  await changed(document.body,()=>toast()?.querySelector('button'),'the Review Undo toast shows');
  check(!dialog.open && toast().parentElement.parentElement===document.body,'The toast shows on the available page.');
  check(toast().querySelector('span').textContent==='Sent to sdlc.','The toast names the linked card owner.');
  const other=await readCard(link('sdlc/C2'));
  check(dialog.open && other.querySelector('[data-state="Done"]') && sendingFrame.hidden,
   'A second card opens while the send waits.');
  close.click();
  toast().querySelector('button').click();
  await changed(dialog,()=>dialog.open && !close.disabled,'Undo reopens the card');
  check(undoRequests===1 && sendReads===0 && status()==='Undone. Edit and send again.' &&
   formInCard().elements.feedback.value===words && card.activeElement===formInCard().elements.feedback,
   'Undo restores the card and focuses its draft.');
  submit('feedback');
  await changed(document.body,()=>toast()?.querySelector('button'),'the next Review Undo toast shows');
  toast().getAnimations()[0].finish();
  await changed(dialog,()=>dialog.open && !close.disabled,'the refusal reopens the card');
  check(status()==='Not saved: The test save failed.','The failure remains visible.');
  check(sendReads===1 && formInCard().elements.feedback.value===words,'Failure restores the card and its words.');
  await checkReviewCardConflict(card,'short',check,advance);
  delayRefresh=true;
 }
 const before=row(),listBefore=doc.querySelector('[data-list]');
 const refreshed=new Promise((resolve,reject)=>{
  const notice=card.querySelector('[data-review-card-notice]');
  const saveObserver=new card.defaultView.MutationObserver(()=>{
   if(notice.textContent.startsWith('Not saved:')){
    saveObserver.disconnect();observer.disconnect();reject(new Error(notice.textContent));
   }
  });
  const observer=new win.MutationObserver(()=>{
   if(!sendingFrame.isConnected){saveObserver.disconnect();observer.disconnect();resolve();}
  });
  saveObserver.observe(notice,{childList:true});
  observer.observe(doc.body,{childList:true,subtree:true,attributes:true,attributeFilter:['open']});
 });
 submit('works');
 check(!dialog.open,'The verdict closes the dialog at Send.');
 if(!phone){
  await changed(sendingFrame,()=>!sendingFrame.hasAttribute('data-saving'),'the verdict is saved');
  check(status()==='' && !card.querySelector('[data-send-status]'),'The verdict shows no status text.');
  await animationFrames(win);
  check(!dialog.open && listReads===0 && row()===before,'The Review refresh waits for the pending List action.');
  releaseList(new win.Response(JSON.stringify({reason:'The test List action was refused.'}),{status:400}));
  await changed(doc.body,()=>!win.recordDetails(row()).querySelector('textarea[name="words"]').form.dataset.sending,
   'the refused List action ends');
  check(listReads===1 && !dialog.open && button('works').disabled,
   'A refused List action starts one refresh and keeps the saved form disabled.');
  submit('works');
  check(reviewPosts===4,'The saving form sends no second POST during the refresh.');
  releaseRefresh();
 }
 await refreshed;
 if(!phone) check(doc.querySelector('[data-review-refresh-error]').hidden,
  'A good refresh clears the previous error.');
 if(!phone) check(JSON.stringify(fades)===JSON.stringify([['ARTICLE',180],['ARTICLE',260]]),
  'Show updates uses the configured card fade durations.');
 check(doc.querySelector('[data-list]')!==listBefore && row()===before &&
  row().querySelector('[data-label="State"]').textContent.includes('Reviews done'),
  'The refresh displays the saved verdict.');
 check(doc.activeElement===link('sdlc/C1'),'The refreshed link receives focus.');
 check(location.pathname==='/proof' && listFrame.contentDocument===doc && history.length===historyLength,
  'Opening, closing, and saving do not navigate.');
 check(reviewPlace()===savedReviewPlace,'The Review filters stay the same.');
 const filters=new URLSearchParams(win.location.hash.slice(1));
 check(new URL(listURL).pathname===win.location.pathname && filters.get('show')==='working' &&
  filters.get('q')==='linked reviews' && doc.querySelector('#list-search').value==='linked reviews' &&
  win.recordIsOpen(row()),'The List keeps the filters, search, and open row.');
 await report();
}
const inspectPage=()=>{void inspect().catch(error=>{failures.push(String(error));void report();});};
if(direct) inspectPage();
else listFrame.addEventListener('load',inspectPage,{once:true});
if(direct || shot){const image=new Image();image.hidden=true;image.src='/complete';document.body.append(image);}
</script>'''.replace('WAITING_KEY', json.dumps(waiting_key)).replace('CONFLICT_CHECK', REVIEW_CARD_CONFLICT_CHECK).replace(
            'REVIEW_TITLE', json.dumps(title)).replace('TALL_TITLE', json.dumps("\n".join(tall_title)))
        standalone_probe = '<script>' + REVIEW_CARD_CONFLICT_CHECK + r'''
(async()=>{
 const failures=[];
 try{
  await checkReviewCardConflict(document,'standalone',(ok,text)=>{if(!ok) failures.push(text);},async()=>{
   if(!(await fetch('/advance-overlay',{method:'POST'})).ok) throw new Error('The source change could not be updated.');
  });
 }catch(error){failures.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>'''
        shell = shell.replace('</body>', probe + '</body>')
        proof = os.environ.get("LIST_OVERLAY_SCREENSHOT")
        _ = (self.root / "config/scratch.conf").write_text(
            f"holds_file={self.root / 'holds.json'}\n"
            + "# whole: 1..\ndefault_life_seconds=86400\n# whole: 1..\nrepo_depth=3\n"
            + "# whole: 1..\nsuffix_length=6\nrepo_skip=target node_modules .git\n"
            + "# whole: 1..\nrecord_lock_attempts=3\n# whole: 1..\nremoval_remote_timeout_seconds=30\n")
        for size in ("1440,1000", "1440,620", "1474,600", "600,900", "390,900", "500,800",
                     "standalone", *(("screenshot",) if proof else ())):
            with self.subTest(size=size):
                initial = original
                if size == "1440,620":
                    initial = original.replace("Open the linked change.",
                                               "Open the linked change. " + "Check each recorded result. " * 160)
                elif size in ("1474,600", "600,900", "390,900", "500,800", "screenshot"):
                    initial = original.replace("Open the linked change.", instructions)
                _ = listing.write_text(initial)
                browser_root = root / size.replace(",", "x")
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(serve.ReviewLinksHandler):
                    @override
                    def html_response(self, status: int, content: bytes, *,
                                      delivery_pending: bool | None = None, builds_version: str = "",
                                      content_security_policy: str = "", cache_gzip: bool = False) -> None:
                        if self.path in (studio_page.PAGE_PATH + "?direct=1", studio_page.PAGE_PATH + "?direct=1&short=1"):
                            content = content.replace(b'</body>', probe.encode() + b'</body>')
                        super().html_response(status, content, delivery_pending=delivery_pending,
                                              builds_version=builds_version,
                                              content_security_policy=content_security_policy, cache_gzip=cache_gzip)

                    @override
                    def review_response(self, content: bytes, content_type: str, prepared: bytes = b"", *,
                                        version: str = "", status: int = 200) -> None:
                        if self.path.endswith("&standalone-proof=1") and status == 200:
                            content = content.replace(b'</body>', standalone_probe.encode() + b'</body>')
                            prepared = b""
                        super().review_response(content, content_type, prepared, version=version, status=status)

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
                            _ = listing.write_text(initial.replace('Open the linked change.',
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
                      patch.object(change_page, "feature_review_id", return_value=""),
                      patch.object(change_review, "lists_directory", return_value=self.root),
                      patch.object(staff, "snapshot", return_value=members),
                      patch.object(list_page, "counts_strip", return_value=""),
                      started_review_server(("127.0.0.1", 0), browser_root, initialize_review=True) as server):
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        viewport = "1440,1000" if size in ("screenshot", "standalone") else size
                        width, height = map(int, viewport.split(","))
                        route = destination
                        if size == "500,800":
                            old_place = list_page.PAGE_PATH + list_place.removeprefix(studio_page.PAGE_PATH)
                            route = old_place.replace("#", "?direct=1&short=1#", 1)
                        elif size == "1474,600":
                            route += "&short=1"
                        elif size == "390,900":
                            route = list_place.replace("#", "?direct=1#", 1)
                        elif size == "1440,620":
                            route += "&cap=1"
                        elif size == "standalone":
                            route = review_page.card_url("sdlc/C1") + "&standalone-proof=1"
                        elif size == "600,900":
                            route += "&phone=1"
                        url = f"http://127.0.0.1:{server.server_port}" + route
                        if size == "screenshot":
                            assert proof is not None
                            result = self.firefox_view(url + "&shot=1", Path(proof), width, height, complete, dark=True)
                            self.assertEqual(result.returncode, 0, result.stderr)
                        elif size == "390,900":
                            result = self.firefox_view(url, None, width, height, complete, dark=True)
                            self.assertEqual(result.returncode, 0, result.stderr)
                        else:
                            with self.firefox().page(url, width, height, dark=True):
                                self.assertTrue(complete.wait(35), "The browser must report the overlay results.")
                    finally:
                        complete.set()
                        server.shutdown()
                        thread.join(5)
                self.assertEqual(results, [[]])
                if size in ("1440,1000", "600,900"):
                    reviewed = {row["#"]: row["Verdict"] for row in change_review.review_snapshot(listing).reviewed}
                    self.assertIn("works", reviewed["C1"].casefold())

    @contextmanager
    def studio_review_server(self) -> Generator[str]:
        root = self.root / "reviews"
        with (patch.object(serve, "loaded_module_stamps", return_value={}),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(change_page, "recover_pending"), patch.object(list_page, "recover_pending"),
              started_review_server(("127.0.0.1", 0), root, initialize_review=True) as server):
            worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            worker.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}"
            finally:
                server.shutdown()
                worker.join(5)

    def open_studio_review_fixture(self, origin: str, fragment: str = "") -> None:
        browser = self.chrome()
        _ = browser.command("Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 900, "deviceScaleFactor": 1, "mobile": False}, browser.session)
        _ = browser.command("Emulation.setEmulatedMedia",
            {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]}, browser.session)
        browser.navigate(origin + studio_page.PAGE_PATH + fragment)

    def studio_review_check(self, steps: str) -> None:
        browser = self.chrome()
        script = r"""(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const dialog=document.querySelector('[data-list-review-dialog]');
 const row=number=>document.querySelector('[data-list-item][data-number="'+number+'"]');
 const close=()=>dialog.querySelector('[data-close]').click();
 const paint=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const until=predicate=>new Promise(resolve=>{
  const observer=new MutationObserver(ready);
  function ready(){if(predicate()){observer.disconnect();resolve();}}
  observer.observe(document.body,{subtree:true,childList:true,attributes:true});ready();
 });
 async function open(link){
  link.click();
  const frame=dialog.querySelector('iframe');
  if(!dialog.open || !frame) throw new Error('The link did not open a card.');
  await new Promise(resolve=>frame.addEventListener('load',resolve,{once:true}));
  return frame.contentDocument;
 }
 try{
 __STUDIO_REVIEW_STEPS__
 }catch(error){failures.push(String(error));}
 return failures;
})()""".replace("__STUDIO_REVIEW_STEPS__", steps)
        result = browser.command("Runtime.evaluate",
                                 {"expression": script, "awaitPromise": True, "returnByValue": True}, browser.session)
        self.assertNotIn("exceptionDetails", result, result)
        value = cast(dict[str, object], result["result"])
        self.assertEqual(value.get("value"), [])

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_studio_review_window_clears_frozen_controls(self) -> None:
        review = investigation.file(OWNER.id, "Check the result", "Read the review.", waiting_why="Work continues.")
        key = review_items.investigation_record_key(review)
        for number in range(1, 19):
            item = self.add(f"Item {number}", number=number)
            if number == 10:
                _ = the_list.link(MORTY.id, item.id, (key,), source="morty")
        with self.studio_review_server() as origin:
            self.open_studio_review_fixture(origin)
            self.studio_review_check(r"""
 const header=document.querySelector('.page-top');
 const controls=header.querySelector('[data-page-top-controls]');
 const summary=row(10).querySelector('.list-row');
 const link=()=>summary.querySelector('a[data-list-review-card]');
 const bottom=()=>header.getBoundingClientRect().bottom;
 const windowTop=()=>dialog.getBoundingClientRect().top;
 await until(link);
 scrollBy({top:summary.getBoundingClientRect().top-bottom()-64,behavior:'instant'});
 await paint();
 check(scrollY>0 && controls.getBoundingClientRect().bottom>0,'The picture tabs stay visible after scrolling.');
 await open(link());
 await paint();
 const first=windowTop();
 check(Math.abs(first-bottom()-16)<1,'The review window clears the frozen top.');
 controls.style.paddingBottom='24px';
 await paint();
 check(windowTop()>first && Math.abs(windowTop()-bottom()-16)<1,
  'The open window moves when the frozen top grows.');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_question_chips_and_mark_read_confirmation(self) -> None:
        members = staff.snapshot()
        incoming = list_exchange.Exchange(list_exchange.Source.STAFF_QUESTION, "question/incoming", "initial",
            list_exchange.Direction.QUESTION_TO_NATE, "Which layout?", NOW, OWNER.id, list_exchange.Nate.USER,
            list_exchange.QuestionWithoutAnswer.OPEN)
        outgoing = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, "submission/question/outgoing", "followup:1",
            list_exchange.Direction.ANSWER_TO_NATE, "Which width?", NOW, list_exchange.Nate.USER, OWNER.id,
            list_exchange.QuestionWithoutAnswer.OPEN)
        answered = replace(outgoing, source_key="submission/question/answered",
            answer=list_exchange.ExchangeAnswer("Use 800 pixels.", NOW, list_exchange.AnswerRead.UNREAD))
        for exchange in (incoming, outgoing, answered):
            with the_list.lock_exchange_source(exchange.source_key) as source:
                _ = the_list.file_exchanges(source, (exchange,), exchange.question, linked=the_list.NextNumber.AVAILABLE,
                                            owner=OWNER.id, members=members)
        with self.studio_review_server() as origin:
            self.open_studio_review_fixture(origin)
            self.studio_review_check(r"""
 const show=value=>document.querySelector('[data-filter-row="show"] button[data-filter-value="'+value+'"]');
 for(const [chip,number,label] of [['questions',1,'Waiting for nate'],['working',2,'Working'],['answers',3,'Answered']]){
  show(chip).click();await paint();
  check(row(number).querySelector('[data-label="State"] .list-state').textContent===label,'The row shows '+label+'.');
  check([1,2,3].filter(index=>!row(index).hidden).join()===String(number),'Only the matching item appears under '+chip+'.');
 }
 const item=row(3);
 check(item.querySelector('[data-new-answer]'),'The answer is unread.');
 item.querySelector('.list-toggle').click();await paint();
 const form=recordDetails(item).querySelector('[data-question-read]'),words=form.elements.words;
 check(words.labels[0]?.textContent==='Your words','The words field has a visible label.');
 const submit=form.querySelector('[value="read"]');
 const ask=form.querySelector('[value="question"]');
 check(ask.disabled && !submit.disabled,'Only Ask question needs words.');
 check(ask.textContent==='Ask question' && sendReceiver(ask)==='sdlc','Ask question shows the assignee.');
 document.querySelector('[data-list]').dataset.undoSeconds='0';
 const originalFetch=window.fetch.bind(window);
 let posts=0;
 window.fetch=(url,options)=>{
  if(url!==location.pathname || options?.method!=='POST') return originalFetch(url,options);
  check(options.body.get('words')==='I will use that width.','Mark read sends the words beside it.');
  if(++posts===1) return Promise.resolve(Response.json({state:'read'}));
  return Promise.resolve(Response.json({words_saved:true,item_state:'Closed',unread:'false',detail_closed:true}));
 };
 words.value='I will use that width.';words.dispatchEvent(new Event('input',{bubbles:true}));
 check(!ask.disabled,'Words enable Ask question.');
 submit.click();
 await until(()=>form.querySelector('[data-send-status]').textContent.includes('unconfirmed'));
 check(words.value==='I will use that width.','An unconfirmed save keeps the draft.');
 submit.click();
 await until(()=>item.dataset.state==='Closed');
 check(words.value==='' && posts===2,'A confirmed save clears the submitted words.');
""")

    def test_question_read_form_takes_priority_over_a_second_waiting_question(self) -> None:
        key = "submission/question/waiting-and-answered"
        exchange = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, key, "followup:1",
            list_exchange.Direction.ANSWER_TO_NATE, "Which width?", NOW, list_exchange.Nate.USER, OWNER.id,
            list_exchange.QuestionWithoutAnswer.OPEN)
        with the_list.lock_exchange_source(key) as source:
            row = the_list.file_exchanges(source, (exchange,), exchange.question, linked=the_list.NextNumber.AVAILABLE,
                                          owner=OWNER.id, members=staff.snapshot())
        identity = f"L{row.number}"
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        with patch.object(change_page, "send_question", return_value=question_outcome()):
            for request, words in (("ask-one", "Which width fits?"), ("ask-two", "Which height fits?")):
                delivery = the_list.ask_question(row.keeper, identity, the_list.PageRequest(request, "question", words),
                                                 status)
                change_page.deliver(delivery, status)
        waiting = list_page.saved_action_state(row.keeper, identity, status=status)
        self.assertEqual(waiting["waiting_for_answer"], "true")
        with patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO()):
            self.assertEqual(the_list.main(["answer", identity, "--question", "Q1", "--text", "Use 800 pixels."]), 0)
        saved = list_page.saved_action_state(row.keeper, identity, status=status, include_detail=True)
        self.assertEqual((saved["item_state"], saved["open_question"], saved["waiting_for_answer"]),
                         ("Answered", "true", "false"))
        detail = saved.get("detail_html", "")
        self.assertIn('data-question="Q2"', detail)
        self.assertIn('>Your words</label>', detail)
        self.assertIn('value="read">Mark read</button>', detail)
        self.assertNotIn('data-waiting-actions', detail)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_answers_chip_includes_a_parked_question_with_a_new_reply(self) -> None:
        key = "submission/question/parked-and-answered"
        exchange = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, key, "followup:1",
            list_exchange.Direction.ANSWER_TO_NATE, "Which width?", NOW, list_exchange.Nate.USER, OWNER.id,
            list_exchange.QuestionWithoutAnswer.OPEN)
        members = staff.snapshot()
        with the_list.lock_exchange_source(key) as source:
            row = the_list.file_exchanges(source, (exchange,), exchange.question, linked=the_list.NextNumber.AVAILABLE,
                                          owner=OWNER.id, members=members)
        _ = the_list.park(row.keeper, f"L{row.number}", NOW + timedelta(days=1), source="nate")
        reply = replace(exchange, answer=list_exchange.ExchangeAnswer(
            "Use 800 pixels.", NOW + timedelta(minutes=1), list_exchange.AnswerRead.UNREAD))
        with the_list.lock_exchange_source(key) as source:
            self.assertEqual(the_list.file_exchanges(source, (reply,), reply.question, linked=row,
                                                      owner=OWNER.id, members=members), row)
        page = without_scripts(list_page.render(self.root / "reviews", include_counts=False, query="show=answers").decode())
        summary = re.search(r'<tbody data-list-item[^>]+data-number="1"[^>]*>', page)
        assert summary is not None
        self.assertNotIn(" hidden", summary[0])
        self.assertIn('data-parked="true"', summary[0])
        with self.studio_review_server() as origin:
            self.open_studio_review_fixture(origin)
            self.studio_review_check(r"""
 const answers=document.querySelector('[data-filter-row="show"] [data-filter-value="answers"]');
 answers.click();await paint();
 check(!row(1).hidden && row(1).dataset.parked==='true','Answers includes the parked question.');
 check(row(1).querySelector('[data-new-answer]'),'The parked question shows the new answer.');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_answers_the_team_question_in_its_open_row(self) -> None:
        item = self.studio_team_question()
        with (patch.object(serve.ReviewLinksHandler, "deliver_review", return_value=change_page.DeliveryConfirmed()),
              self.studio_review_server() as origin):
            self.open_studio_review_fixture(origin, "#show=questions")
            self.studio_review_check(r"""
 const item=row(1);
 check(!item.hidden && !recordIsOpen(item),'The Questions chip shows the closed row.');
 item.querySelector('.list-toggle').click();
 await until(()=>recordDetails(item).querySelector('[data-studio-question-card] [data-review-form]'));
 const card=recordDetails(item).querySelector('[data-studio-question-card]');
 const form=card.querySelector('[data-review-form]');
 check(!dialog.open && !card.querySelector('iframe'),'The question opens in the row.');
 check(card.textContent.includes('Both layouts fit the list.') &&
       card.textContent.includes('so readers can see the controls.'),'The card shows the reason and recommendation.');
 const choices=card.querySelectorAll('input[name=choice]');
 check(choices.length===2,'The card shows both choices.');
 check(form.querySelector('[value="question"]').disabled,'Ask question needs words.');
 choices[1].click();
 check(form.elements.feedback.value==='Use the wide layout.','The choice fills Your words.');
 check(!form.querySelector('[value="question"]').disabled,'Words enable Ask question.');
 check(form.querySelector('[data-later-toggle]').textContent.trim().startsWith('Later'),'The card has Later.');
 form.querySelector('[value="answer"]').click();
 await until(()=>row(1)?.hidden);
 check(!dialog.open,'Answer leaves Questions without opening a window.');
""")
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(saved.state, the_list.ItemState.WORKING)
        exchange, = saved.filed_exchanges
        assert isinstance(exchange.answer, list_exchange.ExchangeAnswer)
        self.assertEqual(exchange.answer.text, "Use the wide layout.")

    def studio_team_question(self) -> the_list.ListItem:
        members = staff.snapshot()
        parts = update_registration.QuestionParts(
            ask="Which layout should the Review page use?",
            context="Both layouts fit the list. The wide layout gives each card more space.",
            items=[], choices=["Use the compact layout.", "Use the wide layout."],
            recommend="Use the wide layout so readers can see the controls.",
        )
        question = update_registration.OutstandingItem(since=NOW.strftime("%Y-%m-%dT%H:%M"), text=parts["ask"],
            question=update_registration.question_text(parts), asked_by=DWIGHT.id, parts=parts)
        folder = record_store.default_question_folder(MORTY.id)
        folder.mkdir(parents=True, exist_ok=True)
        record = update_registration.QuestionRecord(folder / f"question-{DWIGHT.id}-layout.md",
                                                     MORTY.id, DWIGHT.id, question, "open")
        update_registration.write_question_record(record)
        answer_sources.file_saved_question(record, members)
        item, = the_list.records(MORTY.id)
        return item

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_linked_reply_shows_mark_read_and_park_without_schedule_fields(self) -> None:
        key = "submission/question/primary"
        exchange = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, key, "followup:1",
            list_exchange.Direction.ANSWER_TO_NATE, "Which width?", NOW, list_exchange.Nate.USER, OWNER.id,
            list_exchange.QuestionWithoutAnswer.OPEN)
        members = staff.snapshot()
        with the_list.lock_exchange_source(key) as source:
            row = the_list.file_exchanges(source, (exchange,), exchange.question, linked=the_list.NextNumber.AVAILABLE,
                                          owner=OWNER.id, members=members)
        reply_key = "submission/question/linked"
        reply = replace(exchange, source_key=reply_key, question="Which height?", answer=list_exchange.ExchangeAnswer(
            "Use 800 pixels.", NOW + timedelta(minutes=1), list_exchange.AnswerRead.UNREAD))
        with the_list.lock_exchange_source(reply_key) as source:
            self.assertEqual(the_list.file_exchanges(source, (reply,), reply.question, linked=row,
                                                      owner=OWNER.id, members=members), row)
        with self.studio_review_server() as origin:
            self.open_studio_review_fixture(origin)
            self.studio_review_check(r"""
 document.querySelector('[data-filter-row="show"] [data-filter-value="answers"]').click();
 await paint();
 const item=row(1);
 check(!item.hidden && item.dataset.state==='Working','Answers shows the Working item.');
 item.querySelector('.list-toggle').click();
 const form=recordDetails(item).querySelector('[data-feedback-form]');
 check(form.querySelector('[value="read"]'),'The linked reply has a Mark read button.');
 form.querySelector('[data-later-toggle]').click();
 form.querySelector('[data-later-choose]').click();
 await paint();
 check(!form.querySelector('[data-later-custom]').hidden,'Park opens the date and time controls.');
 check(!form.querySelector('[data-schedule-template],[data-schedule-words]'),'Park omits the schedule fields.');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_keeps_linked_replies_under_answers_across_question_state_changes(self) -> None:
        members = staff.snapshot()
        next_number = the_list.NextNumber.AVAILABLE

        def file(exchange: list_exchange.Exchange, linked: investigation.ListDestination | the_list.NextNumber
                 ) -> investigation.ListDestination:
            with the_list.lock_exchange_source(exchange.source_key) as source:
                return the_list.file_exchanges(source, (exchange,), exchange.question, linked=linked,
                                               owner=OWNER.id, members=members)

        prompt = list_exchange.Exchange(list_exchange.Source.STAFF_QUESTION, f"question/{OWNER.id}/waiting.md",
            "initial", list_exchange.Direction.QUESTION_TO_NATE, "Which width?", NOW, OWNER.id, list_exchange.Nate.USER,
            list_exchange.QuestionWithoutAnswer.OPEN)
        answer = list_exchange.ExchangeAnswer("Use 800 pixels.", NOW + timedelta(minutes=2),
                                             list_exchange.AnswerRead.NOT_TRACKED)
        closed_prompt = replace(prompt, source_key=f"question/{OWNER.id}/closed.md", answer=answer)
        waiting = file(prompt, next_number)
        closing = file(closed_prompt, next_number)
        reply = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, "", "followup:1",
            list_exchange.Direction.ANSWER_TO_NATE, "Which height?", NOW, list_exchange.Nate.USER, OWNER.id,
            list_exchange.ExchangeAnswer("Use 600 pixels.", NOW + timedelta(minutes=1), list_exchange.AnswerRead.UNREAD))
        _ = file(replace(reply, source_key="submission/question/reply-while-waiting"), waiting)
        parked = the_list.park(waiting.keeper, f"L{waiting.number}", NOW + timedelta(days=1), source="nate")
        self.assertEqual(len(the_list.unread_answer_ids(parked)), 1)
        _ = file(replace(reply, source_key="submission/question/reply-before-close"), closing)
        closed = the_list.close(closing.keeper, f"L{closing.number}", "The width works.", source=OWNER.name,
                                assigned_to=the_list.ListCloseCaller(OWNER, False))
        self.assertEqual(len(the_list.unread_answer_ids(closed)), 1)
        _ = file(replace(reply, source_key="submission/question/reply-after-close"), closing)

        with self.studio_review_server() as origin:
            self.open_studio_review_fixture(origin)
            self.studio_review_check(r"""
 document.querySelector('[data-filter-row="show"] [data-filter-value="answers"]').click();
 await paint();
 const item=row(1);
 check(!item.hidden && item.dataset.state==='Waiting for nate','Answers shows the Waiting item.');
 item.querySelector('.list-toggle').click();
 check(recordDetails(item).querySelector('[value="read"]'),'The Waiting item has Mark read.');
""")
            _ = file(replace(prompt, answer=answer), waiting)
            updated = the_list.load(waiting.keeper, f"L{waiting.number}")
            self.assertIs(updated.state, the_list.ItemState.WORKING)
            self.assertEqual(len(the_list.unread_answer_ids(updated)), 1)
            self.open_studio_review_fixture(origin)
            self.studio_review_check(r"""
 document.querySelector('[data-list]').dataset.undoSeconds='0';
 document.querySelector('[data-filter-row="show"] [data-filter-value="answers"]').click();
 await paint();
 for(const [number,state] of [[1,'Working'],[2,'Closed']]){
  const item=row(number);
  check(!item.hidden && item.dataset.state===state,'Answers shows the '+state+' item.');
  check(item.querySelector('[data-new-answer]'),'The '+state+' item has an unread reply.');
  item.querySelector('.list-toggle').click();
  const read=()=>recordDetails(item).querySelector('[value="read"]');
  await until(read);
  read().click();
  await until(()=>item.hidden && item.dataset.unreadAnswer==='false');
  check(item.dataset.state===state,'Mark read keeps the '+state+' state.');
 }
""")
        for destination in (waiting, closing):
            item = the_list.load(destination.keeper, f"L{destination.number}")
            self.assertFalse(the_list.unread_answer_ids(item))

    def test_studio_news_counts_a_closed_question_with_an_unread_linked_reply(self) -> None:
        members = staff.snapshot()
        key = f"question/{OWNER.id}/closed.md"
        prompt = list_exchange.Exchange(list_exchange.Source.STAFF_QUESTION, key, "initial",
            list_exchange.Direction.QUESTION_TO_NATE, "Which width?", NOW, OWNER.id, list_exchange.Nate.USER,
            list_exchange.ExchangeAnswer("Use 800 pixels.", NOW, list_exchange.AnswerRead.NOT_TRACKED))
        with the_list.lock_exchange_source(key) as source:
            row = the_list.file_exchanges(source, (prompt,), prompt.question, linked=the_list.NextNumber.AVAILABLE,
                                          owner=OWNER.id, members=members)
        identity = f"L{row.number}"
        _ = the_list.close(row.keeper, identity, "The width works.", source=OWNER.name,
                           assigned_to=the_list.ListCloseCaller(OWNER, False))
        request = console.NewsRequest(self.root)
        self.assertFalse(list_page.waiting_updates(request))

        key = "submission/question/closed-item-reply"
        replied_at = NOW + timedelta(minutes=1)
        reply = list_exchange.Exchange(list_exchange.Source.SCOPED_REPLY, key, "followup:1",
            list_exchange.Direction.ANSWER_TO_NATE, "Which height?", NOW, list_exchange.Nate.USER, OWNER.id,
            list_exchange.ExchangeAnswer("Use 600 pixels.", replied_at, list_exchange.AnswerRead.UNREAD))
        with the_list.lock_exchange_source(key) as source:
            _ = the_list.file_exchanges(source, (reply,), reply.question, linked=row, owner=OWNER.id, members=members)
        closed = the_list.load(row.keeper, identity)
        self.assertIs(closed.state, the_list.ItemState.CLOSED)
        self.assertEqual(list_page.waiting_updates(request),
                         {f"{list_page.ListRecord(row.keeper, closed).key}/{replied_at.isoformat()}"})
        _ = the_list.mark_read(row.keeper, identity, source=the_list.PAGE_SOURCE)
        self.assertFalse(list_page.waiting_updates(request))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_studio_evidence_opens_only_reviews_linked_to_the_item(self) -> None:
        review = investigation.file(OWNER.id, "Find the cause", "Read the failed check.", waiting_why="Work continues.")
        key = review_items.investigation_record_key(review)
        with self.studio_review_server() as origin:
            linked = review_page.PAGE_PATH + "?show=all#show=open&" + urlencode({"jump": key})
            wrapped = "/console/" + console.place_query("review", linked)
            unrelated = review_page.PAGE_PATH + "?" + urlencode({"item": "unrelated/I1"})
            foreign = "https://example.invalid" + linked
            item = self.add("Check the evidence", number=1, evidence="\n".join(
                (origin + linked, origin + wrapped, origin + unrelated, foreign)))
            _ = the_list.link(MORTY.id, item.id, (key,), source="morty")
            self.open_studio_review_fixture(origin)
            self.studio_review_check(r"""
 const item=row(1);item.querySelector('.list-toggle').click();
 const evidence=[...recordDetails(item).querySelectorAll('.item-links a')];
 check(evidence.length===4,'The item shows four Evidence links.');
 for(const link of evidence.slice(0,2)){
  const card=await open(link);
  check(card.querySelector('.review-box')?.dataset.key===KEY,'Evidence opens the linked review card.');
  check(dialog.querySelector('h2').textContent==='Item 1 · Check the evidence','The window names the source item.');
  close();
  check(document.activeElement===link,'Close returns focus to the Evidence link.');
 }
 for(const link of evidence.slice(2)){
  window.addEventListener('click',event=>{
   check(!event.defaultPrevented && !dialog.open && link.target==='_blank','The unrelated URL stays direct.');
   event.preventDefault();
  },{once:true});
  link.click();
 }
""".replace("KEY", json.dumps(key)))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_review_close_restores_the_source_row_after_refresh(self) -> None:
        review = investigation.file(OWNER.id, "Find the cause", "Read the failed check.", waiting_why="Work continues.")
        key = review_items.investigation_record_key(review)
        for number in range(1, 19):
            item = self.add(f"Item {number}", number=number)
            if number == 10:
                _ = the_list.link(MORTY.id, item.id, (key,), source="morty")
        with self.studio_review_server() as origin:
            self.open_studio_review_fixture(origin, "#" + urlencode({"sort": "#", "dir": "desc"}))
            self.studio_review_check(r"""
 const item=row(10),summary=item.querySelector('.list-row');
 await until(()=>summary.querySelector('a[data-list-review-card]'));
 scrollBy({top:summary.getBoundingClientRect().top-180,behavior:'instant'});
 const card=await open(summary.querySelector('a[data-list-review-card]'));
 check(!!card.querySelector('.review-box'),'The linked card is open.');
 window.reviewClosePosition={top:summary.getBoundingClientRect().top,scroll:scrollY};
""")
            for number in range(19, 27):
                _ = self.add(f"New item {number}", number=number)
            self.studio_review_check(r"""
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await until(()=>row(26) && row(10).querySelector('a[data-list-review-card]'));
 await paint();
 check(dialog.open,'The refresh keeps the review window open.');
 const item=row(10),summary=item.querySelector('.list-row'),link=summary.querySelector('a[data-list-review-card]');
 const {top,scroll}=window.reviewClosePosition;
 check(row(26).getBoundingClientRect().top<summary.getBoundingClientRect().top,'The new rows are above the item.');
 close();
 await paint();
 const bounds=link.getBoundingClientRect();
 check(!dialog.open && recordIsOpen(item) && document.activeElement===link,'Close focuses the source link.');
 check(Math.abs(summary.getBoundingClientRect().top-top)<2 && scrollY>scroll,
  'Close restores the row position in the updated table.');
 check(bounds.top>=0 && bounds.bottom<=innerHeight,'The source link is visible.');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_review_mark_read_updates_the_item_behind_the_window(self) -> None:
        review = investigation.file(OWNER.id, "Find the cause", "Read the failed check.", waiting_why="Work continues.")
        key = review_items.investigation_record_key(review)
        item = self.add("Read the linked answer", number=1)
        _ = the_list.link(MORTY.id, item.id, (key,), source="morty")
        with patch("scripts.review_links.answer_sources.file_saved_investigation"):
            _ = investigation.answer(review.owner, review.id, "The timeout was too short.")
        with self.studio_review_server() as origin:
            self.open_studio_review_fixture(origin)
            self.studio_review_check(r"""
 const summary=()=>row(1).querySelector('.list-row [data-list-review-summary]');
 await until(()=>summary()?.querySelector('a'));
 check(summary().textContent==='1 review for you','The linked answer starts unread.');
 const card=await open(summary().querySelector('a'));
 const button=card.querySelector('[data-review-form] button[value="read"]');
 button.form.requestSubmit(button);
 await until(()=>!dialog.open && summary()?.textContent.includes('Reviews done'));
 check(recordIsOpen(row(1)),'The item stays open after Mark read.');
 check(summary().textContent==='Reviews done — decide: close, keep or park','The item shows the completed review.');
""")
        self.assertIs(investigation.load(review.owner, review.id).state, investigation.InvestigationState.READ)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_list_links_show_the_target_inside_the_console(self) -> None:
        source = self.add("Follow the linked items", number=1)
        for number in range(2, 29):
            _ = self.add(f"Fixture item {number}", number=number)
        closed = self.add("Closed linked item", number=29, owner=OTHER.id)
        _ = the_list.close(MORTY.id, closed.id, "The work is complete.", source="morty")
        _ = self.add("Open blocker", number=30)
        other_producer = self.add("Linked item from another producer", number=31)
        _ = the_list.update(MORTY.id, other_producer.id, {"producer": DWIGHT.id}, source="morty")
        _ = the_list.update(MORTY.id, source.id, {"blocked_by": (
            the_list.ItemDependency(30, "The blocker is complete."),)}, source="morty")
        records = list_page.records(include_closed=True)
        for index, entry in enumerate(records):
            if entry.item.id == source.id:
                dependencies = (*entry.item.blocked_by, the_list.ItemDependency(29, "Check the closed blocker."),
                                the_list.ItemDependency(99, "Check the missing blocker."))
                records[index] = list_page.ListRecord(entry.keeper, replace(
                    entry.item, related_to=(2, 31), blocked_by=dependencies, unread_since=NOW))
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
 const links=recordDetails(document.querySelector('[data-number="1"]')).querySelectorAll('a');
 findListLink(links,event.data.clickListLink).click();
});
function findListLink(links,number){
 const label='#'+number;
 return [...links].find(link=>link.textContent===label || link.textContent.startsWith(label+' '));
}
</script></body>''')
        page = console.ConsolePage("studio", "Studio", "clapperboard", "The Studio", console.AvailablePage(studio_page.PAGE_PATH))
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
  if(scenario==='open'){
   const address=new URL(location.href);
   const place=new URL(address.searchParams.get('at.studio'),location.href);
   check(address.searchParams.get('page')==='studio' && !address.searchParams.has('at.list'),
    'The console replaces the old List place with the Studio place.');
   const filters=new URLSearchParams(place.hash.slice(1));
   check(place.pathname===win.location.pathname && (place.searchParams.get('show') || filters.get('show'))==='working'
    && filters.get('item')===source.dataset.key && win.recordIsOpen(source),
    'The old List bookmark opens the same item and keeps its filters.');
  }
  const chip=(name,value)=>doc.querySelector('[data-filter-row="'+name+'"] [data-filter-value="'+value+'"]');
  const selected=name=>doc.querySelector('[data-filter-row="'+name+'"] [aria-pressed="true"]').dataset.filterValue;
  const unread=doc.getElementById('list-unread');
  const filters=()=>[selected('owner'),selected('producer'),search.value,unread.checked];
  const pendingWords=scenario==='pending-words' ? 'item' : '';
  const originalFilters=filters();
  if(pendingWords) originalFilters[2]=pendingWords;
  const link=win.findListLink(win.recordDetails(source).querySelectorAll('a'),number);
  const sourceForm=win.recordDetails(source).querySelector('[data-feedback-form]');
  if(scenario==='closed'){
   sourceForm.elements.words.value='Keep the source draft.';
   sourceForm.elements.words.dispatchEvent(new win.Event('input',{bubbles:true}));
  }
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
  if(scenario==='banner'){
   const measured=new Promise(resolve=>win.addEventListener('page-top-resize',resolve,{once:true}));
   banner.style.minHeight='60px';
   await measured;
  }
  const originalTop=win.scrollY,beforeJump=win.location.href;
  const original=new URL(beforeJump),params=new URLSearchParams(original.hash.slice(1));
  if(pendingWords){params.set('q',pendingWords);original.hash=String(params);}
  const originalPlace=original.href;
  const debounced=pendingWords ? new Promise(done=>search.addEventListener('filterchange',done,{once:true})) : null;
  const consolePlace=()=>new URL(new URLSearchParams(location.search).get('at.studio'),location.href).href;
  const updated=new Promise(resolve=>{
   const done=()=>{
    clearTimeout(timeout);removeEventListener('message',record);resolve();
   };
   const record=event=>{
    if(event.source===win && event.data==='console-place' && win.location.href!==beforeJump &&
       consolePlace()===win.location.href) done();
   };
   addEventListener('message',record);
   const timeout=setTimeout(done,1500);
  });
  win.postMessage({clickListLink:number,pendingWords},location.origin);await updated;await frames(win);
  if(debounced){
   await debounced;
   check(!target.hidden && expanded(target) && search.value==='','the target stays open after the pending input settles');
  }
  const pageNotice=doc.querySelector('[data-missing-item]');
  const sourceNotice=()=>win.recordDetails(source).closest('[data-record-inline-view]')?.querySelector('[data-record-link-notice]');
  const missing=()=>sourceNotice() ?? pageNotice;
  const keptSource=()=>expanded(source) && win.recordDetails(source).contains(sourceForm) &&
   sourceForm.elements.words.value==='Keep the source draft.';
  check(doc===frame.contentDocument,'the click stays inside the same console frame');
  check(consolePlace()===win.location.href,'the console recorded '+consolePlace()+' instead of '+win.location.href);
  if(number==='99' || number==='29'){
   const message=number==='29' ? 'Item #29 is closed' : 'Item #99 is not on the List';
   const notice=missing();
   check((!target || target.hidden) && pageNotice.hidden && notice===sourceNotice() && !notice.hidden &&
    notice.textContent===message && inView(notice),'The expanded row shows the target notice.');
   if(scenario==='closed') check(keptSource(),'A closed target keeps the source form and draft.');
  }else{
   const title=target.querySelector('.list-row');
   const fallback=['producer','open','pending-words','unread-words'].includes(scenario);
   check(selected('show')===(fallback ? 'all' : 'working'),'the view after the click is '+selected('show'));
   check(!target.hidden && expanded(target),'the target is visible and open');
   check(inView(title),'the target row is outside the viewport at '+title.getBoundingClientRect().top);
   check(title.getAnimations().some(animation=>animation.playState==='running'),'the target row has a short highlight');
   if(scenario==='banner'){
    const frozen=banner.closest('.page-top'), box=frozen.getBoundingClientRect();
    const top=target.getBoundingClientRect().top;
    check(win.getComputedStyle(frozen).position==='sticky' && Math.abs(box.top)<1 &&
     banner.getBoundingClientRect().height>=60,'the tall status line stays in the frozen top');
    const gap=top-box.bottom;
    check(Math.abs(gap-16)<=1,'The row starts 16 px below the frozen top. Found '+gap+' px.');
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
   const valid=win.findListLink(win.recordDetails(source).querySelectorAll('[data-record-jump]'),2);
   valid.click();
   check(expanded(row(2)) && !expanded(source) && missing().hidden,'A valid link opens its row and clears the notice.');
   const backToMissing=changed(win);
   history.back();await backToMissing;await frames(win);
   check(win.location.href===missingPlace && keptSource() && !expanded(row(2)) &&
    !missing().hidden && missing().textContent==='Item #29 is closed',
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
   await navigated;await frames(win);
   const targetKey=new URLSearchParams(link.hash.slice(1)).get('item');
   const hasNotice=()=>sourceNotice()?.textContent==='Item #29 is closed' && !sourceNotice()?.hidden;
   const records=()=>win.history.state?.recordTable;
   check(keptSource() && hasNotice() && records()?.opened[0]===source.dataset.key && records()?.linkedItem===targetKey,
    'A direct link to a closed item shows the notice and keeps the source open.');
   const back=changed(win);history.back();await back;await frames(win);
   check(keptSource() && missing().hidden && records()?.opened[0]===source.dataset.key,
    'Back clears the notice and keeps the source draft.');
   const forward=changed(win);history.forward();await forward;await frames(win);
   check(keptSource() && hasNotice() && records()?.linkedItem===targetKey,
    'Forward restores the source draft and the notice for the closed item.');
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
    check(!missing().hidden && doc.querySelector('[data-missing-keys]').hidden,'the restored missing item shows one notice');
    await select();
    const after=new URLSearchParams(win.location.hash.slice(1));
    check(missing().hidden && !doc.querySelector('[data-missing-keys]').textContent,label+' clears the missing item notice');
    check(after.get('item')!==missingKey && after.getAll('key').join()===source.dataset.key,label+' omits the missing item from the address');
   }
  }
 }catch(error){failures.push(String(error));}
 await fetch('/list-jump-result',{method:'POST',body:JSON.stringify(failures)});
},{once:true});
</script><img hidden src="/list-jump-ready">'''
        shell = shell.replace('</body>', probe + '</body>')
        results: list[list[str]] = []
        complete = threading.Event()

        class Probe(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                if path == '/list-jump-ready':
                    _ = complete.wait(35)
                body = listing if path == studio_page.PAGE_PATH else shell.encode() if path == "/console/" else b'{}'
                self.send_response(200)
                self.send_header('Content-Type', 'text/html; charset=utf-8' if body != b'{}' else 'application/json')
                self.end_headers()
                try:
                    _ = self.wfile.write(body)
                except BrokenPipeError:
                    pass

            def do_POST(self) -> None:
                if self.path == '/list-jump-result':
                    results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                    complete.set()
                self.send_response(204)
                self.end_headers()

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(('127.0.0.1', 0), Probe) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                proof = os.environ.get("LIST_JUMP_PROOF")
                cases = [(30, "open"), (29, "closed"), (31, "producer"), (99, "missing"), (29, "repeat"),
                         (2, "history"), (2, "banner"), (30, "pending-words"), (30, "unavailable-producer"),
                         (30, "unread-words")]
                if proof:
                    cases.append((29, "proof"))
                for number, case in cases:
                    with self.subTest(link=case):
                        complete.clear()
                        results.clear()
                        filters = {"sort": "#", "item": f"{MORTY.id}/{source.id}"}
                        if case in ("open", "closed", "producer", "missing"):
                            filters.update(owner=OWNER.id, producer=MORTY.id, q="linked item")
                        if case == "unread-words":
                            filters.update(unread="1", q="item")
                        place = studio_page.PAGE_PATH + "?show=working#" + urlencode(filters)
                        if case == "open":
                            old_place = list_page.PAGE_PATH + place.removeprefix(studio_page.PAGE_PATH)
                            url = "/console/" + console.place_query("list", old_place)
                        else:
                            url = "/console/" + console.place_query("studio", place)
                        destination = f'http://127.0.0.1:{server.server_port}{url}&number={number}&case={case}'
                        if case == "proof" and proof:
                            result = self.firefox_view(destination, Path(proof), 1440, 1100, complete, dark=True)
                            self.assertEqual(result.returncode, 0, result.stderr)
                        else:
                            with self.firefox().page(destination, 1440, 1100, dark=True):
                                self.assertTrue(complete.wait(30), "The browser must report the result of the link click.")
                        self.assertEqual(results, [[]])
            finally:
                complete.set()
                server.shutdown()
                thread.join(5)

    def test_staff_owner_and_producer_names_have_role_colors_but_nate_stays_plain(self) -> None:
        _ = self.add("Staff names")
        _ = self.add("User name", owner=the_list.UnassignedOwner.NATE)
        body = list_page.render(self.root / "reviews").decode()
        self.assertRegex(body, r'data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(body, r'data-color-text="role.producer"[^>]*>morty</span>')
        self.assertIn('<td data-label="Owner"><span class="list-owner-name"><span>nate</span></span></td>', body)

    def test_added_log_shows_staff_colors_local_eta_and_field_labels(self) -> None:
        item = self.add("Log labels")
        _ = the_list.update(MORTY.id, item.id, {"unit": "fixture-unit"}, source="morty")
        body = list_page.render(self.root / "reviews").decode()
        log = body.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]
        self.assertRegex(log, r'Owner</dt><dd><span[^>]*data-color-text="role.showrunner"[^>]*>sdlc</span>')
        self.assertRegex(log, r'<dt><span[^>]*data-color-text="role.producer" style="color:#90fe42">Producer</span></dt>'
                         + r'<dd><span[^>]*data-color-text="role.producer"[^>]*>morty</span>')
        self.assertRegex(log, r'<dt><span[^>]*data-color-text="role.unit-director" style="color:#ffaf02">Unit</span></dt>')
        self.assertIn("ETA</dt><dd>14:00 EST – 15:00 EST</dd>", log)
        self.assertIn("State</dt><dd>Working</dd>", log)
        self.assertIn("Next step</dt><dd>", log)
        self.assertIn("Latest status</dt><dd>", log)
        shown = re.sub(r"<[^>]+>", "", log)
        for text in (OWNER.id, MORTY.id, "EtaRange(", "next_step", "latest_status"):
            self.assertNotIn(text, shown)

    def render_item_links_panel(self, entry: list_page.ListRecord, sections: str, today: datetime,
                                names: staff_names.StaffNames, unread_answers: frozenset[str], *,
                                schedule: list_page.DisplayedSchedule = the_list.NoSchedule.UNSET) -> str:
        progress = list_page.item_progress(entry.item, names, schedule)
        return list_page.detail_panel(entry, sections, today, names, unread_answers, progress, schedule=schedule)

    def test_item_text_links_http_urls_and_escapes_other_text(self) -> None:
        source = 'See <proof> (https://console.test/path?x=1&y=2), http://console.test/end.:<next>"'
        first = ('<a class="item-url" href="https://console.test/path?x=1&amp;y=2" target="_blank" '
                 'rel="noopener">https://console.test/path?x=1&amp;y=2</a>')
        second = ('<a class="item-url" href="http://console.test/end" target="_blank" '
                  'rel="noopener">http://console.test/end</a>')
        self.assertEqual(list_page.item_text_html(source), f'See &lt;proof&gt; ({first}), {second}.:&lt;next&gt;&quot;')
        self.assertEqual(list_page.item_text_html('http://console.test/end>"'), second + '&gt;&quot;')
        self.assertEqual(list_page.item_text_html('http://console.test/end"caption'), second + '&quot;caption')
        self.assertEqual(list_page.item_text_html('javascript:alert(1) ftp://console.test/path'),
                         'javascript:alert(1) ftp://console.test/path')
        self.assertEqual(list_page.item_text_html('javascript:http://console.test/end', paragraphs=True),
                         f'<p>javascript:{second}</p>')
        self.assertEqual(list_page.item_text_html('See **proof** http://console.test/end.', paragraphs=True),
                         f'<p>See <strong>proof</strong> {second}.</p>')

    def test_item_text_splits_adjacent_urls_and_keeps_bold(self) -> None:
        first, second = "https://one.test/a,b", "https://two.test/b"
        markup = list_page.item_text_html(f"{first},{second}")
        anchors = ElementTree.fromstring(f"<p>{markup}</p>").findall("a")
        self.assertEqual([anchor.get("href") for anchor in anchors], [first, second])
        self.assertEqual(anchors[0].tail, ",")
        markup = list_page.item_text_html(f"{first},ftp://two.test/b")
        anchors = ElementTree.fromstring(f"<p>{markup}</p>").findall("a")
        self.assertEqual([anchor.get("href") for anchor in anchors], [first])
        self.assertEqual(anchors[0].tail, ",ftp://two.test/b")
        markup = list_page.item_text_html(f"**{second}**", paragraphs=True)
        bold = ElementTree.fromstring(markup).findall("strong/a")
        self.assertEqual([(anchor.get("href"), anchor.text) for anchor in bold], [(second, second)])

    def test_item_text_uses_the_url_end_rule(self) -> None:
        example = "https://example.test/a"
        wildcard = "https://example.test/build/*/linux"
        apostrophe = "https://example.test/it's/x"
        first, second = "https://one.test/a", "https://two.test/b"
        short_first, short_second = "https://a.test/x", "https://b.test/y"
        cases = (
            (f"See '{first}','{second}'.", (first, second)),
            (f"**{example}**", (example,)),
            (wildcard, (wildcard,)),
            (apostrophe, (apostrophe,)),
            (f"({example}).", (example,)),
            (f"{short_first},{short_second}", (short_first, short_second)),
            (f"https://a.test/{short_second}", ("https://a.test/", short_second)),
            (f"{short_first}{short_second}", ("https://a.test/",)),
            (f"{short_first},ftp://b.test/y", (short_first,)),
            (f"{short_first},ftp://b.test/https://c.test/z", (short_first, "https://c.test/z")),
            (f"{short_first},Git+ssh-2.0://b.test/y,{short_second}", (short_first, short_second)),
            (f"https://a.test/12ftp://b.test/y", ("https://a.test/12",)),
            (f'"{example}"', (example,)),
            (f"Did you read {example}?", (example,)),
        )
        for source, urls in cases:
            with self.subTest(source=source):
                markup = ElementTree.fromstring(f"<p>{list_page.item_text_html(source)}</p>")
                self.assertEqual("".join(markup.itertext()), source)
                self.assertEqual([(anchor.get("href"), anchor.text) for anchor in markup.findall("a")],
                                 [(url, url) for url in urls])

    def test_item_links_exclude_trailing_punctuation_and_list_one_destination(self) -> None:
        url = "https://example.test/check"
        marks = ("!", "?", ";", "]", "'", ".", ",", ")", ":", "?!;].")
        for mark in marks:
            with self.subTest(mark=mark):
                markup = list_page.item_text_html(f"Read {url}{mark}", paragraphs=True)
                anchor = ElementTree.fromstring(markup).find("a")
                assert anchor is not None
                self.assertEqual((anchor.get("href"), anchor.text, anchor.tail), (url, url, mark))
        ipv6 = "https://[2001:db8::1]"
        anchors = ElementTree.fromstring(list_page.item_text_html(f"Read {ipv6}]!", paragraphs=True)).findall("a")
        self.assertEqual([(anchor.get("href"), anchor.tail) for anchor in anchors], [(ipv6, "]!")])

        status = " ".join([*(f"{url}{mark}" for mark in marks), url])
        item = the_list.add(MORTY.id, "Read the check", owner=OWNER.id, source="nate",
                            changes={"latest_status": status})
        panel = self.render_item_links_panel(list_page.ListRecord(MORTY.id, item), "", NOW, staff_names.read(), frozenset())
        section = panel.split('<section class="item-links">', 1)[1].split("</section>", 1)[0]
        anchors = ElementTree.fromstring(f"<section>{section}</section>").findall("ul/li/a")
        self.assertEqual([anchor.get("href") for anchor in anchors], [url])

    def test_links_include_the_scheduled_next_step(self) -> None:
        urls = ["https://one.test/check", "https://two.test/check"]
        run = the_list.ScheduledRun(NOW + timedelta(days=1), ",".join(urls))
        item = the_list.add(MORTY.id, "Check the schedule", owner=OWNER.id, source="nate",
                            changes={"state": the_list.ItemState.SCHEDULED, "schedule": run})
        names = staff_names.read()
        for record in (item, replace(item, log=())):
            with self.subTest(has_log=bool(record.log)):
                panel = self.render_item_links_panel(
                    list_page.ListRecord(MORTY.id, record), "", NOW, names, frozenset(),
                    schedule=list_page.schedule_display(record))
                self.assertIn('<section class="item-links">', panel)
                section = panel.split('<section class="item-links">', 1)[1].split("</section>", 1)[0]
                rows = ElementTree.fromstring(f"<section>{section}</section>").findall("ul/li")
                self.assertEqual([(row[0].get("href"), row[1].text) for row in rows],
                                 [(url, "Next step") for url in urls])

    def test_schedule_log_splits_adjacent_urls(self) -> None:
        urls = ["https://one.test/check", "https://two.test/check"]
        first = the_list.ScheduledRun(NOW + timedelta(days=1), ",".join(urls))
        second_urls = ["https://three.test/check", "https://four.test/check"]
        second = the_list.ScheduledRun(first.start + timedelta(days=1), ",".join(second_urls))
        series = the_list.ScheduledSeries(
            first, repeat_rule.parse("FREQ=DAILY;COUNT=2"), the_list.series_run_limit(),
            exceptions=(the_list.RunException(2, second),))
        item = the_list.add(MORTY.id, "Check the schedule log", owner=OWNER.id, source="nate",
                            changes={"state": the_list.ItemState.SCHEDULED, "schedule": series})
        log = list_page.item_log(list_page.ListRecord(MORTY.id, item), NOW, staff_names.read(), frozenset())
        anchors = ElementTree.fromstring(f"<ol>{log.markup}</ol>").findall(".//a")
        self.assertEqual([(anchor.get("href"), anchor.tail) for anchor in anchors],
                         [(urls[0], ","), (urls[1], None), (second_urls[0], ","), (second_urls[1], None)])

    def test_blocking_condition_links_keep_asterisks_and_apostrophes_inside_paths(self) -> None:
        _ = self.add("Blocking item", number=210)
        wildcard = "https://example.test/build/*/linux"
        apostrophe = "https://example.test/build/o'brien"
        item = the_list.add(MORTY.id, "Inspect the links", owner=OWNER.id, source="nate", changes={
            "blocked_by": (the_list.ItemDependency(210, f"See {wildcard},{apostrophe}'*?!."),),
        })
        panel = self.render_item_links_panel(list_page.ListRecord(MORTY.id, item), "", NOW, staff_names.read(), frozenset())
        section = panel.split('<section class="item-links">', 1)[1].split("</section>", 1)[0]
        parsed = ElementTree.fromstring('<section>' + section.replace(' data-record-jump ', ' data-record-jump="" ')
                                        + '</section>')
        inline = parsed.findall("ul/li/a[@class='item-url']")
        self.assertEqual([(anchor.get("href"), anchor.text, anchor.tail) for anchor in inline], [
            (wildcard, wildcard, ","),
            (apostrophe, apostrophe, "'*?!."),
        ])
        labels = parsed.findall("ul/li/a[@class='item-link-label']")
        self.assertEqual([anchor.get("href") for anchor in labels], [wildcard, apostrophe])

    def test_item_url_labels_follow_generated_console_destinations(self) -> None:
        output = io.StringIO()
        with (patch.object(images, "register", return_value={"id": "sample", "file": "image.png"}),
              patch.object(review_link, "link_host", return_value="console.test"), redirect_stdout(output)):
            self.assertEqual(images.main(["register", "--image", "image.png", "--title", "A fixture image",
                                         "--owner", OWNER.id, "--phase", "itemlinks"]), 0)
        image_link = output.getvalue().splitlines()[1].removeprefix("Image link: ")
        origin = "http://console.test:8765"
        self.assertEqual(urlsplit(image_link).netloc, "console.test:8765")
        relative_image = image_link.removeprefix(origin)
        list_place = studio_page.PAGE_PATH + list_page.item_link(MORTY.id, 210)
        review_place = review_page.PAGE_PATH + "#key=sdlc%2FC1"
        for path, expected in (
            (relative_image, "Images page"),
            (images.PAGE_PATH + "#image=sample", "Images page"),
            ("/console/" + console.place_query("studio", list_place), "List item 210"),
            (list_place, "List item 210"),
            (list_page.PAGE_PATH + list_page.item_link(MORTY.id, 210), "List item 210"),
            ("/console/" + console.place_query("list", list_page.PAGE_PATH + list_page.item_link(MORTY.id, 210)),
             "List item 210"),
            ("/console/" + console.place_query("review", review_place), "Review item"),
            (review_page.card_url("sdlc/C1"), "Review item"),
        ):
            for prefix in ("", "/tenant/desk"):
                with self.subTest(path=path, prefix=prefix):
                    self.assertEqual(list_page.item_url_label(urlsplit(origin + prefix + path)), expected)

    def test_item_links_lists_oldest_sources_once(self) -> None:
        images = "http://console.test:8765/console/images#image=sample"
        related = "http://console.test:8765/console/list.html#item=keeper%2FL210"
        review = "http://console.test:8765/console/review.html#key=owner%2FC1"
        external = "https://example.test/check?x=1&y=2"
        item = the_list.add(MORTY.id, "Review the links", owner=OWNER.id,
                            source="nate", changes={"latest_status": f"See {images}."})
        with patch.object(the_list, "now", return_value=NOW + timedelta(hours=2)):
            item = the_list.update(MORTY.id, item.id,
                                   {"evidence": f"{related}\n{images}"}, source="morty")
        note = the_list.ItemChange(NOW + timedelta(hours=3), f"Also read {external}.", "morty")
        answer = question_record.Answer(f"Check {review}.", (NOW + timedelta(hours=1)).isoformat())
        question = question_record.Question("Q1", NOW.isoformat(), "Where is the review?", answer=answer)
        item = replace(item, log=(*item.log, note), questions=(question,))
        names = staff_names.read()
        entry = list_page.ListRecord(MORTY.id, item)
        panel = self.render_item_links_panel(entry, '<form></form>', NOW.astimezone(user_zone()), names, frozenset({"Q1"}))
        links = panel.split('<section class="item-links">', 1)[1].split("</section>", 1)[0]
        rows = ElementTree.fromstring(f"<section>{links}</section>").findall("ul/li")
        self.assertEqual([(row[0].text, row[1].text, row[2].text) for row in rows], [
            ("Images page", "Latest status", images),
            ("Review item", "Answer", review),
            ("List item 210", "Evidence", related),
            ("example.test/check", "Note", external),
        ])
        self.assertEqual([row[0].get("href") for row in rows], [images, review, related, external])
        self.assertLess(panel.index('class="list-item-fields"'), panel.index('class="item-links"'))
        self.assertLess(panel.index('class="item-links"'), panel.index('<form>'))
        self.assertIn(f'<a class="item-url" href="{html.escape(external)}"', panel)
        questions = list_page.panel_questions(item, NOW, frozenset({"Q1"}))
        self.assertIn(f'<a class="item-url" href="{review}" target="_blank" rel="noopener">', questions)
        empty = the_list.add(MORTY.id, "No addresses", owner=OWNER.id, source="nate", changes={})
        empty_panel = self.render_item_links_panel(
            list_page.ListRecord(MORTY.id, empty), "", NOW, names, frozenset())
        self.assertNotIn('<h3>Links</h3>', empty_panel)

    def test_later_blocking_condition_keeps_the_oldest_url_source_and_order(self) -> None:
        _ = self.add("Blocking item", number=210)
        shared = "https://console.test/console/images#image=sample"
        next_step = "https://example.test/next"
        condition = "https://example.test/condition"
        item = the_list.add(MORTY.id, "Inspect the links", owner=OWNER.id, source="nate",
                            changes={"latest_status": f"See {shared}."})
        with patch.object(the_list, "now", return_value=NOW + timedelta(hours=1)):
            item = the_list.update(MORTY.id, item.id, {"next_step": f"Read {next_step}."}, source="morty")
        with patch.object(the_list, "now", return_value=NOW + timedelta(hours=2)):
            item = the_list.update(MORTY.id, item.id, {
                "blocked_by": (the_list.ItemDependency(210, f"Check {shared} and {condition}."),),
            }, source="morty")
        panel = self.render_item_links_panel(list_page.ListRecord(MORTY.id, item), "", NOW, staff_names.read(), frozenset())
        section = panel.split('<section class="item-links">', 1)[1].split("</section>", 1)[0]
        parsed = ElementTree.fromstring(f"<section>{section.replace(' data-record-jump ', ' data-record-jump="" ')}</section>")
        anchors = parsed.findall("ul/li/a[@class='item-link-label']")
        self.assertEqual([(anchor.get("href"), anchor.text) for anchor in anchors], [
            (shared, "Images page"),
            (next_step, "example.test/next"),
            (condition, "example.test/condition"),
        ])
        self.assertEqual([field.text for field in parsed.findall("ul/li/span[@class='item-link-field']")],
                         ["Latest status", "Next step", "Blocked by"])

    def test_closed_question_keeps_its_original_link_source_and_order(self) -> None:
        shared = "https://example.test/check"
        question_url = "https://example.test/question"
        status_url = "https://example.test/status"
        item = the_list.add(MORTY.id, "Inspect the links", owner=OWNER.id, source="nate", changes={})
        asked_at = NOW + timedelta(hours=1)
        question_clock = Mock(wraps=datetime)
        question_clock.configure_mock(**{"now.return_value": asked_at})
        with patch.object(page_question, "datetime", question_clock):
            fields = self.fields(item, "question", words=f"Did you read {shared} and {question_url}?")
            _ = list_page.submit(self.root / "reviews", urlencode(fields))
        with patch.object(the_list, "now", return_value=NOW + timedelta(hours=2)):
            _ = the_list.update(MORTY.id, item.id, {"latest_status": f"Read {shared} and {status_url}."},
                                source="morty")
        with patch.object(the_list, "now", return_value=NOW + timedelta(hours=3)):
            _ = the_list.close(MORTY.id, item.id, "The check is complete.", source="nate")
        with patch.object(the_list, "now", return_value=NOW + timedelta(hours=4)):
            item = the_list.reopen(MORTY.id, item.id, source="nate")
        self.assertEqual(item.questions, ())
        panel = self.render_item_links_panel(list_page.ListRecord(MORTY.id, item), "", NOW, staff_names.read(), frozenset())
        section = panel.split('<section class="item-links">', 1)[1].split("</section>", 1)[0]
        rows = ElementTree.fromstring(f"<section>{section}</section>").findall("ul/li")
        self.assertEqual([(row[0].get("href"), row[1].text) for row in rows], [
            (shared, "Question"),
            (question_url, "Question"),
            (status_url, "Latest status"),
        ])

    def test_collapsed_list_row_keeps_item_urls_plain(self) -> None:
        url = "https://console.test/check"
        _ = the_list.add(MORTY.id, f"See {url}", owner=OWNER.id, source="nate", changes={
            "next_step": f"Read **this** {url}.",
            "next_check": the_list.CheckCondition(url),
            "eta": the_list.EtaDescription(url),
        })
        body = list_page.render(self.root / "reviews", include_counts=False).decode()
        row = body.split('<tr class="list-row">', 1)[1].split("</tr>", 1)[0]
        self.assertIn(f"<strong>See {url}</strong>", row)
        self.assertIn(f"<p>Read <strong>this</strong> {url}.</p>", row)
        self.assertNotIn("<a ", row)
        detail = body.split('<div class="list-panel">', 1)[1].split('<script', 1)[0]
        self.assertIn(f'href="{url}" target="_blank" rel="noopener"', detail)

    def test_linked_items_lists_a_closed_relation_once_and_keeps_blockers_and_urls(self) -> None:
        related = self.add("Related item", number=210)
        _ = self.add("Blocking item", number=211)
        url = "https://console.test/check"
        item = the_list.add(MORTY.id, "Inspect the links", owner=OWNER.id, source="nate", changes={
            "latest_status": f"See {url}.", "related_to": (210,),
            "blocked_by": (the_list.ItemDependency(211, "The check passes."),),
        })
        _ = the_list.close(MORTY.id, related.id, "The check is complete.", source="morty")
        body = list_page.render(self.root / "reviews", include_counts=False).decode()
        row = body.split(f'data-key="{MORTY.id}/{item.id}"', 1)[1].split("</tbody>", 1)[0]
        self.assertEqual(row.count("<h3>Links</h3>"), 1)
        self.assertIn('<h3>Linked items · 1 of 1 closed</h3>', row)
        link = html.escape(list_page.item_link(MORTY.id, related.number), quote=True)
        self.assertEqual(row.count(f'href="{link}&amp;reveal=1"'), 1)
        self.assertNotIn(f'href="{link}"', row)
        section = row.split('<section class="item-links">', 1)[1].split("</section>", 1)[0]
        parsed = ElementTree.fromstring(f"<section>{section.replace(' data-record-jump ', ' data-record-jump="" ')}</section>")
        anchors = parsed.findall("ul/li/a")
        self.assertEqual([(anchor.text, anchor.get("href"), anchor.get("target")) for anchor in anchors], [
            ("#211", list_page.item_link(MORTY.id, 211), None),
            ("console.test/check", url, "_blank"),
        ])
        self.assertIn("data-record-jump", anchors[0].attrib)
        self.assertEqual(anchors[-1].get("rel"), "noopener")

    def test_blocking_condition_links_the_url_once_in_the_links_section(self) -> None:
        _ = self.add("First blocking item", number=210)
        _ = self.add("Second blocking item", number=211)
        url = "https://example.test/check"
        item = the_list.add(MORTY.id, "Inspect the check", owner=OWNER.id, source="nate", changes={
            "latest_status": f"Read {url}.",
            "blocked_by": (the_list.ItemDependency(210, f"Check <proof> at {url}!"),
                           the_list.ItemDependency(211, f"Confirm {url}.")),
        })
        panel = self.render_item_links_panel(list_page.ListRecord(MORTY.id, item), "", NOW, staff_names.read(), frozenset())
        section = panel.split('<section class="item-links">', 1)[1].split("</section>", 1)[0]
        parsed = ElementTree.fromstring(f"<section>{section.replace(' data-record-jump ', ' data-record-jump=\"\" ')}</section>")
        rows = parsed.findall("ul/li")
        self.assertEqual([row[0].text for row in rows], ["#210", "#211", "example.test/check"])
        self.assertEqual(rows[0][0].tail, " — Check <proof> at ")
        self.assertEqual(rows[1][0].tail, " — Confirm ")
        links = parsed.findall("ul/li/a[@class='item-url']")
        self.assertEqual([anchor.get("href") for anchor in links], [url, url])
        self.assertEqual([(anchor.text, anchor.get("target"), anchor.get("rel"), anchor.tail) for anchor in links],
                         [(url, "_blank", "noopener", "!"), (url, "_blank", "noopener", ".")])
        self.assertIs(links[0], rows[0][1])
        self.assertIs(links[1], rows[1][1])
        self.assertEqual([anchor.get("href") for anchor in parsed.findall("ul/li/a[@class='item-link-label']")], [url])

    def test_links_scan_skips_url_free_history_and_keeps_current_evidence(self) -> None:
        item = the_list.add(MORTY.id, "History without URLs", owner=OWNER.id, source="nate", changes={})
        history = tuple(the_list.ItemChange(NOW, f"Note {index}.", "morty") for index in range(32))
        item = replace(item, log=(*item.log, *history))
        names = staff_names.read()
        with patch.object(list_page, "item_url_matches", wraps=list_page.item_url_matches) as matches:
            panel = self.render_item_links_panel(list_page.ListRecord(MORTY.id, item), "", NOW, names, frozenset())
        matches.assert_not_called()
        self.assertNotIn('<h3>Links</h3>', panel)
        url = "https://console.test/current"
        legacy = replace(item, evidence=f"See {url}.")
        panel = self.render_item_links_panel(list_page.ListRecord(MORTY.id, legacy), "", NOW, names, frozenset())
        self.assertIn(f'href="{url}" target="_blank" rel="noopener">console.test/current</a>', panel)

    def test_browser_latest_status_links_open_a_new_tab_and_wrap(self) -> None:
        url = "https://console.test:8765/check/" + "proof" * 90
        _ = the_list.add(MORTY.id, "Check the result", owner=OWNER.id, source="nate",
                         changes={"latest_status": f"See {url}."})
        self.browser_action_sequence(r"""
 const panel=recordDetails(row).querySelector('.list-panel');
 const link=panel.querySelector('.list-field-latest_status a');
 const section=panel.querySelector('.item-links');
 check(link?.target==='_blank' && link.relList.contains('noopener'),'Latest status opens a new tab.');
 check(link?.href==='https://console.test:8765/check/'+'proof'.repeat(90),'The link keeps the whole URL.');
 check(link?.getClientRects().length>1,'The inline link wraps at phone width.');
 check(section.clientWidth===section.scrollWidth,'The Links section fits at phone width.');
""", add_item=False, reduced_motion=True, window_size=(390, 1000))

    def test_studio_producer_tabs_and_both_tables_label_the_producer(self) -> None:
        _ = self.add("Producer labels")
        body = list_page.render(self.root / "reviews", include_counts=False).decode()
        label = '<span class="staff-name" data-color-text="role.producer" style="color:#90fe42">Producer</span>'
        self.assertIn('aria-label="Producer" data-filter-row="producer"><span aria-hidden="true">Producer</span>', body)
        self.assertIn('<th scope="col" aria-sort="none" data-list-producer>'
                      + '<button type="button" id="list-sort-2" data-sort="2">Producer', body)
        self.assertIn('th[data-list-producer] button{color:var(--producer-label-color,inherit)}', body)
        self.assertIn(f'<th scope="col">{label}</th>', body.split('<section data-linked-item', 1)[1])
        self.assertIn('--producer-label-color:#90fe42', body)
        self.assertIn('td[data-label="Producer"]::before{color:var(--producer-label-color,var(--ink-3))}', body)
        self.assertIn('<td data-label="Producer">', body)

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
        added_html = ('Read <a class="item-url" href="https://example.invalid/proof?x=1&amp;y=2" '
                      'target="_blank" rel="noopener">https://example.invalid/proof?x=1&amp;y=2</a>.')
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
        log = list_page.item_log(list_page.ListRecord(MORTY.id, item), NOW.astimezone(user_zone()),
                                 staff_names.read(), frozenset()).markup
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

    def test_report_hides_latest_status_only_when_it_repeats_the_report_words(self) -> None:
        item = self.file_repaired_report("Fix the menu")
        assert isinstance(item.report, the_list.ListReport)
        report_words = item.report.words
        for status, shows_status in ((report_words, False), ("The cause is known.", True), (report_words, False)):
            with self.subTest(status=status):
                _ = the_list.update(MORTY.id, item.id, {"latest_status": status}, source="dwight")
                saved = list_page.saved_action_state(MORTY.id, item.id, include_detail=True)
                left = saved.get("detail_html", "").split('<section class="list-log-panel">', 1)[0]
                self.assertEqual(left.count(f"<p>{report_words}</p>"), 1)
                self.assertEqual('<section class="list-field-latest_status">' in left, shows_status)
                if shows_status:
                    self.assertIn(f"<p>{status}</p>", left)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_report_page_details_open_separately_from_the_screenshot(self) -> None:
        from scripts.review_links.test_review_submissions import PNG

        words = "The menu hides choices."
        viewport = new_item_dialog.PageViewport(1440, 900, True)
        state = new_item_dialog.BugPageState(
            "http://127.0.0.1/console/list.html", viewport,
            0.25, ("One menu option is missing.",), (), "Show All", True, "The List")
        request = new_item_dialog.NewItemRequest(
            f"r1-{uuid4().hex}-bug-{MORTY.id}", new_item_dialog.SubmissionKind.BUG, MORTY.id,
            words, new_item_dialog.NoEvidence(), page_state=state)
        saved = review_submissions.submit(
            request, screenshot=new_item_dialog.PageScreenshot(base64.b64decode(PNG), viewport))
        native = saved.record
        assert isinstance(native, investigation.Investigation)
        report = the_list.ListReport(the_list.ReportKind.BUG, review_items.investigation_record_key(native), words,
                                     saved.key)
        _ = the_list.file_report(MORTY.id, report, native.title, owner=OWNER.id, producer=DWIGHT.id,
                                 unit="", source="nate")
        self.browser_action_sequence(r"""
 const box=recordDetails(row).querySelector('[data-submission-page-state]');
 const details=box.querySelector('details'),summary=details.querySelector('summary');
 const table=details.querySelector('dl'),thumbnail=box.querySelector('figure img');
 check(!details.open && summary.textContent==='Page details' && box.innerText.includes('Screenshot of The List') &&
  !box.innerText.includes('Show All') && thumbnail.getClientRects().length,
  'Page details start closed and the picture stays visible.');
 const style=getComputedStyle(box);
 check(style.paddingBlockStart==='12px' && style.paddingInlineStart==='14px' && style.marginBlockStart==='12px',
  'The report card has padding and space above it.');
 summary.click();
 check(details.open && table.innerText.includes('Show All') && table.innerText.includes('0.25 s'),
  'The disclosure shows the saved page details.');
 summary.click();
 thumbnail.click();
 const zoom=document.querySelector('[data-image-zoom-dialog]');
 check(zoom.open && zoom.querySelector('img').src===thumbnail.src && !details.open,
  'The picture opens the shared zoom with the original attachment.');
""", add_item=False, window_size=(1440, 1000))

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
        _ = path.write_text(base + "## Log\n\n- 2026-11-16 17:30:00+00:00 Recorded <old words> — nate\n"
            + '- {"time": "2026-11-16T17:35:00+00:00", "source": "the List", '
            + '"action": "Unparked", "delivered_change": "sdlc/C1"}\n')
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
        self.assertIn("Unparked because change sdlc/C1 is ready.", log)
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
        self.assertRegex(log, r'data-color-text="role.producer"[^>]*>Producer</span></dt>'
                         + r'<dd><span[^>]*data-color-text="role.producer"[^>]*>morty</span>')
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
            self.assertIn(label, without_role_colors(log))
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

    def long_log_item(self, entries: int = 30) -> the_list.ListItem:
        item = self.add("Long log", number=1, evidence="")
        for update in range(entries):
            _ = the_list.update(MORTY.id, item.id, {"next_step": f"Check result {update}."}, source="morty")
        return item

    def set_log_max_height(self, rem: int) -> None:
        config = self.root / "config" / "review_links.conf"
        text = config.read_text()
        self.assertIn("list_log_max_height_rem=16\n", text)
        _ = config.write_text(text.replace("list_log_max_height_rem=16\n", f"list_log_max_height_rem={rem}\n"))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_log_scrolls_in_its_own_area(self) -> None:
        _ = self.long_log_item()
        self.set_log_max_height(9)
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        self.assertRegex(markup, r'\bstyle="(?:[^"]*;)?--list-log-max-height:9rem[;"]')
        probe = r'''<script>
document.querySelector('.list-toggle').click();
const panel=document.querySelector('.list-panel'),[left,right]=panel.children;
const log=right.querySelector('.list-log-scroll');
const logStyle=getComputedStyle(log);
const desktop=innerWidth>600,box=log.getBoundingClientRect();
const rem=parseFloat(getComputedStyle(document.documentElement).fontSize);
const failures=[];
if(logStyle.overflowY!=='auto' || log.scrollHeight<=log.clientHeight)
 failures.push('The log scrolls in its own area.');
const ink=getComputedStyle(right.querySelector('h3')).color;
if(logStyle.scrollbarColor!==ink+' rgba(0, 0, 0, 0)')
 failures.push('The log uses the theme for the thumb and shows the panel color through the track.');
if(desktop && !(box.left>left.getBoundingClientRect().right && box.bottom<=panel.getBoundingClientRect().bottom &&
 panel.getBoundingClientRect().height<innerHeight))
 failures.push('On a desktop the log sits beside the details and ends with the panel.');
if(!desktop && Math.abs(log.clientHeight-9*rem)>1)
 failures.push('On a narrow page the log is 9rem tall from the config. Found '+log.clientHeight+' px.');
const result=document.createElement('p');result.id='probe-results';
result.textContent=JSON.stringify(failures);
document.body.append(result);
</script>'''
        for width in (1440, 500):
            with self.subTest(width=width):
                self.browser_actions(markup, "", probe, window_size=(width, 600))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_refresh_keeps_the_open_row_and_its_log_position(self) -> None:
        item = self.long_log_item()
        _ = self.add("Another row", number=2)
        with patch.object(list_page, "counts_strip", return_value=""):
            initial = list_page.render(self.root / "reviews").decode()
            _ = the_list.update(MORTY.id, item.id, {"next_step": "The newest step."}, source="morty")
            fresh = list_page.render(self.root / "reviews").decode()
        steps = r"""
 const log=recordDetails(row).querySelector('.list-log-scroll');
 row.scrollIntoView({block:'start',behavior:'instant'});
 scrollBy({top:-40,behavior:'instant'});
 log.scrollTop=Math.round(log.scrollHeight/2);
 const top=row.getBoundingClientRect().top,logTop=log.getBoundingClientRect().top;
 const first=[...log.querySelectorAll('[data-log-index]')].find(entry=>entry.getBoundingClientRect().bottom>logTop);
 const index=first.dataset.logIndex,offset=first.getBoundingClientRect().top-logTop;
 const entries=log.querySelectorAll('[data-log-index]').length;
 refreshMarkup=FRESH_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 const freshLog=recordDetails(row).querySelector('.list-log-scroll');
 check(freshLog.querySelectorAll('[data-log-index]').length>entries,'The refresh adds the new log entry.');
 check(row.getBoundingClientRect().top===top,
  'The open row keeps its place. Expected '+top+' px, found '+row.getBoundingClientRect().top+' px.');
 const entry=freshLog.querySelector('[data-log-index="'+index+'"]');
 const freshOffset=entry.getBoundingClientRect().top-freshLog.getBoundingClientRect().top;
 check(freshLog.scrollTop>0 && Math.abs(freshOffset-offset)<1,
  'The log keeps its first visible entry at the same place. Expected '+offset+' px, found '+freshOffset+' px.');
"""
        steps = steps.replace("FRESH_PAGE", json.dumps(fresh).replace("</", "<\\/"))
        for width in (1440, 500):
            with self.subTest(width=width):
                self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial,
                                             window_size=(width, 700))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_sort_and_save_keep_the_open_log_position(self) -> None:
        item = self.long_log_item()
        _ = self.add("Another row", number=2)
        with patch.object(list_page, "counts_strip", return_value=""):
            initial = list_page.render(self.root / "reviews").decode()
            _ = the_list.update(MORTY.id, item.id, {"next_step": "The newest step."}, source="morty")
            saved = list_page.render(self.root / "reviews").decode()
        setup = r"""
 const scrolledLog=()=>{
  const log=recordDetails(row).querySelector('.list-log-scroll');
  log.scrollTop=Math.round(log.scrollHeight/2);
  const top=log.getBoundingClientRect().top;
  const first=[...log.querySelectorAll('[data-log-index]')].find(entry=>entry.getBoundingClientRect().bottom>top);
  return {index:first.dataset.logIndex,offset:first.getBoundingClientRect().top-top};
 };
 const checkLog=(position,name)=>{
  const log=recordDetails(row).querySelector('.list-log-scroll');
  const entry=log.querySelector('[data-log-index="'+position.index+'"]');
  const offset=entry.getBoundingClientRect().top-log.getBoundingClientRect().top;
  check(log.scrollTop>0 && Math.abs(offset-position.offset)<1,
   name+' Expected '+position.offset+' px, found '+offset+' px.');
 };
"""
        sort = setup + r"""
 const position=scrolledLog();
 const place=()=>[...document.querySelectorAll('[data-list-item]')].indexOf(row);
 const before=place(),sort=document.querySelector('[data-sort="0"]');
 sort.click();sort.click();
 await flush();
 check(place()!==before,'The sort moves the open row.');
 checkLog(position,'A sort that moves the open row keeps its log position.');
"""
        save = setup + r"""
 const position=scrolledLog();
 const savedRow=new DOMParser().parseFromString(SAVED_PAGE,'text/html').querySelector('[data-number="1"]');
 submit('read');
 finishPost({item_state:'Working',unread:'false',unread_sort:0,detail_html:recordDetails(savedRow).innerHTML,
  detail_revision:savedRow.dataset.detailRevision});
 await flush();await flush();
 check(recordDetails(row).querySelector('.list-log').children.length===savedRow.querySelectorAll('[data-log-index]').length,
  'The save result installs the new detail.');
 checkLog(position,'A save result keeps the log position.');
""".replace("SAVED_PAGE", json.dumps(saved).replace("</", "<\\/"))
        for name, steps in (("sort", sort), ("save", save)):
            for width in (1440, 500):
                with self.subTest(case=name, width=width):
                    self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial,
                                                 window_size=(width, 700))

    def test_log_entries_share_one_label_column(self) -> None:
        self.assertIn(".list-log dl{display:grid;grid-template-columns:min(7rem,42%) minmax(0,1fr)", list_page.STYLE)

    @override
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix="list-page-test-")))
        config = self.root / "config"
        changes = self.root / "change-reviews"
        _ = shutil.copytree(self.browser_root / "config", config)
        with (config / "review_links.conf").open("a") as settings:
            _ = settings.write(f"studio_avatar_root={self.root / 'avatars'}\n")
        _ = write_config_fixture(config / "console.conf", f"page.list.owner={MORTY.id}\n")
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
            next_check: the_list.NextCheck = the_list.NoTimestamp.UNSET,
            evidence: str = "Read https://example.invalid/proof?x=1&y=2.") -> the_list.ListItem:
        return the_list.add(MORTY.id, title, owner=owner, number=number, source="nate", changes={
            "state": state, "next_check": next_check, "next_step": "Read the result and check the page.",
            "latest_status": "The page reads the saved records.", "done_when": "The checks pass.",
            "evidence": evidence,
            "eta": the_list.EtaRange(NOW + timedelta(hours=1), NOW + timedelta(hours=2))})

    def fixture_items(self) -> list[the_list.ListItem]:
        paused = self.add("Shared documentation structure", the_list.ItemState.PAUSED, number=41)
        removed = self.add("Removed task", number=54)
        waiting = self.add("Remove the mesh code", the_list.ItemState.WAITING_FOR_NATE, number=58)
        done = self.add("Read the delivered page", the_list.ItemState.DONE, number=63)
        status = self.add("Console status", number=105, owner=OTHER.id, next_check=NOW - timedelta(minutes=10))
        latest = self.add("The List in the console Straße", number=106, next_check=NOW + timedelta(hours=1))
        items = [waiting, status, latest, paused, done, removed]
        _ = the_list.mark_read(MORTY.id, items[0].id, source="nate")
        _ = the_list.update(MORTY.id, items[1].id, {"producer": DWIGHT.id}, source="morty")
        _ = the_list.update(MORTY.id, items[3].id, {"producer": DWIGHT.id}, source="morty")
        _ = the_list.park(MORTY.id, items[3].id, NOW + timedelta(days=2), source="nate")
        _ = the_list.close(MORTY.id, items[5].id, "The user removed this task.", source="nate")
        return items

    def test_list_publishes_the_first_active_timed_park_deadline(self) -> None:
        earlier = NOW + timedelta(minutes=2)
        later = NOW + timedelta(minutes=3)
        for title, deadline in (("Earlier park", earlier), ("Later park", later)):
            item = self.add(title)
            _ = the_list.park(MORTY.id, item.id, deadline, source="nate")
        closed = self.add("Closed park")
        _ = the_list.park(MORTY.id, closed.id, NOW + timedelta(minutes=1), source="nate")
        _ = the_list.close(MORTY.id, closed.id, "The work is closed.", source="nate")
        delivery = self.add("Park until delivery")
        _ = the_list.park(MORTY.id, delivery.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")

        root = self.root / "reviews"
        for moment, deadline in ((NOW, earlier), (earlier, later), (later, None)):
            with self.subTest(moment=moment), patch.object(time, "time", return_value=moment.timestamp()):
                markup = list_page.render(root, include_counts=False, query="show=working").decode()
                page = re.search(r'<div data-list [^>]*>', markup)
                assert page is not None
                if deadline is None:
                    self.assertNotIn("data-park-deadline", page[0])
                else:
                    self.assertIn(f'data-park-deadline="{deadline.timestamp()}"', page[0])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_keeps_the_first_paint_rows_and_chip_counts_for_query_filters(self) -> None:
        upper, lower = "\u1c89", "\u1c8a"
        for number, character, owner in (
                (1, upper, OWNER.id), (2, lower, OWNER.id), (3, upper, OTHER.id), (4, upper, OWNER.id)):
            item = self.add(f"Check \0 {character}", owner=owner, number=number)
            if number != 4:
                _ = the_list.park(MORTY.id, item.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")
            if number == 3:
                _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        query = urlencode({"show": "parked", "owner": OWNER.id, "q": f"CHECK \0 {upper}"})
        fragment = urlencode({"show": "working", "owner": OTHER.id, "q": "unmatched"})
        key_query = urlencode({"show": "working", "key": f"{MORTY.id}/L4"})
        key_fragment = urlencode({"key": f"{MORTY.id}/L1"})
        numbers = ["1", "2"] if search_fold.fold(upper) == search_fold.fold(lower) else ["1"]
        unread_alias = "show=unread&unread=0"
        all_rows = ["1", "2", "3", "4"]
        unread_rows = ["1", "2", "4"]
        before = r'''<script>
const [address,firstRows,loadedRows,allCount,firstUnread,loadedUnread]=CASE;
history.replaceState(null,'',address);
const snapshot=()=>({
 rows:[...document.querySelectorAll('[data-list-item]:not([hidden])')].map(row=>row.dataset.number).sort(),
 chips:[...document.querySelectorAll('[data-filter-count]')].map(chip=>
  [chip.id,chip.dataset.filterCount,chip.getAttribute('aria-pressed')]),
 unread:document.querySelector('#list-unread').checked,
});
const firstPaint=snapshot();
</script>'''
        after = r'''<script>
const failures=[];
const check=(ok,message)=>{if(!ok) failures.push(message);};
const loaded=snapshot();
check(JSON.stringify(firstPaint.rows)===JSON.stringify(firstRows),'The server shows the matching rows.');
check(JSON.stringify(loaded.rows)===JSON.stringify(loadedRows),'Script start shows the matching rows.');
check(firstPaint.unread===firstUnread && loaded.unread===loadedUnread,'Unread has the expected state.');
if(firstUnread===loadedUnread)
 check(JSON.stringify(firstPaint)===JSON.stringify(loaded),'Script start keeps the rows and chip counts.');
check(document.querySelector('#filter-show-all').dataset.filterCount===String(allCount),'The All chip has the expected count.');
const row=document.querySelector('[data-number="1"]');
check(JSON.parse(row.dataset.sortValues)[3].includes('\0'),'The Item sort key keeps the null character.');
void fetch('/results',{method:'POST',body:JSON.stringify(failures)});
</script>'''
        script = "<script>" + list_page.SCRIPT
        for server_query, browser_fragment, first_rows, loaded_rows, all_count, first_unread, loaded_unread in (
                ("", "", all_rows, all_rows, 4, False, False),
                (query, fragment, numbers, numbers, len(numbers) + 1, False, False),
                (query, fragment + "&unread=1", numbers, numbers, len(numbers) + 1, False, True),
                ("unread=0", "show=unread", all_rows, all_rows, 4, False, False),
                ("show=parked", unread_alias, ["1", "2", "3"], ["1", "2"], 3, False, True),
                ("", unread_alias, all_rows, unread_rows, 3, False, True),
                ("show=unread", "", unread_rows, unread_rows, 3, True, True),
                (unread_alias, "unread=0", unread_rows, unread_rows, 3, True, True),
                (key_query, key_fragment, ["4"], ["4"], 1, False, False),
        ):
            with self.subTest(query=server_query, fragment=browser_fragment):
                address = ("?" + server_query if server_query else "") + ("#" + browser_fragment if browser_fragment else "")
                case = json.dumps((address, first_rows, loaded_rows, all_count, first_unread, loaded_unread))
                markup = list_page.render(self.root / "reviews", include_counts=False, query=server_query).decode()
                page = markup.replace(script, before.replace("CASE", case) + script, 1)
                page = page.replace("</body>", after + "</body>", 1)
                self.assertEqual(self.firefox_probe(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_preserves_staff_ids_in_query_filters(self) -> None:
        owner, producer = staff.StaffId("o\ra"), staff.StaffId("p\rb")
        for number, owner_id, producer_id in (
            (1, owner, producer), (2, owner, staff.StaffId("p%0Db")), (3, staff.StaffId("o%0Da"), producer),
        ):
            item = self.add(f"Check item {number}", owner=owner_id, number=number)
            _ = the_list.update(MORTY.id, item.id, {"producer": producer_id}, source="morty")
        before = r'''<script>
history.replaceState(null,'',ADDRESS);
const visible=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')]
 .map(row=>row.dataset.number).sort().join();
const firstPaint=visible();
</script>'''

        def check_query(query: str, after: str) -> None:
            markup = list_page.render(self.root / "reviews", include_counts=False, query=query).decode()
            script = "<script>" + list_page.SCRIPT
            start = before.replace('ADDRESS', json.dumps(f"?{query}"))
            page = markup.replace(script, start + script, 1).replace("</body>", after + "</body>", 1)
            self.assertEqual(self.firefox_probe(page.encode()), [[]])

        after = r'''<script>
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
const chip=(name,selection)=>document.querySelector('[data-filter-row="'+name+'"] '+selection);
check(firstPaint==='1' && visible()===firstPaint,'Both filters keep the first paint row.');
const ownerChip=chip('owner','[aria-pressed="true"]'),producerChip=chip('producer','[aria-pressed="true"]');
check(ownerChip.dataset.filterCount==='1' && producerChip.dataset.filterCount==='1',
 'Both selected chips show one matching row.');
chip('owner','[data-filter-value="all"]').click();
check(visible()==='1,3','The producer filter keeps its carriage return.');
ownerChip.click();
chip('producer','[data-filter-value="all"]').click();
check(visible()==='1,2','The owner filter keeps its carriage return.');
producerChip.click();
const place=new URLSearchParams(location.hash.slice(1));
check(visible()==='1' && place.get('owner')==='o\ra' && place.get('producer')==='p\rb',
 'The selected chips write the original IDs to the address.');
void fetch('/results',{method:'POST',body:JSON.stringify(failures)});
</script>'''
        with self.subTest(filter="staff"):
            check_query(urlencode({"owner": owner, "producer": producer}), after)

        keeper = staff.StaffId("k\r1")
        item = the_list.add(MORTY.id, "Check the linked item", owner=OTHER.id, number=4,
                            source="nate", changes={"state": the_list.ItemState.WORKING})
        record = (the_list.folder(MORTY.id) / f"{item.id}.md").read_bytes()
        # Only morty's folder allocates numbers, so give each legacy keeper its own copy of L4.
        for keeper_id in (keeper, staff.StaffId("k%0D1")):
            folder = the_list.folder(keeper_id)
            folder.mkdir(parents=True, exist_ok=True)
            _ = (folder / f"{item.id}.md").write_bytes(record)
        (the_list.folder(MORTY.id) / f"{item.id}.md").unlink()
        key = f"{keeper}/L4"
        after = r'''<script>
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
const row=document.querySelector('[data-list-item]:not([hidden])');
const working=document.querySelector('[data-filter-row="show"] [aria-pressed="true"]');
check(firstPaint==='4' && visible()===firstPaint,'The key filter keeps the first paint row.');
check(row?.dataset.key===KEY && working.dataset.filterCount==='1',
 'The selected row keeps its original key and the Working chip counts one row.');
row?.querySelector('.list-toggle').click();
const place=new URLSearchParams(location.hash.slice(1));
check(place.get('item')===KEY && place.getAll('key').join()===KEY,
 'The selected row writes the original key to the address.');
void fetch('/results',{method:'POST',body:JSON.stringify(failures)});
</script>'''.replace('KEY', json.dumps(key))
        with self.subTest(filter="key"):
            check_query(urlencode({"show": "working", "key": key}), after)

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
        controls = body.split('data-record-filters>', 1)[1].split('<p data-hidden-action-status', 1)[0]
        self.assertNotIn('<select', controls)
        self.assertEqual(body.count('type="search"'), 1)
        self.assertEqual([part.split('"', 1)[0] for part in controls.split('data-filter-row="')[1:]],
                         ['owner', 'show'])
        shows = controls.split('data-filter-row="show"', 1)[1].split('</div>', 1)[0]
        self.assertEqual(re.findall(r'data-filter-value="([^"]+)"[^>]*>([^<]+) <b>', shows),
                         [("all", "All"), ("needs-me", "Needs me"), ("producer-answering", "Producer answering"),
                          ("working", "Working"), ("blocked", "Blocked"), ("parked", "Parked"),
                          ("scheduled", "Scheduled")])
        self.assertIn('role="switch" id="list-unread" value="1">Unread only</label>', controls)
        owners = controls.split('data-filter-row="owner"', 1)[1].split('</div>', 1)[0]
        producers = body.split('data-filter-row="producer"', 1)[1].split('</div>', 1)[0]
        chip = r'data-filter-value="([^"]+)"[^>]*>([^<]+) <b>(\d+)</b>'
        self.assertEqual(re.findall(chip, owners),
                         [('all', 'All', '5'), (f'staff:{OTHER.id}', 'builds', '1'), (f'staff:{OWNER.id}', 'sdlc', '4')])
        tab = r'data-filter-value="([^"]+)"[^>]*>.*?<span class="studio-name">([^<]+)</span> <b>(\d+)</b>'
        self.assertEqual(re.findall(tab, producers),
                         [('all', 'All', '5'), (f'staff:{DWIGHT.id}', 'dwight', '2'), (f'staff:{MORTY.id}', 'morty', '3')])
        self.assertEqual(body.count('<td data-label="Producer">dwight</td>'), 2)
        self.assertEqual(body.count('<td data-label="Producer">morty</td>'), 3)
        self.assertIn(f'data-producer="{DWIGHT.id}"', body)
        for text in ('Latest status', 'Done when', 'Log · newest first', 'Links',
                     'Check overdue', '14:00 EST', '15:00 EST'):
            self.assertIn(text, body)
        self.assertNotIn('<h3>Evidence</h3>', body)
        self.assertEqual(the_list.load(MORTY.id, items[0].id).evidence, items[0].evidence)
        self.assertEqual(body.count('<form method="post" data-feedback-form'), 5)
        self.assertEqual(body.count('value="read">Mark read</button>'), 5)
        self.assertEqual([entry.item.number for entry in list_page.records()],
                         [item.number for item in sorted(the_list.records(MORTY.id), key=the_list.order)])
        parked = self.add("Check the delivery", the_list.ItemState.WAITING_FOR_NATE)
        _ = the_list.park(MORTY.id, parked.id, the_list.ParkUntilDelivered.DELIVERED, source="nate")
        parked_markup = list_page.render(self.root / "reviews").decode()
        self.assertEqual(re.findall(r'<tbody data-list-item [^>]*data-number="(\d+)"', parked_markup),
                         ["58", "105", "106", "63", "107", "41"])
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
const unpark=recordDetails(row).querySelector('[value="unpark"]');
if(row.hidden || !unpark?.getClientRects().length) failures.push('The waiting row shows Unpark in Parked');
if(!recordDetails(row).querySelector('[data-list-review-card]')) failures.push('The waiting row shows its change link');
const result=document.createElement('p');result.id='probe-results';
result.textContent=JSON.stringify(failures);document.body.append(result);
</script>""", reduced_motion=True)
        self.browser_actions(after, setup, r"""<script>
const failures=[];
const row=document.querySelector('[data-number="1"]');
document.getElementById('filter-show-needs-me').click();
if(row.hidden || !row.querySelector('.list-dot')) failures.push('Needs me shows the unread delivery');
row.querySelector('.list-toggle').click();
if(!recordDetails(row).querySelector('[value="read"]') || !recordDetails(row).querySelector('[data-list-review-card]'))
 failures.push('The waiting row shows Mark read and its change link');
document.getElementById('filter-show-producer-answering').click();
if(!row.hidden) failures.push('Producer answering excludes the unread delivery');
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
            self.assertIn('>' + label + ' <b>1</b></button>', owners)
        _ = (self.root / "staff.json").write_text('{"schema":1,"rows":[]}')
        self.assertEqual(len(list_page.records()), 3)

    def test_feedback_button_belongs_to_the_words_box_and_starts_disabled(self) -> None:
        item = self.add("Explain this item")
        members = staff.snapshot()
        form = list_page.feedback_form(list_page.ListRecord(MORTY.id, item), "fixture-secret", members=members,
                                       parking_label="", schedule=the_list.NoSchedule.UNSET, today=NOW,
                                       routing=the_list.ListQuestionRouting(members), unread_answers=frozenset())
        actions, words = form.split('</div>', 1)
        self.assertNotIn('value="tell"', actions)
        self.assertNotIn('<label for="words-', words)
        self.assertIn('placeholder="Your words for morty about this item"', words)
        self.assertIn('aria-label="Your words for morty about this item"', words)
        self.assertIn('</textarea><div class="list-tell"><span role="status" data-send-status hidden></span>'
                      + '<button data-send-receiver="morty" aria-label="Ask question, morty"'
                      + ' name="action" value="question" disabled>Ask question</button>'
                      + '<button data-send-receiver="morty" name="action" value="tell" disabled>'
                      + 'Tell morty</button></div>', words)
        self.assertIn("!words.value.trim()", list_page.SCRIPT)
        self.assertIn("const edit=()=>{draftStamp++;retain();persistDraft();};", list_page.SCRIPT)
        self.assertIn("words.addEventListener('input',edit)", list_page.SCRIPT)
        self.assertIn("setCardSubmitting(form,false,stale ? form : recordDetails(item));syncTell()", list_page.SCRIPT)
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

    def test_next_step_renders_one_labeled_command(self) -> None:
        command = "  printf '%s\\n' '<tag> & value'  "
        for source in (command, f"`{command}`", f"```sh\n{command}\n```"):
            with self.subTest(source=source):
                progress = list_page.ListProgress("", NOW, f"Check the result.\n\nRun this command:\n\n{source}", "", True)
                markup = progress.next_step_html(link_urls=True)
                self.assertTrue(markup.startswith('<p>Check the result.</p><p>Run this command:</p>'))
                self.assertIn('<pre><code>' + html.escape(command) + '</code></pre>', markup)
                self.assertIn(page_frame.COPY_BUTTON, markup)
        for source in ("Check the result.\n\nOpen https://example.org/preview.", "Run this command:",
                       "Run this command:\n\n ", "Run this command:\n\nprintf x\n\nRead the result.",
                       "Run this command:\n\nprintf x\n\nRun this command:\n\nprintf y"):
            with self.subTest(source=source):
                progress = list_page.ListProgress("", NOW, source, "", True)
                self.assertEqual(progress.next_step_html(link_urls=True),
                                 list_page.item_text_html(source, paragraphs=True, link_urls=True))

    def test_next_step_finds_command_labels_and_paragraphs_outside_fences(self) -> None:
        example = "```text\nExample\n\nRun this command:\n\nprintf x\n```"
        command = "printf x\n\nprintf y"
        label = "Run this command:\n\n"
        for source in (example, example.removesuffix("\n```"),
                       label + "```sh\nprintf x\n```\n```sh\nprintf y\n```",
                       label + "```sh\n```\n\n```sh\nprintf x\n```"):
            with self.subTest(source=source):
                progress = list_page.ListProgress("", NOW, source, "", True)
                self.assertEqual(progress.next_step_html(),
                                 list_page.item_text_html(source, paragraphs=True, link_urls=False))
        for source in (f"{label}```sh\n{command}\n```", f"{example}\n\n{label}~~~sh\n{command}\n~~~"):
            with self.subTest(source=source):
                progress = list_page.ListProgress("", NOW, source, "", True)
                block = '<div class="list-command">' + page_frame.copy_code_block(command) + '</div>'
                self.assertTrue(progress.next_step_html().endswith(block))

    def test_next_step_normalizes_carriage_returns_in_a_command(self) -> None:
        label = "Run this command:"
        for source in (label + "\r\n\r\nprintf x\rprintf y", label + "\r\r```sh\r\nprintf x\rprintf y\n```"):
            with self.subTest(source=source):
                progress = list_page.ListProgress("", NOW, source, "", True)
                block = '<div class="list-command">' + page_frame.copy_code_block("printf x\nprintf y") + '</div>'
                self.assertEqual(progress.next_step_html(), f'<p>{label}</p>' + block)

    def test_next_step_keeps_original_prose_line_endings(self) -> None:
        prose = "Check the result.\rRead the log."
        expected = "<p>Check the result.</p><p>Read the log.</p>"
        for source, markup in ((prose, expected),
                               (prose + "\rRun this command:", expected + "<p>Run this command:</p>")):
            with self.subTest(source=source):
                progress = list_page.ListProgress("", NOW, source, "", True)
                self.assertEqual(progress.next_step_html(), markup)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for this browser test")
    def test_browser_next_step_fences_keep_each_copy_button_with_its_code(self) -> None:
        label = "Run this command:\n\n"
        first = 'printf "<x> & y"'
        second = "printf z"
        example = "Example\n\n" + label + first
        lf_cases = (
            (f"{label}```sh\n```\n```sh\n{first}\n```", ["", first]),
            (f"{label}```sh\n{first}\n```\n```sh\n{second}\n```", [first, second]),
            (f"```text\n{example}\n```", [example]),
        )
        cases = lf_cases + tuple((source.replace("\n", "\r\n") + ending, codes)
                                for source, codes in lf_cases for ending in ("", "\r\n"))
        sections = "".join(
            "<section>" + list_page.ListProgress("", NOW, source, "", True).next_step_html() + "</section>"
            for source, _ in cases
        )
        probe = r'''
const failures=[];
const expected=EXPECTED_CODES;
const copied=[];
Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>{copied.push(text);}}});
for(const [index,section] of [...document.querySelectorAll('section')].entries()){
 const start=copied.length;
 for(const button of section.querySelectorAll('[data-copy-code]')) button.click();
 const codes=copied.slice(start);
 if(JSON.stringify(codes)!==JSON.stringify(expected[index]))
  failures.push('Unexpected code for case '+index+': '+JSON.stringify(codes));
 if(section.querySelector('.list-command')) failures.push('Unexpected Studio command for case '+index);
}
void fetch('/results',{method:'POST',body:JSON.stringify(failures)});
'''.replace("EXPECTED_CODES", json.dumps([codes for _, codes in cases]))
        page = f"<!doctype html><meta charset=utf-8>{sections}<script>{page_frame.COPY_SCRIPT}\n{probe}</script>"
        self.assertEqual(firefox_probe_results(page.encode()), [[]])

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_studio_copies_the_next_step_command_once_after_refresh(self) -> None:
        root = self.root / "reviews"
        item = self.add("Check the command", number=1)
        command = "printf '%s\\n' '<tag> & \"value\"' '" + "x" * 180 + "'"
        updated = command.replace("<tag>", "<new tag>")

        def page(words: str) -> str:
            _ = the_list.update(MORTY.id, item.id, {"next_step": "Run this command:\n\n" + words}, source="morty")
            return list_page.render(root, include_counts=False).decode()

        initial, fresh = page(command), page(updated)
        steps = r'''
 const copied=[];
 Object.defineProperty(navigator,'clipboard',{configurable:true,value:{writeText:async text=>{copied.push(text);}}});
 const summary=()=>row.querySelector('[data-label="Next step"]');
 const detail=()=>recordDetails(row).querySelector('.list-field-next_step');
 const copy=field=>field.querySelector('[data-copy-code]');
 const oldCopy=copy(detail());
 const code=detail().querySelector('pre code');
 check(innerWidth===VIEWPORT_WIDTH && document.documentElement.scrollWidth<=innerWidth,
  'The Studio fits the requested viewport.');
 code.scrollLeft=code.scrollWidth;
 check(code.scrollLeft>0,'The long command scrolls inside its block.');
 check(code.getBoundingClientRect().right<=oldCopy.getBoundingClientRect().left,
  'The scroll area leaves room for the copy button.');
 oldCopy.focus();
 check(oldCopy.matches(':focus-visible') && getComputedStyle(oldCopy).opacity==='1',
  'Keyboard focus shows the copy button.');
 copy(summary()).click();await flush();
 check(recordIsOpen(row),'A click on the summary copy button keeps the item open.');
 oldCopy.click();await flush();
 check(copied.length===2 && copied.every(text=>text===ORIGINAL_COMMAND),
  'Each click copies the exact original command once.');
 const focusField=FOCUS_DETAIL ? detail : summary;
 const focusedCopy=copy(focusField());
 focusedCopy.focus();
 refreshMarkup=FRESH_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 const newCopy=copy(detail());
 check(pageReads===1 && recordIsOpen(row) && newCopy!==oldCopy,'The refresh replaces the open command block.');
 const renewedCopy=copy(focusField());
 check(renewedCopy!==focusedCopy && document.activeElement===renewedCopy,
  'The replacement copy button has focus in the same field.');
 newCopy.click();await flush();
 check(copied.length===3 && copied[2]===UPDATED_COMMAND,'A click after the refresh copies the new command once.');
'''
        steps = (steps.replace("ORIGINAL_COMMAND", json.dumps(command)).replace("UPDATED_COMMAND", json.dumps(updated))
                 .replace("FRESH_PAGE", json.dumps(fresh).replace("</", "<\\/")))
        for width, focus_detail in ((1440, False), (390, True)):
            with self.subTest(width=width, focus_detail=focus_detail):
                probe = steps.replace("VIEWPORT_WIDTH", str(width)).replace("FOCUS_DETAIL", json.dumps(focus_detail))
                self.browser_action_sequence(probe, markup=initial,
                                             add_item=False, reduced_motion=True, window_size=(width, 900))

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
        pages = tuple(page for page in console.read_pages() if page.identity == "studio")
        root = self.root / "reviews"
        self.assertEqual(studio_page.page_url(), '/console/studio.html')
        self.assertEqual(console.news(root, pages, "changes"), ())
        item = self.add("Unread item")
        self.assertEqual(console.news(root, pages, "changes"), ("studio",))
        self.assertEqual(console.news(root, pages, "studio"), ())
        self.assertEqual(console.news(root, pages, "changes"), ())
        with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=1)):
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "A new result arrived."}, source="morty")
        self.assertEqual(console.news(root, pages, "changes"), ("studio",))
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        self.assertEqual(console.news(root, pages, "changes"), ())

    def test_studio_news_keeps_the_seen_list_updates(self) -> None:
        root = self.root / "reviews"
        studio, = (page for page in console.read_pages() if page.identity == "studio")
        old = replace(studio, identity="list", destination=console.AvailablePage(list_page.PAGE_PATH))
        seen = self.add("Update seen on the List")
        self.assertEqual(console.news(root, (old,), "list"), ())
        unread = self.add("Update added after the last List visit")
        self.assertEqual(console.news(root, (studio,), "review"), ("studio",))
        saved = change_page.console_folder(root) / console.SEEN_NAME
        self.assertEqual(json.loads(saved.read_text()), {"studio": [f"{MORTY.id}/{seen.id}/{NOW.isoformat()}"]})
        self.assertEqual(console.news(root, (studio,), "studio"), ())
        self.assertIn(unread.id, saved.read_text())
        self.assertEqual(console.news(root, (studio,), "review"), ())

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
        states = (the_list.ItemState.NEW, the_list.ItemState.WORKING, the_list.ItemState.BLOCKED,
                  the_list.ItemState.WAITING_FOR_NATE, the_list.ItemState.DELIVERED, the_list.ItemState.DONE,
                  the_list.ItemState.PAUSED, the_list.ItemState.CLOSED)
        for offset in (0, 8):
            for number, state in enumerate(states, start=1 + offset):
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
        bob = replace(DWIGHT, name="bob", id=staff.StaffId(str(uuid4())), current_session=staff.StaffId(str(uuid4())))
        moved = replace(DWIGHT, current_session=staff.StaffId(str(uuid4())))
        for number, state in ((17, the_list.ItemState.DELIVERED), (18, the_list.ItemState.WORKING),
                              (19, the_list.ItemState.WORKING), (20, the_list.ItemState.BLOCKED)):
            item = self.add("Check the question result", state, number=number)
            if number == 17:
                item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
            if number == 18:
                item = the_list.park(MORTY.id, item.id, NOW + timedelta(days=1), source="nate")
            outcome = question_outcome("refused", "Producer offline") if number == 19 else question_outcome()
            with patch.object(session_pane, "session_place", return_value=HerdrPlace("native", "w1:p1")) as place:
                delivery = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check passed?")), status)
            if number == 17:
                place.assert_called_once_with(DWIGHT.current_session)
                _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
                    row.to_json() for row in (MORTY, moved, bob, OWNER, OTHER)]}))
                _ = the_list.update(MORTY.id, item.id, {"producer": bob.id}, source="morty")
                _ = the_list.mark_read(MORTY.id, item.id, source="nate")
            else:
                place.assert_not_called()
            with (patch.object(unrouted_alert, "message_outcome", return_value=outcome) as send,
                  patch.object(change_page, "alert_delivery_failures")):
                list_page.deliver(delivery)
            if number == 17:
                send.assert_called_once()
                self.assertEqual(send.call_args.args[0], f"session:{moved.current_session}")
                self.assertEqual(the_list.load(MORTY.id, item.id).questions[0].recipient, DWIGHT.id)
            if number in (18, 19):
                _ = the_list.update(MORTY.id, item.id, {"latest_status": "A new check passed."}, source="morty")
            if number == 20:
                with (patch.object(staff, "caller_identity", return_value=MORTY.current_session),
                      redirect_stdout(io.StringIO())):
                    self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1",
                                                    "--text", "The gate passed."]), 0)
        parked_answer = self.add("Park the answered item", number=21)
        with (patch.object(change_page, "send_question", return_value=question_outcome()),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO())):
            list_page.deliver(list_page.submit(root, urlencode(self.fields(parked_answer, "question", words="Did it pass?"))))
            self.assertEqual(the_list.main(["answer", parked_answer.id, "--question", "Q1", "--text", "It passed."]), 0)
        answered = the_list.load(MORTY.id, parked_answer.id)
        _ = list_page.submit(root, urlencode(self.fields(answered, "park", until=(NOW + timedelta(days=1)).isoformat())))
        for number in (22, 23):
            _ = the_list.add(MORTY.id, f"Check scheduled item {number}", owner=OWNER.id, source="nate",
                             number=number, changes={
                                 "state": the_list.ItemState.SCHEDULED,
                                 "schedule": the_list.ScheduledRun(NOW + timedelta(days=1), "Check the result."),
                             })
        _ = the_list.mark_read(MORTY.id, "L23", source="nate")
        markup = list_page.render(root, status=status, include_counts=False).decode()
        controls = markup.split('data-filter-row="show"', 1)[1].split('</div>', 1)[0]
        # A park at or after the answer reads the answer and puts item 21 in Parked.
        self.assertEqual(re.findall(r'data-filter-count="(\d+)"', controls), ["21", "3", "0", "0", "2", "2", "2", "9", "2"])
        probe = '''<script>
const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
const chips=[...document.querySelectorAll('[data-filter-row="show"] button')];
const chip=name=>chips.find(button=>button.dataset.filterValue===name);
const unread=document.getElementById('list-unread');
const total=document.querySelector('[data-list-count]');
const entries=[
 ['all',[1,2,3,4,5,6,7,9,10,11,12,13,14,15,17,18,19,20,21,22,23],[1,3,4,6,9,11,13,15,18,19,20,21,22]],
 ['needs-me',[4,5,20],[4,20]],['questions',[],[]],['answers',[],[]],['producer-answering',[17,19],[19]],['working',[1,2],[1]],['blocked',[3,7],[3]],
 ['parked',[9,10,11,12,13,14,15,18,21],[9,11,13,15,18,21]],
 ['scheduled',[22,23],[22]],
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
const detail=number=>(recordIsOpen(row(number)) ? recordDetails(row(number)) : null);
unread.click();
chip('producer-answering').click();
expand(17);
check(!row(17).hidden && detail(17)?.querySelector('[data-waiting-note]')?.textContent==='dwight will answer.',
 'Producer answering shows the saved recipient in the waiting note.');
expand(19);
check(!row(19).hidden && detail(19)?.querySelector('[data-question-delivery-failed]'),
 'The failed question stays in Producer answering with a failure notice.');
const producerChips=document.querySelector('[data-filter-row="producer"]');
for(const number of [17,19]){
 producerChips.querySelector('[data-filter-value="staff:'+row(number).dataset.producer+'"]').click();
 check(count(chip('producer-answering'))===1 && visible().join()===String(number),
  'Producer answering shows and counts the selected producer’s item.');
}
producerChips.querySelector('[data-filter-value="all"]').click();
chip('parked').click();expand(18);
const waitingPanel=detail(18);
check(!row(18).hidden && waitingPanel?.querySelector('[data-waiting-note]') &&
 waitingPanel.querySelector('[data-parked-detail]').getClientRects().length &&
 waitingPanel.querySelector('[value="unpark"]').getClientRects().length &&
 !row(18).querySelector('[data-parked-note]').getClientRects().length,
 'The parked item shows one parking note, the waiting note, and Unpark in its panel.');
chip('needs-me').click();
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

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_unread_answers_take_priority_over_both_park_kinds(self) -> None:
        root = self.root / "reviews"
        parking = (NOW + timedelta(days=7), the_list.ParkUntilDelivered.DELIVERED)
        items = [the_list.park(MORTY.id, self.add("Check the parked item", number=number).id, until, source="nate")
                 for number, until in enumerate(parking, start=1)]
        with (patch.object(change_page, "send_question", return_value=question_outcome()),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO())):
            for item in items:
                list_page.deliver(list_page.submit(root, urlencode(self.fields(item, "question", words="Did it pass?"))))
            initial = list_page.render(root, include_counts=False).decode()
            for item in items:
                self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "It passed."]), 0)

        def check_counts(page: str, needs_me: int, parked: int) -> None:
            controls = page.split('data-filter-row="show"', 1)[1].split('</div>', 1)[0]
            self.assertEqual(re.findall(r'data-filter-count="(\d+)"', controls),
                             ["2", str(needs_me), "0", "0", "0", str(parked), "0"])

        check_counts(initial, 0, 2)
        answered = list_page.render(root, include_counts=False).decode()
        check_counts(answered, 2, 0)
        self.assertEqual(without_scripts(answered).count('<small data-new-answer>New answer</small>'), 2)
        news = list_page.waiting_updates(console.NewsRequest(root))
        self.assertEqual({key.rsplit('/', 1)[0] for key in news}, {f'{MORTY.id}/{item.id}' for item in items})
        read_responses: list[list_page.SavedActionState] = []
        for item, until in zip(items, parking, strict=True):
            current = the_list.load(MORTY.id, item.id)
            self.assertEqual(current.parked_until, until)
            _, state = list_page.save_action(root, urlencode(self.fields(current, "read")), include_detail=True)
            assert state is not None
            read_responses.append(state)
            self.assertEqual((state.get("unread_answer"), state["parked"]), ("false", "true"))
            self.assertEqual(the_list.load(MORTY.id, item.id).parked_until, until)
        self.assertFalse(list_page.waiting_updates(console.NewsRequest(root)))
        check_counts(list_page.render(root, include_counts=False).decode(), 0, 2)

        steps = r'''
 const item=number=>document.querySelector('[data-number="'+number+'"]');
 const count=chip=>document.getElementById('filter-show-'+chip).dataset.filterCount;
 const receipts=READ_RESPONSES;
 const parkNote=number=>item(number).querySelector('[data-parked-note]').textContent;
 const parks=[parkNote(1),parkNote(2)];
 check(count('parked')==='2' && count('needs-me')==='0','Both items start in Parked.');
 refreshMarkup=ANSWERED_PAGE;
 document.dispatchEvent(new Event('visibilitychange'));
 await flush();
 show('needs-me');
 check(!item(1).hidden && !item(2).hidden && count('needs-me')==='2' && count('parked')==='0',
  'Both unread answers appear in Needs me and its count.');
 refreshMarkup=undefined;
 for(const number of [1,2]){
  if(!recordIsOpen(item(number))) item(number).querySelector('.list-toggle').click();
  const detail=recordDetails(item(number));
  check(detail.querySelector('.question-unread')?.textContent==='Unread','The answer is unread.');
  check(item(number).querySelector('[data-new-answer]')?.textContent==='New answer','The row marks the new answer.');
  submit('read',detail.querySelector('form'));
  finishPost(receipts[number-1]);
  await flush();
  check(item(number).hidden && count('needs-me')===String(2-number) && count('parked')===String(number),
   'Mark read returns the item to the Parked count.');
  check(parkNote(number)===parks[number-1],'Mark read keeps the original parking condition.');
  check(!item(number).querySelector('[data-new-answer]'),'Mark read removes the mark from the row.');
 }
 show('parked');
 check(!item(1).hidden && !item(2).hidden,'Both items appear in Parked after Mark read.');
'''.replace('READ_RESPONSES', json.dumps(read_responses).replace('<', r'\u003c')).replace(
            'ANSWERED_PAGE', json.dumps(answered).replace('<', r'\u003c'))
        self.browser_action_sequence(steps, add_item=False, markup=initial, reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_parking_clears_existing_answers_and_keeps_newer_answers(self) -> None:
        root = self.root / "reviews"
        items = [self.add("Check the answer") for _ in range(2)]
        choices = ("delivered", "2026-12-03")

        def answer(question: str) -> None:
            for item in items:
                current = the_list.load(MORTY.id, item.id)
                list_page.deliver(list_page.submit(root, urlencode(self.fields(current, "question", words="Is it ready?"))))
                self.assertEqual(the_list.main(["answer", item.id, "--question", question, "--text", "Yes."]), 0)

        with (patch.object(change_page, "send_question", return_value=question_outcome()),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO())):
            answer("Q1")
            initial = list_page.render(root, include_counts=False).decode()
            requests = [urlencode(self.fields(the_list.load(MORTY.id, item.id), "park", until=choice))
                        for item, choice in zip(items, choices, strict=True)]
            with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=1)):
                states = [list_page.save_action(root, fields, include_detail=True)[1] for fields in requests]
            for state in states:
                assert state is not None
                self.assertEqual((state["parked"], state.get("unread_answer")), ("true", "false"))
            parked = without_scripts(list_page.render(root, include_counts=False).decode())
            controls = parked.split('data-filter-row="show"', 1)[1].split('</div>', 1)[0]
            self.assertEqual(re.findall(r'data-filter-count="(\d+)"', controls), ["2", "0", "0", "0", "0", "0", "0", "2", "0"])
            self.assertNotIn("<small data-new-answer>New answer</small>", parked)
            with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=2)):
                answer("Q2")
            answered = list_page.render(root, include_counts=False).decode()

        for item, fields, choice in zip(items, requests, choices, strict=True):
            _ = list_page.submit(root, fields)
            saved = the_list.load(MORTY.id, item.id)
            self.assertEqual(the_list.unread_answer_ids(saved), {"Q2"})
            with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=3)):
                _ = list_page.submit(root, urlencode(self.fields(saved, "park", until=choice)))
            parked_again = the_list.load(MORTY.id, item.id)
            self.assertEqual(parked_again.parked_until, saved.parked_until)
            self.assertEqual(the_list.unread_answer_ids(parked_again), set())

        steps = r'''
 const item=number=>document.querySelector('[data-number="'+number+'"]');
 const count=chip=>document.getElementById('filter-show-'+chip).dataset.filterCount;
 const states=PARK_STATES,choices=PARK_CHOICES;
 show('needs-me');
 for(const number of [1,2]){
  const row=item(number);
  check(!row.hidden && row.querySelector('[data-new-answer]'),'Needs me shows the first answer.');
  if(!recordIsOpen(row)) row.querySelector('.list-toggle').click();
  const form=recordDetails(row).querySelector('form');
  if(number===1) submit('park-until-delivered',form);
  else {form.elements.until.value=choices[number-1];submit('park',form);}
  check(requests.at(-1).get('until')===choices[number-1],'The form posts the selected park.');
  finishPost(states[number-1]);await flush();
  check(row.hidden && !row.querySelector('[data-new-answer]'),'Park removes the answer mark and hides the row.');
  check(count('needs-me')===String(2-number) && count('parked')===String(number),'Park updates both chip counts.');
 }
 show('parked');
 check(!item(1).hidden && !item(2).hidden,'Both parked items appear in Parked.');
 refreshMarkup=ANSWERED_PAGE;
 document.dispatchEvent(new Event('visibilitychange'));await flush();
 show('needs-me');
 check([1,2].every(number=>!item(number).hidden && item(number).querySelector('[data-new-answer]')),
  'Newer answers return both items to Needs me.');
 check(count('needs-me')==='2' && count('parked')==='0','Newer answers update both chip counts.');
'''.replace('PARK_STATES', json.dumps(states).replace('<', r'\u003c')).replace(
            'PARK_CHOICES', json.dumps(choices)).replace('ANSWERED_PAGE', json.dumps(answered).replace('<', r'\u003c'))
        self.browser_action_sequence(steps, add_item=False, markup=initial, reduced_motion=True)

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
let countRequests=[],sendCounts;
window.fetch=async (url,options)=>{
 countRequests.push([url,options.cache]);
 return new Promise(resolve=>{sendCounts=()=>resolve({headers:new Headers(),ok:true,text:async()=>MARKUP});});
};
</script>""".replace("MARKUP", json.dumps(response))
                probe = """
<script>
(async()=>{
 const footer=document.querySelector('[data-list-counts-url]');
 const failures=[];
 const open=EXPECTED==='1 change for you';
 const row=document.querySelector('[data-list-item]');
 if(open) row.querySelector('.list-toggle').click();
 const detail=recordDetails(row);
 const words=detail.querySelector('textarea');
 if(open){words.value='Keep these words.';words.dispatchEvent(new Event('input'));}
 sendCounts();
 if(!footer.firstChild) await new Promise(resolve=>{
  const observer=new MutationObserver(()=>{observer.disconnect();resolve();});
  observer.observe(footer,{childList:true});
 });
 if(countRequests.length!==1 || countRequests[0].join()!==REQUEST)
  failures.push('The footer uses one independent request.');
 if(!document.querySelector('[data-list-item]') || footer.querySelector('a')?.textContent!=='2 changes')
  failures.push('The counts appear without replacing the List rows.');
 if(open && (document.querySelector('[data-list-review-summary]')?.textContent!=='1 change for you' ||
  detail.querySelector('[data-list-review-links] a')?.textContent!=='sdlc/C1' ||
  detail.querySelector('textarea')!==words || words.value!=='Keep these words.'))
  failures.push('The response fills the linked summary and details and keeps the draft.');
 const result=document.createElement('p');
 result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>""".replace("REQUEST", json.dumps(f"{studio_page.PAGE_PATH}?{query},no-store")).replace(
                    "EXPECTED", json.dumps(expected))
                self.browser_actions(markup, setup, probe)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_refresh_updates_other_page_counts_with_a_row_open(self) -> None:
        _ = self.add("Keep this row open")
        markup = list_page.render(self.root / "reviews", defer_counts=True, review_records={}).decode()
        markup = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', markup, count=1)
        setup = r'''<script>
const snapshot=SNAPSHOT;
let countRequests=0,countMarkup='<aside class="list-counts">2 changes</aside>',countStatus=200;
window.fetch=async url=>{
 if(new URL(url,location.href).searchParams.has('counts')){
  countRequests++;
  return {ok:countStatus===200,text:async()=>countMarkup};
 }
 return {ok:true,text:async()=>snapshot};
};
</script>'''.replace('SNAPSHOT', json.dumps(markup).replace('<', r'\u003c'))
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
 const refresh=async()=>{document.dispatchEvent(new Event('visibilitychange'));await flush();};
 const row=()=>document.querySelector('[data-list-item]');
 const counts=()=>document.querySelector('[data-list-counts-url]').textContent;
 try{
  await flush();
  check(counts()==='2 changes' && countRequests===1,'The initial counts load.');
  row().querySelector('.list-toggle').click();
  countMarkup='<aside class="list-counts">3 changes</aside>';
  await refresh();
  check(countRequests===2 && counts()==='3 changes' && recordIsOpen(row()),
   'The refresh applies the new counts and keeps the row open.');
  await refresh();
  check(countRequests===3 && counts()==='3 changes','The next refresh also requests counts.');
  countStatus=503;
  await refresh();
  check(countRequests===4 && counts()==='3 changes','A failed read keeps the last counts.');
  countStatus=200;
  countMarkup='';
  await refresh();
  check(countRequests===5 && counts()==='' && recordIsOpen(row()),'An empty successful read clears the open row footer.');
  row().querySelector('.list-toggle').click();
  await flush();
  check(!recordIsOpen(row()) && counts()==='' && countRequests===5,'Collapsing the row keeps the footer clear.');
  countMarkup='<aside class="list-counts">1 change</aside>';
  await refresh();
  check(counts()==='1 change' && countRequests===6,'A closed row shows fresh counts immediately.');
  countMarkup='';
  await refresh();
  check(counts()==='' && countRequests===7,'A successful empty response clears the footer when the row is closed.');
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
 document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, setup, probe, reduced_motion=True)

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
  const request={url,signal:options.signal,finish:markup=>resolve({headers:new Headers(),ok:true,text:()=>Promise.resolve(markup)})};
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
 check(requests.length===1 && !first.signal.aborted,'Refreshes keep the active request until it completes.');
 first.finish('<aside>First counts</aside>'+FIRST_UPDATES);
 await new Promise(resolve=>setTimeout(resolve,0));
 check(footer().textContent==='' && summary(2).textContent==='Checking linked reviews…',
  'The old response changes neither the footer nor an unchanged linked row.');
 check(summary(1).textContent==='Checking linked reviews…',
  'The old response also skips the changed row.');
 check(requests.length===2,'The latest snapshot starts one follow-up without another refresh.');
 const timedOut=changed(summary(1));
 deadlines.get(requests[1].signal).timer.abort();
 await timedOut;
 check(summary(1).textContent===UNAVAILABLE && document.querySelector('.list-toggle').matches(':enabled'),
  'A timeout leaves the row usable and identifies the unavailable status.');
 check(requests.length===2,'A timeout does not repeat the same request.');
 await refresh();
 const loaded=changed(footer());
 requests[2].finish('<aside>New counts</aside>'+CURRENT_UPDATES);
 await loaded;
 check(footer().textContent==='New counts' && summary(1).textContent==='Current review status',
  'The next refresh loads counts and status after a timeout.');
 page=COUNTS_ONLY_PAGE;
 await refresh();
 const counts=requests[3];
 check(counts.url.endsWith('?counts=1') && !summary(2),'The counts-only request starts before the new row appears.');
 await refresh();
 check(requests.length===4 && !counts.signal.aborted,'A refresh with the same rows keeps the active request.');
 counts.finish('<aside>Counts from the old snapshot</aside>');
 await new Promise(resolve=>setTimeout(resolve,0));
 check(footer().textContent==='New counts','The newer snapshot skips the old counts when the rows did not change.');
 check(requests.length===5 && requests[4].url.endsWith('?counts=1'),
  'The newer snapshot requests its own counts when the rows did not change.');
 page=UPDATED_PAGE;
 await refresh();
 check(summary(2).textContent==='Checking linked reviews…','The new linked row waits for current status.');
 requests[4].finish('<aside>Counts before linked status</aside>');
 await new Promise(resolve=>setTimeout(resolve,0));
 check(footer().textContent==='New counts' && requests.length===6 &&
  requests[5].url.endsWith('?counts=1&linked-reviews=1'),'The latest snapshot requests counts and linked status.');
 const links=requests[5];
 if(links){
  const statusLoaded=changed(summary(2));
  links.finish('<aside>Final counts</aside>'+CURRENT_UPDATES);
  await statusLoaded;
  check(summary(2).textContent==='Unchanged review status' && footer().textContent==='Final counts',
   'The follow-up fills the new row and the footer.');
 }
 check(requests.length===6 && [...deadlines.values()].every(deadline=>deadline.milliseconds===4000),
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
        form = list_page.signed_form_fields(list_page.ListRecord(MORTY.id, item), secret)
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', form))
        return {**fields, "action": action, "words": "", "until": "", **changes}

    def test_report_verdicts_use_a_signed_repair_and_send_to_both_recipients(self) -> None:
        root = self.root / "reviews"
        for kind in the_list.ReportKind:
            with self.subTest(kind=kind):
                native = investigation.file(OWNER.id, "Check the menu", "The menu hides half the choices.",
                                            waiting_why="sdlc checks the menu.")
                report = the_list.ListReport(kind, review_items.investigation_record_key(native), native.question)
                destination = the_list.file_report(MORTY.id, report, native.title, owner=OWNER.id, producer=DWIGHT.id,
                                                   unit="", source="nate")
                item = the_list.load(destination.keeper, f"L{destination.number}")
                self.assertEqual((item.state, item.state_entered), (the_list.ItemState.NEW, item.opened))
                with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=1)):
                    working = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.WORKING}, source="dwight")
                self.assertEqual((working.state, working.state_entered),
                                 (the_list.ItemState.WORKING, NOW + timedelta(minutes=1)))
                with self.assertRaisesRegex(the_list.ListError, "Use Works or Not fixed"):
                    _ = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.DELIVERED}, source="dwight")
                instructions = "Open the menu and count the choices."
                with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=2)):
                    repaired = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.REPAIRED,
                                                "next_step": instructions}, source="dwight")
                page = list_page.render(root, include_counts=False, query="show=needs-me").decode()
                self.assertIn(f'<strong>{native.title}</strong>', page)
                self.assertIn('<h3>How to try</h3>', page[:page.index('</main>')])
                self.assertIn(instructions, page[:page.index('</main>')])
                self.assertIn('value="works">Works</button>', page)
                self.assertIn('value="not works" disabled>Not fixed</button>', page)
                self.assertEqual(repaired.state_entered, NOW + timedelta(minutes=2))
                stale = self.fields(repaired, "works")
                with self.assertRaisesRegex(question_record.AnswerRefused, "List token"):
                    _ = list_page.submit(root, urlencode({**stale, "token": "wrong"}))
                revision = repaired.repair_revision
                _ = the_list.save_image_answer(MORTY.id, item.id, f"review:picture-{destination.number}",
                                               "The image shows the chooser.", image_id="000000000001", approved=True)
                after_image = the_list.load(MORTY.id, item.id)
                self.assertEqual((after_image.state, after_image.next_step, after_image.repair_revision),
                                 (the_list.ItemState.REPAIRED, instructions, revision))
                words = "The last two choices still disappear."
                rejected = self.fields(repaired, "not works", words=f"\x85 {words}\x1c\u3000")
                with patch.object(change_page, "send_to_staff") as send:
                    delivery = list_page.submit(root, urlencode(rejected))
                    self.assertEqual(the_list.load(MORTY.id, item.id).state, the_list.ItemState.WORKING)
                    self.assertEqual(the_list.load(MORTY.id, item.id).next_step, "")
                    send.assert_not_called()
                    list_page.deliver(delivery)
                    list_page.deliver(list_page.submit(root, urlencode(rejected)))
                    self.assertEqual({call.args[0] for call in send.call_args_list}, {OWNER.id, DWIGHT.id})
                    self.assertEqual([call.args[2] for call in send.call_args_list],
                                     [f"Studio: Not fixed for item {item.number}\n{words}"] * 2)

                panel = list_page.saved_action_state(MORTY.id, item.id, include_detail=True)
                self.assertIn(f"<p>Not fixed: {words}</p>", panel.get("detail_html", ""))
                self.assertEqual(panel.get("next_step_sort"), "dwight repairs it again")
                _ = the_list.update(MORTY.id, item.id, {"latest_status": "The producer found the cause.",
                                                     "next_step": "Check the menu code."}, source="dwight")
                panel = list_page.saved_action_state(MORTY.id, item.id, include_detail=True)
                self.assertIn("<p>The producer found the cause.</p>", panel.get("detail_html", ""))
                self.assertEqual(panel.get("next_step_html"), "<p>Check the menu code.</p>")

                with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=3)):
                    repaired_again = the_list.update(MORTY.id, item.id, {"state": the_list.ItemState.REPAIRED,
                                                      "next_step": instructions}, source="dwight")
                with self.assertRaisesRegex(the_list.ListError, "The repair changed"):
                    _ = list_page.submit(root, urlencode(stale))
                with patch.object(the_list, "now", return_value=NOW + timedelta(minutes=4)):
                    _ = list_page.submit(root, urlencode(self.fields(repaired_again, "works")))
                delivered = the_list.load(MORTY.id, item.id)
                self.assertEqual((delivered.state, delivered.state_entered),
                                 (the_list.ItemState.DELIVERED, NOW + timedelta(minutes=4)))
                self.assertEqual(delivered.next_step, "")
                panel = list_page.saved_action_state(MORTY.id, item.id, include_detail=True)
                self.assertEqual(panel.get("next_step_html"), "")
                self.assertNotIn('<h3>Next step</h3>', panel.get("detail_html", ""))
                done_page = list_page.render(root, include_counts=False, query="show=needs-me").decode()
                row = re.search(r'<tbody data-list-item [^>]+data-number="'+str(destination.number)+r'"[^>]*>', done_page)
                self.assertIsNotNone(row)
                assert row is not None
                self.assertIn(" hidden", row[0])
                _ = the_list.close(MORTY.id, item.id, "Test finished.", source="nate")

    def test_repair_feedback_recovers_after_pending_or_partial_delivery(self) -> None:
        root = self.root / "reviews"
        _ = write_config_fixture(conf.path("review_links"), "delivery_tries=1\n")
        words = "The menu still hides the last choice."
        for scenario in ("both pending", "owner pending", "owner failed", "owner failed and alerted"):
            with self.subTest(scenario=scenario):
                native = investigation.file(OWNER.id, "Check the menu", words, waiting_why="sdlc checks the menu.")
                report = the_list.ListReport(the_list.ReportKind.BUG, review_items.investigation_record_key(native), words)
                destination = the_list.file_report(MORTY.id, report, native.title, owner=OWNER.id, producer=DWIGHT.id,
                                                   unit="", source="nate")
                identity = f"L{destination.number}"
                _ = the_list.update(MORTY.id, identity, {"state": the_list.ItemState.WORKING}, source="dwight")
                repaired = the_list.update(MORTY.id, identity, {"state": the_list.ItemState.REPAIRED,
                            "next_step": "Open the menu."}, source="dwight")
                fields = self.fields(repaired, "not works", words=words)
                delivery = list_page.submit(root, urlencode(fields))
                accepted: list[tuple[staff.StaffId, str]] = []
                recover = False

                def send(recipient: staff.StaffId, _sender: str, message: str, *,
                         key: str) -> Literal["pending", "skipped"] | None:
                    self.assertTrue(message.endswith(words))
                    if not recover:
                        if recipient == OWNER.id and scenario.startswith("owner failed"):
                            raise OSError("The owner is unavailable.")
                        if recipient == OWNER.id or scenario == "both pending":
                            return "pending"
                    if (recipient, key) in accepted:
                        return "skipped"
                    accepted.append((recipient, key))
                    return None

                alerted = scenario == "owner failed and alerted"
                with (patch.object(change_page, "send_to_staff", side_effect=send),
                      patch.object(list_page, "alert_showrunners", return_value=(OTHER,) if alerted else ()),
                      patch.object(change_page, "alert_delivery_failures", return_value=alerted) as alert):
                    first = list_page.deliver_with_outcome(delivery)
                    expected = (change_page.FailedDelivery if scenario.startswith("owner failed")
                                else change_page.DeliveryUnconfirmed)
                    self.assertIsInstance(first, expected)
                    saved = the_list.load(MORTY.id, identity)
                    feedback = the_list.feedback_requests(saved)[fields["request"]]
                    self.assertEqual(feedback.state, "alerted" if alerted else "pending")
                    self.assertEqual(saved.next_step, "")
                    recover = True
                    if alerted:
                        alert.assert_called_once()
                    else:
                        list_page.recover_pending(root)
                        list_page.recover_pending(root)
                    self.assertIsInstance(list_page.deliver_with_outcome(list_page.submit(root, urlencode(fields))),
                                          change_page.DeliveryConfirmed)
                done = the_list.load(MORTY.id, identity)
                notices = [entry.what for entry in done.log if isinstance(entry.what, the_list.PageFeedback)]
                self.assertEqual(notices, [replace(feedback, state="sent", alerted_to="")])
                self.assertCountEqual([target for target, _ in accepted], [DWIGHT.id, OWNER.id])
                self.assertEqual(len({key for _, key in accepted}), 1)
                _ = the_list.close(MORTY.id, identity, "Test finished.", source="nate")

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
                             (MORTY.id, "Studio: nate unparked item 106."))
            delivered = the_list.load(MORTY.id, item.id)
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(the_list.load(MORTY.id, item.id), delivered)
            fields = self.fields(delivered, "unpark")
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            saved = the_list.load(MORTY.id, item.id)
            receipt = the_list.UnparkNoChangeReceipt(
                the_list.PageRequest(fields["request"], "unpark", "Studio: nate unparked item 106."),
                the_list.UnparkNoChangeReason.NO_ACTIVE_PARKING)
            self.assertEqual(saved.log[-1].what, receipt)
            self.assertEqual(replace(saved, log=saved.log[:-1]), delivered)
            self.assertEqual(send.call_count, 1)

    def test_unpark_retry_uses_the_saved_list_page_notice(self) -> None:
        root = self.root / "reviews"
        item = self.add("Unpark this item", number=106)
        parked = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        fields = self.fields(parked, "unpark")
        message = "List page: nate unparked item 106."
        notice = the_list.PageItemNotice(fields["request"], message, "unpark")
        _ = the_list.unpark(MORTY.id, item.id, source=the_list.PAGE_SOURCE, notice=notice)
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], message)

    def test_mark_read_on_a_waiting_item_sends_its_producer_one_next_step_request(self) -> None:
        root = self.root / "reviews"
        item, fields = self.waiting_dwight_item()
        with patch.object(change_page, "send_to_staff") as send:
            delivery, state = list_page.save_action(root, urlencode(fields), include_detail=True)
            assert state is not None
            self.assertEqual((state["item_state"], state["unread"]), ("Working", "false"))
            send.assert_not_called()
            list_page.deliver(delivery)
            self.assertEqual(send.call_count, 1)
            recipient, _, message = cast(tuple[str, object, str], send.call_args.args[:3])
            self.assertEqual(recipient, DWIGHT.id)
            self.assertTrue(message.startswith("Studio: nate read item 107, so it is now Working. Write its next step:"))
            self.assertNotIn("\n", message)
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(send.call_count, 1)
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual(the_list.feedback_requests(saved)[fields["request"]].state, "sent")
        body = list_page.render(root, include_counts=False).decode()
        self.assertIn("Feedback sent to dwight", without_role_colors(body))

    def waiting_dwight_item(self) -> tuple[the_list.ListItem, dict[str, str]]:
        item = self.add("Read the waiting result", the_list.ItemState.WAITING_FOR_NATE, number=107)
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        return item, self.fields(item, "read")

    def test_mark_read_notice_goes_to_its_saved_producer_after_a_producer_move(self) -> None:
        root = self.root / "reviews"
        item, fields = self.waiting_dwight_item()
        _ = list_page.submit(root, urlencode(fields))
        moved = the_list.move_open_producer(MORTY.id, item.id, DWIGHT.id, MORTY.id, source="morty")
        self.assertEqual(moved.producer, MORTY.id)
        log = list_page.render(root, include_counts=False).decode().split('<ol class="list-log">', 1)[1]
        self.assertIn("Feedback for dwight", without_role_colors(log.split("</ol>", 1)[0]))
        with patch.object(change_page, "send_to_staff") as send:
            list_page.recover_pending(self.root / "reviews")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(send.call_args.args[0], DWIGHT.id)

    def test_mark_read_alert_leaves_out_the_producer_and_keeps_morty(self) -> None:
        item, fields = self.waiting_dwight_item()
        delivery = list_page.submit(self.root / "reviews", urlencode(fields))
        with (patch.object(change_page, "send_with_retries",
                           return_value=[change_page.DeliveryFailure(DWIGHT.id, "offline")]),
              patch.object(list_page, "alert_showrunners", return_value=(DWIGHT, MORTY)),
              patch.object(change_page, "alert_delivery_failures", return_value=True) as alert):
            list_page.deliver(delivery)
        self.assertEqual(cast(tuple[str, ...], alert.call_args.kwargs["recipients"]), (MORTY.id,))
        feedback = the_list.feedback_requests(the_list.load(MORTY.id, item.id))[fields["request"]]
        self.assertEqual((feedback.state, feedback.alerted_to), ("alerted", "morty"))

    def test_mark_read_on_a_waiting_item_with_an_open_question_sends_no_message(self) -> None:
        root = self.root / "reviews"
        item = self.add("Answer the waiting question", the_list.ItemState.WAITING_FOR_NATE)
        with (patch.object(change_page, "send_question", return_value=question_outcome()),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO())):
            list_page.deliver(list_page.submit(root, urlencode(self.fields(item, "question", words="Did it pass?"))))
        asked = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result."}, source="morty")
        with patch.object(change_page, "send_to_staff") as send:
            delivery, state = list_page.save_action(root, urlencode(self.fields(asked, "read")), include_detail=True)
            list_page.deliver(delivery)
            send.assert_not_called()
        assert state is not None
        self.assertEqual((state["item_state"], state["open_question"]), ("Waiting for nate", "true"))
        self.assertIsInstance(delivery, change_page.NoMessageDelivery)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for browser checks")
    def test_browser_mark_read_moves_a_waiting_row_out_of_needs_me_in_place(self) -> None:
        root = self.root / "reviews"
        items = [self.add(f"Waiting result {number}", the_list.ItemState.WAITING_FOR_NATE, number=number)
                 for number in (1, 2)]
        initial = list_page.render(root, include_counts=False).decode()
        with patch.object(change_page, "send_to_staff"):
            _, state = list_page.save_action(root, urlencode(self.fields(items[0], "read")), include_detail=True)
        assert state is not None
        steps = r'''
 const item=number=>document.querySelector('[data-number="'+number+'"]');
 const count=chip=>document.getElementById('filter-show-'+chip).dataset.filterCount;
 const pressed=chip=>document.getElementById('filter-show-'+chip).getAttribute('aria-pressed');
 show('needs-me');
 await finishRowMotion();
 check(!item(1).hidden && !item(2).hidden && count('needs-me')==='2','Both waiting rows start in Needs me.');
 if(!recordIsOpen(item(1))) item(1).querySelector('.list-toggle').click();
 const address=location.href,top=scrollY,otherTop=item(2).getBoundingClientRect().top;
 submit('read',recordDetails(item(1)).querySelector('form'));
 finishPost(READ_RESPONSE);
 await flush();
 await finishRowMotion();
 check(item(1).hidden && item(1).dataset.state==='Working','The read row leaves Needs me as Working.');
 check(count('needs-me')==='1' && count('working')==='1','The counts move the row from Needs me to Working.');
 check(pressed('needs-me')==='true' && !item(2).hidden,'Needs me stays selected with the other row.');
 check(scrollY===top,'The page keeps its scroll position.');
 check(location.href===address.replace(/&item=[^&]*/,''),'The address keeps its filters and releases only the item.');
 check(item(2).getBoundingClientRect().top<=otherTop,'The other row keeps its place or moves up.');
 show('working');
 await finishRowMotion();
 check(!item(1).hidden && item(1).querySelector('.list-state').textContent==='Working',
  'Working shows the read row with its new state.');
'''.replace('READ_RESPONSE', json.dumps(state).replace('<', r'<'))
        self.browser_action_sequence(steps, add_item=False, markup=initial, reduced_motion=True)

    def test_remove_closes_before_delivery_and_repeated_requests_do_nothing(self) -> None:
        root = self.root / "reviews"
        for words in ("", " \t\n", " Keep <this> & that.\n\nRemove the duplicate.  \n"):
            with self.subTest(words=words), patch.object(change_page, "send_to_staff") as send:
                item = self.add("Remove this item now")
                fields = self.fields(item, "remove", words=words)
                delivery = list_page.submit(root, urlencode(fields))
                closed = the_list.load(MORTY.id, item.id)
                self.assertEqual(closed.state, the_list.ItemState.CLOSED)
                reason = words if words.strip() else ""
                self.assertEqual(closed.closing_words, reason or "Removed by nate on the Studio.")
                self.assertEqual(sum(isinstance(entry.what, the_list.FieldChange) and entry.what.action == "Closed"
                                     for entry in closed.log), 1)
                send.assert_not_called()
                list_page.deliver(delivery)
                expected = f"Studio: nate removed item {item.number}." + (f"\n\n{reason}" if reason else "")
                self.assertEqual((send.call_args.args[0], send.call_args.args[2]), (MORTY.id, expected))
                saved = the_list.load(MORTY.id, item.id)
                if reason:
                    log = list_page.item_log(list_page.ListRecord(MORTY.id, saved), NOW, staff_names.read(), frozenset())
                    escaped = "Keep &lt;this&gt; &amp; that."
                    self.assertIn(f"<dt>Closing words</dt><dd><p>{escaped}</p>", log.markup)
                    self.assertIn(f"<p>Studio: nate removed item {item.number}.</p><p> {escaped}</p>", log.markup)
                    self.assertNotIn("<this>", log.markup)
                list_page.deliver(list_page.submit(root, urlencode(fields)))
                send.assert_called_once()
                self.assertEqual(the_list.load(MORTY.id, item.id), saved)
                with self.assertRaisesRegex(the_list.ListError, "different action or words"):
                    _ = list_page.submit(root, urlencode({**fields, "words": "Changed on retry."}))

    def test_remove_retry_uses_the_saved_list_page_notice(self) -> None:
        root = self.root / "reviews"
        item = self.add("Remove the duplicate")
        fields = self.fields(item, "remove", words="Keep the original.")
        message = f"List page: nate removed item {item.number}."
        notice = the_list.PageItemNotice(fields["request"], message, "remove", fields["words"])
        closed = the_list.close(MORTY.id, item.id, "Removed by nate on the List page.",
                                 source=the_list.PAGE_SOURCE, notice=notice)
        with patch.object(change_page, "send_to_staff") as send:
            delivery = list_page.submit(root, urlencode(fields))
            self.assertEqual(the_list.load(MORTY.id, item.id), closed)
            list_page.deliver(delivery)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[2], message)

    def test_remove_retries_a_receipt_with_a_trimmed_reason(self) -> None:
        root = self.root / "reviews"
        reason = "  Keep the original.  \n"
        for scoped in (False, True):
            with self.subTest(scoped=scoped), patch.object(change_page, "send_to_staff") as send:
                if scoped:
                    start = the_list.ScheduledRun(NOW + timedelta(days=1), "Check the figures.")
                    series = the_list.ScheduledSeries(start, repeat_rule.parse("FREQ=DAILY"),
                                                      the_list.series_run_limit())
                    item = the_list.add(MORTY.id, "Check the figures", owner=OWNER.id, source="nate",
                                        changes={"state": the_list.ItemState.SCHEDULED, "schedule": series})
                    fields = self.fields(item, "remove", scope="this", run="1",
                                         schedule_revision=the_list.schedule_revision(item.schedule))
                else:
                    item = self.add("Remove the duplicate")
                    fields = self.fields(item, "remove")
                list_page.deliver(list_page.submit(root, urlencode({**fields, "words": reason.strip()})))
                saved = the_list.load(MORTY.id, item.id)
                list_page.deliver(list_page.submit(root, urlencode({**fields, "words": reason})))
                send.assert_called_once()
                self.assertEqual(the_list.load(MORTY.id, item.id), saved)

    def test_list_adds_the_next_number_after_a_page_remove(self) -> None:
        first = self.add("Keep the first item", number=206)
        removed = self.add("Remove the highest item", number=207)
        root = self.root / "reviews"
        _ = list_page.submit(root, urlencode(self.fields(removed, "remove")))
        saved = the_list.load(MORTY.id, removed.id)
        self.assertEqual(saved.state, the_list.ItemState.CLOSED)
        self.assertEqual(saved.log[:-2], removed.log)
        self.assertEqual([entry.item.id for entry in list_page.records()], [first.id])
        self.assertEqual(the_list.load(MORTY.id, removed.id), saved)
        # The counter keeps the removed number used even when its file is gone.
        (the_list.folder(MORTY.id) / f"{removed.id}.md").unlink()
        self.assertEqual([self.add(f"Add item {number}").number for number in (208, 209)], [208, 209])

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
            self.assertEqual(send.call_args.args[2], f"Studio: nate parked item {item.number} until 2026-12-03.")
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
        notice = the_list.feedback_requests(parked)[fields["request"]]
        self.assertEqual(notice.parked_until, parked.parked_until)
        self.assertEqual(notice.words, f"Studio: nate parked item {item.number} until delivered.")
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
        with self.list_clock_after_lock(midnight):
            with self.assertRaisesRegex(ValueError, "Choose a future parking date"):
                _ = list_page.submit(root, urlencode(fields))
        self.assertEqual(the_list.load(MORTY.id, item.id), item)

    @staticmethod
    @contextmanager
    def list_clock_after_lock(moment: datetime) -> Generator[None]:
        @contextmanager
        def lock_at_time(path: Path) -> Generator[file_lock.LockResult]:
            with file_lock.exclusive(path) as lock, patch.object(the_list, "now", return_value=moment):
                yield lock

        with patch.object(record_store, "exclusive", lock_at_time):
            yield

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_item_actions_unread_sync_and_words_fields(self) -> None:
        _ = self.fixture_items()
        request = the_list.add(MORTY.id, "Report the result", owner=OWNER.id, number=107, source="nate",
                               changes={"state": the_list.ItemState.BLOCKED,
                                        "latest_status": 'Please Tell Morty & confirm the "release result".'})
        _ = the_list.update(MORTY.id, request.id, {"latest_status": "The result is ready."}, source="morty")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
const requests=[];let finishPost;
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 requests.push(new URLSearchParams(options.body));
 return new Promise(resolve=>finishPost=result=>resolve({headers:new Headers(),ok:result.ok!==false,status:result.ok===false ? 400 : 200,
  json:async()=>({unread:'false',...result})}));
};
</script>'''
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,name)=>{if(!ok) failures.push(name);};
 const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
 const row=number=>document.querySelector('[data-number="'+number+'"]');
 const visible=()=>[...document.querySelectorAll('[data-list-item]')].filter(item=>!item.hidden).map(item=>item.dataset.number).sort().join(',');
 const choose=(show,query)=>new Promise(done=>{
  document.querySelector('[data-filter-row="show"] [data-filter-value="'+show+'"]').click();
  const field=document.getElementById('list-search');
  field.addEventListener('filterchange',done,{once:true});field.value=query;field.dispatchEvent(new Event('input'));
 });
 const submit=(number,action)=>{const form=recordDetails(row(number)).querySelector('form');form.requestSubmit(form.querySelector('[value="'+action+'"]'));return form;};
 try{
  for(const [query,expected] of [['105','105'],['L105','105'],['#105','105'],['BUILDS','105'],
    ['Straße','106'],['WORKING','105,106'],['Waiting','58'],['Parked','41'],['Closed',''],
    ['tELL mORTY','107'],['& confirm the "release result"','107'],['missing request phrase','']]){
   await choose('all',query);check(visible()===expected,'words filter: '+query);
  }
  await choose('working','106');row('106').querySelector('.list-toggle').click();
  const removed=submit('106','remove');
  check(row('106').hidden,'Remove hides the row before the response');
  check(row('106').dataset.unread==='false' && !row('106').querySelector('.list-dot') &&
   !removed.querySelector('[value="read"]'),'Remove clears the unread controls before the response');
  check([...removed.querySelectorAll('button')].every(button=>button.disabled),'Remove disables the form buttons');
  submit('106','remove');check(requests.length===1,'a second Remove press sends nothing');
  finishPost({item_state:'Closed',parked:'false'});await flush();await flush();
  check(row('106').hidden,'Remove stays outside Working after success');
  await choose('all','106');check(visible()==='','Remove keeps the item out of All');
  check(row('106').querySelector('.list-state').textContent==='Closed','Remove updates the state text');
  await choose('working','105');row('105').querySelector('.list-toggle').click();
  const refused=submit('105','remove');check(row('105').hidden,'a refused Remove hides the row while pending');
  finishPost({headers:new Headers(),ok:false,reason:'The fixture refuses this close.'});await flush();await flush();
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
  finishPost({item_state:'Working',parked:'true'});await flush();await flush();
  check(row('105').hidden,'Park stays outside Working after success');
  await choose('parked','105');check(visible()==='105','Park updates the Parked view');
  check(!document.querySelector('[data-record-inline-view]') && !parked.querySelector('[data-send-status]').textContent,
   'Park closes the dialog without repeating the saved state.');
  await choose('all','58');row('58').querySelector('.list-toggle').click();
  const form=recordDetails(row('58')).querySelector('form');
  check(row('58').dataset.unread==='false' && !form.querySelector('[value="read"]'),'the unread sync case starts read');
  form.elements.words.value='Keep this result.';form.elements.words.dispatchEvent(new Event('input'));
  submit('58','tell');finishPost({item_state:'Waiting for nate',parked:'false',unread:'true'});await flush();await flush();
  check(row('58').dataset.unread==='true' && row('58').querySelector('.list-dot'),'an unread response restores the dot');
  check(Boolean(form.querySelector('[value="read"]')),'an unread response restores Mark read');
  if(form.querySelector('[value="read"]')){
   document.getElementById('list-unread').click();
   submit('58','read');finishPost({item_state:'Waiting for nate',parked:'false',unread:'false'});await flush();await flush();
   check(!row('58').querySelector('.list-dot') && !form.querySelector('[value="read"]'),'the restored Mark read button works');
   check(row('58').hidden && document.querySelector('[data-record-motion]')?.getAnimations({subtree:true}).length,
    'Mark read animates the row when it leaves Unread');
  }
 }catch(error){failures.push(String(error));}
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>'''
        self.browser_actions(markup, setup, probe)

    @staticmethod
    def finish_next_scheduled_run(identity: str) -> the_list.ListItem:
        store = record_store.NumberedRecords(MORTY.id, "list", "L", "list item", the_list.ListError)
        with store.locked():
            schedule = the_list.load(MORTY.id, identity).schedule
            assert isinstance(schedule, the_list.ScheduledSeries)
            selected = schedule.next_run()
            assert isinstance(selected, the_list.SeriesRun)
            finished = schedule.finish_runs((selected.number,))
            fields, body = store.read(identity)
            saved = cast(dict[str, object], json.loads(fields["schedule"]))
            saved.update(runs_used=finished.runs_used, finished_runs=sorted(finished.finished_runs))
            fields["schedule"] = json.dumps(saved)
            store.write(identity, fields, body)
            return the_list.load(MORTY.id, identity)

    def test_schedule_saves_the_instruction_and_applies_repeat_scopes(self) -> None:
        root = self.root / "reviews"
        first = datetime(2026, 11, 30, 14, tzinfo=UTC)
        item = self.add("Check the account predictions")
        create = self.fields(item, "schedule", until=first.isoformat(),
                             schedule_words="Compare the predictions.", repeat="FREQ=WEEKLY;BYDAY=MO;COUNT=5")
        _ = list_page.submit(root, urlencode(create))
        saved = the_list.load(MORTY.id, item.id)
        self.assertIs(saved.state, the_list.ItemState.SCHEDULED)
        self.assertIs(saved.parked_until, the_list.NoTimestamp.UNSET)
        schedule = saved.schedule
        assert isinstance(schedule, the_list.ScheduledSeries)
        self.assertEqual((schedule.first.start, schedule.first.words), (first, create["schedule_words"]))
        _ = list_page.submit(root, urlencode(create))
        self.assertEqual(the_list.load(MORTY.id, item.id), saved)
        self.assertTrue(list_page.saved_action_state(MORTY.id, item.id, park_request=create["request"]).get("park_request_saved"))

        second = first + timedelta(days=7)
        saved = self.finish_next_scheduled_run(item.id)
        with patch.object(the_list, "now", return_value=first + timedelta(days=1)):
            revision = the_list.schedule_revision(saved.schedule)
            for action, scope, run, reason in (
                ("park", "this", "2", "to park a repeated item"),
                ("schedule", "this", "1", "The next run changed"),
            ):
                with self.subTest(action=action, run=run):
                    fields = self.fields(saved, action, scope=scope, run=run, until=second.isoformat(),
                                         schedule_words="Check the second run.", schedule_revision=revision)
                    with self.assertRaisesRegex(ValueError, reason):
                        _ = list_page.submit(root, urlencode(fields))
                    self.assertEqual(the_list.load(MORTY.id, item.id), saved)
            change = self.fields(saved, "schedule", scope="this", run="2", schedule_words="Check the second run.")
            _ = list_page.submit(root, urlencode(change))
            next_run = the_list.load(MORTY.id, item.id).schedule
            assert isinstance(next_run, the_list.ScheduledSeries)
            self.assertEqual(next_run.exceptions[0].run, the_list.ScheduledRun(second, change["schedule_words"]))
            self.assertEqual(next_run.first.words, create["schedule_words"])
            current = the_list.load(MORTY.id, item.id)
            for action, scope in (("schedule", "all"), ("unschedule", "all"), ("remove", "this"), ("park", "following")):
                with self.subTest(action=action, scope=scope):
                    until = second.isoformat() if action == "park" else ""
                    fields = self.fields(saved, action, scope=scope, run="2", schedule_words="Use the old schedule.",
                                         schedule_revision=revision, until=until)
                    with self.assertRaisesRegex(the_list.ScheduleSelectionChanged, "The schedule changed"):
                        _ = list_page.submit(root, urlencode(fields))
                    self.assertEqual(the_list.load(MORTY.id, item.id), current)
            change = self.fields(saved, "schedule", scope="following", run="2",
                                 schedule_words="Check the remaining runs.")
            _ = list_page.submit(root, urlencode(change))
            remaining = the_list.load(MORTY.id, item.id).schedule
            assert isinstance(remaining, the_list.ScheduledSeries)
            self.assertEqual((remaining.first.start, remaining.first.words, remaining.rule.rrule),
                             (second, change["schedule_words"], "FREQ=WEEKLY;BYDAY=MO;COUNT=4"))
            remove = self.fields(saved, "remove", scope="this", run="2",
                                 schedule_revision=the_list.schedule_revision(remaining))
            _ = list_page.submit(root, urlencode(remove))
            _ = list_page.submit(root, urlencode(remove))
            skipped = the_list.load(MORTY.id, item.id).schedule
            assert isinstance(skipped, the_list.ScheduledSeries)
            self.assertEqual(skipped.exceptions, (the_list.RunException(
                2, the_list.SkippedRun("Removed by nate on the Studio.")),))
            other = self.fields(saved, "remove", scope="this", run="3")["request"]
            for request, expected in ((remove["request"], True), (create["request"], False), (other, False)):
                with self.subTest(request=request):
                    state = list_page.saved_action_state(MORTY.id, item.id, park_request=request)
                    self.assertEqual(state.get("remove_request_saved"), expected)
            _ = list_page.submit(root, urlencode(self.fields(saved, "remove", scope="all", run="3")))
        self.assertIs(the_list.load(MORTY.id, item.id).state, the_list.ItemState.CLOSED)

        exhausted = the_list.add(
            MORTY.id, "Review a finished series", owner=OWNER.id, source="nate",
            changes={"state": the_list.ItemState.SCHEDULED, "schedule": the_list.ScheduledSeries(
                the_list.ScheduledRun(first, "Review the result."), repeat_rule.parse("FREQ=DAILY;COUNT=1"),
                the_list.series_run_limit())},
        )
        for today, short_eta, note in (
            (NOW, "Nov 30 09:00 EST", " · next Nov 30 09:00 EST"),
            (first + timedelta(days=1), "No time yet", " · No next run"),
        ):
            with self.subTest(today=today), patch.object(the_list, "now", return_value=today):
                if today > first:
                    _ = self.finish_next_scheduled_run(exhausted.id)
                state = list_page.saved_action_state(MORTY.id, exhausted.id, include_detail=True)
                self.assertEqual(state.get("eta_short"), short_eta)
                self.assertTrue(state.get("scheduled_note", "").endswith(note))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_schedule_filters_repeat_menu_and_scopes(self) -> None:
        # Close idle Firefox before this Chrome test uses several renderers.
        if self._firefox is not None:
            self._firefox.close()
        root = self.root / "reviews"
        start = the_list.ScheduledRun(datetime(2026, 11, 30, 14, tzinfo=UTC), "Compare the account predictions.")
        one_time = the_list.ScheduledRun(datetime(2026, 11, 27, 21, tzinfo=UTC), "Set the time zone to Eastern time.")
        limit = the_list.series_run_limit()
        sequence = the_list.ScheduledSeries(start, repeat_rule.parse("FREQ=WEEKLY;BYDAY=MO"), limit,
                                             run_number_offset=1, runs_used=1, finished_runs=frozenset({1}))
        _ = the_list.add(MORTY.id, "Check the account predictions", owner=OWNER.id, source="nate",
                         changes={"state": the_list.ItemState.SCHEDULED, "schedule": sequence})
        _ = the_list.add(MORTY.id, "Set the time zone to Eastern time", owner=OTHER.id, source="nate",
                         changes={"state": the_list.ItemState.SCHEDULED, "schedule": one_time})
        first = the_list.ScheduledSeries(start, repeat_rule.parse("FREQ=DAILY"), limit)
        _ = the_list.add(MORTY.id, "Check the first run", owner=OWNER.id, source="nate",
                         changes={"state": the_list.ItemState.SCHEDULED, "schedule": first})
        _ = self.add("Give the result", the_list.ItemState.WAITING_FOR_NATE)
        for title in ("Park this item", "Start once", "Repeat daily", "Repeat weekly", "Repeat monthly",
                      "Repeat annually", "Repeat on weekdays", "Repeat with custom settings",
                      "Repeat on Thursday"):
            _ = self.add(title)

        finish_next_run = self.finish_next_scheduled_run
        advanced = False

        class FixtureList(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                url = urlsplit(self.path)
                if url.path != studio_page.PAGE_PATH:
                    self.send_error(404)
                    return
                query = parse_qs(url.query)
                if "item-state" in query:
                    state = list_page.saved_action_state(query["keeper"][0], query["item-state"][0],
                                                         park_request=query["park-request"][0], include_detail=True)
                    mime, content = "application/json", json.dumps(state).encode()
                else:
                    mime, content = "text/html; charset=utf-8", list_page.render(root, include_counts=False)
                self.send_response(200)
                self.send_header("Content-Type", mime)
                self.end_headers()
                _ = self.wfile.write(content)

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers["Content-Length"])).decode()
                nonlocal advanced
                if self.headers.get("X-Test-Advance-Run") and not advanced:
                    _ = finish_next_run("L1")
                    advanced = True
                try:
                    _, state = list_page.save_action(root, body, include_detail=True)
                    code, result = 200, state
                except ValueError as error:
                    code, result = 400, {"reason": str(error)}
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                _ = self.wfile.write(json.dumps(result).encode())

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        browser = self.chrome()
        with ThreadingHTTPServer(("127.0.0.1", 0), FixtureList) as server:
            server.daemon_threads = False
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                session = browser.session
                _ = browser.command("Emulation.setDeviceMetricsOverride",
                                    {"width": 1440, "height": 1024, "deviceScaleFactor": 1, "mobile": False}, session)
                _ = browser.command("Emulation.setEmulatedMedia", {"features": [
                    {"name": "prefers-color-scheme", "value": "dark"},
                    {"name": "prefers-reduced-motion", "value": "reduce"}]}, session)
                browser.navigate(f"http://127.0.0.1:{server.server_port}{studio_page.PAGE_PATH}")
                proof = os.environ.get("LIST_SCHEDULE_PROOF")

                def evaluate(expression: str) -> None:
                    answer = browser.command("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                             "returnByValue": True}, session)
                    self.assertNotIn("exceptionDetails", answer, answer)

                def capture_proof(selector: str, path: Path) -> None:
                    clip = browser.command("Runtime.evaluate", {"returnByValue": True, "expression": """
(() => {
 const box=document.querySelector(""" + json.dumps(selector) + """).getBoundingClientRect();
 return {x:box.left+scrollX,y:box.top+scrollY,width:box.width,height:box.height,scale:1};
})()
"""}, session)
                    rect = cast(dict[str, object], clip["result"])["value"]
                    result = cast(dict[str, str], browser.command(
                        "Page.captureScreenshot", {"format": "png", "clip": rect}, session))
                    _ = path.write_bytes(base64.b64decode(result["data"]))

                evaluate(r"""
(() => {
 Date.now=()=>Date.parse('2026-11-16T18:00:00Z');
 window.check=(ok,message)=>{if(!ok) throw new Error(message);};
 window.row=number=>document.querySelector('[data-number="'+number+'"]');
 window.show=value=>document.getElementById('filter-show-'+value).click();
 window.openItem=number=>{
  row(number).querySelector('.list-toggle').click();
  return recordDetails(row(number)).querySelector('form');
 };
 window.showParkScope=number=>{
  const form=recordIsOpen(row(number)) ? recordDetails(row(number)).querySelector('form') : openItem(number);
  const picker=form.querySelector('[data-later-menu]');
  if(picker.hidden) form.querySelector('[data-later-toggle]').click();
  const words=picker.querySelector('[data-schedule-words]');
  words.value='';
  words.dispatchEvent(new Event('input',{bubbles:true}));
  picker.querySelector('[value=park]').click();
  const dialog=document.querySelector('.list-schedule-dialog[open]');
  return {dialog,closePicker:()=>form.querySelector('[data-later-toggle]').click(),cancel:async()=>{
   const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
   dialog.querySelector('[data-cancel]').click();
   await closed;
   words.value=words.defaultValue;
   words.dispatchEvent(new Event('input',{bubbles:true}));
  }};
 };
 show('needs-me');
 check(row(1).hidden && row(2).hidden && row(3).hidden && !row(4).hidden,'Needs me hides scheduled items.');
 show('scheduled');
 check(!row(1).hidden && !row(2).hidden && !row(3).hidden && row(4).hidden,'Scheduled lists only scheduled items.');
 check(document.getElementById('filter-show-scheduled').dataset.filterCount==='3','The Scheduled chip counts its rows.');
 check(row(2).compareDocumentPosition(row(1)) & Node.DOCUMENT_POSITION_FOLLOWING,'The earliest start is first.');
 check(row(2).querySelector('.list-state').textContent==='Scheduled','The row has a Scheduled state mark.');
 check(row(2).querySelector('[data-scheduled-note]').textContent==='Starts Nov 27 16:00 EST','The one-time row shows its start.');
 check(row(1).querySelector('[data-scheduled-note]').textContent==='Weekly on Monday · next Nov 30 09:00 EST',
  'The repeated row shows the rule and next start.');
})();
""")
                evaluate(r"""
(() => {
 const form=openItem(1);
 form.querySelector('[data-later-toggle]').click();
 const select=form.querySelector('[data-repeat]');
 check(select.value==='weekly','The saved weekly rule selects the weekly preset.');
 select.value='custom';select.dispatchEvent(new Event('change'));
 const dialog=document.querySelector('.list-schedule-dialog[open]');
 check(dialog?.getAttribute('aria-label')==='Custom recurrence','Custom opens the editor.');
 dialog.querySelector('[data-repeat-interval]').value='2';
 dialog.querySelector('[data-weekday="TH"]').click();
})();
""")
                _ = browser.command("Emulation.setDeviceMetricsOverride",
                                    {"width": 1024, "height": 1024, "deviceScaleFactor": 1, "mobile": False}, session)
                evaluate(r"""
(async () => {
 const dialog=document.querySelector('.list-schedule-dialog[open]');
 const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
 dialog.querySelector('[data-cancel]').click();
 await closed;
 await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const picker=recordDetails(row(1)).querySelector('[data-later-menu]');
 window.checkPicker=picker=>{
  const box=picker.getBoundingClientRect();
  check(box.left>=0 && box.right<=innerWidth && box.top>=0 && box.bottom<=innerHeight &&
   picker.scrollWidth<=picker.clientWidth,'The schedule picker fits the viewport.');
  const instruction=picker.querySelector('[data-schedule-words]');
  const calendar=picker.querySelector('[data-later-grid]');
  const save=picker.querySelector('[value=park]');
  check(calendar.getBoundingClientRect().bottom<instruction.getBoundingClientRect().top,
   'The picker stacks the fields at this width.');
  check(instruction.getBoundingClientRect().bottom<save.getBoundingClientRect().top &&
   picker.querySelector('[data-repeat]').getBoundingClientRect().bottom<save.getBoundingClientRect().top,
   'Save is below the instruction and Repeat.');
 };
 checkPicker(picker);
})();
""")
                if proof:
                    capture_proof("[data-later-menu]:not([hidden])", Path(proof) / "scheduled-picker-dark.png")
                scope_proof = os.environ.get("LIST_PARK_SCOPE_PROOF")
                if scope_proof:
                    evaluate("window.parkScopeProof=showParkScope(1);")
                    capture_proof(".list-schedule-dialog[open]", Path(scope_proof))
                    evaluate("parkScopeProof.cancel();")
                evaluate(r"""
(() => {
 const select=recordDetails(row(1)).querySelector('[data-repeat]');
 select.value='custom';select.dispatchEvent(new Event('change'));
})();
""")
                _ = browser.command("Emulation.setDeviceMetricsOverride",
                                    {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True}, session)
                _ = browser.command("Emulation.setEmulatedMedia", {"features": [
                    {"name": "prefers-color-scheme", "value": "light"},
                    {"name": "prefers-reduced-motion", "value": "reduce"}]}, session)
                evaluate(r"""
(async () => {
 let dialog=document.querySelector('.list-schedule-dialog[open]');
 const box=dialog.getBoundingClientRect();
 check(box.left>=0 && box.right<=innerWidth && box.top>=0 && box.bottom<=innerHeight &&
  dialog.scrollWidth<=dialog.clientWidth,'The Custom dialog fits a phone.');
 const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
 dialog.querySelector('[data-cancel]').click();
 await closed;
 await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const form=recordDetails(row(1)).querySelector('form');
 const picker=form.querySelector('[data-later-menu]');
 const pickerBox=picker.getBoundingClientRect();
 check(pickerBox.left>=0 && pickerBox.right<=innerWidth && pickerBox.top>=0 && pickerBox.bottom<=innerHeight &&
  picker.scrollWidth<=picker.clientWidth,'The schedule picker fits a phone.');
 checkPicker(picker);
 form.querySelector('[data-later-toggle]').click();
 form.querySelector('[value=remove]').click();
 dialog=document.querySelector('.list-schedule-dialog[open]');
 check([...dialog.querySelectorAll('label:not([hidden])')].map(label=>label.textContent.trim()).join(',')===
  'This run,This and following runs,All runs','A later run offers all three scopes.');
 const cancelled=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
 dialog.querySelector('[data-cancel]').click();
 await cancelled;
 openItem(3).querySelector('[value=remove]').click();
 dialog=document.querySelector('.list-schedule-dialog[open]');
 check(dialog.querySelector('[data-scope-following]').hidden,'The first run omits the following scope.');
 const firstRunClosed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
 dialog.querySelector('[data-cancel]').click();
 await firstRunClosed;
 for(const [number,scope] of [[1,'following'],[3,'all']]){
  const park=showParkScope(number);
  const reason=park.dialog.querySelector('[data-scope-park]');
  check(park.dialog.querySelector('[value=this]').disabled &&
   park.dialog.querySelector('[name=scope-choice]:checked').value===scope &&
   !reason.hidden && reason.textContent==='Parking applies to the item.',
   'Park explains why This run is unavailable and selects the first allowed scope.');
  await park.cancel();
  park.closePicker();
 }
})();
""")
                for control, expected in (("data-schedule-words", "Read the result.\n"), ("data-repeat", None)):
                    with self.subTest(control=control):
                        evaluate(r"""
(() => {
 const form=openItem(1);
 form.querySelector('[data-later-toggle]').click();
 const field=form.querySelector('['+""" + json.dumps(control) + r"""+']');
 window.keySubmissions=0;
 window.parkKeySubmit=event=>{event.preventDefault();event.stopImmediatePropagation();keySubmissions++;};
 form.addEventListener('submit',parkKeySubmit,{capture:true});
 if(field.matches('textarea')){
  field.value='Read the result.';
  field.focus();
  check(document.activeElement===field,'The instruction field has focus.');
  field.setSelectionRange(field.value.length,field.value.length);
 }else{
  field.focus();
  const enter=new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true});
  field.dispatchEvent(enter);
  check(!enter.defaultPrevented,'Repeat receives Enter.');
 }
})();
""")
                        if expected is not None:
                            key = {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13}
                            _ = browser.command("Input.dispatchKeyEvent", {**key, "type": "keyDown", "text": "\r"}, session)
                            _ = browser.command("Input.dispatchKeyEvent", {**key, "type": "keyUp"}, session)
                        evaluate(r"""
(() => {
 const form=recordDetails(row(1)).querySelector('form');
 const instruction=form.querySelector('[data-schedule-words]');
 const expected=""" + json.dumps(expected) + r""";
 check(keySubmissions===0,'Enter in the schedule fields does not submit.');
 if(expected!==null){
  check(instruction.value===expected,'Enter inserts a newline in the instruction.');
  instruction.value=instruction.defaultValue;
  instruction.dispatchEvent(new Event('input',{bubbles:true}));
 }
 form.removeEventListener('submit',parkKeySubmit,{capture:true});
 form.querySelector('[data-later-toggle]').click();
 row(1).querySelector('.list-toggle').click();
})();
""")
                for number in (14, 15):
                    _ = the_list.add(MORTY.id, f"Review the schedule draft for item {number}", owner=OWNER.id, source="nate",
                                     changes={"state": the_list.ItemState.SCHEDULED, "schedule": one_time})
                tokyo = the_list.ScheduledRun(datetime(2026, 12, 1, 0, 30, tzinfo=ZoneInfo("Asia/Tokyo")),
                                              "Review the weekly figures.")
                tokyo_item = the_list.add(MORTY.id, "Review the Tokyo schedule", owner=OWNER.id, source="nate",
                                          changes={"state": the_list.ItemState.SCHEDULED, "schedule": tokyo})
                tokyo_series = replace(sequence, first=tokyo, rule=repeat_rule.parse("FREQ=WEEKLY;BYDAY=TU"))
                for number in range(17, 21):
                    _ = the_list.add(MORTY.id, f"Edit the Tokyo series for item {number}", owner=OWNER.id, source="nate",
                                     changes={"state": the_list.ItemState.SCHEDULED, "schedule": tokyo_series})
                for number, rule in (
                    (21, "FREQ=WEEKLY;BYDAY=MO"),
                    (22, "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"),
                    (23, "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO"),
                    (24, "FREQ=MONTHLY;BYDAY=-1MO"),
                    (25, "FREQ=YEARLY"),
                    (26, "FREQ=MONTHLY;BYMONTHDAY=30"),
                    (27, "FREQ=DAILY;UNTIL=20261130T140000Z"),
                ):
                    series = replace(sequence, rule=repeat_rule.parse(rule))
                    _ = the_list.add(MORTY.id, f"Move the series for item {number}", owner=OWNER.id, source="nate",
                                     changes={"state": the_list.ItemState.SCHEDULED, "schedule": series})
                evaluate(r"""
(async () => {
 window.refreshSchedules=()=>new Promise(resolve=>{
  const list=document.querySelector('[data-list]');
  const observer=new MutationObserver(()=>{
   if(!list.isConnected){observer.disconnect();resolve();}
  });
  observer.observe(list.parentElement,{childList:true});
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 });
 await refreshSchedules();
 show('all');
 const cases=[
  [5,'','none'],[6,'Start once','none'],[7,'Run daily','daily'],[8,'Run weekly','weekly'],
  [9,'Run monthly','monthly'],[10,'Run annually','yearly'],[11,'Run each weekday','weekdays'],
  [12,'Run a custom rule','custom'],[13,'Run every other Thursday','custom','2026-12-03'],
 ];
 const words=form=>form.querySelector('[data-schedule-words]');
 const edit=control=>control.dispatchEvent(new Event('input',{bubbles:true}));
 const pickDate=(form,date)=>{
  form.querySelector('[data-later-month-step="1"]').click();
  form.querySelector('[data-date="'+date+'"]').click();
 };
 const waitForSave=async(form,save,scope,{lost=false,date,instruction,rule,repeatZone,refusal,
                                        advanceRun=false,reason,laterWords}={})=>{
  let posted;
  let sentPost=false;
  let readback;
  const send=window.fetch;
  const response=new Promise(resolve=>{
   window.fetch=async(...args)=>{
    if(args[1]?.method==='POST'){
     sentPost=true;
     if(advanceRun) args[1].headers={'X-Test-Advance-Run':'1'};
     if(laterWords){form.elements.words.value=laterWords;edit(form.elements.words);}
    }
    const reply=await send(...args);
    if(args[1]?.method==='POST'){
     posted=new URLSearchParams(args[1].body);
     resolve(await reply.clone().json());
     if(lost) throw new Error('Response lost.');
    }
    if(new URL(args[0],location.href).searchParams.has('item-state')) readback=await reply.clone().json();
    return reply;
   };
  });
  save.click();
  if(scope){
   const dialog=document.querySelector('.list-schedule-dialog[open]');
   const selected=dialog.querySelector('[value='+scope+']');
   check(!selected.disabled,'The requested scope is available.');
   selected.click();
   const status=form.querySelector('[data-schedule-status]');
   const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
   dialog.querySelector('[data-done]').click();
   await closed;
   if(refusal){
    window.fetch=send;
    check(status.textContent===refusal && !sentPost && !form.querySelector('[data-later-menu]').hidden,
     'The picker shows the refusal without sending the edit.');
    return;
   }
   if(rule || date) check(!status.textContent,status.textContent);
  }
  const result=await response;
  if(!advanceRun) check(!result.reason,result.reason);
  if(scope) check(posted.get('scope')===scope,'The selected scope is sent.');
  if(reason) check(posted.get('words')===reason,'Remove sends the submitted reason.');
  if(date) check(posted.get('until')===date,'The edit sends the selected instant.');
  if(date && !rule) check(!posted.has('repeat'),'The move keeps the saved repeat rule.');
  if(instruction) check(posted.get('schedule_words')===instruction && !posted.get('until') && !posted.has('repeat'),
   'The edit sends only the instruction for the selected scope.');
  if(rule) check(posted.get('repeat')===rule && (date || !posted.get('until')),
   'The edit sends the new repeat rule and only the selected date.');
  if(repeatZone){
   const detail=new DOMParser().parseFromString(result.detail_html,'text/html');
   const fields=detail.querySelector('[data-schedule-template]').content.firstElementChild;
   check(fields.dataset.repeatZone===repeatZone,'The saved rule uses the expected time zone.');
  }
  await new Promise(resolve=>{
   const sent=()=>!form.dataset.sending;
   const observer=new MutationObserver(()=>{if(sent()){observer.disconnect();resolve();}});
   observer.observe(form,{attributes:true});
   if(sent()){observer.disconnect();resolve();}
  });
  window.fetch=send;
  if(advanceRun){
   const reason='The next run changed. Open the item again.';
   check(result.reason===reason && recordIsOpen(row(1)) &&
    form.querySelector('[data-send-status]').textContent.includes(reason),'The page shows why the stale action was refused.');
  }
  if(lost) check(readback?.remove_request_saved && readback.item_state==='Scheduled' &&
   !recordIsOpen(row(1)),'The removal receipt confirms the save and closes the detail.');
 };
 for(const [number,task,repeat,newStart] of cases){
  const form=openItem(number);
  form.querySelector('[data-later-toggle]').click();
  if(form.querySelector('[data-later-custom]').hidden) form.querySelector('[data-later-choose]').click();
  form.elements.until.value='2026-11-30T09:00';edit(form.elements.until);
  words(form).value=task;edit(words(form));
  if(task){
   const select=form.querySelector('[data-repeat]');
   check([...select.options].map(option=>option.textContent).join('|')===
    'Does not repeat|Daily|Weekly on Monday|Monthly on the last Monday|Annually on November 30|Every weekday (Monday to Friday)|Custom…',
    'The repeat options follow the picked date.');
   select.value=repeat;select.dispatchEvent(new Event('change'));
   if(repeat==='custom'){
    const dialog=document.querySelector('.list-schedule-dialog[open]');
    const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
    dialog.querySelector('[data-repeat-interval]').value='2';
    dialog.querySelector('[data-weekday="TH"]').click();
    dialog.querySelector('[value=count]').click();
    dialog.querySelector('[data-repeat-count]').value='5';
    if(newStart){
     dialog.querySelector('[data-weekday="MO"]').click();
     check(dialog.querySelector('[data-done]').disabled &&
      dialog.querySelector('[data-repeat-start-status]').textContent.includes('selected repeat day'),
      'Custom explains why the selected days require a new start.');
     dialog.querySelector('[data-change-start]').click();
     await closed;
     check(document.activeElement.matches('[data-later-grid] [tabindex="0"]'),
      'Change start date returns focus to the calendar.');
     pickDate(form,newStart);
    }else{
     dialog.querySelector('[data-done]').click();
     await closed;
    }
   }
  }
  await waitForSave(form,form.querySelector('[value=park]'));
 }
 for(const [number,action,scope,options] of [
  [1,'park','this',{date:'2026-12-01T14:00:00.000Z'}],
  [1,'park','all',{instruction:'Compare the revised predictions.'}],
  [1,'park','following',{instruction:'Compare the remaining predictions.'}],
  [1,'park','following',{repeat:'custom',rule:'FREQ=WEEKLY;BYDAY=TU',note:'Weekly on Tuesday · next Dec 1 09:00 EST'}],
  [16,'park',null,{repeat:'weekly',rule:'FREQ=WEEKLY;BYDAY=MO',note:'Weekly on Monday · next Nov 30 10:30 EST'}],
  [17,'park','following',{instruction:'Use the updated figures.',repeatZone:'Asia/Tokyo',
   savedLabel:'Weekly on Tuesday',label:'Weekly on Tuesday',
   note:'Weekly on Tuesday · next Nov 30 10:30 EST'}],
  [18,'park','following',{wall:'2026-11-30T11:00',date:'2026-11-30T16:00:00.000Z',repeatZone:'Asia/Tokyo',
   savedLabel:'Weekly on Tuesday',label:'Weekly on Tuesday',
   note:'Weekly on Tuesday · next Nov 30 11:00 EST'}],
  [19,'park','following',{repeat:'weekly',rule:'FREQ=WEEKLY;BYDAY=MO',repeatZone:'America/New_York',
   note:'Weekly on Monday · next Nov 30 10:30 EST'}],
  [20,'park','following',{wall:'2026-11-30T11:00',date:'2026-11-30T16:00:00.000Z',
   repeat:'weekly',rule:'FREQ=WEEKLY;BYDAY=MO',repeatZone:'America/New_York',
   note:'Weekly on Monday · next Nov 30 11:00 EST'}],
  [21,'park','following',{date:'2026-12-01T14:00:00.000Z',savedLabel:'Weekly on Monday',
   label:'Weekly on Tuesday',rule:'FREQ=WEEKLY;BYDAY=TU',repeatZone:'America/New_York',
   note:'Weekly on Tuesday · next Dec 1 09:00 EST'}],
  [22,'park','following',{wall:'2026-12-07T09:00',date:'2026-12-07T14:00:00.000Z',
   savedLabel:'Custom · Every 2 weeks on Monday',label:'Custom · Every 2 weeks on Monday',
   note:'Every 2 weeks on Monday · next Dec 7 09:00 EST'}],
  [23,'park','following',{date:'2026-12-01T14:00:00.000Z',
   savedLabel:'Custom · Every 2 weeks on Monday',label:'Custom · Every 2 weeks on Monday',
   refusal:'The start date must fall on a selected repeat day.',
   note:'Every 2 weeks on Monday · next Nov 30 09:00 EST'}],
  [24,'park','following',{date:'2026-12-01T14:00:00.000Z',savedLabel:'Monthly on the last Monday',
   label:'Monthly on the first Tuesday',rule:'FREQ=MONTHLY;BYDAY=1TU',repeatZone:'America/New_York',
   note:'Monthly on the first Tuesday · next Dec 1 09:00 EST'}],
  [25,'park','following',{date:'2026-12-01T14:00:00.000Z',savedLabel:'Annually on November 30',
   label:'Annually on December 1',rule:'FREQ=YEARLY',repeatZone:'America/New_York',
   note:'Annually on December 1 · next Dec 1 09:00 EST'}],
  [26,'park','following',{date:'2026-12-01T14:00:00.000Z',
   savedLabel:'Custom · Monthly on day 30',label:'Custom · Monthly on day 30',
   refusal:'The start date must match the monthly repeat day.',
   note:'Monthly on day 30 · next Nov 30 09:00 EST'}],
  [27,'park','following',{date:'2026-12-01T14:00:00.000Z',
   refusal:'Choose an end date on or after the start.',
   note:'Daily until 2026-11-30 14:00:00 UTC · next Nov 30 09:00 EST'}],
  [2,'unpark',null,{}],
  [1,'remove','this',{lost:true,reason:'Skip this run.'}],
  [1,'remove','this',{reason:'Skip another run.',laterWords:'Keep this new draft.'}],
  [1,'park','all',{repeat:'daily',rule:'FREQ=DAILY',advanceRun:true}],
  [1,'park','following',{advanceRun:true}],
  [1,'remove','this',{advanceRun:true,reason:'Keep the refused reason.'}],
  [3,'remove','all',{}],
  [1,'remove','following',{refresh:true}],
 ]){
  if(options.refresh) await refreshSchedules();
  const form=recordIsOpen(row(number)) ? recordDetails(row(number)).querySelector('form') : openItem(number);
  if(options.date || options.instruction || options.repeat || (action==='park' && options.advanceRun)){
   form.querySelector('[data-later-toggle]').click();
   const select=form.querySelector('[data-repeat]');
   if(options.savedLabel)
    check(select.selectedOptions[0].textContent===options.savedLabel,'Repeat shows the saved rule.');
   if(options.wall){form.elements.until.value=options.wall;edit(form.elements.until);}
   else if(options.date) pickDate(form,'2026-12-01');
   if(options.label)
    check(select.selectedOptions[0].textContent===options.label,'Repeat shows the rule for the selected start.');
   if(options.refusal)
    check(form.querySelector('[data-schedule-status]').textContent===options.refusal,
     'The picker explains the invalid start before Save.');
   if(options.instruction){words(form).value=options.instruction;edit(words(form));}
   if(options.repeat){
    select.value=options.repeat;select.dispatchEvent(new Event('change'));
    if(options.repeat==='custom'){
     const dialog=document.querySelector('.list-schedule-dialog[open]');
     const closed=new Promise(resolve=>dialog.addEventListener('close',resolve,{once:true}));
     dialog.querySelector('[data-done]').click();
     await closed;
    }
   }else if(options.advanceRun){
    words(form).value='';edit(words(form));
    form.elements.until.value='2026-12-23T09:00';edit(form.elements.until);
   }
  }
  if(options.reason){form.elements.words.value=options.reason;edit(form.elements.words);}
  try{await waitForSave(form,form.querySelector('[value='+action+']'),scope,options);}
  catch(error){throw new Error('Item '+number+', '+action+', '+scope+': '+error.message);}
  if(options.reason){
   const current=recordIsOpen(row(number)) ? recordDetails(row(number)).querySelector('form') : openItem(number);
   const expected=options.advanceRun ? options.reason : options.laterWords ?? '';
   check(current.elements.words.value===expected,'Remove clears only its confirmed reason.');
  }
  if(number===1 && options.instruction && scope==='following')
   check(row(number).querySelector('[data-scheduled-note]').textContent.endsWith('Dec 1 09:00 EST'),
    'The instruction edit keeps the moved start.');
  if(options.note)
   check(row(number).querySelector('[data-scheduled-note]').textContent===options.note,
    'The changed rule keeps the displayed start.');
  if(action==='unpark'){
   const current=openItem(number);
   current.querySelector('[data-later-toggle]').click();
   current.querySelector('[data-later-choose]').click();
   check(words(current).value==='','Unpark clears the saved instruction from the next editor.');
   current.querySelector('[data-later-toggle]').click();
  }
 }
})();
""")
                evaluate(r"""
(async () => {
 for(const [number,edited] of [[14,false],[15,true]]){
  const form=openItem(number);
  form.querySelector('[data-later-toggle]').click();
  check(form.elements.until.value==='2026-11-27T16:00','The editor opens at the saved start.');
  if(edited){
   const words=form.querySelector('[data-schedule-words]');
   words.value='Use this draft.';
   words.dispatchEvent(new Event('input',{bubbles:true}));
   form.querySelector('[data-date="2026-11-28"]').click();
  }
  form.querySelector('[data-later-toggle]').click();
 }
})();
""")
                _ = the_list.update(MORTY.id, "L15", {"latest_status": "The schedule did not change."}, source="nate")
                evaluate(r"""
(async () => {
 await refreshSchedules();
 const form=recordDetails(row(15)).querySelector('form');
 form.querySelector('[data-later-toggle]').click();
 check(form.elements.until.value==='2026-11-28T16:00' &&
  form.querySelector('[data-schedule-words]').value==='Use this draft.',
  'Refresh preserves edits against the same schedule revision.');
 form.querySelector('[data-later-toggle]').click();
})();
""")
                for number in (14, 15):
                    changed = the_list.ScheduledRun(one_time.start + timedelta(days=3), "Use the current schedule.")
                    _ = the_list.update(MORTY.id, f"L{number}", {"schedule": changed}, source="nate")
                evaluate(r"""
(async () => {
 await refreshSchedules();
 for(const number of [15,14]){
  const form=recordIsOpen(row(number)) ? recordDetails(row(number)).querySelector('form') : openItem(number);
  form.querySelector('[data-later-toggle]').click();
  check(form.elements.until.value==='2026-11-30T16:00' &&
   form.querySelector('[data-schedule-words]').value==='Use the current schedule.',
   'Refresh uses the new start and instruction for item '+number+'.');
  form.querySelector('[data-later-toggle]').click();
 }
})();
""")
            finally:
                try:
                    browser.navigate("about:blank")
                finally:
                    server.shutdown()
                    thread.join()

        parked, single = (the_list.load(MORTY.id, f"L{number}") for number in (5, 6))
        self.assertEqual(parked.parked_until, start.start)
        self.assertIs(parked.state, the_list.ItemState.WORKING)
        self.assertEqual(single.schedule, the_list.ScheduledRun(start.start, "Start once"))
        saved_tokyo = the_list.load(MORTY.id, tokyo_item.id).schedule
        assert isinstance(saved_tokyo, the_list.ScheduledSeries)
        self.assertEqual(saved_tokyo.first.start, tokyo.start)
        self.assertEqual(saved_tokyo.first.start.tzinfo, ZoneInfo("America/New_York"))
        for number, rule in enumerate(("FREQ=DAILY", "FREQ=WEEKLY;BYDAY=MO", "FREQ=MONTHLY;BYDAY=-1MO",
                                       "FREQ=YEARLY", "FREQ=WEEKLY;BYDAY=MO,TU,WE,TH,FR",
                                       "FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,TH;COUNT=5",
                                       "FREQ=WEEKLY;INTERVAL=2;BYDAY=TH;COUNT=5"), 7):
            with self.subTest(number=number):
                schedule = the_list.load(MORTY.id, f"L{number}").schedule
                assert isinstance(schedule, the_list.ScheduledSeries)
                self.assertEqual(schedule.rule.rrule, rule)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_opens_the_shared_picker_and_submits_the_chosen_time(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-12-02T13:00:00Z');
 const toggle=form.querySelector('[data-later-toggle]');
 check(!form.querySelector('input[type=date]'),'Park has no native date input');
 toggle.click();
 form.querySelector('[data-later-choose]').click();
 const sheet=form.querySelector('[data-later-custom]');
 check(!sheet.hidden && !sheet.closest('[data-later-menu]').hidden,'Pick a date opens the shared sheet');
 check(document.activeElement.dataset.date==='2026-12-03','Focus starts on the selected date');
 sheet.querySelector('[data-date="2026-12-03"]').click();
 const field=sheet.querySelector('[data-later-time]');
 field.value='1930';field.dispatchEvent(new Event('input',{bubbles:true}));
 const summary=sheet.querySelector('[data-later-summary]').textContent;
 check(summary==='Parks until Dec 3 19:30 EST','The Park picker shows the short time in the user zone');
 check(!requests.length,'Changing the selection does not submit the form');
 field.focus();
 field.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true,cancelable:true}));
 check(sheet.hidden && requests.length===1,'Enter in Time closes the picker and submits once');
 check(requests[0]?.get('action')==='park' && requests[0].get('until')==='2026-12-04T00:30:00.000Z',
  'Save sends the selected instant');
 finishPost({item_state:'Working',parked:'true',parked_until:'Dec 3 19:30 EST'});
 await flush();
 check(row.querySelector('[data-parked-note]').textContent===summary.replace('Parks until','Parked until'),
  'The row shows the same time as the picker');
 check(form.querySelector('[data-parked-detail]').textContent==='Parked until Dec 3 19:30 EST',
  'The expanded form uses the same saved time.');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_parks_until_delivered_with_one_post(self) -> None:
        _ = self.add("Test List action retries", number=1)
        _ = self.add("Check the other item", number=2)
        self.browser_action_sequence(r"""
 const toggle=form.querySelector('[data-later-toggle]');
 const menu=form.querySelector('[data-later-menu]');
 const preset=menu.querySelector('[data-later-preset]');
 toggle.click();
 check(menu.firstElementChild===preset && preset.textContent==='Until delivered','The preset comes first');
 check(menu.querySelector('[data-later-custom]').hidden,'The date controls wait for Pick a date');
 preset.click();
 check(menu.hidden && requests.length===1,'One click closes the menu and posts once');
 check(requests[0]?.get('action')==='park' && requests[0].get('until')==='delivered','The POST uses the delivery value');
 finishPost({item_state:'Working',parked:'true',parked_until:'delivered'});
 await flush();
 check(!row.hidden && row.nextElementSibling?.dataset.number==='2',
  'All keeps the server order until a refresh.');
 check(document.querySelector('#filter-show-all b').textContent==='2' &&
  document.querySelector('#filter-show-parked b').textContent==='1','All and Parked count the parked item.');
 const status=document.querySelector('[data-hidden-action-status]');
 check(!recordIsOpen(row) && status.hidden && !form.querySelector('[data-send-status]').textContent,'Park collapses the row without a status message.');
 check(document.activeElement===row.querySelector('.list-toggle'),'All returns focus to the visible row.');
 show('working');
 check(row.hidden,'Working hides the item until delivery');
 check(status.hidden && !status.textContent,'The hidden parked row leaves no action status.');
 show('parked');
 check(!row.hidden && !form.querySelector('[value="unpark"]').hidden,'Parked shows the item and Unpark');
 check(form.querySelector('[data-parked-detail]').textContent==='Parked until delivered','The panel shows the condition');
 check(row.querySelector('[data-parked-note]').textContent==='Parked until delivered','The state cell has the condition');
 row.querySelector('.list-toggle').click();
 submit('unpark');
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'});
 await flush();
 check(row.hidden,'Unpark removes the item from Parked');
 show('working');
 check(!row.hidden,'Unpark returns the item to Working');
 show('all');
 check(row.nextElementSibling?.dataset.number==='2','All restores the default order after Unpark.');
 show('working');
 row.querySelector('.list-toggle').click();
 toggle.click();preset.focus();preset.click();
 finishPost({item_state:'Working',parked:'true',parked_until:'delivered'});
 await flush();
 check(row.hidden && !recordIsOpen(row),'Until delivered collapses the row and hides it in Working.');
 check(document.activeElement===document.getElementById('filter-show-working'),
  'Until delivered moves focus to the selected Show chip when the row is hidden.');
""", reduced_motion=True, add_item=False)
        self.browser_waiting_parking_refresh(unpark=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_menu_supports_keyboard_navigation(self) -> None:
        self.browser_action_sequence(r"""
 const errors=[];
 addEventListener('error',event=>errors.push(event.message));
 const toggle=form.querySelector('[data-later-toggle]');
 toggle.click();
 const choices=[...form.querySelectorAll('[role=menuitem]')];
 check(document.activeElement===choices[0],'Park focuses the first choice');
 for(const [key,index] of [['ArrowUp',4],['ArrowDown',0],['End',4],['Home',0]]){
  const event=new KeyboardEvent('keydown',{key,bubbles:true,cancelable:true});
  document.activeElement.dispatchEvent(event);
  check(event.defaultPrevented && document.activeElement===choices[index],'The menu handles '+key);
 }
 const words=form.elements.words;
 words.focus();
 check(form.querySelector('[data-later-menu]').hidden && toggle.getAttribute('aria-expanded')==='false' &&
  document.activeElement===words,'Focus outside Park closes the menu');
 check(!dateTimePickerOpen(form),'Park no longer holds refresh');
 check(errors.length===0,'The keys cause no JavaScript errors');
 check(requests.length===0,'Navigation sends no POST');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_uses_the_click_time_after_midnight(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-11-23T04:59:00Z');
 Date.now=()=>now;
 const toggle=form.querySelector('[data-later-toggle]'),menu=form.querySelector('[data-later-menu]');
 const preset=name=>menu.querySelector('[data-later-preset="'+name+'"]');
 const list=document.querySelector('[data-list]'),settings=list.dataset.parking;
 for(const [minutes,label] of [[1,'1 minute'],[90,'90 minutes'],[60,'1 hour'],[120,'2 hours']]){
  list.dataset.parking=JSON.stringify({...JSON.parse(settings),soon:minutes});
  toggle.click();
  check(preset('soon').querySelector('span').textContent===label,'The delay label follows the setting');
  toggle.click();
 }
 list.dataset.parking=JSON.stringify({...JSON.parse(settings),weekDay:3,weekTime:635});
 toggle.click();
 check(preset('week').querySelector('small').textContent==='Wed 10:35',
  'Next week shows the configured weekday and time');
 toggle.click();
 list.dataset.parking=settings;
 toggle.click();
 const labels=[...menu.querySelectorAll('[role=menuitem] span')].map(label=>label.textContent);
 check(labels.join('|')==='Until delivered|1 hour|Tomorrow|Next week|Pick a date…','Park shows five choices');
 check(preset('soon').querySelector('small').textContent==='' &&
  preset('morning').querySelector('small').textContent==='09:00' &&
  preset('week').querySelector('small').textContent==='Mon 09:00','The menu shows relative choices');
 check(!preset('week').hasAttribute('data-at'),'Opening the menu does not resolve a deadline');
 now=Date.parse('2026-11-23T05:01:00Z');
 preset('week').click();
 check(menu.hidden && requests.length===1,'One click after midnight submits the preset');
 check(requests[0]?.get('action')==='park' && requests[0].get('until')==='2026-11-30T14:00:00.000Z',
  'Next week uses the day of the click');
 if(requests.length) {finishPost({parked:'true',parked_until:'Nov 30 09:00 EST'});await flush();}
""", reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_attaches_suggestions_to_the_editable_time(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-11-23T04:53:00Z');
 const toggle=form.querySelector('[data-later-toggle]'),choose=form.querySelector('[data-later-choose]');
 toggle.click();choose.click();
 const menu=form.querySelector('[data-later-menu]'),time=menu.querySelector('[data-later-time]');
 const times=menu.querySelector('[data-later-times]');
 const key=name=>time.dispatchEvent(new KeyboardEvent('keydown',{key:name,bubbles:true,cancelable:true}));
 check(time.getAttribute('aria-controls')===times.id && times.hidden && !time.closest('.later-time').querySelector('select') && !time.form,
  'The editable Time field controls the list and has no implicit form action');
 check([...times.children].slice(0,3).map(option=>option.textContent).join()==='00:00,00:15,00:30',
  'The suggestions start at the next quarter hour across midnight');
 time.focus();time.value='1937';time.dispatchEvent(new Event('input',{bubbles:true}));
 check(document.activeElement===time && form.elements.until.value==='2026-11-23T19:37',
  'The Time field accepts a value between suggestions');
 menu.querySelector('[data-later-time-choices]').click();
 check(!times.hidden && document.activeElement===time,'The suggestion button opens the list and keeps Time focused');
 for(const name of ['ArrowLeft','ArrowRight','Home','End','Tab']){
  check(key(name),'The page leaves '+name+' to the browser');
 }
 key('Escape');
 check(times.hidden && !menu.hidden && time.value==='1937','Escape closes the list and keeps the typed draft');
 key('ArrowDown');key('ArrowDown');
 check(times.querySelector('[aria-selected="true"]')===times.children[1],'ArrowDown marks the second suggestion');
 key('ArrowUp');
 check(times.querySelector('[aria-selected="true"]')===times.children[0],'ArrowUp marks the first suggestion');
 key('ArrowDown');
 key('Enter');
 check(times.hidden && !menu.hidden && form.elements.until.value==='2026-11-23T00:15',
  'Enter in the list updates the draft and closes the list');
 check(requests.length===0,'Accepting a suggestion sends no request');
 form.elements.words.focus();
 check(time.value==='00:15' && menu.hidden,'Focus outside the date picker keeps the time and closes it');
 toggle.click();choose.click();time.focus();key('Escape');
 check(menu.hidden && document.activeElement===toggle && requests.length===0,
  'Escape in Time closes the picker and returns focus to Park');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_preset_retries_once_and_uses_a_fresh_request_for_a_new_click(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-11-23T04:59:00Z');
 Date.now=()=>now;
 show('all');
 const toggle=form.querySelector('[data-later-toggle]'),week=form.querySelector('[data-later-preset="week"]');
 const words=form.elements.words,notice=form.querySelector('[data-send-status]');
 words.value='Original words';
 toggle.click();week.click();
 const first=requests[0];
 check(first?.get('until')==='2026-11-23T14:00:00.000Z','The first click sends the deadline');
 now=Date.parse('2026-11-30T14:01:00Z');
 words.value='Edited words';words.dispatchEvent(new Event('input',{bubbles:true}));
 await loseParkPost();
 const replay=requests[1];
 check(notice.hidden && !form.querySelector('[value="retry-park"]'),
  'The pending retry shows no failure line or Retry Park button');
 check(replay?.toString()===first.toString() && replay.get('words')==='' && words.value==='Edited words',
  'The automatic retry sends the original request and keeps the edited draft');
 losePost();await flush();await finishRowMotion();
 const above=document.querySelector('[data-hidden-action-status]');
 check(requests.length===2 && !notice.hidden &&
  notice.textContent==='Save not confirmed: Fixture lost response' && above.hidden,
  'The final failure shows one notice');
 toggle.click();
 check(week.querySelector('small').textContent==='Mon 09:00','Next week stays available as a relative choice');
 week.click();
 const second=requests[2];
 check(requests.length===3 && second.get('request')!==first.get('request') &&
  second.get('until')==='2026-12-07T14:00:00.000Z' && second.get('words')==='',
  'A new preset click sends a fresh request with a new deadline');
 if(second){finishPost({parked:'true',parked_until:'Dec 7 09:00 EST'});await flush();}
 check(notice.hidden && requests.length===3,'A confirmed save leaves no failure or retry');
""", reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_one_hour_starts_at_the_click(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-11-23T04:59:59.500Z');
 Date.now=()=>now;
 form.querySelector('[data-later-toggle]').click();
 now+=1000;
 form.querySelector('[data-later-preset="soon"]').click();
 check(requests.length===1 && requests[0].get('until')==='2026-11-23T06:00:00.500Z',
  'One click parks for one hour from the click across a minute boundary');
 if(requests.length){finishPost({parked:'true',parked_until:'Nov 23 01:00 EST'});await flush();}
""", reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_focus_moves_to_the_calendar_when_previous_month_disables(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-11-30T15:00:00Z');
 form.querySelector('[data-later-toggle]').click();
 form.querySelector('[data-later-choose]').click();
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
    def test_browser_retries_the_same_parking_time_automatically_after_it_passes(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-12-02T13:00:00Z');
 Date.now=()=>now;
 show('all');
 const toggle=form.querySelector('[data-later-toggle]');
 toggle.click();
 form.querySelector('[data-later-choose]').click();
 const time=form.querySelector('[data-later-time]'),save=form.querySelector('[value="park"]');
 time.value='1930';time.dispatchEvent(new Event('input',{bubbles:true}));
 save.click();
 const first=requests[0];
 check(first?.get('until')==='2026-12-04T00:30:00.000Z','The first save sends the chosen instant');
 now=Date.parse('2026-12-05T13:00:00Z');
 finishPost({status:503,reason:'The save is unconfirmed.'});
 await flush();await flush();
 check(requests.length===2 && requests[1].toString()===first.toString(),
  'The automatic retry after a service error uses the same ID and instant');
 if(requests.length===2){finishPost({parked:'false',parked_until:''});await flush();}
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reopening_an_expired_park_retries_the_original_request(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-12-02T13:00:00Z');
 Date.now=()=>now;
 show('all');
 const toggle=form.querySelector('[data-later-toggle]'),choose=form.querySelector('[data-later-choose]');
 const time=form.querySelector('[data-later-time]'),save=form.querySelector('[value="park"]');
 toggle.click();choose.click();
 time.value='19:30';time.dispatchEvent(new Event('input',{bubbles:true}));
 const draft=form.elements.until.value;
 save.click();
 const first=requests[0];
 check(first?.get('until')==='2026-12-04T00:30:00.000Z','The first save uses the selected time');
 await loseParkPost();
 losePost();await flush();await finishRowMotion();
 now=Date.parse('2026-12-05T13:00:00Z');
 toggle.click();choose.click();
 check(form.elements.until.value===draft && time.value==='19:30','The picker keeps the unconfirmed draft');
 check(!save.disabled,'Save remains enabled for the unconfirmed Park');
 save.click();
 check(requests.length===3 && requests[2]?.toString()===first?.toString(),
  'Save sends the original request after the deadline');
 if(requests.length===3){finishPost({parked:'false',parked_until:''});await flush();}
""", reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_refusal_restores_focus(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-12-02T13:00:00Z');
 const toggle=form.querySelector('[data-later-toggle]');
 toggle.click();
 form.querySelector('[data-later-choose]').click();
 const save=form.querySelector('[value="park"]');
 show('working');
 save.focus();save.click();
 check(requests.length===1 && row.hidden,'Save submits Park and hides the row from Working');
 finishPost({reason:'The fixture refuses Park'});
 await flush();
 for(const animation of document.getAnimations()) animation.finish();
 await flush();
 check(!row.hidden && !toggle.disabled && document.activeElement===toggle,
  'The restored row returns focus to Park');
 check(requests.length===1,'The browser does not retry a refusal');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_expired_park_draft_keeps_the_calendar_in_the_tab_order(self) -> None:
        self.browser_action_sequence(r"""
 let now=Date.parse('2026-12-02T13:00:00Z');
 Date.now=()=>now;
 show('all');
 const toggle=form.querySelector('[data-later-toggle]'),grid=form.querySelector('[data-later-grid]');
 const save=form.querySelector('[value="park"]');
 toggle.click();
 form.querySelector('[data-later-choose]').click();
 save.click();
 const first=requests[0];
 await loseParkPost();
 losePost();await flush();
 for(const animation of document.getAnimations()) animation.finish();
 await flush();
 now=Date.parse('2026-12-15T13:00:00Z');
 toggle.click();
 form.querySelector('[data-later-choose]').click();
 const entry=grid.querySelector('button:not(:disabled)[tabindex="0"]');
 check(entry?.dataset.date==='2026-12-15','The calendar makes today reachable after the draft expires');
 check(grid.querySelector('[aria-selected="true"]')?.dataset.date==='2026-12-03',
  'The calendar keeps the unconfirmed date selected');
 check(entry===document.activeElement && !save.disabled,'The calendar focuses the date and enables Save');
 document.activeElement.dispatchEvent(new KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
 now=Date.parse('2027-01-02T13:00:00Z');
 toggle.click();
 form.querySelector('[data-later-choose]').click();
 const nextMonth=grid.querySelector('button:not(:disabled)[tabindex="0"]');
 check(nextMonth?.dataset.date==='2027-01-02','The calendar opens on today in the new month');
 if(nextMonth){
  nextMonth.dispatchEvent(new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true,cancelable:true}));
  check(document.activeElement.dataset.date==='2027-01-03','ArrowRight selects a new date from the focused day');
  save.click();
  check(requests[2]?.get('request')!==first.get('request') && requests[2]?.get('until')?.startsWith('2027-01-03'),
   'The new date sends a new request');
  if(requests.length===3){finishPost({parked:'true'});await flush();}
 }
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_park_refusal_keeps_focus_on_search(self) -> None:
        self.browser_action_sequence(r"""
 Date.now=()=>Date.parse('2026-12-02T13:00:00Z');
 form.querySelector('[data-later-toggle]').click();
 form.querySelector('[data-later-choose]').click();
 const save=form.querySelector('[value="park"]');
 show('working');
 save.focus();save.click();
 check(requests.length===1 && row.hidden,'Save submits Park and hides the row from Working');
 finishPost({reason:'The fixture refuses Park'});
 await flush();
 const search=document.getElementById('list-search');
 document.querySelector('[data-list-item] .list-toggle[aria-expanded=true]').click();
 search.focus();
 check(document.activeElement===search,'Search receives focus while the row returns');
 for(const animation of document.getAnimations()) animation.finish();
 await flush();
 check(!row.hidden && document.activeElement===search,'A refused Park keeps focus on Search');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reserves_a_lost_park_id_and_updates_the_saved_date_label(self) -> None:
        item = self.add("Change the parking date", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 const labels=()=>[...row.querySelectorAll('[data-label="State"] small')].map(note=>note.textContent);
 check(labels().join()==='Parked until Nov 18 13:00 EST','The row starts with one saved time label');
 form.elements.until.value='2026-12-03';
 const park=submit('park');
 await loseParkPost();
 check(requests[1]?.get('request')===park.id,'The automatic Park retry keeps its request ID');
 losePost();await flush();await finishRowMotion();
 check(submit('remove').id!==park.id,'Remove gets a different ID after a lost Park response');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 await finishRowMotion();
 form.elements.until.value='2026-12-04';
 check(submit('park').id!==park.id,'A different Park date gets a different ID');
 finishPost({item_state:'Working',parked:'true',parked_until:'Dec 5 00:00 EST'});await flush();
 check(labels().length===1,'A successful Park keeps one date label');
 check(labels().join()==='Parked until Dec 5 00:00 EST','The label uses the saved date from the response');
''', add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_unpark_controls_layout_and_focus(self) -> None:
        parked = self.add("Parked item", number=1, next_check=NOW + timedelta(hours=1))
        unparked = self.add("Unparked item", number=2)
        _ = the_list.park(MORTY.id, parked.id, NOW + timedelta(days=2), source="nate")
        _ = staff.set_producer_top_item(MORTY.id, staff.TopItem(unparked.number))
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
  const pick=selector=>recordDetails(item).querySelector(selector);
  const box=rectangle(pick('textarea'));
  const remove=rectangle(pick('[value=remove]')),park=rectangle(pick('[data-later-toggle]'));
  check(pick('textarea').rows===4 && remove.bottom<box.top,'four lines below the controls');
  check(near(remove.left,box.left) && near(park.right,box.right) && near(remove.top,park.top),'one aligned control row');
  check(pick('[value=unpark]').hidden===(item===other),'Unpark only shows on the parked item');
  check(pick('[data-parked-detail]').hidden===(item===other),'parking line only shows on the parked item');
  const padding=item===row ? '8px' : '12px';
  check(['[value=remove]','[data-later-toggle]'].every(selector=>
   getComputedStyle(pick(selector)).paddingInlineStart===padding),'The row uses its expected control padding.');
  if(item===row){
   const top=pick('[value=top]');
   check(top?.textContent==='Make top priority' && near(rectangle(top).top,remove.top),
    'The parked row shows Make top priority on the control row.');
   check(near(park.left-rectangle(pick('[value=unpark]')).right,8),'parking controls are adjacent');
   check(recordDetails(item).querySelectorAll('[data-parked-detail]:not([hidden])').length===1,
    'The expanded row shows one parking line.');
   pick('[data-later-toggle]').click();
   check(near(rectangle(pick('[data-later-menu]')).right,box.right),'picker aligns with the text box');
   pick('[data-later-toggle]').click();
  }
 }
 const unpark=form.querySelector('[value=unpark]');
 show('parked');
 if(!recordIsOpen(row)) row.querySelector('.list-toggle').click();
 check(!row.querySelector('[data-list-check]'),'The parked row omits the future check.');
 unpark.focus();
 const first=submit('unpark');
 losePost();await flush();
 check(!row.hidden && !unpark.hidden,'a lost response leaves Unpark available');
 check(document.activeElement===unpark,'a lost response returns focus to Unpark');
 check(submit('unpark').id===first.id,'an exact Unpark retry keeps its request ID');
 finishPost(SAVED_UNPARK);await flush();
 check(row.hidden && document.activeElement===document.querySelector('#filter-show-parked'),'focus moves to Parked');
 const status=document.querySelector('[data-hidden-action-status][role="status"]');
 check(!status.hidden && status.textContent==='Item #1 unparked.','Parked confirms Unpark.');
 show('all');
 row.querySelector('.list-toggle').click();
 check(status.hidden,'All shows the confirmation in the row');
 const nextCheck=row.querySelector('[data-list-check]');
 check(nextCheck.getClientRects().length>0 &&
  nextCheck.querySelector('.warning')?.textContent==='Check overdue Nov 16 14:00 EST',
  'Unpark shows the current check with the warning style before a refresh.');
 form.elements.words.value='Keep these words.';
 form.elements.until.value='2026-12-03';
 submit('park');
 check(!nextCheck.getClientRects().length,'The row hides the check while Park is pending.');
 finishPost({item_state:'Working',parked:'true',parked_until:'Dec 3 00:00 EST',check_html:''});await flush();
 show('parked');
 check(!recordIsOpen(row) && !row.querySelector('[data-list-check]'),
  'The Park response collapses the row and removes the check before a refresh.');
 document.querySelector('[data-sort="3"]').click();
 row.querySelector('.list-toggle').click();
 unpark.focus();
 submit('unpark');
 show('all');
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'});await flush();
 check(!row.hidden && recordIsOpen(row),'Unpark in All keeps the row open');
 check(document.querySelector('[data-list-item]')===row,'Unpark preserves the item order by title');
 check(document.activeElement===document.querySelector('[data-list-item] .list-toggle[aria-expanded=true]'),'Focus stays on a control for the item.');
 check(unpark.hidden && form.querySelector('[data-parked-detail]').hidden,'the parking controls update');
 check(form.elements.until.value==='' && form.elements.words.value==='Keep these words.',
       'Unpark clears the parking draft and keeps the words');
 show('working');
 check(!row.hidden && recordIsOpen(row),'Working shows the expanded unparked row');
""".replace("SAVED_UNPARK", saved), reduced_motion=True, add_item=False, window_size=(1440, 1120), markup=markup)
        self.browser_waiting_parking_refresh(unpark=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_park_collapses_the_active_waiting_row_and_keeps_unsent_words(self) -> None:
        self.browser_waiting_parking_refresh(unpark=False, active_park=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_park_readback_collapses_the_active_waiting_row_and_keeps_unsent_words(self) -> None:
        self.browser_waiting_parking_refresh(unpark=False, active_park=True, lost=True)

    def browser_waiting_parking_refresh(self, *, unpark: bool, active_park: bool = False,
                                        lost: bool = False) -> None:
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
            "lost": lost,
        }).replace("</", "<\\/")
        if active_park:
            self.browser_action_sequence(r"""
 const fixture=FIXTURE;
 const item=()=>document.querySelector('[data-number="3"]');
 const refresh=async markup=>{
  refreshMarkup=markup;
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();
 };
 form.elements.words.value='Keep these unsent words.';
 form.elements.words.dispatchEvent(new Event('input'));
 refreshMarkup=fixture.saved;
 submit('park-until-delivered');
 check(document.querySelector('[data-record-inline-view]') && recordIsOpen(item()),'The item stays active while Park waits for its result.');
 check(!requests[0].get('words'),'Park does not send the draft.');
 if(fixture.lost){readback=()=>({...fixture.response,park_request_saved:true});await loseParkPost();losePost();}
 else finishPost(fixture.response);
 await flush();
 check(!document.querySelector('[data-record-inline-view]') && !recordIsOpen(item()),'Park collapses the active row when the saved item is waiting.');
 show('parked');
 check(!item().hidden && item().dataset.waitingForAnswer==='true','The waiting item appears in Parked.');
 item().querySelector('.list-toggle').click();
 check(document.querySelector('[data-record-inline-view]') && recordDetails(item()).querySelector('[data-waiting-actions]'),
  'Reopening the item shows the waiting form.');
 document.querySelector('[data-list-item] .list-toggle[aria-expanded=true]').click();
 await refresh(fixture.answered);
 show('all');item().querySelector('.list-toggle').click();
 check(document.querySelector('[data-record-inline-view]') && recordDetails(item()).querySelector('textarea[name="words"]').value==='Keep these unsent words.',
  'The dialog retains the unsent text through Park, waiting, closure and the answer.');
""".replace("FIXTURE", fixture), reduced_motion=True, add_item=False, markup=before, open_number=3)
            return
        self.browser_action_sequence(r"""
 const fixture=FIXTURE;
 const parks=fixture.action==='park';
 const current=()=>document.querySelector('[data-number="3"]');
 const other=()=>document.querySelector('[data-number="2"]');
 const numbers=()=>[...document.querySelectorAll('[data-number="2"],[data-number="3"]')]
  .map(item=>item.dataset.number).join(',');
 const refresh=async markup=>{
  refreshMarkup=markup;
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();
 };
 const input=recordDetails(current()).querySelector('textarea');
 input.value='Keep the words for this item.';
 input.dispatchEvent(new Event('input'));
 if(fixture.waiting) await refresh(fixture.waiting);
 const prior=current(),beforeOrder=numbers(),otherRow=other();
 const draft=recordDetails(otherRow).querySelector('textarea');
 const editOther=()=>{
  otherRow.querySelector('.list-toggle').click();
  draft.value='Keep the words for the other item.';
  draft.dispatchEvent(new Event('input'));
  draft.focus();draft.setSelectionRange(5,14,'backward');
 };
 if(!parks){
  editOther();prior.querySelector('.list-toggle').click();
  const unpark=recordDetails(prior).querySelector('[data-waiting-actions] [value="unpark"]');
  check(getComputedStyle(unpark).paddingInlineStart==='12px','The waiting row keeps its control padding.');
 }
 const methods=[];
 window.fetch=new Proxy(window.fetch,{apply(fetch,receiver,args){
  methods.push(args[1]?.method || 'GET');return Reflect.apply(fetch,receiver,args);
 }});
 refreshMarkup=fixture.saved;
 submit(parks ? 'park-until-delivered' : 'unpark',recordDetails(prior).querySelector('form'));
 if(parks) editOther();
 else recordDetails(prior).querySelector('.list-log-scroll').focus();
 finishPost(fixture.response);
 await flush();
 check(methods.join()==='POST,GET','The save makes one List GET while the item waits.');
 const refreshed=current(),freshDraft=recordDetails(other()).querySelector('textarea');
 // The open row keeps its node; a closed row takes the fresh one.
 check((parks ? refreshed!==prior && other()===otherRow : refreshed===prior) && beforeOrder===(parks ? '3,2' : '2,3') &&
  numbers()===(parks ? '2,3' : '3,2'),'The refresh moves the waiting row to its new group.');
 if(parks){
  check(freshDraft.value==='Keep the words for the other item.','Refresh keeps the other draft.');
  check(!recordIsOpen(refreshed),'Refresh keeps the row closed after the user opens another row.');
  check(document.activeElement===freshDraft && freshDraft.selectionStart===5 &&
   freshDraft.selectionEnd===14 && freshDraft.selectionDirection==='backward',
   'Park keeps focus and the selection in the other draft.');
  refreshed.querySelector('.list-toggle').click();
 }else check(document.activeElement===recordDetails(refreshed).querySelector('.list-log-scroll'),
  'Unpark keeps focus on the log in the waiting row.');
 check(recordIsOpen(refreshed) &&
  recordDetails(refreshed).querySelector('[data-waiting-questions]').textContent.includes('Which check passed?'),
  'The open row shows the waiting question.');
 await refresh(fixture.answered);
 check(recordDetails(current()).querySelector('textarea').value==='Keep the words for this item.',
  'The item draft returns after the producer answers.');
 if(!parks){
  other().querySelector('.list-toggle').click();
  check(recordDetails(other()).querySelector('textarea').value==='Keep the words for the other item.',
   'The other draft returns when its item reopens.');
 }
""".replace("FIXTURE", fixture), reduced_motion=True, add_item=False, markup=before, open_number=3)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reports_expired_parking_and_keeps_the_parking_draft(self) -> None:
        item = self.add("Parking expires before Unpark", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        self.browser_action_sequence('''
 show('parked');
 form.elements.until.value='2026-12-03';
 submit('unpark');
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'expired'});
 await flush();
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
 losePost();await flush();
 check(submit('unpark').id===first.id,'The retry sends the original request ID.');
 finishPost({item_state:'Working',parked:'true',parked_until:'Dec 4 00:00 EST',
  unpark_outcome:'cleared_newer_parking_active'});
 await flush();
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
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'no_active_parking'});
 await flush();
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
 submit('unpark',recordDetails(second).querySelector('form'));
 const result={item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'};
 const status=document.querySelector('[data-hidden-action-status]');
 finishFirst(result);await flush();
 check(row.hidden && !second.hidden && !status.hidden && status.textContent==='Item #1 unparked.',
       'The first request shows its result while the second request runs.');
 finishPost(result);await flush();
 check(second.hidden && !status.hidden && status.textContent==='Item #1 unparked. Item #2 unparked.',
       'Both results stay visible after both rows leave Parked.');
 show('all');
 second.querySelector('.list-toggle').click();
 check(!status.hidden && status.textContent==='Item #1 unparked.' &&
       !second.hidden && recordIsOpen(second) &&
       recordDetails(second).querySelector('[data-send-status]').textContent==='Unparked',
       'All shows the first result above the table and the second result in the visible details.');
 show('parked');
 check(!status.hidden && status.textContent==='Item #1 unparked. Item #2 unparked.',
       'Parked shows both results again.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_brings_parked_filter_into_view(self) -> None:
        for number in range(1, 25):
            item = self.add(f"Parked item {number}", number=number)
            _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        steps = '''
 document.querySelector('[data-list-item] .list-toggle[aria-expanded=true]').click();
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
 row.querySelector('.list-toggle').scrollIntoView({block:'center',behavior:'instant'});
 row.querySelector('.list-toggle').click();
 unpark.focus({preventScroll:true});
 check(filter.getBoundingClientRect().bottom<0,'The Parked filter scrolls out of view.');
 submit('unpark');
 finishPost({item_state:'Working',parked:'false',parked_until:'',unpark_outcome:'cleared'});
 await flush();
 const withinViewport=element=>{
  const {top,bottom}=element.getBoundingClientRect();
  return !element.hidden && top>=0 && bottom<=innerHeight;
 };
 const status=document.querySelector('[data-hidden-action-status]');
 const bounds=filter.getBoundingClientRect();
 const hit=document.elementFromPoint(bounds.left+bounds.width/2,bounds.top+bounds.height/2);
 const screen=JSON.stringify({innerWidth,innerHeight,scrollY,
  focus:document.activeElement.id,top:bounds.top,
  hit:hit?.outerHTML.slice(0,150),pageStatus:document.querySelector('[data-page-status]').getBoundingClientRect().height});
 check(row.hidden && document.activeElement===filter && withinViewport(filter) && filter.contains(hit),
       'Unpark moves focus and brings the Parked filter into view. '+screen);
 check(withinViewport(status) && status.textContent==='Item #1 unparked.',
       'The Unpark confirmation is visible. '+screen);
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
 finishPost({reason:'Fixture refuses Remove'});await flush();
 check(!unpark.hidden,'A refusal restores Unpark.');
 await finishRowMotion();
 submit('remove');
 finishPost({item_state:'Closed',parked:'true',unread:'false'});await flush();
 check(unpark.hidden && row.dataset.parked==='true','A confirmed Remove leaves Unpark hidden.');
 const sortValues=JSON.parse(row.dataset.sortValues);
 check(sortValues[3]==='active parked item' && sortValues[4]==='closed',
       'Remove keeps the Item key and updates the State key.');
''', add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_refresh_keeps_the_open_row_at_the_same_pixel(self) -> None:
        root = self.root / 'reviews'
        for number in range(7, 28):
            title = 'A row that returns above the open item' if number == 13 else f'List item {number}'
            _ = self.add(title, number=number)
        _ = the_list.close(MORTY.id, 'L13', 'The row returns later.', source='nate')

        def page() -> str:
            return list_page.render(root, include_counts=False).decode().replace(
                '</head>', '<style>.list-row td { line-height: 22px; }</style></head>')

        initial = page()
        _ = the_list.reopen(MORTY.id, 'L13', source='nate')
        added = page()
        _ = the_list.close(MORTY.id, 'L12', 'The work has finished.', source='nate')
        removed_above = page()
        _ = the_list.update(MORTY.id, 'L14', {
            'title': 'The refreshed title above the open row uses enough words to wrap across several lines.',
        }, source='morty')
        _ = the_list.update(MORTY.id, 'L15', {
            'title': 'The refreshed title also changes the open row.',
            'latest_status': 'The fresh List result. ' + 'The producer reports another completed check. ' * 12,
        }, source='morty')
        fresh = page()
        _ = the_list.close(MORTY.id, 'L15', 'The work has finished.', source='nate')
        removed = page()
        steps = r'''
 const findRow=()=>document.querySelector('[data-number="15"]');
 const toggle=()=>findRow().querySelector('.list-toggle');
 const detail=()=>recordDetails(findRow());
 const refreshList=async markup=>{
  refreshMarkup=markup;
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 };
 check(recordIsOpen(findRow()) && detail().contains(form) &&
  detail().closest('[data-detail]').previousElementSibling===findRow().querySelector('.list-row') &&
  !document.querySelector('dialog[open]'),'The row opens its form directly below its summary.');
 toggle().click();
 check(!recordIsOpen(findRow()) && findRow().querySelector('[data-detail]').hidden,'The toggle collapses the detail row.');
 findRow().querySelector('.list-row td:last-child').click();
 check(recordIsOpen(findRow()),'A click on the row opens its detail.');
 const second=document.querySelector('[data-number="16"]');
 const follow=async key=>{
  const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams({item:key}).toString();
  await changed;
 };
 await follow(second.dataset.key);
 check(recordIsOpen(second) && !recordIsOpen(findRow()),'The address expands only the linked row.');
 await follow(findRow().dataset.key);
 check(detail().contains(form) && !recordIsOpen(second),'The first link restores its expanded row.');
 const words=form.elements.words;
 words.value='Keep these words.';
 words.dispatchEvent(new Event('input',{bubbles:true}));
 form.scrollIntoView({block:'start',behavior:'instant'});
 scrollBy({top:90,behavior:'instant'});
 words.focus({preventScroll:true});words.setSelectionRange(2,5);
 const rowSummary=number=>document.querySelector('[data-number="'+number+'"] .list-row');
 const currentTop=()=>rowSummary(15).getBoundingClientRect().top;
 scrollBy({top:currentTop()-125,behavior:'instant'});
 const top=currentTop(),heightAbove=rowSummary(14).getBoundingClientRect().height;
 const heightOfDetail=detail().getBoundingClientRect().height;
 check(rowSummary(12).getBoundingClientRect().top<top && rowSummary(14).getBoundingClientRect().top<top &&
  top>0 && top<innerHeight && !rowSummary(13),'The open row is below the affected rows.');
 const checkTop=message=>check(recordIsOpen(findRow()) && currentTop()===top,
  message+' Expected '+top+' px. Found '+currentTop()+' px.');
 const beforeAdd=scrollY;
 await refreshList(ADDED_PAGE);
 checkTop('The open row keeps its position after a row appears above it.');
 check(rowSummary(13).getBoundingClientRect().top<top && scrollY>beforeAdd,'The new row appears above it.');
 const beforeRemove=scrollY;
 await refreshList(REMOVED_ABOVE);
 checkTop('The open row keeps its position after a row disappears above it.');
 check(!rowSummary(12) && scrollY<beforeRemove,'The refresh removes the row above it.');
 await refreshList(FRESH_PAGE);
 checkTop('The open row keeps its position after the two rows change height.');
 const refreshedWords=detail().querySelector('textarea');
 check(rowSummary(14).getBoundingClientRect().height>heightAbove,'The row above grows.');
 check(detail().getBoundingClientRect().height!==heightOfDetail && detail().textContent.includes('The fresh List result.'),
  'The open detail displays the new content and changes height.');
 check(refreshedWords.value==='Keep these words.' && document.activeElement===refreshedWords &&
  refreshedWords.selectionStart===2 && refreshedWords.selectionEnd===5,'The draft keeps focus and selection.');
 check(!document.querySelector('[data-show-record-updates]'),'The expanded row has no update control.');
 await refreshList(REMOVED_PAGE);
 check(!findRow() && !document.querySelector('[data-record-inline-view]'),'The refresh closes a removed row.');
'''
        for key, value in (('ADDED_PAGE', added), ('REMOVED_ABOVE', removed_above),
                           ('FRESH_PAGE', fresh), ('REMOVED_PAGE', removed)):
            steps = steps.replace(key, json.dumps(value).replace('</', '<\\/'))
        self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial,
                                     open_number=15, window_size=(1280, 720))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_refresh_keeps_the_open_row_at_the_scroll_limits(self) -> None:
        root = self.root / "reviews"
        for number in range(1, 14):
            _ = self.add(f"List item {number}", number=number)

        def page() -> str:
            return list_page.render(root, include_counts=False).decode()

        cases = (("top", 2, (1,)), ("bottom", 9, (10, 11, 12, 13)))
        for edge, opened, removed in cases:
            with self.subTest(edge=edge):
                before = page()
                for number in removed:
                    _ = the_list.close(MORTY.id, f"L{number}", "The work has finished.", source="nate")
                after = page()
                steps = r"""
 const edge=EDGE,scroller=document.scrollingElement;
 const summary=()=>document.querySelector('[data-number="OPEN_NUMBER"] .list-row');
 const bottom=()=>scroller.scrollHeight-scroller.clientHeight;
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 scrollTo({top:edge==='top' ? 0 : bottom(),behavior:'instant'});
 await frame();
 const top=summary().getBoundingClientRect().top;
 check(top>0 && top<innerHeight && scrollY===(edge==='top' ? 0 : bottom()),
  'The open row is visible at the '+edge+' limit. Its top is '+top+' px.');
 refreshMarkup=AFTER_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 await frame();
 const newTop=summary().getBoundingClientRect().top;
 check(recordIsOpen(summary().parentElement) && newTop===top,
  'The open row stays at '+top+' px at the '+edge+' limit. Found '+newTop+' px.');
 check(REMOVED_ROWS.every(number=>!document.querySelector('[data-number="'+number+'"]')),
  'The refresh removes the rows.');
 const main=document.querySelector('main');
 const openHeight=main.getBoundingClientRect().height;
 const detailHeight=summary().nextElementSibling.getBoundingClientRect().height;
 summary().querySelector('.list-toggle').click();
 await frame();
 check(openHeight-main.getBoundingClientRect().height>detailHeight,'Closing the row removes the remaining space.');
"""
                for key, value in (("EDGE", edge), ("AFTER_PAGE", after), ("REMOVED_ROWS", removed)):
                    steps = steps.replace(key, json.dumps(value).replace("</", "<\\/"))
                self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=before,
                                             open_number=opened, window_size=(1280, 1150))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_refresh_preserves_the_open_row_when_the_page_fits(self) -> None:
        root = self.root / "reviews"
        for number in range(1, 6):
            _ = self.add(f"List item {number}", number=number)
        before = list_page.render(root, include_counts=False).decode()
        for number in range(2, 6):
            _ = the_list.close(MORTY.id, f"L{number}", "The work has finished.", source="nate")
        after = list_page.render(root, include_counts=False).decode()
        steps = r"""
 const scroller=document.scrollingElement;
 const summary=()=>document.querySelector('[data-number="1"] .list-row');
 const bottom=()=>scroller.scrollHeight-scroller.clientHeight;
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 scrollTo({top:bottom(),behavior:'instant'});
 await frame();
 const top=summary().getBoundingClientRect().top;
 check(top>0 && top<innerHeight && scrollY===bottom() && scrollY>0,'The open row is visible at the bottom limit.');
 refreshMarkup=AFTER_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 await frame();
 const list=document.querySelector('[data-list]'),main=list.closest('main');
 const pageHeight=list.getBoundingClientRect().bottom+scrollY+parseFloat(getComputedStyle(main).paddingBottom);
 check(pageHeight<scroller.clientHeight && !document.querySelector('[data-number="2"]'),
  'The shortened page is smaller than the viewport before adding scroll space.');
 const newTop=summary().getBoundingClientRect().top;
 check(recordIsOpen(summary().parentElement) && newTop===top,
  'The open row stays at '+top+' px after the page becomes shorter than the viewport. Found '+newTop+' px.');
 scrollTo({top:0,behavior:'instant'});
 await frame();
 check(scroller.scrollHeight===scroller.clientHeight,'The scroll removes all space below the list.');
"""
        self.browser_action_sequence(steps.replace("AFTER_PAGE", json.dumps(after).replace("</", r"<\/")),
                                     reduced_motion=True, add_item=False, markup=before, window_size=(1280, 1320))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_scroll_input_removes_the_top_space_on_a_short_page(self) -> None:
        root = self.root / "reviews"
        _ = self.add("The row above", number=1)
        _ = self.add("The open row", number=2)
        before = list_page.render(root, include_counts=False).decode()
        _ = the_list.close(MORTY.id, "L1", "The work has finished.", source="nate")
        after = list_page.render(root, include_counts=False).decode()
        steps = r"""
 const scroller=document.scrollingElement,main=document.querySelector('main');
 const summary=()=>document.querySelector('[data-number="2"] .list-row');
 const toggle=()=>summary().querySelector('.list-toggle');
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 const refresh=async markup=>{
  refreshMarkup=markup;
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();
  await frame();
 };
 const intents=[
  new WheelEvent('wheel',{deltaY:60,bubbles:true}),
  new Event('touchmove',{bubbles:true}),
  new KeyboardEvent('keydown',{key:'PageDown',bubbles:true}),
 ];
 for(const [index,intent] of intents.entries()){
  if(index){
   await refresh(BEFORE_PAGE);
   toggle().click();
   await frame();
  }
  const top=summary().getBoundingClientRect().top;
  await refresh(AFTER_PAGE);
  const afterRefresh=summary().getBoundingClientRect().top,withSpace=main.getBoundingClientRect().height;
  check(afterRefresh===top && recordIsOpen(summary().parentElement),
   'The row stays at '+top+' px before '+intent.type+'. Found '+afterRefresh+' px.');
  check(!scrollY && scroller.scrollHeight===scroller.clientHeight,'The open page fits in the viewport.');
  document.body.dispatchEvent(intent);
  check(main.getBoundingClientRect().height<withSpace && summary().getBoundingClientRect().top<top && !scrollY,
   'The first '+intent.type+' removes the top space without a scroll event.');
  toggle().click();
 }
 await refresh(BEFORE_PAGE);
 toggle().click();
 await refresh(AFTER_PAGE);
 main.style.paddingBottom='900px';
 scrollTo({top:160,behavior:'instant'});
 const top=summary().getBoundingClientRect().top,withSpace=main.getBoundingClientRect().height;
 await frame();
 check(main.getBoundingClientRect().height<withSpace && summary().getBoundingClientRect().top===top,
  'When the document can scroll, removing the space keeps the row at the same pixel.');
"""
        for key, markup in (("BEFORE_PAGE", before), ("AFTER_PAGE", after)):
            steps = steps.replace(key, json.dumps(markup).replace("</", r"<\/"))
        self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=before,
                                     open_number=2, window_size=(1280, 1300))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_save_keeps_the_open_row_in_place_during_the_animation(self) -> None:
        steps = r"""
 let height=Infinity;
 for(const fraction of [0,.25,.5,.75]){
  animation.currentTime=duration*fraction;
  await frame();
  const nextHeight=clip.getBoundingClientRect().height;
  check(nextHeight<height,'The row above shrinks at '+fraction+'.');
  height=nextHeight;
  check(currentTop()===top,'The open row stays at '+top+' px at '+fraction+'. Found '+currentTop()+' px.');
 }
 animation.finish();
 await flush();
 await frame();
 check(!document.querySelector('[data-record-motion]') && recordIsOpen(summary().parentElement) && currentTop()===top,
  'The animation ends with the row still open at '+top+' px. Found '+currentTop()+' px.');
"""
        self.browser_list_save_animation((steps,), scroll_start="row")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_scrollbar_drag_releases_the_open_row_during_animation(self) -> None:
        steps = r"""
 animation.currentTime=duration*.5;
 await frame();
 check(currentTop()===top && main.firstElementChild!==firstContent,
  'The animation adds space above the list and keeps the open row at the same pixel.');
 const heightBeforeDrag=clip.getBoundingClientRect().height;
 const space=main.firstElementChild.getBoundingClientRect().height;
 scrollTo({top:240,behavior:'instant'});
 check(scrollY===240,'The scrollbar moves to the requested position.');
 if(FINISH_IMMEDIATELY) animation.finish();
 const draggedTop=currentTop();
 await frame();
 const scrollAfterDrag=scrollY;
 check(main.firstElementChild===firstContent && scrollAfterDrag===240-space,
  'The drag removes the top space and keeps the resulting scroll position.');
 if(!FINISH_IMMEDIATELY){
  check(currentTop()===draggedTop,'Removing the top space keeps the row at the pixel selected by the drag.');
  animation.currentTime=duration*.75;
  await frame();
  check(clip.getBoundingClientRect().height<heightBeforeDrag && currentTop()<draggedTop &&
   scrollY===scrollAfterDrag && main.firstElementChild===firstContent,
   'The animation continues without changing the scroll position or adding space.');
  animation.finish();
 }
 await flush();
 await frame();
 check(scrollY===scrollAfterDrag && recordIsOpen(summary().parentElement) && main.firstElementChild===firstContent,
  'The animation ends without changing the new scroll position.');
"""
        cases = tuple(steps.replace("FINISH_IMMEDIATELY", json.dumps(finish)) for finish in (False, True))
        self.browser_list_save_animation(cases, scroll_start="top")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_list_animation_keeps_the_open_row_at_the_bottom_limit(self) -> None:
        steps = r"""
 check(startingScroll>0 && startingScroll===startingLimit,'The page starts at its bottom limit.');
 let height=Infinity;
 for(const fraction of [0,.25,.5,.75]){
  animation.currentTime=duration*fraction;
  await frame();
  const nextHeight=clip.getBoundingClientRect().height;
  check(nextHeight<height,'The row above shrinks at '+fraction+'.');
  height=nextHeight;
  check(currentTop()===top,'The open row stays at '+top+' px at '+fraction+'. Found '+currentTop()+' px.');
 }
 animation.finish();
 await flush();
 await frame();
 check(!document.querySelector('[data-record-motion]') && recordIsOpen(summary().parentElement) && currentTop()===top,
  'The row stays at '+top+' px when the animation ends. Found '+currentTop()+' px.');
"""
        self.browser_list_save_animation((steps,), scroll_start="bottom", open_number=17)

    def browser_list_save_animation(self, cases: tuple[str, ...], *,
                                    scroll_start: Literal["top", "row", "bottom"], open_number: int = 5) -> None:
        root = self.root / "reviews"
        for number in range(1, 18):
            _ = self.add(f"List item {number}", number=number)
        before = list_page.render(root, include_counts=False).decode()
        _ = the_list.update(MORTY.id, "L1", {"state": the_list.ItemState.BLOCKED}, source="morty")
        after = list_page.render(root, include_counts=False).decode()
        steps = r"""
 const main=document.querySelector('main'),firstContent=main.firstElementChild;
 const summary=()=>document.querySelector('[data-number="ANCHOR_NUMBER"] .list-row');
 const currentTop=()=>summary().getBoundingClientRect().top;
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 show('working');
 form.elements.words.value='Keep this result.';
 form.elements.words.dispatchEvent(new Event('input'));
 submit('tell');
 summary().querySelector('.list-toggle').click();
 const bottom=()=>document.scrollingElement.scrollHeight-document.scrollingElement.clientHeight;
 const positions={top:0,row:currentTop()-150,bottom:bottom()};
 scrollTo({top:positions[SCROLL_START],behavior:'instant'});
 await frame();
 const top=currentTop(),startingScroll=scrollY,startingLimit=bottom();
 check(top>0 && top<innerHeight && row.querySelector('.list-row').getBoundingClientRect().top<top,
  'The saved item is above the new open row.');
 refreshMarkup=AFTER_PAGE;
 finishPost({item_state:'Blocked',unread:'false'});
 await flush();
 const animation=document.querySelector('[data-record-motion]')?.getAnimations({subtree:true})[0];
 if(!animation) throw new Error('The save result did not animate the row above.');
 animation.pause();
 const clip=animation.effect.target;
 const duration=animation.effect.getTiming().duration;
"""
        steps = steps.replace("ANCHOR_NUMBER", str(open_number)).replace("SCROLL_START", json.dumps(scroll_start)).replace(
            "AFTER_PAGE", json.dumps(after).replace("</", r"<\/"))
        for case, checks in enumerate(cases):
            with self.subTest(case=case):
                self.browser_action_sequence(steps + checks, add_item=False, markup=before, window_size=(1280, 800))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_lost_park_reply_keeps_the_other_open_row_at_the_same_pixel(self) -> None:
        root = self.root / "reviews"
        for number in range(1, 17):
            state = the_list.ItemState.DELIVERED if number == 5 else the_list.ItemState.WORKING
            _ = self.add(f"List item {number}", state, number=number)
        parked = self.add("The item to park", the_list.ItemState.BLOCKED, number=17)
        before = list_page.render(root, include_counts=False).decode()
        _ = the_list.park(MORTY.id, parked.id, NOW + timedelta(days=2), source="nate")
        _ = the_list.update(MORTY.id, parked.id, {"state": the_list.ItemState.WORKING}, source="morty")
        recovered = {**list_page.saved_action_state(MORTY.id, parked.id), "park_request_saved": True}
        steps = r"""
 const other=document.querySelector('[data-number="5"]'),summary=item=>item.querySelector('.list-row');
 const topOf=item=>summary(item).getBoundingClientRect().top;
 const frame=()=>new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
 document.querySelector('[data-sort="4"]').click();
 submit('park-until-delivered');
 other.querySelector('.list-toggle').click();
 scrollBy({top:topOf(other)-140,behavior:'instant'});
 await frame();
 const top=topOf(other);
 check(top>0 && top<innerHeight && topOf(row)<top,'The parked row starts above the open row.');
 readback=(key,request)=>{
  check(key===row.dataset.key && request===requests[0].get('request'),'The read uses the original Park request.');
  return RECOVERED;
 };
 await loseParkPost();
 losePost();
 await flush();
 await frame();
 check(row.dataset.state==='Working' && topOf(row)>topOf(other),'Recovery sorts the parked row below the open row.');
 check(recordIsOpen(other) && topOf(other)===top,
  'Park recovery keeps the other open row at '+top+' px. Found '+topOf(other)+' px.');
"""
        steps = steps.replace("RECOVERED", json.dumps(recovered).replace("</", "<\\/"))
        self.browser_action_sequence(steps, add_item=False, markup=before,
                                     open_number=17, window_size=(1280, 800))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_filter_closes_the_open_row_after_a_refresh(self) -> None:
        root = self.root / "reviews"
        _ = self.add("Open item", owner=OTHER.id, number=1)
        visible = self.add("Original title", number=2)
        initial = list_page.render(root, include_counts=False).decode()
        _ = the_list.update(MORTY.id, visible.id, {"title": "Updated title"}, source="morty")
        fresh = list_page.render(root, include_counts=False).decode()
        steps = r'''
 const first=()=>document.querySelector('[data-number="1"]');
 const second=()=>document.querySelector('[data-number="2"]');
 const title=()=>second().querySelector('strong').textContent;
 refreshMarkup=FRESH_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 check(recordIsOpen(first()) && title()==='Updated title','The refresh applies the new title and keeps the first row open.');
 const owner=document.getElementById('filter-owner-all');
 const filter=owner.parentElement.querySelector('[data-filter-value="staff:'+second().dataset.owner+'"]');
 filter.click();
 await flush();
 check(first().hidden && !recordIsOpen(first()) && first().querySelector('[data-detail]').hidden,
  'The filter closes the hidden row.');
 check(!second().hidden && title()==='Updated title' && pageReads===1,
  'The visible row keeps its updated title after one read.');
 check(!new URLSearchParams(location.hash.slice(1)).has('item'),'The address clears the closed item.');
'''
        steps = steps.replace("FRESH_PAGE", json.dumps(fresh).replace("</", "<\\/"))
        self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_refresh_applies_new_data_to_the_open_row_in_place(self) -> None:
        root = self.root / "reviews"
        item = self.add("Original title", number=1)
        initial = list_page.render(root, include_counts=False).decode()
        _ = the_list.update(MORTY.id, item.id, {"title": "Updated title", "latest_status": "The refreshed status.",
                                                "state": the_list.ItemState.BLOCKED}, source="morty")
        fresh = list_page.render(root, include_counts=False).decode()
        steps = r'''
 const input=form.elements.words;
 input.value='Keep these words';input.dispatchEvent(new Event('input'));
 input.focus();input.setSelectionRange(5,10);
 const summary=row.querySelector('.list-row'),revision=row.dataset.detailRevision;
 refreshMarkup=FRESH_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 const current=document.querySelector('[data-number="1"]'),words=recordDetails(current).querySelector('[name="words"]');
 check(current===row && recordIsOpen(row) && pageReads===1,'The refresh keeps the open row node and keeps it open.');
 check(row.querySelector('.list-row')!==summary && row.querySelector('strong').textContent==='Updated title' &&
  row.querySelector('.list-state').textContent==='Blocked','The open row shows the new summary and state.');
 check(row.dataset.state==='Blocked' && row.dataset.detailRevision!==revision,'The open row takes the new data attributes.');
 check(recordDetails(row).textContent.includes('The refreshed status.'),'The open row shows the new detail.');
 check(words?.value==='Keep these words' && document.activeElement===words &&
  words.selectionStart===5 && words.selectionEnd===10,'The draft, its focus and its selection stay.');
'''
        steps = steps.replace("FRESH_PAGE", json.dumps(fresh).replace("</", "<\\/"))
        self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial)

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
const button=document.querySelector('[data-record-inline-detail] [value=unpark]');
button.focus();
const focusedBefore=document.activeElement===button;
const observer=new MutationObserver(()=>{
 if(original.isConnected) return;
 observer.disconnect();
 const unpark=document.querySelector('[data-record-inline-detail] [value=unpark]');
 const failures=[];
 if(!unpark?.closest('[data-record-inline-detail]').textContent.includes('The refreshed status.'))
  failures.push('The expanded row shows the new status.');
 if(!focusedBefore || document.activeElement!==unpark || !button.id || unpark.id!==button.id)
  failures.push('The refresh restores focus to Unpark with the same ID.');
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
 document.body.append(result);
});
observer.observe(original.parentElement,{childList:true});
</script>'''
        self.browser_actions(markup, setup, probe)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_keeps_a_deadline_refresh_due_until_the_menu_closes(self) -> None:
        root = self.root / "reviews"
        item = self.add("Timed park", number=1)
        initial = list_page.render(root, include_counts=False).decode()
        deadline = NOW + timedelta(minutes=1)
        _ = the_list.park(MORTY.id, item.id, deadline, source="nate")
        timed = list_page.render(root, include_counts=False).decode()
        with patch.object(time, "time", return_value=deadline.timestamp()):
            expired = list_page.render(root, include_counts=False).decode()
        initial = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', initial, count=1)
        setup = r'''<script>
const replies=[];
let clock=1000;
Object.defineProperty(performance,'now',{value:()=>clock});
window.fetch=()=>new Promise(resolve=>replies.push(resolve));
</script>'''
        probe = r'''<script>
(async()=>{
 const failures=[],check=(ok,text)=>{if(!ok) failures.push(text);};
 const [timed,expired]=SNAPSHOTS;
 const nextTask=()=>new Promise(resolve=>setTimeout(resolve,0));
 const reply=(index,markup)=>replies[index]?.({ok:true,text:async()=>markup});
 document.dispatchEvent(new Event('visibilitychange'));
 clock+=61000;
 reply(0,timed);
 await nextTask();await nextTask();
 check(replies.length===2,'A response that arrives after the deadline requests a refresh at once.');
 if(replies.length===2){
  const original=document.querySelector('[data-list]');
  original.querySelector('.list-toggle').click();
  const toggle=original.querySelector('[data-later-toggle]');
  toggle.click();
  check(toggle.getAttribute('aria-expanded')==='true','The Park menu opens during the request.');
  reply(1,expired);
  await nextTask();
  check(original.isConnected && replies.length===2,'The List keeps the row while the menu is open.');
  toggle.click();
  await nextTask();
  check(replies.length===3,'Closing the menu requests the refresh that the List owes.');
  reply(2,expired);
  await nextTask();
  check(document.querySelector('[data-number="1"]').dataset.parked==='false' && replies.length===3,
   'The List applies the expired state after one refresh on close.');
 }
 const result=document.createElement('p');result.id='probe-results';result.textContent=JSON.stringify(failures);
 document.body.append(result);
})();
</script>'''.replace("SNAPSHOTS", json.dumps((timed, expired)).replace("</", "<\\/"))
        self.browser_actions(initial, setup, probe, reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_retries_a_lost_tell_after_a_refused_park(self) -> None:
        self.browser_action_sequence('''
 form.elements.words.value='Tell morty once.';
 const tell=submit('tell');losePost();await flush();
 form.elements.until.value='2026-12-03';
 const park=submit('park');
 check(park.id!==tell.id,'Park uses its own request ID');
 finishPost({reason:'Fixture refuses Park'});await flush();
 await finishRowMotion();
 check(submit('park').id!==park.id,'a refused Park leaves no unconfirmed request');
 finishPost({reason:'Fixture refuses Park again'});await flush();
 await finishRowMotion();
 form.elements.until.value='';
 const retry=submit('tell');
 check(retry.id===tell.id && retry.token===tell.token,'Tell retains its signed ID across refusals');
 finishPost({item_state:'Working',parked:'false'});await flush();
 form.elements.words.value='Tell morty once.';
 check(submit('tell').id!==tell.id,'a confirmed Tell leaves no unconfirmed request');
 finishPost({item_state:'Working',parked:'false'});await flush();
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_retries_two_lost_park_dates_after_a_confirmed_remove(self) -> None:
        root = self.root / "reviews"
        item = self.add("Test List action retries", number=1)
        before = list_page.render(root, include_counts=False).decode()
        _ = the_list.close(MORTY.id, item.id, "Removed by nate on the List page.", source="nate")
        _ = the_list.reopen(MORTY.id, item.id, source="morty")
        reopened = json.dumps(list_page.render(root, include_counts=False).decode()).replace("</", "<\\/")
        self.browser_action_sequence('''
 const originalParks=[];
 for(const date of ['2026-12-03','2026-12-04']){
  form.elements.until.value=date;
  const original=submit('park');
  originalParks.push(original);
  await loseParkPost();
  const replay=requests.at(-1);
  check(replay.get('request')===original.id && replay.get('token')===original.token &&
   replay.get('until')===date,'Park automatically retries the signed request for '+date);
  losePost();await flush();await finishRowMotion();
 }
 const [first,second]=originalParks;
 check(first.id!==second.id,'Each Park date gets its own request ID');
 const remove=submit('remove');
 check(!originalParks.some(park=>park.id===remove.id),'Remove uses its own request ID');
 finishPost({item_state:'Closed',parked:'true',unread:'false'});await flush();
 await finishRowMotion();
 check(!document.querySelector('[data-record-inline-view]') && row.hidden,'The confirmed Remove closes the dialog and hides the row.');
 refreshMarkup=REOPENED_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 refreshMarkup=undefined;
 show('all');
 const reopened=document.querySelector('[data-number="1"]');
 check(reopened!==row && !reopened.hidden && reopened.dataset.state==='New',
  'The List refresh shows the item that morty reopened.');
 for(const [date,original] of [['2026-12-03',first],['2026-12-04',second]]){
  reopened.querySelector('.list-toggle').click();
  const retryForm=recordDetails(reopened).querySelector('form');
  check(document.querySelector('[data-record-inline-view]') && retryForm!==form,'The retry uses the open dialog after the refresh.');
  retryForm.elements.until.value=date;
  const retry=submit('park',retryForm);
  check(retry.id===original.id && retry.token===original.token,'Park retains the signed ID for '+date);
  finishPost({item_state:'New',parked:'true',unread:'false'});await flush();
  await finishRowMotion();
 }
'''.replace('REOPENED_PAGE', reopened), add_item=False, markup=before)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_shows_a_lost_remove_notice_when_its_details_are_closed(self) -> None:
        self.browser_action_sequence('''
 show('all');
 submit('remove');
 check(row.hidden,'the row leaves All during Remove');
 check(document.querySelector('[data-record-motion]'),'Remove starts its exit animation');
 await finishRowMotion();
 losePost();await flush();
 await finishRowMotion();
 check(!row.hidden && row.dataset.unread==='true' && row.querySelector('.list-dot'),
  'a lost Remove restores the row and unread mark');
 row.querySelector('.list-row').click();
 check(!recordIsOpen(row),'the user can close the details after a lost response');
 const hiddenStatus=document.querySelector('[data-hidden-action-status]');
 check(!hiddenStatus.hidden && hiddenStatus.textContent==='Item 1: Save not confirmed: Fixture lost response',
  'the table shows the lost response while the details are closed');
 row.querySelector('.list-toggle').click();
 check(hiddenStatus.hidden && form.querySelector('[data-send-status]').getClientRects().length>0,
  'opening the details shows the inline notice only');
 row.querySelector('.list-toggle').click();
 check(!hiddenStatus.hidden,'closing the details restores the notice above the table');
 row.querySelector('.list-toggle').click();
 submit('remove');finishPost({reason:'Fixture refuses Remove'});await flush();
 check(row.dataset.state==='Working' && row.dataset.unread==='true' && row.querySelector('.list-dot'),
  'a refused Remove retry restores the last saved unread state');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_switching_rows_moves_a_lost_remove_notice(self) -> None:
        _ = self.add("Test List action retries", number=1)
        _ = self.add("Second List item", number=2)
        self.browser_action_sequence('''
 show('all');
 submit('remove');
 await finishRowMotion();
 losePost();await flush();
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
''', add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_shows_only_the_latest_hidden_remove_notice(self) -> None:
        _ = self.add("Test List action retries", number=1)
        _ = self.add("Second List item", number=2)
        self.browser_action_sequence('''
 show('all');
 const second=document.querySelector('[data-number="2"]');
 for(const item of [row,second]){
  if(!recordIsOpen(item)) item.querySelector('.list-toggle').click();
  submit('remove',recordDetails(item).querySelector('form'));
  losePost();await flush();
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
''', add_item=False)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_clears_the_list_save_notice(self) -> None:
        for number in range(1, 9):
            _ = self.add(f"List item {number}", number=number)
        with patch.object(list_page, "counts_strip", return_value=""):
            page = list_page.render(self.root / "reviews")
        def load(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            return self.firefox_view(arguments[-1], None, 1366, 768)

        with patch.object(browser_test_support, "run_process_group", side_effect=load):
            self.assertEqual(save_notice_failures(page, 'remove'), [[]])

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_keeps_the_question_request_after_a_service_failure(self) -> None:
        self.browser_action_sequence('''
 form.elements.words.value='Which check passed?';
 form.elements.words.dispatchEvent(new Event('input'));
 const attempt=submit('question');
 finishPost({status:503,reason:'Fixture delivery status unavailable'});await flush();
 check(form.querySelector('[data-send-status]').textContent===
  'Save not confirmed: Fixture delivery status unavailable','a service error leaves the save unconfirmed');
 const retry=submit('question');
 check(retry.id===attempt.id && retry.token===attempt.token,'the retry uses the original signature');
 finishPost({item_state:'Working',parked:'false'});await flush();
 check(form.elements.words.value==='','the confirmed reply clears the question');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_ask_uses_its_own_request_and_keeps_the_exact_retry(self) -> None:
        item = self.add("Review a question retry", number=1)
        self.browser_action_sequence('''
 const words=form.elements.words,ask=form.querySelector('[value="question"]');
 check(ask?.disabled,'Ask question starts disabled');
 words.value=' ';words.dispatchEvent(new Event('input'));
 check(ask?.disabled,'spaces keep Ask question disabled');
 words.value='What changed?';words.dispatchEvent(new Event('input'));
 check(!ask?.disabled,'words enable Ask question');
 const question=submit('question');losePost();await flush();
 const feedback=submit('tell');losePost();await flush();
 check(question.id!==feedback.id,'Ask and Tell use different request IDs');
 const retry=submit('question');
 check(retry.id===question.id && retry.token===question.token,'an exact Ask retry keeps its signature');
 finishPost({item_state:'Working',parked:'false'});await flush();
 check(words.value==='' && ask.disabled,'a saved question clears the box');
''', add_item=False)

        root = self.root / "reviews"
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        with patch.object(session_pane, "session_place"):
            before = list_page.render(root, include_counts=False).decode()
        _ = the_list.update(MORTY.id, item.id, {"producer": MORTY.id}, source="morty")
        after = json.dumps(list_page.render(root, include_counts=False).decode()).replace("</", r"<\/")
        steps = r"""
 const original=form.querySelector('[value="question"]');
 const name=original.getAttribute('aria-label').split(', ')[1];
 const words=form.elements.words;
 words.value='Retry the question.';words.dispatchEvent(new Event('input'));
 const first=submit('question');losePost();await flush();
 refreshMarkup=AFTER_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 const current=recordDetails(document.querySelector('[data-number="1"]')).querySelector('[data-feedback-form]');
 const ask=current.querySelector('[value="question"]');
 check(current!==form && current.querySelector('[value="tell"]').textContent==='Tell morty',
       'The refreshed form names the new producer for Tell.');
 check(ask.getAttribute('aria-label')==='Ask question, '+name,
       'The exact Ask retry keeps the original receiver on its button.');
 const listFetch=window.fetch;
 window.fetch=(path,options={})=>{
  if(options.method==='POST' && new Headers(options.headers).get('X-Undo-Hold')==='1'){
   requests.push(new URLSearchParams(options.body));
   return Response.json({hold:'a'.repeat(24),seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
  }
  return listFetch(path,options);
 };
 const toast=await new Promise(resolve=>{
  const observer=new MutationObserver(()=>{
   const text=document.querySelector('[data-send-undo] span');
   if(text){observer.disconnect();resolve(text.textContent);}
  });
  observer.observe(document.body,{childList:true,subtree:true});
  const retry=submit('question',current);
  check(retry.id===first.id && retry.token===first.token,'The retry keeps the signed Ask.');
 });
 check(toast==='Sent to '+name+'.','The retry toast names the receiver on the Ask button.');
"""
        self.browser_action_sequence(steps.replace("AFTER_PAGE", after), add_item=False, markup=before,
                                     reduced_motion=True)

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
            opened = without_scripts(list_page.render(root, status=status).decode())
            self.assertIn('data-waiting-for-answer="true"', opened)
            self.assertRegex(opened, r'<p data-waiting-note><span[^>]*data-color-text="role.producer"[^>]*>morty</span> will answer.</p>')
            status_cell = opened.split('<td data-label="State">', 1)[1].split('</td>', 1)[0]
            self.assertRegex(status_cell, r'<small data-open-question-note><span[^>]*data-color-text="role.producer"'
                             + r' style="color:#90fe42">Producer</span> answering</small>')
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
        answered = without_scripts(list_page.render(root).decode())
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
        read = without_scripts(list_page.render(root).decode())
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
        legacy_log = list_page.item_log(list_page.ListRecord(MORTY.id, legacy), NOW.astimezone(user_zone()),
                                        staff_names.read(), frozenset()).markup
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
            self.assertIn('value="read">Mark read</button>', waiting)
            fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', waiting))
            _ = list_page.submit(root, urlencode({**stale, **fields}), status)
            self.assertIn('data-log-index="Q1-answered"',
                          list_page.saved_action_state(MORTY.id, item.id, status=status).get("waiting_details_html", ""))
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q2", "--text", "The next check passed."]), 0)

        current = the_list.load(MORTY.id, item.id)
        for fields in (stale, already_read):
            with self.subTest(request=fields["request"]):
                _ = list_page.submit(root, urlencode(fields), status)
                saved = the_list.load(MORTY.id, item.id)
                self.assertEqual(the_list.unread_update(saved), the_list.unread_update(current))
                markup = without_scripts(list_page.render(root, include_counts=False, status=status).decode())
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
                if not unpark:
                    current = the_list.load(MORTY.id, item.id)
                    _, read = list_page.save_action(root, urlencode(self.fields(current, "read")), include_detail=True)
                    assert read is not None
                    self.assertEqual((read.get("unread_answer"), read["parked"]), ("false", "true"))
                    self.assertEqual(the_list.load(MORTY.id, item.id).parked_until, until)

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
                    markup = without_scripts(list_page.render(root, include_counts=False, status=status).decode())
                    self.assertRegex(markup, r'<tbody data-list-item[^>]*data-waiting-for-answer="false"[^>]*">')
                    sections: dict[str, str] = {
                        match.group(1): match.group(2)
                        for match in re.finditer(r'<section class="page-question" data-question="(Q[12])">(.*?)</section>',
                                                 markup, re.DOTALL)}
                    self.assertEqual(set(sections), {"Q1", "Q2"})
                    self.assertIn("Question not delivered.", sections["Q1"])
                    self.assertNotIn("Question not delivered.", sections["Q2"])
                    self.assertEqual(markup.count("Question not delivered."), 1)
                    self.assertNotIn('value="resend-', markup)

    def test_a_malformed_job_id_keeps_sender_exclusion_until_the_lock_releases(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Keep the sender lock")
        fields = urlencode(self.fields(item, "question", words="Which check finished?"))
        asked = list_page.submit(root, fields, status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        path = status.path(delivery.page, delivery.row)
        claimed = delivery.job.record
        lock_path = Path(cast(str, delivery.job.lock.name))
        self.assertEqual(lock_path.name, claimed.attempt_id)
        lock_path.with_name(path.name).touch()
        current = the_list.load(MORTY.id, item.id)
        reopened = change_page.DeliveryStatus(root)

        def send_with_damage(_recipient: staff.StaffId, _message: str, _message_id: str
                             ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            with status.locked():
                sending = change_page.read_question_attempt(path)
                assert isinstance(sending.owner, delivery_state.QuestionSendOwner)
                change_page.publish_question_attempt(path, replace(sending, owner=replace(sending.owner, job_id="damaged")))
            self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=reopened)["waiting_for_answer"], "false")
            body = without_scripts(list_page.render(root, include_counts=False, status=reopened).decode())
            self.assertIn("Which check finished?", body)
            self.assertNotIn("Question not delivered.", body)
            self.assertNotIn('value="resend-Q1"', body)
            change_page.recover_review_messages(reopened)
            change_page.recover_deferred_questions(reopened)
            list_page.deliver(list_page.submit(root, fields, reopened))
            self.assertTrue(reopened.has_deferred_questions())
            return question_outcome()

        with (patch.object(change_page, "send_question", side_effect=send_with_damage) as send,
              redirect_stderr(io.StringIO()), self.assertRaisesRegex(ValueError, "Invalid delivery field job_id")):
            list_page.deliver(asked)
        send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
        with self.assertRaisesRegex(ValueError, "Invalid delivery field job_id"):
            _ = change_page.read_question_attempt(path)
        with patch.object(change_page, "send_question", return_value=question_outcome("skipped")) as send:
            change_page.recover_deferred_questions(reopened)
            change_page.recover_deferred_questions(reopened)
            list_page.deliver(list_page.submit(root, fields, reopened))
        send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
        self.assertFalse(reopened.has_deferred_questions())
        saved = change_page.read_question_attempt(path)
        self.assertIs(saved.state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(saved.attempt_id, claimed.attempt_id)
        self.assertEqual(the_list.load(MORTY.id, item.id), current)

    def test_recovery_creates_a_missing_attempt_lock_for_a_damaged_job_id(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Recover without a lock file")
        asked = list_page.submit(root, urlencode(self.fields(item, "question", words="Which check finished?")), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        path = status.path(delivery.page, delivery.row)
        lock_path = Path(cast(str, delivery.job.lock.name))
        claimed = delivery.job.record
        assert isinstance(claimed.owner, delivery_state.QuestionSendOwner)
        delivery.job.release()
        lock_path.unlink()
        with status.locked():
            change_page.publish_question_attempt(path, replace(
                claimed, state=delivery_state.QuestionSendState.SENDING, owner=replace(claimed.owner, job_id="damaged")))

        def send_while_locked(_recipient: staff.StaffId, _message: str, _message_id: str
                              ) -> tuple[send_result.ConfirmedSendOutcome, str]:
            with file_lock.exclusive(lock_path, wait=False) as lock:
                self.assertIs(lock, file_lock.LockResult.BUSY)
            return question_outcome("skipped")

        reopened = change_page.DeliveryStatus(root)
        with patch.object(change_page, "send_question", side_effect=send_while_locked) as send:
            change_page.recover_review_messages(reopened)
        send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
        self.assertFalse(reopened.has_deferred_questions())
        saved = change_page.read_question_attempt(path)
        self.assertIs(saved.state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(saved.attempt_id, claimed.attempt_id)

    def test_recovery_waits_for_each_sender_lock(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        for held in ("row", "job", "attempt"):
            with self.subTest(held=held):
                item = self.add(f"Recover with the {held} lock held")
                fields = self.fields(item, "question", words="Which check finished?")
                asked = list_page.submit(root, urlencode(fields), status)
                assert isinstance(asked, list_page.ListQuestionDelivery)
                delivery = asked.delivery
                assert isinstance(delivery, change_page.PageQuestionSend)
                assert isinstance(delivery.job, change_page.QuestionJob)
                path = status.path(delivery.page, delivery.row)
                attempt_lock = Path(cast(str, delivery.job.lock.name))
                claimed = delivery.job.record
                assert isinstance(claimed.owner, delivery_state.QuestionSendOwner)
                old_job_id = uuid4().hex + uuid4().hex
                sending = replace(claimed, state=delivery_state.QuestionSendState.SENDING,
                                  owner=replace(claimed.owner, job_id=old_job_id))
                delivery.job.release()
                with status.locked():
                    change_page.publish_question_attempt(path, sending)
                locks = {"row": attempt_lock.with_name(path.name), "job": attempt_lock.with_name(old_job_id),
                         "attempt": attempt_lock}

                def send_while_locked(_recipient: staff.StaffId, _message: str, _message_id: str
                                      ) -> tuple[send_result.ConfirmedSendOutcome, str]:
                    for kind, candidate in locks.items():
                        with file_lock.exclusive(candidate, wait=False) as lock:
                            self.assertIs(lock, file_lock.LockResult.BUSY, kind)
                    return question_outcome("skipped")

                reopened = change_page.DeliveryStatus(root)
                with patch.object(change_page, "send_question", side_effect=send_while_locked) as send:
                    with file_lock.exclusive(locks[held]):
                        change_page.recover_review_messages(reopened)
                        send.assert_not_called()
                        self.assertTrue(reopened.has_deferred_questions())
                        self.assertEqual(change_page.read_question_attempt(path), sending)
                    change_page.recover_deferred_questions(reopened)
                    change_page.recover_deferred_questions(reopened)
                send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
                self.assertFalse(reopened.has_deferred_questions())
                saved = change_page.read_question_attempt(path)
                self.assertIs(saved.state, delivery_state.QuestionSendState.SENT)
                self.assertEqual(saved.attempt_id, claimed.attempt_id)

    def test_startup_requeues_a_question_when_a_sender_lock_cannot_open(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Recover after a lock error")
        fields = self.fields(item, "question", words="Which check finished?")
        asked = list_page.submit(root, urlencode(fields), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        path = status.path(delivery.page, delivery.row)
        claimed = delivery.job.record
        attempt_lock = Path(cast(str, delivery.job.lock.name))
        delivery.job.release()
        attempt_lock.unlink()
        attempt_lock.mkdir()

        reopened = change_page.DeliveryStatus(root)
        with (patch.object(change_page, "send_question", return_value=question_outcome()) as send,
              redirect_stderr(io.StringIO()) as errors):
            change_page.recover_review_messages(reopened)
            send.assert_not_called()
            self.assertTrue(reopened.has_deferred_questions())
            self.assertEqual(change_page.read_question_attempt(path), claimed)
            with file_lock.exclusive(attempt_lock.with_name(path.name), wait=False) as lock:
                self.assertIs(lock, file_lock.LockResult.ACQUIRED)
            attempt_lock.rmdir()
            change_page.recover_deferred_questions(reopened)
        lines = errors.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith(f"review-links question {delivery.row}: "))
        self.assertIn(attempt_lock.name, lines[0])
        send.assert_called_once_with(MORTY.id, claimed.message, claimed.message_id)
        self.assertFalse(reopened.has_deferred_questions())
        saved = change_page.read_question_attempt(path)
        self.assertIs(saved.state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(saved.attempt_id, claimed.attempt_id)

    def test_a_new_ask_of_the_same_words_starts_an_attempt_after_a_confirmed_failure(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Ask again after a failure")
        words = "Which check passed?"
        fields = self.fields(item, "question", words=words)
        first = list_page.submit(root, urlencode(fields), status)
        assert isinstance(first, list_page.ListQuestionDelivery)
        path = status.path(first.delivery.page, first.delivery.row)
        with (patch.object(change_page, "send_question", return_value=question_outcome("refused", "Producer offline")),
              patch.object(change_page, "alert_delivery_failures")):
            list_page.deliver(first)
        failed = change_page.read_question_attempt(path)
        self.assertIs(failed.state, delivery_state.QuestionSendState.FAILED)
        current = the_list.load(MORTY.id, item.id)
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            list_page.deliver(list_page.submit(root, urlencode(fields), change_page.DeliveryStatus(root)))
        send.assert_not_called()
        self.assertEqual(change_page.read_question_attempt(path), failed)
        self.assertEqual(the_list.load(MORTY.id, item.id), current)
        next_fields = self.fields(current, "question", words=words)
        self.assertNotEqual(next_fields["request"], fields["request"])
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            list_page.deliver(list_page.submit(root, urlencode(next_fields), status))
            list_page.deliver(list_page.submit(root, urlencode(next_fields), status))
        send.assert_called_once_with(MORTY.id, failed.message, failed.message_id)
        accepted = change_page.read_question_attempt(path)
        self.assertIs(accepted.state, delivery_state.QuestionSendState.SENT)
        self.assertNotEqual(accepted.attempt_id, failed.attempt_id)
        self.assertEqual(the_list.load(MORTY.id, item.id).questions, current.questions)

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
        markup = without_scripts(list_page.render(root, include_counts=False).decode())
        self.assertRegex(markup, r'<tbody data-list-item[^>]*data-waiting-for-answer="false"[^>]*">')
        self.assertIn("Question not delivered.", markup)
        self.assertNotIn('value="resend-', markup)

    def test_startup_reuses_the_list_question_key_after_a_result_write_failure(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Check the repair")
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        with patch.object(session_pane, "session_place", return_value=HerdrPlace("native", "w1:p1")):
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
            self.assertEqual(sending.recipient_id, DWIGHT.id)
            _ = the_list.update(MORTY.id, item.id, {"producer": MORTY.id}, source="morty")
            for _ in range(2):
                change_page.recover_review_messages(change_page.DeliveryStatus(root))
        self.assertEqual(transport.calls, [sending.message_id] * 2)
        self.assertEqual(len(transport.receipts), 1)
        saved = change_page.read_question_attempt(path)
        self.assertEqual(saved.state, delivery_state.QuestionSendState.SENT)
        self.assertEqual(saved.attempt_id, sending.attempt_id)

    def test_ask_sends_to_the_keeper_when_the_producer_is_unavailable(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        item = self.add("Check a producer's work")
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        words = "Which check passed?"
        def form(identity: str) -> tuple[dict[str, str], str]:
            current = the_list.load(MORTY.id, identity)
            markup = list_page.action_state(MORTY.id, current, status=status, include_detail=True).get("detail_html", "")
            feedback = markup.split('<form method="post" data-feedback-form>', 1)[1].split('</form>', 1)[0]
            fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)">', feedback))
            button = re.search(r'<button[^>]+value="question"[^>]+>', feedback)
            assert button is not None
            receiver = re.search(r'aria-label="Ask question, ([^"]+)"', button[0])
            assert receiver is not None
            return {**fields, "action": "question", "words": words, "until": ""}, receiver[1]

        with patch.object(session_pane, "session_place"):
            old_form, previous = form(item.id)
        self.assertEqual(previous, DWIGHT.name)
        with patch.object(session_pane, "session_place", side_effect=OSError("Producer offline")):
            with self.assertRaisesRegex(question_record.AnswerRefused, "The question recipient changed"):
                _ = list_page.submit(root, urlencode(old_form), status)
            self.assertEqual(the_list.load(MORTY.id, item.id), item)
            new_form, receiver = form(item.id)
            submission = list_page.submit(root, urlencode(new_form), status)
        saved = the_list.load(MORTY.id, item.id).questions[0]
        self.assertEqual(saved.recipient, question_record.KeeperForUnavailableProducer(MORTY.id, DWIGHT.id))
        markup = without_scripts(list_page.render(root, include_counts=False, status=status).decode())
        self.assertIn(words, markup)
        note_match = re.search(r'<p data-waiting-note(?: hidden)?>(.*?)</p>', markup)
        assert note_match is not None
        note = ElementTree.fromstring(f"<p>{note_match[1]}</p>")
        self.assertEqual("".join(note.itertext()), "dwight is not live, so morty will answer.")
        self.assertEqual([(name.text, name.get("data-color-text"), name.get("style")) for name in note],
                         [("dwight", "role.producer", "color:#90fe42"), ("morty", "role.producer", "color:#90fe42")])
        for text in ("Resend question", "ready to send", "may not have reached", "Question not delivered."):
            self.assertNotIn(text, markup)
        assert isinstance(submission, list_page.ListQuestionDelivery)
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            list_page.deliver(submission)
        path = status.path(submission.delivery.page, submission.delivery.row)
        record = change_page.read_question_attempt(path)
        self.assertIs(record.state, delivery_state.QuestionSendState.SENT)
        send.assert_called_once_with(MORTY.id, record.message, record.message_id)
        delivered_to = staff.snapshot().by_id(record.recipient_id)
        assert delivered_to is not None
        self.assertEqual(receiver, delivered_to.name)

        retry_item = self.add("Ask the producer when available")
        _ = the_list.update(MORTY.id, retry_item.id, {"producer": DWIGHT.id}, source="morty")
        with patch.object(session_pane, "session_place", side_effect=OSError("Producer offline")):
            offline, _ = form(retry_item.id)
            first = list_page.submit(root, urlencode(offline), status)
        assert isinstance(first, list_page.ListQuestionDelivery)
        with (patch.object(change_page, "send_question", return_value=question_outcome("refused", "Keeper offline")),
              patch.object(change_page, "alert_delivery_failures")):
            list_page.deliver(first)
        old_question, = the_list.load(MORTY.id, retry_item.id).questions
        with patch.object(session_pane, "session_place"):
            stale_form, _ = form(retry_item.id)
            renamed = staff.rename(DWIGHT.id, "renamed-dwight")
            with self.assertRaisesRegex(question_record.AnswerRefused, "The question recipient changed"):
                _ = list_page.submit(root, urlencode(stale_form), status)
            fresh_form, receiver = form(retry_item.id)
            fresh = list_page.submit(root, urlencode(fresh_form), status)
        assert isinstance(fresh, list_page.ListQuestionDelivery)
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            list_page.deliver(fresh)
        questions = the_list.load(MORTY.id, retry_item.id).questions
        self.assertEqual(questions[0], old_question)
        self.assertEqual(len(questions), 2)
        self.assertEqual(questions[1].words, old_question.words)
        self.assertIsNone(questions[1].answer)
        self.assertEqual(receiver, renamed.name)
        self.assertEqual(questions[1].recipient_id(MORTY.id), renamed.id)
        self.assertEqual(send.call_args.args[0], renamed.id)

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
                markup = without_scripts(list_page.render(root, include_counts=False, status=recovering).decode())
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
        markup = without_scripts(list_page.render(root, include_counts=False).decode())
        self.assertIn('data-waiting-for-answer="true"', markup)
        self.assertNotIn('value="resend-Q1"', markup)
        self.assertEqual(len(transport.calls), 1)
        self.assertEqual(len(transport.receipts), 1)

    def test_a_title_edit_preserves_ready_sent_and_queued_questions(self) -> None:
        root = self.root / "reviews"
        status = change_page.DeliveryStatus(root)
        state = delivery_state.QuestionSendState
        cases = ((state.READY, "keeper"), (state.SENT, "keeper"), (state.QUEUED, "keeper"),
                 (state.SENT, "no live process"), (state.SENT, "invalid registry"),
                 (state.SENT, "failed save"), (state.SENT, "exited"))
        for number, (previous_state, producer_state) in enumerate(cases, 1):
            with self.subTest(state=previous_state, producer_state=producer_state):
                item = self.add("Original title", number=number)
                if producer_state != "keeper":
                    item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
                if producer_state == "exited":
                    producer = replace(DWIGHT, exited=staff.Exited(NOW.isoformat(), "nate", "Stopped work"))
                    _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
                        row.to_json() for row in (MORTY, producer, OWNER, OTHER)]}))
                words = "Which check passed?\nWhat did you find?"
                fields = urlencode(self.fields(item, "question", words=words))
                with (patch.object(session_pane, "session_place", wraps=session_pane.session_place) as place,
                      patch.object(session_pane, "read_registry", return_value=sessions.RegistryRead((), ())) as registry):
                    if producer_state == "invalid registry":
                        registry.side_effect = ValueError("Invalid registry setting.")
                    if producer_state == "failed save":
                        with (patch.object(the_list, "_save", side_effect=OSError("Cannot save the question.")),
                              self.assertRaisesRegex(OSError, "Cannot save the question")):
                            _ = list_page.submit(root, fields, status)
                        self.assertEqual(the_list.load(MORTY.id, item.id), item)
                    if producer_state in ("failed save", "exited"):
                        place.return_value = HerdrPlace("native", "w1:p1")
                    asked = list_page.submit(root, fields, status)
                    if producer_state in ("keeper", "exited"):
                        place.assert_not_called()
                    else:
                        place.assert_called_once_with(DWIGHT.current_session)
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
                    with patch.object(unrouted_alert, "message_outcome", return_value=question_outcome(outcome)) as send:
                        list_page.deliver(asked)
                    send.assert_called_once()
                    self.assertEqual(send.call_args.args[0], f"session:{MORTY.current_session}")

                edited = the_list.update(MORTY.id, item.id, {"title": "New title"}, source="morty")
                target = change_page.list_question_target(Path(claimed.source.path), edited.id, edited.number,
                                                          edited.title, MORTY.id, keeper=MORTY.id)
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
                saved = list_page.saved_action_state(MORTY.id, item.id, status=status)
                self.assertEqual(saved["waiting_for_answer"], "true")
                if producer_state != "keeper":
                    self.assertEqual(edited.questions[0].recipient,
                                     question_record.KeeperForUnavailableProducer(MORTY.id, DWIGHT.id))
                    note = '<p data-waiting-note>dwight is not live, so morty will answer.</p>'
                    waiting_html = saved.get("waiting_details_html", "")
                    self.assertIn(note, without_role_colors(waiting_html))
                    self.assertRegex(waiting_html, r'data-color-text="role.producer"[^>]*>morty</span> will answer.')
                body = without_scripts(list_page.render(root, include_counts=False, status=status).decode())
                self.assertIn(f'data-key="{MORTY.id}/{item.id}"', body)
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
                    body = without_scripts(list_page.render(root, include_counts=False, status=status).decode())
                    self.assertIn(f'data-key="{MORTY.id}/{item.id}"', body)
                    self.assertNotIn('value="resend-', body)
                    current = the_list.load(MORTY.id, item.id)
                    fields = urlencode(self.fields(current, "question", words=words))
                    list_page.deliver(list_page.submit(root, fields, status))
                    send.assert_not_called()
                self.assertEqual(the_list.load(MORTY.id, item.id).questions, current.questions)

    def save_attempt_before_the_keeper_flag(self, status: change_page.DeliveryStatus,
                                            saved_state: delivery_state.QuestionSendState,
                                            receipt: delivery_state.NoQuestionSendReceipt | delivery_state.QuestionSendReceipt
                                            ) -> tuple[Path, delivery_state.QuestionAttempt, change_page.SavedQuestionDelivery]:
        item = self.add("The question from before the keeper flag")
        asked = list_page.submit(status.root, urlencode(self.fields(item, "question", words="Which check passed?")), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        claimed = delivery.job.record
        current = the_list.load(MORTY.id, item.id)
        saved = change_page.list_question_target(Path(claimed.source.path), current.id, current.number, current.title,
                                                 MORTY.id, keeper=MORTY.id).saved(current.questions[0])
        before_keeper = saved.message.replace(f" --keeper {shlex.quote(MORTY.id)}", "")
        self.assertNotEqual(before_keeper, saved.message)
        path = status.path(delivery.page, delivery.row)
        with status.locked():
            change_page.advance_question_attempt(path, claimed, replace(
                claimed, state=saved_state, message=before_keeper,
                owner=delivery_state.NoQuestionSendOwner(), receipt=receipt))
        _ = delivery.job.release()
        return path, change_page.read_question_attempt(path), saved

    def test_a_sent_question_from_before_the_keeper_flag_waits_for_an_answer(self) -> None:
        status = change_page.DeliveryStatus(self.root / "reviews")
        sent = delivery_state.QuestionSendState.SENT
        _, attempt, saved = self.save_attempt_before_the_keeper_flag(
            status, sent, delivery_state.QuestionSendReceipt("sent", ""))
        with status.locked():
            self.assertEqual(change_page.question_delivery_view(status, saved).state, sent)
        state = list_page.saved_action_state(MORTY.id, attempt.source.item_id, status=status)
        self.assertEqual(state["waiting_for_answer"], "true")

    def test_restart_resends_a_ready_question_from_before_the_keeper_flag(self) -> None:
        root = self.root / "reviews"
        path, attempt, _ = self.save_attempt_before_the_keeper_flag(
            change_page.DeliveryStatus(root), delivery_state.QuestionSendState.READY,
            delivery_state.NoQuestionSendReceipt())
        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            change_page.recover_review_messages(change_page.DeliveryStatus(root))
        send.assert_called_once_with(MORTY.id, attempt.message, attempt.message_id)
        self.assertEqual(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)

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
        body = without_scripts(list_page.render(root, include_counts=False, status=reopened).decode())
        self.assertIn('data-waiting-for-answer="false"', body)
        self.assertIn('aria-label="Unread update"', body)
        self.assertEqual(body.count("Question not delivered."), 1)
        self.assertNotIn('value="resend-', body)
        self.assertIn('data-unread-answer="true"', body)
        self.assertRegex(body, r'data-filter-value="needs-me"[^>]*data-filter-count="1"')
        self.assertEqual(len(list_page.waiting_updates(console.NewsRequest(root))), 1)
        self.assertEqual(list_page.saved_action_state(MORTY.id, item.id, status=reopened)["unread"], "true")
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        read = without_scripts(list_page.render(root, include_counts=False, status=reopened).decode())
        self.assertRegex(read, r'data-filter-value="needs-me"[^>]*data-filter-count="0"')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_moves_a_saved_question_to_producer_answering(self) -> None:
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
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
 check(!row.hidden,'The saved row stays in All.');
 const note=row.querySelector('[data-open-question-note]');
 check(note?.getClientRects().length && note.textContent==='Producer answering' &&
  note.firstElementChild?.textContent==='Producer' &&
  note.firstElementChild?.dataset.colorText==='role.producer' &&
  note.firstElementChild?.style.color==='rgb(144, 254, 66)' &&
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
 location.hash=new URLSearchParams({show:'working',item:row.dataset.key}).toString();
 await flush();
 check(!row.hidden && row.closest('[data-linked-item]:not([hidden])') && recordIsOpen(row) &&
  recordDetails(row).querySelector('[data-waiting-note]').textContent==='morty will answer.' &&
  !recordDetails(row).querySelector('[value="question"]'),'A direct link opens only the waiting row without Ask');
 check(count.textContent.startsWith('1 of 2'),'The linked row counts in All and stays outside Working.');
 for(const savedShow of ['waiting-answer','morty-answering']){
  location.hash=new URLSearchParams({show:savedShow,item:row.dataset.key}).toString();
  await flush();
  check(document.getElementById('filter-show-producer-answering').getAttribute('aria-pressed')==='true' && !row.hidden,
   savedShow+' selects Producer answering.');
 }
 input.value='Only this row';input.dispatchEvent(new Event('input'));
 await new Promise(resolve=>setTimeout(resolve,150));
 check(!row.hidden && count.textContent.startsWith('1 of 2'),'Search finds the waiting row in Producer answering.');
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
 finishPost({parked:'true',parked_until:'Nov 26 00:00 EST',waiting_for_answer:'true',
  open_question:'true',unpark_outcome:'cleared_newer_parking_active'});
 await flush();
 check(!row.hidden && chip.dataset.filterCount==='1','The newer parking keeps the waiting item in Parked.');
 check(recordDetails(row).querySelector('[data-waiting-note]') && row.dataset.waitingForAnswer==='true',
  'The parked row shows that the producer will answer.');
 check(row.querySelector('[data-parked-note]').textContent==='Parked until Nov 26 00:00 EST',
  'The parked row shows the newer time.');
 show('all');
 check(!row.hidden,'All shows the waiting item with the newer park.');
''', reduced_motion=True, add_item=False)
        steps = '''
 show('parked');
 const chip=document.querySelector('[data-filter-row="show"] [data-filter-value="parked"]');
 const status=document.querySelector('[data-hidden-action-status]');
 const unpark=form.querySelector('[value="unpark"]');
 check(!row.hidden && chip.dataset.filterCount==='1','Parked includes the item before the question.');
 unpark.focus();
 check(!document.hasFocus() && document.activeElement===unpark,
  'The background List retains Unpark as its active element.');
 submit('unpark');
 finishPost({parked:'false',parked_until:'',waiting_for_answer:'true',open_question:'true',unpark_outcome:'cleared'});
 await flush();
 check(row.hidden && chip.dataset.filterCount==='0','The waiting item leaves Parked and its count.');
 check(document.activeElement===chip,'The Parked filter receives focus.');
 check(!document.hasFocus(),'Unpark does not activate the List tab.');
 check(!status.hidden && status.textContent==='Item #1 unparked.','Parked shows the Unpark confirmation.');
 location.hash=new URLSearchParams({show:'parked',item:row.dataset.key}).toString();
 await flush();
 check(!row.hidden && recordIsOpen(row) &&
  recordDetails(row).querySelector('[data-waiting-note]').textContent==='morty will answer.' &&
  !recordDetails(row).querySelector('form'),'The direct link opens the item with only the waiting line.');
 check(chip.dataset.filterCount==='0' && !status.hidden,
  'The direct link leaves the item out of the count and keeps the Unpark confirmation.');
 show('producer-answering');
 check(!row.hidden && !status.hidden,'Producer answering keeps the Unpark confirmation.');
'''
        browser = self.chrome()
        info = cast(dict[str, str], browser.command("Target.getTargetInfo", {}, browser.session)["targetInfo"])
        other_tab = cast(str, browser.command("Target.createTarget", {
            "url": "about:blank", "browserContextId": info["browserContextId"],
        })["targetId"])
        try:
            _ = browser.command("Target.activateTarget", {"targetId": other_tab})
            self.browser_action_sequence(steps, reduced_motion=True, add_item=False)
        finally:
            if not browser.closed:
                browser.close_tab(other_tab)
                _ = browser.command("Page.bringToFront", {}, browser.session)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_unpark_focuses_the_parked_filter_without_window_focus(self) -> None:
        item = self.add("Check the parked item", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=7), source="morty")
        self.browser_action_sequence('''
 document.hasFocus=()=>false;
 show('parked');
 const chip=document.getElementById('filter-show-parked');
 const unpark=form.querySelector('[value="unpark"]');
 unpark.focus();
 check(parent===window && !document.hasFocus(),'The top-level page reports no window focus.');
 check(!row.hidden && recordIsOpen(row) && document.activeElement===unpark,'Unpark has focus in the open row.');
 submit('unpark');
 finishPost({parked:'false',parked_until:'',waiting_for_answer:'true',open_question:'true',unpark_outcome:'cleared'});
 await flush();
 check(row.hidden && !recordIsOpen(row),'Unpark hides and closes the row.');
 check(document.activeElement===chip,'The Parked filter receives focus.');
''', reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_open_row_and_shows_the_refusal_after_a_lost_remove(self) -> None:
        self.browser_action_sequence('''
 show('working');
 const remove=submit('remove');
 check(row.hidden,'Remove immediately hides the row in Working');
 losePost();await flush();
 check(!row.hidden && row.dataset.state==='Working','the lost response restores the Working row');
 await finishRowMotion();
 check(submit('remove').id===remove.id,'the exact Remove retry uses the same ID');
 show('blocked');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 const hiddenStatus=document.querySelector('[data-hidden-action-status]');
 check(row.hidden && hiddenStatus.getClientRects().length>0 &&
  hiddenStatus.textContent==='Item 1: Not saved: Fixture refuses Remove',
  'the refusal is visible when the Blocked filter hides the restored row');
 show('working');
 check(!row.hidden && row.dataset.state==='Working','the refusal restores the confirmed row to Working');
 const noticeBox=hiddenStatus.getBoundingClientRect();
 check(!recordIsOpen(row) && hiddenStatus.textContent==='Item 1: Not saved: Fixture refuses Remove' &&
  noticeBox.height>0 && noticeBox.top>=0 && noticeBox.bottom<=innerHeight,
  'the Blocked filter closed the row, so the page notice shows the refusal in view');
 row.querySelector('.list-toggle').click();
 const notice=form.querySelector('[data-send-status]');
 check(notice.getClientRects().length>0 && notice.textContent==='Not saved: Fixture refuses Remove',
  'the refusal is visible on the reopened row');
 check(hiddenStatus.hidden,'the page hides the extra notice when the reopened row shows the refusal');
''')

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_clears_the_remove_draft_after_a_lost_reply_without_a_notice(self) -> None:
        root = self.root / "reviews"
        item = self.add("Remove the duplicate", number=1)
        words = "Close the duplicate."
        first = self.fields(item, "remove", words=words)
        second = self.fields(item, "remove", words=words)
        with patch.object(list_page, "signed_item_request", return_value=(second["request"], second["token"])):
            initial = list_page.render(root, include_counts=False).decode()
        _ = list_page.submit(root, urlencode(first))
        delivery = list_page.submit(root, urlencode(second))
        self.assertIsInstance(delivery, change_page.NoMessageDelivery)
        closed = the_list.load(MORTY.id, item.id)
        self.assertNotIn(second["request"], the_list.feedback_requests(closed))
        state = list_page.saved_action_state(MORTY.id, item.id, park_request=second["request"], include_detail=True)
        _ = the_list.reopen(MORTY.id, item.id, source="morty")
        reopened = list_page.render(root, include_counts=False).decode()
        self.browser_action_sequence('''
 show('all');
 form.elements.words.value='Close the duplicate.';
 form.elements.words.dispatchEvent(new Event('input',{bubbles:true}));
 readback=(key,request)=>{
  check(key===row.dataset.key && request===EXPECTED_REQUEST,'Readback asks for the second Remove receipt.');
  return SAVED_STATE;
 };
 submit('remove');losePost();await flush();
 refreshMarkup=REOPENED_PAGE;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 const reopened=document.querySelector('[data-number="1"]');
 check(!reopened.hidden && reopened.dataset.state==='New','Refresh shows the reopened item.');
 reopened.querySelector('.list-toggle').click();
 check(recordDetails(reopened).querySelector('form').elements.words.value==='','Readback clears the saved draft.');
'''.replace('EXPECTED_REQUEST', json.dumps(second["request"]))
   .replace('SAVED_STATE', json.dumps(state).replace('</', '<\\/'))
   .replace('REOPENED_PAGE', json.dumps(reopened).replace('</', '<\\/')),
                                     reduced_motion=True, add_item=False, markup=initial)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_reads_the_saved_item_after_a_lost_park_or_remove_response(self) -> None:
        self.browser_action_sequence('''
 show('working');
 let finishReadback,readKey;
 readback=key=>{readKey=key;return new Promise(resolve=>{finishReadback=resolve;});};
 form.elements.until.value='2026-12-03';
 const park=submit('park');
 await loseParkPost();
 check(requests[1]?.get('request')===park.id && readKey===undefined,'Park retries before reading the item.');
 losePost();await flush();
 check(row.hidden && readKey===row.dataset.key,'Park reads the current item after both responses are lost.');
 finishReadback({item_state:'Working',unread:'true',parked:'true',parked_until:'Dec 3 00:00 EST',park_request_saved:true});
 await flush();
 check(row.hidden,'The saved Park stays out of Working.');
 show('parked');
 check(!row.hidden && row.querySelector('[data-parked-note]').textContent==='Parked until Dec 3 00:00 EST',
  'Parked shows the saved parking time.');
 check(requests.length===2 && form.querySelector('[data-send-status]').hidden,
  'A saved receipt confirms Park without another request or failure line.');
 row.querySelector('.list-toggle').click();
 submit('remove');losePost();await flush();
 finishReadback({item_state:'Closed',unread:'false',parked:'true',parked_until:'Dec 3 00:00 EST'});
 await flush();
 show('all');
 check(row.hidden && row.dataset.state==='Closed','The saved Remove stays closed after readback.');
''', reduced_motion=True)
        for action in ("park", "park-until-delivered"):
            with self.subTest(action=action, failed_readback=True):
                self.browser_action_sequence('''
 show('working');
 refreshMarkup=listSnapshot();
 const fetchList=window.fetch;
 let finishRefresh;
 window.fetch=async(url,options)=>{
  if(options.method==='POST' || new URL(url,location.href).searchParams.has('item-state'))
   return fetchList(url,options);
  await new Promise(resolve=>{finishRefresh=resolve;});
  return fetchList(url,options);
 };
 form.elements.until.value='2026-12-03';
 let readRequest;
 readback=(key,request)=>{
  readRequest=request;
  throw new Error('The item read failed.');
 };
 const park=submit(PARK_ACTION);
 check(row.hidden,'The pending park hides the row from Working.');
 await loseParkPost();
 losePost();await flush();
 check(readRequest===park.id,'The item read uses the original request ID.');
 const refreshed=document.querySelector('[data-number="1"]');
 check(refreshed===row && !row.hidden && row.dataset.parked==='false' && recordIsOpen(row),
  'The failed readback restores the unparked row without replacing the table.');
 const list=document.querySelector('[data-list]');
 finishRefresh();await flush();
 const latest=document.querySelector('[data-number="1"]');
 check(document.querySelector('[data-list]')!==list && latest===row && !latest.hidden &&
  latest.dataset.parked==='false' && recordIsOpen(latest),
  'The follow-up refresh keeps the open row node, shows it unparked and keeps it open.');
'''.replace('PARK_ACTION', json.dumps(action)), reduced_motion=True, add_item=False)
        item = the_list.load(MORTY.id, "L1")
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=1), source="morty")
        _ = the_list.mark_read(MORTY.id, item.id, source="nate")
        for action in ("park", "park-until-delivered"):
            for saved in (False, True):
                with self.subTest(action=action, park_request_saved=saved):
                    self.browser_action_sequence('''
 location.hash=new URLSearchParams({show:'parked',unread:'1',item:row.dataset.key}).toString();
 await flush();
 const linked=document.querySelector('[data-linked-item]');
 const isLinked=()=>!linked.hidden && linked.contains(row) && recordIsOpen(row);
 check(isLinked(),'The read item starts linked under Parked with Unread only on.');
 const saved=PARK_RECEIPT;
 const action=PARK_ACTION;
 const previous='Nov 17 13:00 EST';
 const until=saved && action==='park-until-delivered' ? 'delivered' : previous;
 let readReceipt;
 readback=(key,request)=>{
  check(key===row.dataset.key && request===park.id,'The read uses the exact Park request ID.');
  return new Promise(resolve=>{readReceipt=()=>resolve({item_state:'Working',unread:'false',parked:'true',
   parked_until:until,park_request_saved:saved});});
 };
 form.elements.until.value='2026-12-03';
 const park=submit(action);
 check(!isLinked(),'Park releases the link at submission.');
 await loseParkPost();
 check(requests[1]?.toString()===requests[0].toString(),'The retry sends the exact Park request.');
 const notice=form.querySelector('[data-send-status]');
 const above=document.querySelector('[data-hidden-action-status]');
 finishPost({status:403,reason:'The fixture refuses the signed request.'});await flush();
 check(readReceipt && notice.hidden && above.hidden,'A refused retry waits for the receipt before showing a failure.');
 readReceipt?.();await flush();
 check(isLinked()===!saved,'Only a missing receipt restores the link.');
 const visible=[recordDetails(row).querySelector('[data-send-status]'),above]
  .filter(status=>status?.getClientRects().length);
 check(saved ? visible.length===0 :
  visible.length===1 && visible[0].textContent.endsWith('Not saved: The fixture refuses the signed request.'),
  'A refused retry shows one failure line only when the receipt is missing.');
 if(!saved){
  check(row.querySelector('[data-parked-note]').textContent==='Parked until '+previous,
   'The restored row shows the previous saved parking time.');
 }
'''.replace('PARK_RECEIPT', json.dumps(saved)).replace('PARK_ACTION', json.dumps(action)),
                                                 reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_unconfirmed_park_or_remove_refreshes_the_open_body(self) -> None:
        steps = r"""
 const text='A newer waiting question from morty.';
 const detail=recordDetails(row),original=detail.querySelector('[data-feedback-form]');
 const markup=detail.innerHTML.replace('</form>','</form><p>'+text+'</p>');
 readback=()=>({item_state:'Working',unread:'true',parked:'false',parked_until:'',
  waiting_for_answer:'true',waiting_details_html:markup,detail_html:markup,
  detail_revision:'new-waiting-revision',park_request_saved:false});
 form.elements.words.value='Keep these unsent words.';
 form.elements.words.dispatchEvent(new Event('input'));
 form.elements.until.value='2026-12-03';
 submit(ACTION);
 if(ACTION==='park') await loseParkPost();
 losePost();await flush();
 check(recordIsOpen(row) && row.dataset.waitingForAnswer==='true' &&
  detail.querySelector('[data-feedback-form]')!==original && detail.textContent.includes(text) &&
  row.dataset.detailRevision==='new-waiting-revision' &&
  detail.querySelector('[name="words"]').value==='Keep these unsent words.',
  'Recovery installs the current body and keeps the open row and draft.');
"""
        for action in ("park", "remove"):
            with self.subTest(action=action):
                self.browser_action_sequence(steps.replace("ACTION", json.dumps(action)), reduced_motion=True,
                                             add_item=action == "park")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_updates_the_unread_sort_before_refresh(self) -> None:
        _ = self.add("Test List action retries", number=1)
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
 finishPost({reason:'Fixture refuses Remove'});await flush();
 check(updateKey()===original && first()==='1','a refusal restores the exact Update sort key');
 submit('remove');
 finishPost({item_state:'Closed',parked:'false',unread:'false'});await flush();
 check(updateKey()===0 && first()==='2','a confirmed Remove sorts as read without a refresh');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_refusal_restores_update_order_during_motion(self) -> None:
        _ = self.add("Test List action retries", number=1)
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
 finishPost({reason:'Fixture refuses Remove'});await flush();
 const restore=animation();
 check(!row.hidden && restore && restore!==exit,'the refusal reverses the exit animation');
 check(updateKey()===original && first()==='1','the restored Update key puts the row first');
 restore?.finish();await flush();
 check(!animation() && first()==='1','the correct order remains after the animation');
""", add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_closed_response_clears_unread_when_the_server_reports_read(self) -> None:
        self.browser_action_sequence(r"""
 show('all');
 const updateKey=()=>JSON.parse(row.dataset.sortValues)[7];
 check(updateKey()>0 && row.querySelector('.list-dot'),'the item starts unread');
 submit('remove');
 finishPost({item_state:'Closed',unread:'false'});await flush();
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
 const navigate=async params=>{
  const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams(params);
  await changed;
 };
 check(!chip('closed'),'Show has no Closed chip.');
 check(chip('producer-answering').dataset.filterCount==='0','The closed question does not count.');
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
 losePost();await flush();await finishRowMotion();
 check(!row.hidden && row.dataset.unread==='true','a lost Remove restores the unread update.');
 form.elements.until.value='2026-12-03';
 submit('park');
 finishPost({reason:'Fixture refuses Park'});await flush();
 show('all');onlyUnread();
 check(!row.hidden && row.dataset.state==='Working','the refusal returns the Working row to Unread');
 check(row.querySelector('.list-dot') && form.querySelector('[value="read"]'),
  'the refusal restores the unread controls');
 check(JSON.parse(row.dataset.sortValues)[7]===original,'the refusal restores the Update sort key');
""")

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_remove_collapses_and_refusal_restores_the_row(self) -> None:
        _ = self.add("Test List action retries", number=1)
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
 check(leaving?.querySelector('[data-detail]:not([hidden])'),'The expanded row fades with its detail.');
 const exit=leaving?.getAnimations({subtree:true})[0];
 check(exit,'Remove starts the shared animation');
 if(!exit) throw new Error('No exit animation');
 halfway(exit);
 const opacity=Number(getComputedStyle(exit.effect.target).opacity);
 check(opacity>0 && opacity<1,'the row fades before it disappears');
 check(top(next)<start && top(next)>first,'the next row moves up during the fade');
 exit.finish();await flush();
 check(!motion() && Math.abs(top(next)-first)<2,'the next row ends in the removed row position');
 finishPost({reason:'Fixture refuses Remove'});await flush();
 const restored=motion()?.getAnimations({subtree:true})[0];
 check(!row.hidden && restored,'the refusal restores the row with an animation');
 if(!restored) throw new Error('No restore animation');
 halfway(restored);
 check(top(next)<start && top(next)>first,'the next row moves down during restore');
 form.requestSubmit(form.querySelector('[value="remove"]'));
 check(!row.hidden && requests.length===1,'A second Remove during the fade sends no POST.');
 restored.finish();await flush();
 submit('remove');
 await finishRowMotion();
 check(!motion() && Math.abs(top(next)-first)<2,'Remove works after the fade ends.');
 finishPost({item_state:'Closed',unread:'false'});await flush();
 show('all');
 check(row.hidden && !motion(),'a filter change keeps the Closed item hidden');
""", add_item=False)

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
 finishPost({reason:'Fixture refuses Remove'});await flush();
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
 losePost();await flush();
 const restored=motion();
 check(!row.hidden && restored && restored!==firstExit,'a lost Mark read response slides the row back');
 check(form.querySelector('[data-send-status]').textContent==='Save not confirmed: Fixture lost response',
  'the restored item shows the failure reason');
 await finishRowMotion();
 check(!form.querySelector('[value="read"]').disabled,'the restored item can retry Mark read');
 check(submit('read').id===read.id,'the exact Mark read retry uses the same ID');
 finishPost({unread:'true'});await flush();
 check(!row.hidden && row.dataset.unread==='true','a newer unread update returns the row to Unread');
 await finishRowMotion();
 form.elements.words.value='Send the update.';
 form.elements.words.dispatchEvent(new Event('input'));
 submit('tell');
 check(row.hidden && motion(),'Tell morty starts the fade before the response in Unread');
 finishPost({unread:'false'});await flush();
 check(row.hidden,'Tell morty keeps the row outside Unread after confirmation');
""", add_item=False)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_undo_restores_park_mark_read_and_remove(self) -> None:
        item = self.add("Restore the original item", number=1)
        _ = self.add("Keep the next item in place", number=2)
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "An update is ready."}, source="morty")
        _ = write_config_fixture(self.root / "config/review_links.conf", "undo_seconds=30\n")
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        probe = '<script>' + UNDO_ACTION_SCRIPT + r"""
(async()=>{
  const {realFetch,posts,until,undo}=fixtureUndo();
 const failures=[],check=(ok,label)=>{if(!ok) failures.push(label);};
 try{
  frameElement.style.width='1280px';frameElement.style.height='820px';
  const row=document.querySelector('[data-list-item][data-number="1"]');
  row.querySelector('.list-toggle').click();
  const form=recordDetails(row).querySelector('form');
  const order=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')].map(item=>item.dataset.key).join(',');
  const state=()=>JSON.stringify([row.dataset.state,row.dataset.parked,row.dataset.unread,JSON.parse(row.dataset.sortValues)]);
  const before=order(),original=state();
  form.elements.words.value='Keep this draft.';
  for(const action of ['park','park-until-delivered','read','remove']){
   if(action.startsWith('park')){
    form.querySelector('[data-later-toggle]').click();
    form.elements.until.value='2026-12-03';
   }
   form.querySelector('[value="'+action+'"]').click();
   check(row.hidden,action+' hides the row at once.');
   if(action==='remove'){
    const toast=()=>window.top.document.querySelector('[data-send-undo]');
    await until(()=>toast()?.querySelector('button'),'Remove did not show the Undo toast.');
    check(!document.querySelector('dialog[open]'),'Remove opens no confirmation window.');
    check(toast().getAnimations().some(animation=>animation.effect.getKeyframes().at(-1).opacity==='0'),
     'The Remove toast fades.');
   }
   await undo();
   await until(()=>!row.hidden && recordIsOpen(row) &&
    form.querySelector('[value="read"]:enabled'),action+' did not restore the open row.');
   check(order()===before,action+' restores the row order.');
   check(state()===original,action+' restores the row state.');
   check(form.elements.words.value==='Keep this draft.',action+' keeps the draft.');
   await until(()=>!document.querySelector('[data-record-motion]'),'The return animation did not finish.');
  }
  check(posts.map(post=>post.get('action')).join(',')==='park,park,read,remove','Each action creates one hold.');
  check(posts[1]?.get('until')==='delivered','Until delivered keeps its parking value.');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        self.assertEqual(self.firefox_probe(markup.replace('</body>', probe+'</body>').encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_undo_unpark_restores_the_parked_row(self) -> None:
        item = self.add("Keep the parking time", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        _ = write_config_fixture(self.root / "config/review_links.conf", "undo_seconds=30\n")
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        probe = '<script>' + UNDO_ACTION_SCRIPT + r"""
(async()=>{
 const {realFetch,posts,until,undo}=fixtureUndo(),failures=[];
 try{
  document.querySelector('#filter-show-parked').click();
  const row=document.querySelector('[data-list-item][data-number="1"]'),parking=row.dataset.parked;
  row.querySelector('.list-toggle').click();
  const button=recordDetails(row).querySelector('[value=unpark]');
  button.click();
  if(!row.hidden) failures.push('Unpark hides the row at once.');
  await undo();
  await until(()=>!row.hidden && recordIsOpen(row) && !button.disabled,'Undo did not restore the parked row.');
  if(!parking || row.dataset.parked!==parking || button.hidden) failures.push('Undo keeps the original parking time.');
  if(posts.map(post=>post.get('action')).join()!=='unpark') failures.push('Unpark creates one hold.');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        self.assertEqual(self.firefox_probe(markup.replace('</body>', probe+'</body>').encode()), [[]])

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_undo_unpark_restores_the_linked_row(self) -> None:
        item = self.add("Keep the parking time", number=1)
        _ = the_list.park(MORTY.id, item.id, NOW + timedelta(days=2), source="nate")
        _ = write_config_fixture(self.root / "config/review_links.conf", "undo_seconds=30\n")
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        probe = '<script>' + UNDO_ACTION_SCRIPT + r"""
(async()=>{
 const {realFetch,posts,until,undo}=fixtureUndo(),failures=[];
 try{
  const row=document.querySelector('[data-list-item][data-number="1"]'),parking=row.dataset.parked;
  const key=row.dataset.key,linked=document.querySelector('[data-linked-item]');
  location.hash=new URLSearchParams({show:'working',item:key,reveal:'1'});
  await until(()=>!linked.hidden && linked.contains(row) && recordIsOpen(row),'The link did not open the parked row.');
  const button=recordDetails(row).querySelector('[value=unpark]');
  button.click();
  if(!row.hidden || history.state.recordTable.linkedItem) failures.push('Held Unpark hides the row and releases the link.');
  await until(()=>linked.hidden,'The linked section did not hide.');
  await undo();
  await until(()=>!linked.hidden && linked.contains(row) && !row.hidden && recordIsOpen(row),
   'Undo did not restore the linked row.');
  if(history.state.recordTable.linkedItem!==key || row.dataset.parked!==parking)
   failures.push('Undo keeps the link and the original parking time.');
  if(posts.length!==1 || posts[0].get('action')!=='unpark') failures.push('Unpark creates one hold.');
 }catch(error){failures.push(String(error));}
 await realFetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        self.assertEqual(self.firefox_probe(markup.replace('</body>', probe+'</body>').encode()), [[]])

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_explicit_actions_release_open_rows_from_unread_only(self) -> None:
        _ = self.add("Check the current result", number=1)
        _ = self.add("Keep the next result in view", number=2)
        for actions in (("park", "read"), ("read", "park"), ("read",)):
            with self.subTest(actions=actions):
                self.browser_action_sequence("const actions=" + json.dumps(actions) + r""";
 const linked=document.querySelector('[data-linked-item]');
 const follow=async()=>{
  const changed=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
  location.hash=new URLSearchParams({show:'all',unread:'1',item:row.dataset.key});
  await changed;
 };
 onlyUnread();
 await follow();
 for(const action of actions){
  if(action==='read' && row.dataset.parked==='true') show('parked');
  if(action==='read' && !recordIsOpen(row)) row.querySelector('.list-toggle').click();
  if(action==='park' && row.dataset.unread==='false'){
   await follow();
   check(!linked.hidden && linked.contains(row),'The read item opens through its link outside Unread only.');
  }
  check(!row.hidden && recordIsOpen(row),'The action starts with open details.');
  const section=row.closest('[data-linked-item]');
  const staysVisible=action==='park' && row.dataset.unread==='true';
  form.elements.until.value='2026-12-03';
  submit(action);
  if(staysVisible) check(!row.hidden,'All keeps the unread row visible during Park.');
  else check(row.hidden && (section || document).querySelector('[data-record-motion]'),
   action+' starts the fade in the same table before the response.');
  if(section) check(!section.hidden,'The linked section stays visible during its fade.');
  const parked=action==='park' || row.dataset.parked==='true';
  finishPost({unread:action==='read' ? 'false' : 'true',parked:String(parked),
   parked_until:parked ? 'Dec 3 00:00 EST' : ''});
  await flush();
  for(const animation of document.getAnimations()) animation.finish();
  await flush();
  check(row.hidden===(action==='read') && linked.hidden,
   action+' uses the saved unread state to show or hide the row in the main table.');
  check(!history.state.recordTable.linkedItem,'The action releases the link.');
 }
 if(actions.includes('park')){
  onlyUnread(false);
  show('all');
  check(!row.hidden && !linked.contains(row) && linked.hidden,'All shows the parked row in the main table.');
  for(const view of ['needs-me','producer-answering','working','blocked']){
   show(view);
   check(row.hidden,'The parked row is absent from '+view+'.');
  }
  show('parked');
  const note=!recordIsOpen(row) ?
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
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
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
 const close=document.querySelector('[data-list-item] .list-toggle[aria-expanded=true]');
 if(destination==='user selection'){close.click();all.focus();}
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
 const expected=destination==='same row' ? close :
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
 recordDetails(row).querySelector('a[data-record-jump]').click();
 const place=new URLSearchParams(location.hash.slice(1));
 check(!target.hidden && target.closest('[data-linked-item]:not([hidden])') &&
  recordIsOpen(target),'The related link opens the waiting item outside the results.');
 check(place.get('show')==='working' && place.get('owner')===row.dataset.owner && search.value==='check',
  'The related link keeps Show, Owner and the search');
 staffChip('producer',row.dataset.producer).click();
 check(!target.hidden && recordIsOpen(target) &&
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
 location.hash=new URLSearchParams({show:'all',unread:'1',item:target.dataset.key}).toString();
 await flush();
 check(unread.checked && results.querySelectorAll('[data-list-item]:not([hidden])').length===1 &&
  count.textContent.startsWith('1 of 2'),'Unread only keeps one row in the results.');
 check(!section.hidden && section.contains(target) && !target.hidden && recordIsOpen(target),
  'The direct link opens the read item outside the results.');
 onlyUnread(false);
 check(section.hidden && results.contains(target) && !target.hidden && count.textContent.startsWith('2 of 2'),
  'The read item rejoins the results when Unread only is off.');
""", add_item=False, reduced_motion=True)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
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
  const expanded=item=>recordIsOpen(item);
  const linked=item=>!section().hidden && section().contains(item) && !item.hidden && expanded(item);
  const framePlace=()=>location.pathname+location.search+location.hash;
  const consolePlace=()=>new URL(parent.location.href).searchParams.get('at.studio');
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
  recordDetails(source).querySelector('a[data-record-jump]').click();
  const waiting=row();
  check(linked(waiting) && chip('producer-answering').dataset.filterCount==='1',
   'The link opens the waiting item outside Working and counts it in Producer answering.');
  const list=document.querySelector('[data-list]');
  const refreshed=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{
    if(list.isConnected) return;
    observer.disconnect();resolve();
   });
   observer.observe(list.parentElement,{childList:true});
  });
  document.dispatchEvent(new Event('visibilitychange'));
  await refreshed;
  const answered=row();
  check(answered===waiting,'The refresh keeps the open linked row node.');
  check(linked(answered) &&
   recordDetails(answered).querySelector('[data-question="Q1"]')?.textContent.includes('The gate passed.'),
   'The refresh displays the answer and keeps the linked item open.');
  check(chip('working').getAttribute('aria-pressed')==='true' &&
   chip('needs-me').dataset.filterCount==='1' && chip('producer-answering').dataset.filterCount==='0' &&
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
    def test_list_save_keeps_later_edits_and_ignores_a_response_after_reopen(self) -> None:
        self.browser_action_sequence(r'''
 const view=document.querySelector('[data-record-inline-view]');
 const close=document.querySelector('[data-list-item] .list-toggle[aria-expanded=true]');
 const open=()=>row.querySelector('.list-toggle').click();
 const details=()=>recordDetails(row);
 const edit=(field,value)=>{field.value=value;field.dispatchEvent(new Event('input'));};
 const words=form.elements.words;
 edit(words,'Check the logs.');submit('tell');
 edit(words,'Check the logs again.');edit(words,'Check the logs.');
 words.focus();words.setSelectionRange(2,5);
 finishPost({item_state:'Working',unread:'false'});await flush();
 check(words.value==='Check the logs.' && document.activeElement===words &&
  words.selectionStart===2 && words.selectionEnd===5,
  'A saved Tell keeps words edited during the save and keeps their selection.');
 submit('tell');close.click();open();
 const reopened=details().querySelector('[data-feedback-form]');
 edit(reopened.elements.words,'Keep these later words.');
 finishPost({item_state:'Closed',unread:'false'});await flush();
 check(document.querySelector('[data-record-inline-view]') && details().querySelector('[data-feedback-form]')===reopened &&
  reopened.elements.words.value==='Keep these later words.',
  'A response from before Close leaves the reopened item and its draft in place.');
 submit('tell',reopened);
 edit(reopened.elements.words,'Keep the words after Tell.');
 failPostBody();await flush();
 const error=view.querySelector('[data-record-error]');
 check(!error.hidden && error.textContent==='Saved; the item could not refresh.' &&
  reopened.elements.words.value==='Keep the words after Tell.',
  'An unreadable successful response reports a display error and keeps the draft.');
''', reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_undo_keeps_the_words_and_leaves_the_row_unsent(self) -> None:
        self.browser_action_sequence(r'''
 const until=(test,label)=>new Promise((resolve,reject)=>{
  const ready=()=>{
   if(!test()) return;
   observer.disconnect();clearTimeout(timer);resolve();
  };
  const observer=new MutationObserver(ready);
  observer.observe(document,{subtree:true,childList:true,characterData:true});
  const timer=setTimeout(()=>{
   observer.disconnect();reject(new Error(label));
  },HANG_GUARD_MS);
  ready();
 });
 const before={state:row.dataset.state,unread:row.dataset.unread};
 const listFetch=window.fetch;let undos=0;
 window.fetch=async(path,options={})=>{
  if(String(path).endsWith('/undo')){undos++;return Response.json({outcome:'undone'});}
  if(options.method==='POST' && !requests.length){
   requests.push(new URLSearchParams(options.body));
   return Response.json({hold:'a'.repeat(24),seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
  }
  return listFetch(path,options);
 };
 const words=form.elements.words,notice=form.querySelector('[data-send-status]');
 words.value='Tell morty after Undo.';words.dispatchEvent(new Event('input'));
 submit('tell');
 await until(()=>document.querySelector('[data-send-undo] button'),'The Undo toast did not show.');
 check(document.querySelector('[data-send-undo] span').textContent==='Sent to morty.',
  'The toast names the record keeper.');
 check(!row.getClientRects().length,'The List item leaves when the Undo toast shows.');
 document.querySelector('[data-send-undo] button').click();
 await until(()=>notice.textContent==='Undone. Edit and send again.','The row did not say that the send was undone.');
 check(words.value==='Tell morty after Undo.','After Undo the Tell keeps its words.');
 await until(()=>row.getClientRects().length && document.activeElement===words,'Undo did not show the row and focus the draft.');
 check(row.dataset.state===before.state && row.dataset.unread===before.unread,
  'After Undo the row keeps its state and its unread mark, so it is not marked sent.');
 check(undos===1 && requests.length===1,'Undo posts once and nothing else is sent.');
'''.replace('HANG_GUARD_MS', str(TEST_HANG_GUARD_SECS * 1000)))

    def file_repaired_report(self, title: str, instructions: str = "Open the menu.") -> the_list.ListItem:
        native = investigation.file(OWNER.id, title, "The menu hides choices.", waiting_why="sdlc checks the menu.")
        report = the_list.ListReport(the_list.ReportKind.BUG, review_items.investigation_record_key(native), native.question)
        destination = the_list.file_report(MORTY.id, report, native.title, owner=OWNER.id, producer=DWIGHT.id,
                                           unit="", source="nate")
        identity = f"L{destination.number}"
        _ = the_list.update(MORTY.id, identity, {"state": the_list.ItemState.WORKING}, source="dwight")
        return the_list.update(MORTY.id, identity, {"state": the_list.ItemState.REPAIRED, "next_step": instructions},
                               source="dwight")

    def test_repair_and_not_fixed_keep_links_in_the_open_item(self) -> None:
        repair_url = "https://example.test/repair"
        feedback_url = "https://example.test/feedback"
        item = self.file_repaired_report("Fix the menu", f"Try {repair_url}.")
        root = self.root / "reviews"
        body = list_page.render(root, include_counts=False).decode()
        row = body.split('<tr class="list-row">', 1)[1].split("</tr>", 1)[0]
        self.assertIn(f"Try {repair_url}.", row)
        self.assertNotIn("<a ", row)
        self.assertIn('<h3>How to try</h3><p>Try <a class="item-url"', body)
        self.assertIn(f'href="{repair_url}" target="_blank" rel="noopener"', body)

        fields = self.fields(item, "not works", words=f"See {feedback_url}.")
        _ = list_page.submit(root, urlencode(fields))
        saved = list_page.saved_action_state(MORTY.id, item.id, include_detail=True)
        self.assertEqual(saved.get("next_step_sort"), "dwight repairs it again")
        self.assertIn('class="staff-name', saved.get("next_step_html", ""))
        panel = saved.get("detail_html", "")
        self.assertIn(f'<p>Not fixed: See <a class="item-url" href="{feedback_url}" '
                      + 'target="_blank" rel="noopener">', panel)
        links = panel.split('<section class="item-links">', 1)[1].split("</section>", 1)[0]
        rows = ElementTree.fromstring(f"<section>{links}</section>").findall("ul/li")
        self.assertEqual([(row[0].get("href"), row[1].text) for row in rows],
                         [(repair_url, "Next step"), (feedback_url, "Feedback")])

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_repair_undo_restores_the_draft_and_works_leaves_needs_me(self) -> None:
        _ = self.file_repaired_report("Fix the menu", "Open the menu and count all six choices.")
        steps = r"""
 document.querySelector('[data-undo-enabled]').dataset.undoEnabled='true';
 const originalFetch=window.fetch;let undos=0;
 const undoOutcome=UNDO_OUTCOME;
 let undoPath='';
 const delivered={item_state:'Delivered',unread:'false',state_time_html:'<time>13:00</time>'};
 window.fetch=async(path,options={})=>{
  if(String(path).endsWith('/undo')){
   undos++;undoPath=String(path);
   if(undoOutcome===null) return new Response('Invalid JSON');
   return Response.json({outcome:undoOutcome});
  }
  if(String(path).startsWith('/send-hold/')){
   if(undoOutcome==='undone') return Response.json(delivered);
   return Response.json({reason:'The hold result is unavailable.'},
    {status:404,headers:{'X-Send-Hold-Outcome':'missing'}});
  }
  if(options.method==='POST'){
   const fields=new URLSearchParams(options.body);
   check(new Headers(options.headers).get('X-Undo-Hold')==='1',
    fields.get('action')+' uses the shared Undo flow.');
   requests.push(fields);
   return Response.json({hold:new Headers(options.headers).get('X-Undo-Request'),seconds:30},
    {status:202,headers:{'X-Send-Hold':'held'}});
  }
  return originalFetch(path,options);
 };
 const until=async(test,label)=>{
  const end=performance.now()+5000;
  while(!test()){
   if(performance.now()>end) throw new Error(label);
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 const words=form.elements.words;
 const text='The last choice is missing.';
 const reject=form.querySelector('[value="not works"]');
 const toast=()=>document.querySelector('[data-send-undo]:has(button)');
 show('needs-me');
 check(row.getClientRects().length && recordIsOpen(row),'Needs me contains the open Repaired item.');
 check(recordDetails(row).textContent.includes('Open the menu and count all six choices.'),'The panel shows how to try it.');
 check(reject.disabled,'Not fixed needs words.');
 for(const blank of ['\u0085','\u001c\t\u2028','\u00a0\n\u3000']){
  words.value=blank;words.dispatchEvent(new Event('input'));
  check(reject.disabled,'Not fixed rejects words with only server whitespace.');
  form.requestSubmit(reject);
  check(!requests.length,'A blank draft sends no request.');
 }
 words.value=text;words.dispatchEvent(new Event('input'));
 const store=Storage.prototype.setItem;
 Storage.prototype.setItem=function(){throw new Error('Storage unavailable');};
 reject.click();
 Storage.prototype.setItem=store;
 check(!requests.length && form.querySelector('[data-send-status]').textContent.includes('cannot store Not fixed'),
  'A storage failure shows before any request leaves.');
 submit('not works');
 await until(toast,'The Undo toast did not show.');
 check(toast().textContent.includes('Sent to sdlc and dwight.'),'The toast names the owner and producer.');
 check(!row.getClientRects().length,'The held repair report is hidden.');
 toast().querySelector('button').click();
 if(undoOutcome!=='undone'){
  await until(()=>!form.dataset.sending && row.getClientRects().length,'An uncertain Undo did not restore the row.');
  const key=Object.keys(sessionStorage).find(name=>name.includes('/list-not-fixed-v1/'));
  const saved=JSON.parse(sessionStorage.getItem(key));
  check(undoPath==='/send-hold/'+saved?.hold+'/undo','Undo uses the original hold.');
  check(saved?.payload.request===requests[0].get('request'),'An uncertain Undo keeps the saved request.');
  check(words.value===text && !reject.disabled,'An uncertain Undo keeps the words and permits retry.');
  check(!form.querySelector('[data-send-status]').textContent.startsWith('Undone.'),'The card reports uncertainty.');
 }else{
  await until(()=>row.getClientRects().length && words===document.activeElement,'Undo did not restore the draft.');
  check(row.dataset.state==='Repaired' && words.value===text && undos===1,'Undo keeps the Repaired state and words.');
  check(!Object.keys(sessionStorage).some(key=>key.includes('/list-not-fixed-v1/')),
   'Undo before acceptance removes the request from storage.');
  submit('works');
  await until(toast,'Works did not show the Undo toast.');
  check(toast().textContent.includes('Saved.') && row.dataset.state==='Repaired',
   'Works offers Undo before it changes the state.');
  toast().querySelector('button').click();
  await until(()=>row.getClientRects().length && words===document.activeElement,'Undo did not restore the repair.');
  check(row.dataset.state==='Repaired' && undos===2,'Undo keeps the repair in Needs me.');
  submit('works');
  await until(toast,'Works did not offer Undo again.');
  toast().getAnimations()[0].finish();
  await until(()=>row.dataset.state==='Delivered' && row.hidden,'Works did not leave Needs me.');
  show('all');
  check(row.getClientRects().length && row.dataset.state==='Delivered','All shows the Delivered item.');
  check(row.querySelector('[data-list-state-time]').textContent==='13:00','The row shows the Delivered time.');
 }
"""
        for outcome in ("undone", "missing", None):
            with self.subTest(outcome=outcome):
                self.browser_action_sequence(steps.replace("UNDO_OUTCOME", json.dumps(outcome)),
                                             add_item=False, reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_not_fixed_undo_times_out_while_reading_the_body(self) -> None:
        _ = self.file_repaired_report("Fix the menu")
        self.browser_action_sequence(r"""
 const deadlines=new Map(),originalFetch=window.fetch;
 AbortSignal.timeout=milliseconds=>{
  const controller=new AbortController();
  deadlines.set(controller.signal,{controller,milliseconds});
  return controller.signal;
 };
 const words=form.elements.words,button=form.querySelector('[value="not works"]');
 const text='The last menu choice still disappears.';
 let undoSignal,resultSignal,bodyReads=0;
 const until=async(condition,message)=>{
  const deadline=performance.now()+4000;
  while(!condition()){
   if(performance.now()>deadline) throw new Error(message);
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 window.fetch=async(path,options={})=>{
  if(String(path).endsWith('/undo')){
   undoSignal=options.signal;
   const response=new Response(new ReadableStream({start(controller){
    controller.enqueue(new TextEncoder().encode('{"outcome":'));
    undoSignal?.addEventListener('abort',()=>controller.error(undoSignal.reason),{once:true});
   }}));
   const read=response.json.bind(response);
   response.json=()=>{bodyReads++;return read();};
   return response;
  }
  if(String(path).startsWith('/send-hold/')){
   resultSignal=options.signal;
   return Response.json({reason:'The hold result is unavailable.'},
    {status:404,headers:{'X-Send-Hold-Outcome':'missing'}});
  }
  if(new URL(path,location.href).searchParams.has('not-fixed-request')){
   const request=requests[0].get('request');
   return Response.json({not_fixed_receipt:{request,state:'absent'}});
  }
  if(options.method!=='POST' || requests.length) return originalFetch(path,options);
  requests.push(new URLSearchParams(options.body));
  const hold=new Headers(options.headers).get('X-Undo-Request');
  return Response.json({hold,seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
 };
 words.value=text;words.dispatchEvent(new Event('input'));
 const initial=submit('not works');
 const undo=()=>document.querySelector('[data-send-undo] button');
 await until(undo,'The Undo toast did not show.');
 undo().click();
 await until(()=>bodyReads===1,'The browser did not read the Undo body.');
 const deadline=deadlines.get(undoSignal);
 if(!deadline) throw new Error('The Undo POST has no request deadline.');
 check(Boolean(form.dataset.sending),'The card waits for the incomplete Undo body.');
 deadline.controller.abort(new DOMException('The fixture timed out.','TimeoutError'));
 await until(()=>!form.dataset.sending && !button.disabled,'The Undo timeout did not release the card.');
 check(!undo() && !resultSignal,'The timeout clears the Undo toast and defers the result read until retry.');
 const key=Object.keys(sessionStorage).find(name=>name.includes('/list-not-fixed-v1/'));
 const saved=JSON.parse(sessionStorage.getItem(key));
 check(saved?.payload.request===initial.id && words.value===text,'The timeout keeps the request and the words.');
 check(form.querySelector('[data-send-status]').textContent.includes('The Undo response timed out.'),
  'The card reports the timeout.');
 button.click();
 await until(()=>requests.length===2,'Retry did not send the original request.');
 check(deadline.milliseconds===deadlines.get(resultSignal)?.milliseconds,
  'Undo and hold result reads use the same time limit.');
 check(requests[1].toString()===requests[0].toString(),'Retry uses the original signed payload.');
 finishPost({status:503,reason:'The fixture did not confirm delivery.'});
 await until(()=>!form.dataset.sending,'Retry did not finish.');
""", add_item=False, reduced_motion=True)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_not_fixed_keeps_the_draft_until_delivery_is_confirmed(self) -> None:
        repaired = self.file_repaired_report("Fix the menu")
        root = self.root / "reviews"
        before = list_page.render(root, include_counts=False).decode()
        words = "The last choice is missing."
        _ = list_page.submit(root, urlencode(self.fields(repaired, "not works", words=words)))
        saved = list_page.saved_action_state(MORTY.id, "L1", include_detail=True)
        probe = r"""
 const result=SAVED,expected=EXPECTED;
 const until=async(condition,message)=>{
  const limit=performance.now()+4000;
  while(!condition()){
   if(performance.now()>limit) throw new Error(message);
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 show('needs-me');
 const text='The last choice is missing.', words=form.elements.words;
 words.value=text;words.dispatchEvent(new Event('input'));
 const first=submit('not works');
 const receipt=state=>({not_fixed_receipt:{request:first.id,state}});
 finishPost({...result,...receipt(expected ? 'delivered' : 'accepted')});
 await until(()=>row.dataset.state==='Working' && row.hidden,'The saved item did not leave Needs me.');
 show('working');
 row.querySelector('.list-toggle').click();
 await until(()=>recordDetails(row)?.querySelector('[data-feedback-form]'),'Working did not open the saved card.');
 const detail=recordDetails(row),nextStep=detail.querySelector('.list-field-next_step');
 const nextStepCell=row.querySelector('[data-label="Next step"]');
 check(nextStep?.textContent==='Next stepdwight repairs it again','The card says who repairs it again.');
 check(nextStepCell.textContent==='dwight repairs it again','The row says who repairs it again.');
 check([nextStep,nextStepCell].every(field=>field.querySelector('.staff-name')?.textContent==='dwight'),
  'The next step uses the producer role color.');
 check(detail.querySelector('.list-field-latest_status p')?.textContent==='Not fixed: '+text,
  'The latest status shows the repair feedback.');
 const draft=detail.querySelector('textarea[name=words]').value;
 check(draft===(expected ? '' : text),'The card clears the draft only after confirmed delivery.');
 const retryForm=detail.querySelector('[data-feedback-form]'),retry=retryForm.querySelector('[value="not works"]');
 check(Boolean(retry.getClientRects().length)===!expected,'Only unconfirmed delivery shows Not fixed on the Working card.');
 if(!expected){
  retryForm.requestSubmit(retry);
  await until(()=>requests.length===2,'The test retry did not start.');
  const second=requests.at(-1);
  check(second.get('request')===first.id && second.get('token')===first.token,'The retry uses the signed request that saved the words.');
  check(second.get('words')===text,'The retry sends the original words.');
  finishPost({...CONFIRMED,...receipt('delivered')});
  await until(()=>!recordDetails(row).querySelector('textarea[name=words]')?.value,
   'The confirmed retry did not clear the words.');
  const settled=recordDetails(row).querySelector('[value="not works"]');
  check(!settled?.getClientRects().length,'The confirmed retry hides Not fixed.');
 }
"""
        for case in ({}, {"delivery_pending": True}, {"send_failure": "The owner is unavailable."}):
            with self.subTest(result=case):
                steps = probe.replace("SAVED", json.dumps({**saved, **case}).replace("</", "<\\/"))
                steps = steps.replace("EXPECTED", json.dumps(not case))
                steps = steps.replace("CONFIRMED", json.dumps(saved).replace("</", "<\\/"))
                self.browser_action_sequence(steps, add_item=False, markup=before, reduced_motion=True)

        self.browser_action_sequence(r"""
 const words=form.elements.words,button=form.querySelector('[value="not works"]');
 const edit=value=>{words.value=value;words.dispatchEvent(new Event('input'));};
 const storedKey=()=>Object.keys(sessionStorage).find(key=>key.includes('/list-not-fixed-v1/'));
 for(const [invalid,corrected] of [['The original words.','\u0085The corrected words.\u001c'],
                                  ['\ufeff','\u00a0The final words.\u2029']]){
  edit(invalid);
  const first=submit('not works');
  finishPost({reason:'The server refused these words.',not_fixed_receipt:{request:first.id,state:'refused'}});
  await flush();
  check(!storedKey() && words.value===invalid,'An exact refusal removes the request and keeps the draft.');
  edit(corrected);
  const retry=submit('not works');
  check(retry.id!==first.id,'Corrected words start a new request.');
  check(requests.at(-1).get('words')===corrected.slice(1,-1),'The browser sends words without edge whitespace.');
  finishPost({reason:'The server refused these words.',not_fixed_receipt:{request:retry.id,state:'refused'}});
  await flush();
  check(!storedKey() && !button.disabled,'A refused correction can be edited again.');
 }
""", add_item=False, markup=before, reduced_motion=True)

        for scenario in ("temporary failure", "lost hold result", "tell"):
            with self.subTest(scenario=scenario):
                item = self.file_repaired_report(f"Fix the menu in case {scenario}")
                self.check_not_fixed_retry_after_reload(root, item, words, scenario=scenario)

    def check_not_fixed_retry_after_reload(self, root: Path, item: the_list.ListItem, words: str,
                                          *, scenario: Literal["temporary failure", "lost hold result", "tell"]) -> None:
        lose_hold_result = scenario == "lost hold result"
        tell_after_failure = scenario == "tell"
        tell_words = "A separate Tell before the retry."
        _ = write_config_fixture(conf.path("review_links"), f"undo_seconds={int(lose_hold_result)}\ndelivery_tries=1\n")
        posted: list[dict[str, list[str]]] = []
        tells: list[str] = []
        accepted: set[tuple[staff.StaffId, str]] = set()
        save_action = list_page.save_action

        def save(request_root: Path, body: str, status: change_page.DeliveryStatus | None = None, *,
                 review_records: list_page.ImmediateReviewRecords, include_detail: bool,
                 top_priority: list_page.TopPriority) -> tuple[list_page.ListActionSubmission, list_page.SavedActionState | None]:
            fields = parse_qs(body, keep_blank_values=True)
            if fields["action"] == ["not works"]:
                posted.append(fields)
                if scenario == "temporary failure" and len(posted) == 2:
                    raise OSError("The List is temporarily unavailable.")
            return save_action(request_root, body, status, review_records=review_records,
                               include_detail=include_detail, top_priority=top_priority)

        def send(recipient: staff.StaffId, _sender: str, message: str, *,
                 key: str) -> Literal["pending", "skipped"] | None:
            if tell_after_failure and message.endswith(tell_words):
                self.assertEqual(recipient, DWIGHT.id)
                tells.append(message)
                return None
            self.assertTrue(message.endswith(words))
            if recipient == OWNER.id and len(posted) == 1:
                return "pending"
            if (recipient, key) in accepted:
                return "skipped"
            accepted.add((recipient, key))
            return None

        script = r"""
(async()=>{
 const frame=document.createElement('iframe'),failures=[];
 const loseHoldResult=LOSE_HOLD_RESULT,tellAfterFailure=TELL_AFTER_FAILURE,holdReads=[];
 const expectedDraft=tellAfterFailure ? '' : 'A separate edit for later.';
 const check=(ok,message)=>{if(!ok) failures.push(message);};
 const until=async(test,message)=>{
  const end=performance.now()+4500;
  while(!test()){
   if(performance.now()>end) throw new Error(message);
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 const load=action=>new Promise(resolve=>{frame.addEventListener('load',resolve,{once:true});action();});
 const win=()=>frame.contentWindow;
 const row=()=>win().document.querySelector('[data-number="ITEM_NUMBER"]');
 const form=()=>win().recordDetails(row()).querySelector('[data-feedback-form]');
 const open=()=>{
  if(!win().recordIsOpen(row())) row().querySelector('.list-toggle').click();
 };
 const words=()=>form().elements.words;
 const edit=value=>{words().value=value;words().dispatchEvent(new (win().Event)('input'));};
 const click=async({loseResult=false}={})=>{
  const view=win(),target=form(),button=target.querySelector('[value="not works"]'),actual=view.fetch;
  let result;
  view.fetch=async(url,options)=>{
   const response=await actual.call(view,url,options);
   if(String(url).startsWith('/send-hold/') && !options?.method){
    holdReads.push(url);
    if(loseResult && response.status===200){
     result={status:503,fields:{reason:'The hold result was lost.'}};
     return Response.json(result.fields,{status:result.status});
    }
   }else if(options?.method==='POST') result={status:response.status,fields:await response.clone().json()};
   return response;
  };
  try{
   check(!button.disabled && button.getClientRects().length,'Not fixed is available on the card.');
   button.click();
   await until(()=>result && !target.dataset.sending,'Not fixed did not finish.');
   return result;
  }finally{view.fetch=actual;}
 };
 try{
  await load(()=>{frame.src=location.pathname;document.body.append(frame);});
  open();edit(SUBMITTED_WORDS);
  const first=await click({loseResult:loseHoldResult}),receipt=first.fields.not_fixed_receipt;
  if(loseHoldResult){
   check(first.status===503 && holdReads.length===1,'The first held result is lost.');
  }else{
   check(first.status===200 && first.fields.send_failure && receipt?.state==='accepted','The first send saves without delivery.');
   check(row().dataset.state==='Working' && words().value===SUBMITTED_WORDS,'Working keeps the submitted words.');
  }
  edit('');
  check(!form().querySelector('[value="not works"]').disabled,'An empty draft still permits retry.');
  if(tellAfterFailure){
   edit(TELL_WORDS);
   form().querySelector('[value="tell"]').click();
   await until(()=>!words().value && !form().dataset.sending,'Tell did not clear the draft.');
  }else edit(expectedDraft);
  if(!loseHoldResult && !tellAfterFailure){
   const second=await click();
   check(second.status===503,'The second send has a temporary failure.');
  }
  await load(()=>win().location.reload());
  open();
  check(words().value===expectedDraft,'Reload restores only unsent draft words.');
  const beforeRefresh=win().document.querySelector('[data-list]');
  const resumed=await click();
  check(resumed.status===200 && resumed.fields.not_fixed_receipt?.state==='delivered',
   'The original request confirms delivery after reload.');
  if(loseHoldResult) check(holdReads.length===2 && holdReads[0]===holdReads[1],'The retry reads the original hold.');
  else check(resumed.fields.not_fixed_receipt.request===receipt?.request,'The delivery receipt names the original request.');
  check(words().value===expectedDraft,'Confirmation keeps the current draft.');
  check(!form().querySelector('[value="not works"]').getClientRects().length,'The confirmed request leaves the card.');
  await until(()=>!beforeRefresh.isConnected,'The List did not refresh after the confirmed retry.');
 }catch(error){failures.push(String(error));}
 return failures;
})()
""".replace("ITEM_NUMBER", str(item.number)).replace("SUBMITTED_WORDS", json.dumps(words))
        script = script.replace("LOSE_HOLD_RESULT", json.dumps(lose_hold_result))
        script = script.replace("TELL_AFTER_FAILURE", json.dumps(tell_after_failure))
        script = script.replace("TELL_WORDS", json.dumps(tell_words))
        with (patch.dict(os.environ, {"REVIEW_LINKS_ROOT": str(root)}),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(serve, "loaded_module_stamps", return_value={}),
              patch.object(list_page, "save_action", side_effect=save),
              patch.object(list_page, "counts_strip", return_value=""),
              patch.object(list_page, "recover_pending"),
              patch.object(change_page, "send_to_staff", side_effect=send),
              started_review_server(("127.0.0.1", 0), root) as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            chrome = self.chrome()
            try:
                chrome.navigate(f"http://127.0.0.1:{server.server_port}{studio_page.PAGE_PATH}")
                reply = chrome.command("Runtime.evaluate", {"expression": script, "awaitPromise": True,
                                       "returnByValue": True}, chrome.session)
                self.assertNotIn("exceptionDetails", reply)
                self.assertEqual(cast(dict[str, object], reply["result"])["value"], [])
            finally:
                chrome.navigate("about:blank")
                server.shutdown()
                thread.join()
        self.assertEqual(len(posted), 3 if scenario == "temporary failure" else 2)
        self.assertEqual(posted, [posted[0]] * len(posted))
        self.assertEqual(posted[0]["words"], [words])
        request, = posted[0]["request"]
        feedback = the_list.feedback_requests(the_list.load(MORTY.id, item.id))
        if tell_after_failure:
            self.assertEqual(len(tells), 1)
            self.assertEqual([entry.words for entry in feedback.values() if entry.action == "tell"], [tell_words])
            self.assertEqual(len(feedback), 2)
            self.assertEqual(feedback[request].action, the_list.ReportVerdict.NOT_FIXED)
        else:
            self.assertEqual(list(feedback), [request])
        self.assertEqual((feedback[request].state, feedback[request].words), ("sent", words))
        self.assertCountEqual([recipient for recipient, _ in accepted], (OWNER.id, DWIGHT.id))
        self.assertEqual(len({key for _, key in accepted}), 1)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_held_question_hides_the_item_and_failure_restores_it(self) -> None:
        item = self.add("Ask about the first item", number=1)
        _ = self.add("Keep the second item visible", number=2)
        steps = r'''
 const loaded=window.fetch;
 window.fetch=(url,options={})=>{
  if(String(url).startsWith('/send-hold/'))
   return Promise.resolve(Response.json({reason:'The question failed.'},{status:400}));
  if(options.method!=='POST') return loaded(url,options);
  requests.push(new URLSearchParams(options.body));
  return Promise.resolve(Response.json({hold:'a'.repeat(24),seconds:30},
   {status:202,headers:{'X-Send-Hold':'held'}}));
 };
 const shown=()=>[...document.querySelectorAll('[data-list-item]')].filter(item=>item.getClientRects().length);
 const all=document.querySelector('[data-filter-row="show"] [data-filter-value="all"]');
 const order=()=>shown().map(item=>item.dataset.key).join();
 const before=order(),words=form.elements.words;
 const notice=form.querySelector('[data-send-status]'),text='Which check passed?';
 const changed=async(test,message)=>{
  const end=performance.now()+4000;
  while(!test()){
   if(performance.now()>end) throw new Error(message);
   await new Promise(resolve=>requestAnimationFrame(resolve));
  }
 };
 words.value=text;words.dispatchEvent(new Event('input'));
 const ask=form.querySelector('[value="question"]');
 const recipient=ask.getAttribute('aria-label')?.slice(ask.textContent.length+2);
 submit('question');
 const toast=()=>document.querySelector('[data-send-undo]:has(button)');
 await changed(toast,'The Undo toast did not show.');
 const message=toast().querySelector('span').textContent;
 check(message==='Sent to '+recipient+'.','The Ask button and toast name the same receiver.');
 check(shown().length===1 && !row.getClientRects().length && all.dataset.filterCount==='1',
  'The held question removes its item from the List and the chip count.');
 toast().getAnimations()[0].finish();
 await changed(()=>notice.textContent==='Not saved: The question failed.','The row did not report the failure.');
 await changed(()=>row.getClientRects().length,'The failed question did not return.');
 check(order()===before && recordIsOpen(row) && words.value===text && all.dataset.filterCount==='2',
  'Failure restores the open item, its draft, and the chip count.');
'''
        for producer in (MORTY, DWIGHT):
            with self.subTest(producer=producer.name):
                _ = the_list.update(MORTY.id, item.id, {"producer": producer.id}, source="morty")
                with patch.object(session_pane, "session_place"):
                    self.browser_action_sequence(steps, add_item=False, window_size=(500, 900))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_ask_button_toast_and_delivery_name_the_producer(self) -> None:
        item = self.add("Check the assigned item", number=1)
        _ = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        root = self.root / "reviews"
        _ = write_config_fixture(conf.path("review_links"), "undo_seconds=1\n")
        script = r"""
(async()=>{
 const row=document.querySelector('[data-number="1"]');
 row.querySelector('.list-toggle').click();
 const form=recordDetails(row).querySelector('[data-feedback-form]');
 const ask=form.querySelector('[value="question"]');
 const label=ask.getAttribute('aria-label');
 const toast=new Promise(resolve=>{
  const observer=new MutationObserver(()=>{
   const text=document.querySelector('[data-send-undo] span');
   if(text){observer.disconnect();resolve(text.textContent);}
  });
  observer.observe(document.body,{childList:true,subtree:true});
 });
 const sent=new Promise(resolve=>{
  const request=window.fetch;
  window.fetch=async(url,options)=>{
   const response=await request(url,options);
   if(String(url).startsWith('/send-hold/') && response.status!==202){
    const result=await response.clone().json();
    resolve(response.ok && !result.send_failure && !result.no_message);
   }
   return response;
  };
 });
 form.elements.words.value='Which check passed?';
 form.elements.words.dispatchEvent(new Event('input',{bubbles:true}));
 if(ask.disabled) throw new Error('Ask question is disabled.');
 ask.click();
 return [label,await toast,await sent];
})()
"""
        with (patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(serve, "loaded_module_stamps", return_value={}),
              patch.object(list_page, "counts_strip", return_value=""),
              patch.object(session_pane, "session_place"),
              patch.object(unrouted_alert, "message_outcome", return_value=question_outcome()) as send,
              started_review_server(("127.0.0.1", 0), root) as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            browser = self.chrome()
            try:
                browser.navigate(f"http://127.0.0.1:{server.server_port}{list_page.PAGE_PATH}")
                result = browser.command("Runtime.evaluate", {"expression": script, "awaitPromise": True,
                                                               "returnByValue": True}, browser.session)
                self.assertNotIn("exceptionDetails", result)
                self.assertEqual(cast(dict[str, object], result["result"])["value"],
                                 [f"Ask question, {DWIGHT.name}", f"Sent to {DWIGHT.name}.", True])
            finally:
                browser.navigate("about:blank")
                server.shutdown()
                thread.join()
        send.assert_called_once()
        assert send.call_args is not None
        self.assertEqual(send.call_args.args[0], f"session:{DWIGHT.current_session}")
        self.assertIn("Which check passed?", cast(str, send.call_args.args[2]))

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_tell_undo_toast_names_the_signed_producer(self) -> None:
        item = self.add("Check the assigned item", number=1)
        steps = r"""
 const listFetch=window.fetch;
 window.fetch=(path,options={})=>{
  if(options.method==='POST' && new Headers(options.headers).get('X-Undo-Hold')==='1'){
   requests.push(new URLSearchParams(options.body));
   return Response.json({hold:'a'.repeat(24),seconds:30},{status:202,headers:{'X-Send-Hold':'held'}});
  }
  return listFetch(path,options);
 };
 const toast=new Promise(resolve=>{
  const observer=new MutationObserver(()=>{
   const text=document.querySelector('[data-send-undo] span');
   if(text){observer.disconnect();resolve(text.textContent);}
  });
  observer.observe(document.body,{childList:true,subtree:true});
 });
 const words=form.elements.words;
 words.value='Check the result.';words.dispatchEvent(new Event('input'));
 const label=form.querySelector('[value="tell"]').textContent;
 const name=label.replace(/^Tell /,'');
 submit('tell');
 const shown=await toast;
 check(shown==='Sent to '+name+'.','The Tell button and toast name the same receiver.');
 if(DIFFERENT_KEEPER) check(shown==='Sent to dwight.','The Undo toast names the signed producer.');
"""
        for producer in (MORTY, DWIGHT):
            with self.subTest(producer=producer.name):
                _ = the_list.update(MORTY.id, item.id, {"producer": producer.id}, source="morty")
                self.browser_action_sequence(
                    steps.replace('DIFFERENT_KEEPER', json.dumps(producer.id != MORTY.id)), add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_shows_saved_actions_and_refreshes_until_delivery_ends(self) -> None:
        steps = r'''
 const view=document.querySelector('[data-record-inline-view]');
 const detail=()=>view.querySelector('[data-record-inline-detail]');
 const list=()=>document.querySelector('[data-list]');
 const refresh=async()=>{
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();
 };
 const html=detail().innerHTML;
 form.elements.words.value='Check the logs.';
 form.elements.words.dispatchEvent(new Event('input'));
 submit(ACTION);
 const body=words=>'<p>'+words+'</p>'+html;
 const fresh=(revision,words)=>{
  const page=document.createElement('template');
  page.innerHTML=list().outerHTML;
  const fresh=page.content.querySelector('[data-list-item]');
  fresh.dataset.detailRevision=revision;
  recordDetails(fresh).innerHTML=body(words);
  return page.innerHTML;
 };
 const ask=ACTION==='question';
 const savedBody='Saved '+(ask ? 'question' : 'feedback')+'.';
 refreshMarkup=fresh('saved',savedBody);
 deliveryPending=true;
 const result={item_state:'Working',unread:'false',detail_revision:'saved',delivery_pending:true,delivery:'fixture-delivery'};
 if(!ask) result.detail_html=body(savedBody);
 finishPost(result);await flush();
 check(detail().textContent.includes(savedBody),'The confirmed save appears immediately.');
 const shown=list(),reads=pageReads;
 refreshMarkup=fresh('during','Delivery in progress.');
 await refresh();
 check(pageReads===reads+1 && list()!==shown && detail().textContent.includes('Delivery in progress.'),
  'A delivery refresh updates the open row.');
 refreshMarkup=fresh('after','Delivery ended.');
 deliveryPending=false;
 await refresh();
 check(pageReads===reads+2 && detail().textContent.includes('Delivery ended.'),
  'The next refresh applies the completed delivery result.');

'''
        _ = self.add("Own actions", number=1)
        for action in ("tell", "question"):
            with self.subTest(action=action):
                script = steps.replace("ACTION", json.dumps(action))
                self.browser_action_sequence(script, reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_ask_compares_inline_and_deferred_review_links(self) -> None:
        root = self.root / "reviews"
        item = self.add("Keep the linked Review in place", number=1)
        _ = the_list.link(MORTY.id, item.id, ("sdlc/C1",), source="morty")
        change = review_items.ReviewItem("sdlc/C1", "sdlc/C1", review_items.ReviewType.CHANGE,
            OWNER.id, OWNER.id, "Check the change", review_items.ReviewState.OPEN,
            review_items.ReviewTimes(NOW, NOW), (), (), review_items.ProposalLedgerSource(self.root / "review.md"))
        published: list_page.ReviewRecords = {change.key: change}
        initial = list_page.render(root, include_counts=False, review_records=published).decode()
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "The question was saved."}, source="morty")
        saved = list_page.render(root, include_counts=False, review_records=published).decode()
        deferred = list_page.render(root, defer_counts=True, review_records={}).decode()
        updates = list_page.render_review_updates(root, staff.snapshot(), published)
        changed = list_page.render(root, include_counts=False, review_records={
            change.key: replace(change, state=review_items.ReviewState.DONE)}).decode()
        steps = r'''
 const view=document.querySelector('[data-record-inline-view]');
 const detail=view.querySelector('[data-record-inline-detail]');
 const links=()=>detail.querySelector('[data-list-review-links]');
 const fetchPage=window.fetch;
 let countReads=0,lastPageQuery;
 window.fetch=(url,options)=>{
  const query=new URL(url,location.href).searchParams;
  if(query.has('counts')){countReads++;return Promise.resolve({headers:new Headers(),ok:true,text:async()=>DEFERRED_LINKS});}
  if(options?.method!=='POST') lastPageQuery=query;
  return fetchPage(url,options);
 };
 const refresh=async markup=>{
  refreshMarkup=markup;
  dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
  await flush();
 };
 const savedPage=new DOMParser().parseFromString(SAVED_PAGE,'text/html');
 const savedRow=savedPage.querySelector('[data-list-item]');
 form.elements.words.value='Did the review change?';
 form.elements.words.dispatchEvent(new Event('input'));
 submit('question');
 refreshMarkup=DEFERRED_PAGE;deliveryPending=false;
 finishPost({item_state:'Working',unread:savedRow.dataset.unread,unread_sort:JSON.parse(savedRow.dataset.sortValues)[7],
  detail_html:recordDetails(savedRow).innerHTML,
  detail_revision:savedRow.dataset.detailRevision,delivery_pending:true,delivery:'fixture-delivery'});
 await flush();
 check(countReads===1 && lastPageQuery.has('delivery') && links().textContent.includes('1 change for you'),
  'The saved Ask keeps the same links when the page defers them.');
 const shownForm=detail.querySelector('form');
 await refresh(SAVED_PAGE);
 check(!lastPageQuery.has('delivery') && detail.querySelector('form')===shownForm,
  'Identical inline links keep the same form after delivery ends.');
 await refresh(CHANGED_PAGE);
 check(links().textContent.includes('Reviews done') && detail.querySelector('form')===shownForm,
  'The refresh applies the Review status and keeps the form.');
'''
        for key, value in (("SAVED_PAGE", saved), ("DEFERRED_PAGE", deferred),
                           ("DEFERRED_LINKS", updates), ("CHANGED_PAGE", changed)):
            steps = steps.replace(key, json.dumps(value).replace("</", "<\\/"))
        self.browser_action_sequence(steps, reduced_motion=True, add_item=False, markup=initial)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_save_readbacks_time_out_before_headers_and_during_the_body(self) -> None:
        _ = write_config_fixture(conf.path("review_links"), "page_request_timeout_seconds=4\n")
        _ = self.add("Keep the List usable after a timeout", number=1)
        markup = list_page.render(self.root / "reviews", include_counts=False).decode()
        steps = r'''
 const action=ACTION,headerStall=HEADER_STALL;
 const view=document.querySelector('[data-record-inline-view]');
 const error=view.querySelector('[data-record-error]');
 const fetchPage=window.fetch,deadlines=new Map();
 AbortSignal.timeout=milliseconds=>{
  const timer=new AbortController();
  deadlines.set(timer.signal,{timer,milliseconds});
  return timer.signal;
 };
 let stalled,reads=0,followUps=0;
 window.fetch=(url,options)=>{
  if(options?.method==='POST') return fetchPage(url,options);
  if(stalled){followUps++;return fetchPage(url,options);}
  const query=new URL(url,location.href).searchParams;
  const readback=action!=='question';
  check(query.has('item-state')===readback,'The save starts the expected readback request.');
  stalled=options?.signal;
  if(!stalled) throw new Error('The readback has no timeout signal.');
  if(headerStall) return new Promise((resolve,reject)=>{
   stalled.addEventListener('abort',()=>reject(stalled.reason),{once:true});
  });
  const stream=new ReadableStream({start(controller){
   stalled.addEventListener('abort',()=>controller.error(stalled.reason),{once:true});
  }});
  const response=new Response(stream);
  const method=readback ? 'json' : 'text';
  const read=response[method].bind(response);
  response[method]=()=>{reads++;return read();};
  return Promise.resolve(response);
 };
 form.elements.until.value='2026-12-03';
 form.elements.words.value='Did the check pass?';
 form.elements.words.dispatchEvent(new Event('input'));
 submit(action);
 if(action==='question') finishPost({delivery_pending:false,detail_unavailable:true});
 else if(action==='park'){await loseParkPost();losePost();}
 else losePost();
 await flush();
 check(reads===(headerStall ? 0 : 1),'The request stalls at the selected stage.');
 const deadline=deadlines.get(stalled);
 check(deadline?.milliseconds===4000,'The readback uses the configured timeout.');
 deadline.timer.abort(new DOMException('The fixture timed out.','TimeoutError'));
 await flush();
 if(action==='question')
  check(!error.hidden && error.textContent==='Saved; the item could not refresh.',
   'The confirmed save reports a display error.');
 else
  check((form.querySelector('[data-send-status]').textContent+
   document.querySelector('[data-hidden-action-status]').textContent).includes('Save not confirmed: Fixture lost response'),
   'The closing save stays unconfirmed.');
 check(!form.dataset.sending && followUps>0,'The timeout releases the save and lets a List refresh start.');
'''
        for action in ("question", "park", "remove"):
            for header_stall in (True, False):
                with self.subTest(action=action, header_stall=header_stall):
                    script = steps.replace("ACTION", json.dumps(action)).replace("HEADER_STALL", json.dumps(header_stall))
                    self.browser_action_sequence(script, reduced_motion=True, add_item=False, markup=markup)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_list_closing_action_closes_when_the_saved_detail_is_unavailable(self) -> None:
        _ = self.add("Test List action retries", number=1)
        steps = r'''
 form.elements.until.value='2026-12-03';
 submit(ACTION);
 finishPost({detail_unavailable:true,delivery_pending:true});
 await flush();
 const error=document.querySelector('[data-hidden-action-status]');
 check(!document.querySelector('[data-record-inline-view]') && !error.hidden && error.textContent.includes('Saved; the item could not refresh.'),
  'A confirmed closing action collapses the row and reports the display failure.');
'''
        for action in ("park", "remove"):
            with self.subTest(action=action):
                self.browser_action_sequence(steps.replace("ACTION", json.dumps(action)), reduced_motion=True,
                                             add_item=False)

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
 losePost();await flush();
 check(!row.hidden && !ask.disabled && document.activeElement===ask,'The restored Ask button has focus');
 submit('question');
 const search=document.getElementById('list-search');
 document.querySelector('[data-list-item] .list-toggle[aria-expanded=true]').click();
 search.focus();
 finishPost({reason:'The POST was refused'});await flush();
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
 losePost();await flush();
 const spacer=document.querySelector('[data-record-motion]');
 const animation=spacer?.getAnimations({subtree:true})[0];
 check(animation && getComputedStyle(row).display==='none','The row is still hidden by the animation');
 if(!animation) throw new Error('The restored row must animate.');
 const search=document.querySelector('#list-search');
 if(USER_SELECTS_SEARCH) search.focus();
 animation.finish();await flush();
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
 check(Boolean(recordDetails(row).querySelector('[data-question-delivery-failed]')),'The older question has a delivery failure');
 refreshMarkup=listSnapshot();
 input.value='Which check is next?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 ask.blur();
 finishPost({unread:'false',waiting_for_answer:'false'});await flush();
 const refreshed=document.querySelector('[data-number="1"]');
 check(refreshed===row && !refreshed.hidden,'The refresh keeps the same row visible');
 check(recordIsOpen(refreshed) && document.activeElement===refreshed.querySelector('.list-toggle'),
  'The expanded row keeps focus through refresh');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_moves_focus_when_a_mixed_question_save_removes_the_row_from_unread(self) -> None:
        item = self.add_item_with_failed_question()
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "A newer result arrived."}, source="morty")
        _ = self.add("The next unread item", number=2)
        self.browser_action_sequence(r"""
 onlyUnread();
 check(!row.hidden && recordDetails(row).querySelector('[data-question-delivery-failed]'),
  'Unread shows the item with a failed question');
 const snapshot=document.documentElement.cloneNode(true);
 snapshot.querySelector('[data-number="1"]').dataset.unread='false';
 refreshMarkup=snapshot.outerHTML;
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check is next?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 ask.blur();
 finishPost({unread:'false',waiting_for_answer:'false'});await flush();
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
 const remove=recordDetails(other).querySelector('button[value="remove"]');
 refreshMarkup=listSnapshot();
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 input.value='Which check is next?';input.dispatchEvent(new Event('input'));
 ask.focus();
 submit('question');
 other.querySelector('.list-toggle').click();
 remove.focus();
 check(document.activeElement===remove,'The user selects Remove on the other item during the request');
 finishPost({unread:'false',waiting_for_answer:'false'});await flush();
 const refreshed=document.querySelector('[data-number="2"]');
 check(refreshed===other && !refreshed.hidden,'The refresh leaves the other open row in place');
 check(document.activeElement===recordDetails(refreshed).querySelector('button[value="remove"]'),
  'Remove on the same item keeps focus through refresh');
""", reduced_motion=True, add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_restores_the_column_sort_focus_after_refresh(self) -> None:
        _ = self.add_item_with_failed_question()
        self.browser_action_sequence(r"""
 const sort=document.querySelector('[data-sort="3"]');
 refreshMarkup=listSnapshot();
 form.elements.words.value='Which check is next?';form.elements.words.dispatchEvent(new Event('input'));
 form.querySelector('[value="question"]').focus();
 submit('question');
 document.querySelector('[data-list-item] .list-toggle[aria-expanded=true]').click();
 sort.focus();
 finishPost({unread:'false',waiting_for_answer:'false'});await flush();
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
 ask(recordDetails(second).querySelector('form'));
 failFirst({reason:'The save failed'});await flush();
 const returning=row.nextElementSibling.getAnimations({subtree:true})[0];
 check(!row.hidden && !row.querySelector('.list-toggle').getClientRects().length,
  'The first row returns but its animation still hides its toggle');
 returning.pause();
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
 check(document.activeElement===document.getElementById('list-search'),
  'Search receives focus because neither row has an available toggle');
 returning.finish();
""", add_item=False)

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_question_response_preserves_focus_in_the_parent_console(self) -> None:
        _ = self.add("The question item", number=1)
        _ = self.add("The next item", number=2, owner=OTHER.id)
        self.browser_action_sequence(r"""
 show('working');
 const ask=form.querySelector('[value="question"]'),input=form.elements.words;
 const nav=parent.document.getElementById('console-nav');
 input.value='Which check passed?';input.dispatchEvent(new Event('input'));
 ask.focus();
 check(document.hasFocus() && document.activeElement===ask,'Ask starts with focus in the List frame');
 submit('question');
 check(!row.hidden,'The question item stays in Working while the request is pending.');
 nav.focus();
 check(!document.hasFocus() && parent.document.activeElement===nav,'The user focuses the console during the request');
 finishPost({unread:'false',waiting_for_answer:'true',open_question:'true'});await flush();
 check(row.hidden && !document.querySelector('[data-record-inline-view]'),
  'The saved question leaves Working and closes the dialog.');
 check(parent.document.activeElement===nav,'The response preserves the console focus');
""", add_item=False, reduced_motion=True, in_frame=True)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_open_needs_me_answer_stays_unread_after_refresh(self) -> None:
        root = self.root / "reviews"
        item = self.add("Check the result")
        with (patch.object(change_page, "send_question", return_value=question_outcome()),
              patch.object(staff, "caller_identity", return_value=MORTY.current_session), redirect_stdout(io.StringIO())):
            fields = self.fields(item, "question", words="Which check passed?")
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(the_list.main(["answer", item.id, "--question", "Q1", "--text", "The gate passed."]), 0)
        markup = list_page.render(root, include_counts=False).decode()
        markup = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', markup, count=1)
        _ = the_list.update(MORTY.id, item.id, {"latest_status": "A newer update from morty."}, source="morty")
        refreshed = list_page.render(root, include_counts=False).decode()
        setup = r"""<script>
history.replaceState(null,'','#show=needs-me');
const snapshot=SNAPSHOT,reads=[];
window.fetch=async(_url,options)=>{
 if(options?.method==='POST'){
  reads.push(new URLSearchParams(options.body));
  return new Response(JSON.stringify({unread:'false'}));
 }
 return new Response(snapshot);
};
</script>""".replace('SNAPSHOT', json.dumps(refreshed).replace("<", r"\u003c"))
        probe = r"""<script>
(async()=>{
 const failures=[],check=(ok,message)=>{if(!ok) failures.push(message);};
 try{
  const row=()=>document.querySelector('[data-list-item]');
  const detail=()=>recordDetails(row());
  const original=row();
  original.querySelector('.list-toggle').click();
  const answer=detail().querySelector('[data-question="Q1"] p:last-child');
  answer.scrollIntoView({block:'center',behavior:'instant'});
  const shown=await new Promise(resolve=>{
   const observer=new IntersectionObserver(([entry])=>{observer.disconnect();resolve(entry);});
   observer.observe(answer);
  });
  await new Promise(resolve=>requestAnimationFrame(()=>requestAnimationFrame(resolve)));
  check(shown.isIntersecting && reads.length===0,'Opening a visible answer sends no read.');
  const needsMe=()=>document.getElementById('filter-show-needs-me');
  const count=()=>document.querySelector('[data-list-count]').textContent;
  const checkUnread=()=>{
   check(!row().hidden && recordIsOpen(row()) && detail().querySelector('.question-unread') &&
    detail().querySelector('[value="read"]')?.textContent==='Mark read','Needs me keeps the open unread answer.');
   check(needsMe().dataset.filterCount==='1' && count().startsWith('1 of 1 items shown · 1 need you'),
    'The Needs me count and status agree with the visible row.');
  };
  checkUnread();
  const list=document.querySelector('[data-list]');
  const refreshed=new Promise(resolve=>{
   const observer=new MutationObserver(()=>{
    if(list.isConnected) return;
    observer.disconnect();resolve();
   });
   observer.observe(list.parentElement,{childList:true});
  });
  document.dispatchEvent(new Event('visibilitychange'));
  await refreshed;
  check(row()===original,'The refresh keeps the open row node.');
  checkUnread();
  check(detail().textContent.includes('A newer update from morty.'),
   'The refresh applies the new text and keeps the answer unread.');
  check(reads.length===0,'Refresh sends no read.');
 }catch(error){failures.push(String(error));}
 await parent.fetch('/results',{method:'POST',body:JSON.stringify(failures)});
})();
</script>"""
        self.browser_answer_visibility(markup, setup, probe, viewport_height=1120)

    def browser_answer_visibility(self, markup: str, setup: str, probe: str, *,
                                  viewport_height: int | None = None, reload_markup: str = "",
                                  reload_console: bool = False, console_fragment: str = "",
                                  viewport_width: int | None = None) -> None:
        if viewport_width is not None:
            probe = '<img hidden src="/complete">' + probe
        page = markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>').encode()
        reloaded = reload_markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>').encode()
        loaded = threading.Event()
        height = f"{viewport_height}px" if viewport_height is not None else "calc(100vh - 24px)"
        frame_path = "/page"
        frame_url = frame_path
        console_shell = b""
        if reload_console or console_fragment:
            frame_path = studio_page.PAGE_PATH
            listing = console.ConsolePage("studio", "Studio", "clapperboard", "The Studio", console.AvailablePage(frame_path))
            console_shell = console.render((listing,), console.UnavailablePage())
            place = frame_path + (console_fragment or "#show=working")
            frame_url = "/console/?" + urlencode({"page": "studio", "at.studio": place})
            if console_fragment:
                console_shell = console_shell.replace(b"</body>", b'<img hidden src="/complete"></body>')
        host_page = (f'<!doctype html><style>iframe{{border:0;width:100%;height:{height}}}'
                     + 'iframe[hidden]{display:block;visibility:hidden}</style>'
                     + f'<iframe data-src="{html.escape(frame_url)}"></iframe><img hidden src="/complete">'
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
                if self.path == '/complete':
                    _ = complete.wait(TEST_HANG_GUARD_SECS)
                    self.json_response(200, {})
                elif self.path == frame_path:
                    self.html_response(200, reloaded if reloaded and loaded.is_set() else page)
                    loaded.set()
                elif console_shell and urlsplit(self.path).path == "/console/":
                    self.html_response(200, console_shell)
                elif self.path == "/":
                    self.html_response(200, host_page)
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
              started_review_server(("127.0.0.1", 0), self.root / "reviews") as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                window_height = max(900, (viewport_height or 0) + 32)
                destination = frame_url if console_fragment or viewport_width is not None else "/"
                browser = self.firefox_view(f"http://127.0.0.1:{server.server_port}{destination}",
                                           None, viewport_width or 1280, window_height, complete)
                self.assertEqual(browser.returncode, 0, browser.stderr)
                self.assertEqual(results, [[]])
            finally:
                complete.set()
                server.shutdown()
                thread.join()

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
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
const recordDetails=item=>frame.contentWindow.recordDetails(item);
const recordIsOpen=item=>frame.contentWindow.recordIsOpen(item);
const hasDot=()=>Boolean(row().querySelector('.list-dot'));
const act=async (action,words='')=>{
 const win=frame.contentWindow, current=row();
 const form=recordDetails(current).querySelector('form'), input=form.elements.words;
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
  if(!recordIsOpen(current)) current.querySelector('.list-toggle').click();
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
  check(await act('question','Which check passed?')==='','the first question saves without a status message');
  check(row().hidden && !hasDot(),'the saved question leaves Unread and clears the dot');
  check(recordDetails(row()).querySelector('[data-waiting-questions] time[datetime]'),
   'the save response supplies the asked time for the waiting row');
  const key=row().dataset.key;
  await open('working');
  check(row().hidden && !row().querySelector('[data-open-question-note]').hidden,
   'The saved question has its note and stays out of Working.');
  await open('all',key);
  check(!row().hidden && recordIsOpen(row()) &&
   recordDetails(row()).querySelector('[data-waiting-note]')?.textContent==='morty will answer.' &&
   !recordDetails(row()).querySelector('[value="question"]'),'a direct link shows the waiting line without Ask');
  const waitingQuestion=recordDetails(row()).querySelector('[data-waiting-questions] [data-question="Q1"]');
  check(/(?:EST|EDT)$/.test(waitingQuestion?.querySelector('time[datetime]')?.textContent),
   'the waiting question shows the asked time in the user zone');
  await change('/answer');
  await open('needs-me','','1');
  const unreadQuestion=recordDetails(row()).querySelector('.list-panel [data-question="Q1"]');
  check(!row().hidden && hasDot() && unreadQuestion?.textContent.includes('Which check passed?') &&
   unreadQuestion.textContent.includes('The gate passed.') && !row().querySelector('[data-open-question-note]'),
   'Needs me with Unread only shows the answer in the panel and clears the question note.');
  check(unreadQuestion?.querySelectorAll('time[datetime]').length===2 &&
   unreadQuestion.querySelector('.question-unread')?.textContent==='Unread',
   'the unread answer shows both times and the Unread mark');
  check(!recordDetails(row()).querySelector('.list-log').textContent.includes('The gate passed.'),
   'the log has no unread answer');
  check(await act('read')==='','Mark read saves without a status message');
  await open('working',key);
  const log=recordDetails(row()).querySelector('.list-log');
  check(!hasDot() && !recordDetails(row()).querySelector('[data-question="Q1"]'),
   'the read question leaves the panel');
  check(log.querySelector('[data-log-index="Q1-answered"]').textContent.includes('Answer — morty') &&
   log.querySelector('[data-log-index="Q1-asked"]').textContent.includes('Question Q1 — nate'),
   'the log shows both parts of the read question with their authors');
  await change('/update');
  await open('working','','1');
  await change('/newer');
  check(await act('question','Which check is next?')==='',
   'the saved question shows no status message for a newer unread update');
  check(hasDot() && row().hidden,'The unread item leaves Working after the question saves.');
  frame.contentDocument.getElementById('filter-show-producer-answering').click();
  check(hasDot() && !row().hidden,'Producer answering shows the unread item.');
  const nextQuestion=(current)=>recordDetails(current).querySelector('[data-waiting-questions] [data-question="Q2"]');
  const firstAnswer=(current)=>recordDetails(current).querySelector('[data-log-index="Q1-answered"]');
  check(nextQuestion(row()) && firstAnswer(row())?.textContent.includes('The gate passed.'),
   'the Ask response keeps the first answer in the log beside the new waiting question');
  await open('all',key);
  check(recordIsOpen(row()) && nextQuestion(row()) && firstAnswer(row()),
   'a direct link shows both the waiting question and the first answer in the log');
  const {loggedAt}=await change('/second-answer');
  await open('needs-me');
  const original=row(), doc=frame.contentDocument,win=frame.contentWindow;
  const panelQuestion=recordDetails(original).querySelector('[data-question="Q2"]');
  const askedAt=Date.parse(panelQuestion.querySelector('time').dateTime);
  let posts=0;
  const fetchList=win.fetch.bind(win);
  win.fetch=(url,options)=>{
   if(options?.method==='POST') posts++;
   return fetchList(url,options);
  };
  const checkNeedsMe=message=>{
   const current=row(),currentDoc=frame.contentDocument;
   const detail=recordDetails(current),button=detail.querySelector('[value="read"]');
   check(!current.hidden && hasDot() && detail.querySelector('[data-question="Q2"] .question-unread') &&
    recordIsOpen(current) && button?.getClientRects().length && !button.disabled &&
    currentDoc.getElementById('filter-show-needs-me').dataset.filterCount==='1' &&
    currentDoc.querySelector('[data-list-count]').textContent==='1 of 2 items shown · 1 need you · 2 unread',message);
  };
  original.querySelector('.list-toggle').click();
  const answer=panelQuestion.querySelector('p:last-child');
  answer.scrollIntoView({block:'center',behavior:'instant'});
  await new Promise(resolve=>{
   const observer=new win.IntersectionObserver(([entry])=>{observer.disconnect();resolve(entry.isIntersecting);});
   observer.observe(answer);
  }).then(visible=>check(visible,'The answer appears in the viewport.'));
  checkNeedsMe('Opening the item keeps it unread in Needs me and shows Mark read.');
  doc.dispatchEvent(new win.Event('visibilitychange'));
  await new Promise(resolve=>win.requestAnimationFrame(()=>win.requestAnimationFrame(resolve)));
  check(row()===original,'The open row keeps its DOM during refresh.');
  check(posts===0,'Opening and refreshing the item sends no write.');
  checkNeedsMe('Refresh keeps the item unread in Needs me and shows Mark read.');
  doc.getElementById('filter-show-all').click();
  doc.getElementById('filter-show-needs-me').click();
  checkNeedsMe('Changing the Show filter keeps the unread item in Needs me.');
  await open('needs-me',key);
  checkNeedsMe('Reloading the open item keeps it unread in Needs me.');
  check(await act('read')==='','Mark read saves without action status.');
  check(row().hidden && !hasDot() &&
   frame.contentDocument.getElementById('filter-show-needs-me').dataset.filterCount==='0' &&
   frame.contentDocument.querySelector('[data-list-count]').textContent==='0 of 2 items shown · 0 need you · 1 unread',
   'Mark read removes the item from Needs me and updates the counts.');
  await open('working',key);
  check(recordDetails(row()).querySelector('[data-log-index="Q2-answered"]').textContent.includes('The next check passed.'),
   'Mark read moves the answer to the log.');
  const asked=recordDetails(row()).querySelector('[data-log-index="Q2-asked"]');
  const answered=recordDetails(row()).querySelector('[data-log-index="Q2-answered"]');
  check(asked?.textContent.includes('Which check is next?') && Date.parse(asked.querySelector('time').dateTime)===askedAt &&
   Date.parse(answered.querySelector('time').dateTime)===Date.parse(loggedAt) && !recordDetails(row()).querySelector('[data-question="Q2"]'),
   'The log shows the question and answer with their recorded times after the panel clears.');
 }catch(error){errors.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(errors)});
})();
</script>""".replace('LIST_PATH', studio_page.PAGE_PATH).encode()

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
              started_review_server(("127.0.0.1", 0), root) as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                browser = self.firefox_view(f'http://127.0.0.1:{server.server_port}/proof',
                                            None, 1440, 900, complete)
                self.assertEqual(browser.returncode, 0, browser.stderr)
                self.assertEqual(results, [[]])
            finally:
                complete.set()
                server.shutdown()
                thread.join()

    def test_go_box_role_text_log_line_and_hidden_boxes(self) -> None:
        merge = staff.StaffRow(staff.StaffId("6c0c0a8e-3c1f-4d8e-9e43-0d51f0a7c2b1"), "unit", "natemerge",
                               staff.ReportsTo(OWNER.id), current_session=staff.StaffId("1b7f6f43-5d55-4a8e-b0a4-7a3e2b9b6c10"),
                               unit_type=staff.UnitDetails("merge"))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            row.to_json() for row in (MORTY, DWIGHT, OWNER, OTHER, merge)]}))
        members = staff.snapshot()
        names = staff_names.StaffNames(members)
        def text(markup: str) -> str:
            return " ".join(re.sub(r"<[^>]+>", " ", markup).split()).replace(" :", ":")
        self.assertEqual(text(list_page.staff_role_html(merge, names)), "the merge director of sdlc")
        self.assertEqual(text(list_page.staff_role_html(MORTY, names)), "the List producer")
        self.assertRegex(list_page.staff_role_html(merge, names),
                         r'data-color-text="role.unit-director" style="color:#ffaf02">merge director</span>')
        self.assertRegex(list_page.staff_role_html(MORTY, names),
                         r'data-color-text="role.producer" style="color:#90fe42">List producer</span>')
        item = self.add("Merge the needsme unit", number=1)
        ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=merge.id, what="Merge needsme to main.",
                               why="The merge needs nate's go.", config=False, requester=MORTY.id, source="morty")
        unknown = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.APPROVAL, target=staff.StaffId("gone-session"),
                                   what="Publish the key.", why="It needs nate.", config=True, requester=MORTY.id, source="morty")
        opened = the_list.load(MORTY.id, item.id)
        self.assertEqual(the_list.answerable_asks(opened, members), (ask,))
        self.assertNotIn(unknown, the_list.answerable_asks(opened, members))
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        self.assertEqual(markup.count("data-ask="), 1)
        self.assertIn("natemerge, the merge director of sdlc", text(without_role_colors(markup)))
        self.assertRegex(markup, r'Pressing Go sends your go to <span[^>]*data-color-text="role.unit-director"[^>]*>natemerge</span>\.')
        _ = the_list.give_go(MORTY.id, item.id, ask.ask_id, "0" * 32, source="nate")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        self.assertNotIn("data-ask=", markup)
        self.assertIn("Go — nate: Merge needsme to main. → natemerge", text(without_role_colors(markup)))
        closed = self.add("Closed with an open ask", number=2)
        _ = the_list.add_ask(MORTY.id, closed.id, kind=the_list.AskKind.GO, target=merge.id, what="Merge.",
                             why="It needs nate.", config=False, requester=MORTY.id, source="morty")
        closed = the_list.close(MORTY.id, closed.id, "Done.", source="nate")
        self.assertEqual((len(the_list.open_asks(closed)), the_list.answerable_asks(closed, members)), (1, ()))
        entry = next(entry for entry in list_page.records(include_closed=True) if entry.item.id == closed.id)
        self.assertEqual(list_page.ask_boxes(entry, "secret", names), "")

    def test_withdrawn_ask_loses_its_button_and_cannot_grant_a_go(self) -> None:
        item = self.add("Merge the needsme unit", number=1)
        ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge.",
                               why="The merge needs nate's go.", config=False, requester=MORTY.id, source="morty")
        root = self.root / "reviews"
        # Save the record as a click does before it closes the ask.
        entry = go_ledger.record(root, ask=ask, item=f"{MORTY.id}/{item.id}", recipients=(OWNER.id,),
                                 client="192.0.2.2", page="/console/list.html", moment=the_list.now())
        with patch.object(list_page, "counts_strip", return_value=""):
            self.assertIn('data-ask="A1"', list_page.render(root).decode())
            the_list.withdraw_ask(MORTY.id, item.id, ask.ask_id, "The merge is complete.", caller=MORTY)
            markup = list_page.render(root).decode()
        self.assertNotIn('data-ask="A1"', markup)
        self.assertIn("Ask A1 withdrawn", markup)
        self.assertIn("The merge is complete.", markup)
        with self.assertRaisesRegex(the_list.ListError, "This ask is no longer open"):
            _ = the_list.give_go(MORTY.id, item.id, ask.ask_id, entry["go_id"], source="nate")
        self.assertEqual(go_ledger.check(root, entry["go_id"], OWNER.id),
                         {"ok": False, "text": "the List has no closed ask for this Go."})
        self.assertFalse(go_ledger.read(root, entry["go_id"])["used"])

    def test_go_box_displays_the_role_of_a_standalone_session(self) -> None:
        identity = staff.StaffId("6c0c0a8e-3c1f-4d8e-9e43-0d51f0a7c2b1")
        clone = staff.StaffRow(identity, "session", "alpha-copy", staff.StaffRoot(), current_session=identity,
                               launch_folder=staff.SessionLaunchFolder("natedev", self.root),
                               home=staff.SessionHome(self.root / ".claude"))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            row.to_json() for row in (MORTY, DWIGHT, OWNER, OTHER, clone)]}))
        item = self.add("Review the clone's work")
        ask = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=identity,
                               what="Continue the review.", why="The clone needs nate's go.",
                               config=False, requester=MORTY.id, source="morty")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        self.assertIn(f'data-ask="{ask.ask_id}"', markup)
        self.assertIn("alpha-copy, a standalone session", re.sub(r"<[^>]+>", "", without_role_colors(markup)))

    def test_browser_go_closes_the_ask_and_the_item_leaves_needs_me(self) -> None:
        added = self.add("Merge the needsme unit", the_list.ItemState.WAITING_FOR_NATE, number=1)
        item = the_list.mark_read(MORTY.id, added.id, source="nate")
        _ = the_list.add_ask(MORTY.id, item.id, kind=the_list.AskKind.GO, target=OWNER.id, what="Merge needsme to main.",
                             why="The merge needs nate's go.", config=False, requester=MORTY.id, source="morty")
        root = self.root / "reviews"
        results: list[list[str]] = []
        complete = threading.Event()
        key = f"{MORTY.id}/{item.id}"
        probe = """<!doctype html><meta charset="utf-8">
<style>iframe{width:100%;height:calc(100vh - 24px);border:0}</style>
<iframe></iframe><script>
const frame=document.querySelector('iframe'), errors=[];
const check=(ok,message)=>{if(!ok) errors.push(message);};
const settle=async (done,message)=>{
 for(let tries=0;tries<200 && !done();tries++) await new Promise(resolve=>setTimeout(resolve,25));
 check(done(),message);
};
(async()=>{
 try{
  await new Promise(resolve=>{
   frame.onload=resolve;
   frame.src='LIST_PATH#'+new URLSearchParams({show:'needs-me',item:'KEY'});
  });
  const doc=frame.contentDocument,win=frame.contentWindow;
  const row=doc.querySelector('[data-number="1"]');
  await settle(()=>!row.hidden && win.recordIsOpen(row),'The item shows open in Needs me.');
  const box=win.recordDetails(row).querySelector('[data-ask]');
  const button=box?.querySelector('[data-go-form] button');
  const note=button && doc.getElementById(button.getAttribute('aria-describedby'));
  check(box?.querySelector('h3')?.textContent==='Needs your go' &&
   box.compareDocumentPosition(win.recordDetails(row).querySelector('.list-item-fields'))&Node.DOCUMENT_POSITION_FOLLOWING,
   'The open row shows the Go box above the item fields.');
  check(button?.textContent==='Go' && note?.tagName==='SMALL' && note.textContent==='Pressing Go sends your go to sdlc.',
   'The Go button has the small text through aria-describedby.');
  button.focus();
  check(doc.activeElement===button,'The Go button takes the keyboard focus.');
  button.click();
  await settle(()=>box.isConnected && !button.disabled &&
   win.recordDetails(row).querySelector('[data-send-status]')?.textContent.includes("Could not check natedev's addresses."),
   'The row shows the address error and the ask stays open.');
  button.click();
  await settle(()=>!box.isConnected && row.hidden,'A Go closes the box and the item leaves Needs me.');
  check(doc.getElementById('filter-show-needs-me').dataset.filterCount==='0','The Needs me count is 0 after the Go.');
  check(row.dataset.state==='Working' && row.querySelector('.list-state').textContent==='Working',
   'The Go on the last ask sets the waiting row to Working in place.');
 }catch(error){errors.push(String(error));}
 await fetch('/results',{method:'POST',body:JSON.stringify(errors)});
})();
</script>""".replace('LIST_PATH', studio_page.PAGE_PATH).replace('KEY', key).encode()

        class Probe(serve.ReviewLinksHandler):
            @override
            def send_head(self) -> BinaryIO | None:
                if self.path == '/proof':
                    self.html_response(200, probe)
                    return None
                return super().send_head()

            @override
            def do_POST(self) -> None:
                if self.path == '/results':
                    results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                    self.json_response(200, {})
                    complete.set()
                else:
                    super().do_POST()

        with (patch.object(serve, "ReviewLinksHandler", Probe),
              patch.object(review_page.ReviewSnapshotCache, "start"),
              patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
              patch.object(review_link, "tailscale_ip",
                           side_effect=["192.0.2.1", ValueError("No IPv6 address."), "192.0.2.1", "2001:db8::1"]),
              patch.object(list_page, "counts_strip", return_value=""),
              patch.object(change_page, "send_to_staff") as delivery,
              patch.object(go_ledger, "natedev_address", return_value=False),
              serve.ReviewLinksServer(("127.0.0.1", 0), root) as server):
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                browser = self.firefox_view(f'http://127.0.0.1:{server.server_port}/proof', None, 1440, 900, complete)
                self.assertEqual(browser.returncode, 0, browser.stderr)
                self.assertEqual(results, [[]])
            finally:
                complete.set()
                server.shutdown()
                thread.join()
        saved = the_list.load(MORTY.id, item.id)
        self.assertEqual((the_list.open_asks(saved), saved.state), ((), the_list.ItemState.WORKING))
        self.assertIn(the_list.GoGiven(saved.asks[0]), [change.what for change in saved.log])
        self.assertEqual(sorted(cast(str, sent.args[0]) for sent in delivery.call_args_list), sorted([OWNER.id, MORTY.id]))

    def browser_action_sequence(self, steps: str, *, reduced_motion: bool = False, add_item: bool = True,
                                window_size: tuple[int, int] | None = None, in_frame: bool = False,
                                markup: str | None = None, open_number: int = 1) -> None:
        if add_item:
            _ = self.add("Test List action retries", number=1)
        if markup is None:
            with patch.object(list_page, "counts_strip", return_value=""):
                markup = list_page.render(self.root / "reviews").decode()
        markup = re.sub(r'data-refresh-seconds="[^"]+"', 'data-refresh-seconds="0"', markup, count=1)
        markup = re.sub(r'data-park-retry-seconds="[^"]+"', 'data-park-retry-seconds="0"', markup, count=1)
        setup = '''<script>
const requests=[],itemSnapshots=new Map();
let finishPost,losePost,lostHold,failPostBody,refreshMarkup,readback,deliveryPending,pageReads=0;
window.fetch=async(path,options={})=>{
 if(options.method!=='POST'){
  const query=new URL(path,location.href).searchParams;
  if(query.has('item-state')){
   const key=query.get('keeper')+'/'+query.get('item-state');
   return {ok:true,json:async()=>readback ? readback(key,query.get('park-request')) : {
    ...itemSnapshots.get(key),park_request_saved:false}};
  }
  if(refreshMarkup===undefined) throw new Error('Fixture refresh refused');
  const headers=new Headers();
  pageReads++;
  if(typeof deliveryPending==='boolean') headers.set('X-Item-Delivery-Pending',String(deliveryPending));
  return {ok:true,headers,text:async()=>refreshMarkup};
 }
 const hold=new Headers(options.headers).get('X-Undo-Request');
 if(lostHold && hold===lostHold && !options.keepalive){
  lostHold=null;
  throw new Error('Fixture lost response');
 }
 requests.push(new URLSearchParams(options.body));
 return new Promise((resolve,reject)=>{
  losePost=()=>{
   if(options.keepalive) lostHold=hold;
   reject(new Error('Fixture lost response'));
  };
  failPostBody=()=>resolve({headers:new Headers(),ok:true,status:200,json:async()=>{throw new Error('The response is unreadable.');}});
  finishPost=result=>resolve({headers:new Headers(),ok:!result.reason,status:result.status ?? (result.reason ? 400 : 200),
   json:async()=>({unread:'true',...result})});
 });
};
</script>'''
        probe = '''<script>
(async()=>{
 const failures=[],check=(ok,name)=>{if(!ok) failures.push(name);};
 const row=document.querySelector('[data-number="OPEN_NUMBER"]'),form=recordDetails(row).querySelector('form');
 const submit=(action,target=form)=>{
  const count=requests.length;
  target.requestSubmit(target.querySelector('[value="'+action+'"]'));
  if(requests.length!==count+1) throw new Error('The test submission did not start.');
  const request=requests.at(-1);
  return {id:request.get('request'),token:request.get('token')};
 };
 const show=value=>document.querySelector('[data-filter-row="show"] [data-filter-value="'+value+'"]').click();
 const onlyUnread=(on=true)=>{const control=document.getElementById('list-unread');if(control.checked!==on) control.click();};
 const flush=()=>new Promise(resolve=>setTimeout(resolve,0));
 const loseParkPost=async()=>{losePost();await flush();await flush();};
 const listSnapshot=()=>{
  const page=document.documentElement.cloneNode(true);
  const view=page.querySelector('[data-record-inline-view]');
  if(view) view.replaceWith(...view.querySelector('[data-record-inline-detail]').childNodes);
  return page.outerHTML;
 };
 const finishRowMotion=async()=>{
  const motions=document.querySelectorAll('[data-record-motion]');
  for(const motion of motions) for(const animation of motion.getAnimations({subtree:true})) animation.finish();
  await flush();
 };
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
        _ = page.write_text(markup.replace('<head>', '<head>' + setup, 1).replace('</body>', probe + '</body>'))
        if in_frame:
            page = self.root / 'actions-console.html'
            _ = page.write_text('<!doctype html><button id="console-nav">Console navigation</button>'
                                + '<iframe title="The List" src="actions.html"></iframe>')
        result = self.chrome().dom(page, "probe-results", reduced_motion=reduced_motion, window_size=window_size)
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = '<p id="probe-results">'
        self.assertIn(marker, result.stdout[-3000:], 'The browser must complete the item action checks.')
        failures = cast(list[str], json.loads(html.unescape(result.stdout.split(marker, 1)[1].split('</p>', 1)[0])))
        self.assertEqual(failures, [])

    def test_delivery_notices_color_staff_names_and_keep_the_recorded_words(self) -> None:
        item = self.add("Delivery notice")
        _ = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        cases: tuple[tuple[the_list.PageFeedback, str | None, tuple[str, ...]], ...] = (
            (the_list.PageFeedback("request", "Words"), None, ()),
            (the_list.PageFeedback("request", "Words", "sent"), None, ()),
            (the_list.PageFeedback("request", "Words", action="read", recipient=DWIGHT.id), None, ()),
            (the_list.PageFeedback("request", "Words", action="read", recipient=MORTY.id), None, ()),
            (the_list.PageFeedback("request", "Words", "alerted", "sdlc"), "not sent to morty; sdlc told", ("morty", "sdlc")),
            (the_list.PageFeedback("request", "Words", "alerted", "<unknown>"), "not sent to morty; <unknown> told", ("morty",)),
            (the_list.PageFeedback("request", "Words", "alerted"), "not sent to morty; no showrunner for alert", ("morty",)),
        )
        for feedback, text, colored in cases:
            with (self.subTest(state=feedback.state, action=feedback.action, alerted_to=feedback.alerted_to),
                  patch.object(the_list, "feedback_notice_time", return_value=NOW),
                  patch.object(the_list, "feedback_requests", return_value={feedback.request: feedback})):
                body = list_page.render(self.root / "reviews").decode()
                cell = body.split('<td data-label="Update">', 1)[1].split('</td>', 1)[0]
                notice = ElementTree.fromstring('<root>' + cell + '</root>').find('small')
                if text is None:
                    self.assertIsNone(notice)
                    continue
                assert notice is not None
                self.assertEqual(''.join(notice.itertext()), text + ", 13:00 EST")
                self.assertEqual(tuple(span.text for span in notice.findall('span')), colored)

    def test_assigned_producer_receives_and_answers_a_legacy_list_question(self) -> None:
        status = change_page.DeliveryStatus(self.root / "reviews")
        keeper = staff.StaffId("legacy-keeper")
        for recover in (False, True):
            with self.subTest(recover=recover):
                item = self.add("Check the legacy List item")
                item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
                legacy = the_list.folder(keeper)
                legacy.mkdir(parents=True, exist_ok=True)
                source = legacy / f"{item.id}.md"
                _ = (the_list.folder(MORTY.id) / source.name).rename(source)
                request, token = list_page.signed_item_request(
                    list_page.ListRecord(keeper, item), list_page.form_secret(status.root))
                with patch.object(session_pane, "session_place", return_value=HerdrPlace("native", "w1:p1")):
                    asked = list_page.submit(status.root, urlencode(dict(
                        keeper=keeper, id=item.id, request=request, token=token, action="question",
                        words="Which check passed?", until="")), status)
                assert isinstance(asked, list_page.ListQuestionDelivery)
                delivery = asked.delivery
                assert isinstance(delivery, change_page.PageQuestionSend)
                assert isinstance(delivery.job, change_page.QuestionJob)
                claimed = delivery.job.record
                self.assertEqual(claimed.recipient_id, DWIGHT.id)
                self.assertEqual(claimed.source.path, str(source.resolve()))
                path = status.path(delivery.page, delivery.row)
                with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
                    if recover:
                        with status.locked():
                            change_page.advance_question_attempt(path, claimed, replace(
                                claimed, state=delivery_state.QuestionSendState.READY,
                                owner=delivery_state.NoQuestionSendOwner()))
                        _ = delivery.job.release()
                        change_page.recover_review_messages(status)
                    else:
                        list_page.deliver(asked)
                with self.subTest(check="delivery"):
                    send.assert_called_once_with(DWIGHT.id, claimed.message, claimed.message_id)
                    self.assertIs(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)
                command = claimed.message.split("Answer with this command: ", 1)[1].splitlines()[0]
                arguments = ["The smoke test passed." if word == "<answer>" else word
                             for word in shlex.split(command)[2:]]
                with (patch.object(staff, "caller_identity", return_value=DWIGHT.current_session),
                      redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors):
                    result = the_list.main(arguments)
                self.assertEqual(result, 0, errors.getvalue())
                answered = the_list.load(keeper, item.id)
                answer = answered.questions[0].answer
                assert isinstance(answer, question_record.Answer)
                self.assertEqual(answer.text, "The smoke test passed.")
                self.assertEqual(answered.log[-1].source, DWIGHT.name)
                self.assertEqual(answered.unread_since, NOW)

    def test_list_question_delivery_accepts_a_keeper_folder_symlink(self) -> None:
        status = change_page.DeliveryStatus(self.root / "reviews")
        legacy_keeper = staff.StaffId("legacy-keeper")
        for keeper in (MORTY.id, legacy_keeper):
            folder = the_list.folder(keeper)
            folder.parent.mkdir(parents=True, exist_ok=True)
            external = self.root / ("external-" + keeper)
            external.mkdir()
            folder.symlink_to(external, target_is_directory=True)
        for mode in ("direct", "recovery", "old recovery"):
            with self.subTest(mode=mode):
                keeper = MORTY.id if mode == "old recovery" else legacy_keeper
                item = self.add("Check the linked List folder")
                item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
                source = the_list.folder(keeper) / f"{item.id}.md"
                if keeper != MORTY.id:
                    _ = (the_list.folder(MORTY.id) / source.name).rename(source)
                request, token = list_page.signed_item_request(
                    list_page.ListRecord(keeper, item), list_page.form_secret(status.root))
                with patch.object(session_pane, "session_place", return_value=HerdrPlace("native", "w1:p1")):
                    asked = list_page.submit(status.root, urlencode(dict(
                        keeper=keeper, id=item.id, request=request, token=token, action="question",
                        words="Which check passed?", until="")), status)
                assert isinstance(asked, list_page.ListQuestionDelivery)
                delivery = asked.delivery
                assert isinstance(delivery, change_page.PageQuestionSend)
                assert isinstance(delivery.job, change_page.QuestionJob)
                claimed = delivery.job.record
                self.assertEqual(claimed.source.path, str(source.resolve()))
                self.assertEqual(claimed.recipient_id, DWIGHT.id)
                path = status.path(delivery.page, delivery.row)
                with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
                    if mode == "direct":
                        list_page.deliver(asked)
                    else:
                        with status.locked():
                            change_page.advance_question_attempt(path, claimed, replace(
                                claimed, state=delivery_state.QuestionSendState.READY,
                                owner=delivery_state.NoQuestionSendOwner()))
                            if mode == "old recovery":
                                fields = delivery_state.read_fields(path)
                                attempt = cast(dict[str, object], fields["question_attempt"])
                                saved_source = cast(dict[str, object], attempt["source"])
                                _ = saved_source.pop("keeper", None)
                                _ = path.write_text(json.dumps(fields) + "\n")
                        _ = delivery.job.release()
                        change_page.recover_review_messages(change_page.DeliveryStatus(status.root))
                send.assert_called_once_with(DWIGHT.id, claimed.message, claimed.message_id)
                delivered = change_page.read_question_attempt(path)
                self.assertIs(delivered.state, delivery_state.QuestionSendState.SENT)
                self.assertEqual(delivered.message_id, claimed.message_id)
                fields = delivery_state.read_fields(path)
                attempt = cast(dict[str, object], fields["question_attempt"])
                saved_source = cast(dict[str, object], attempt["source"])
                if mode == "old recovery":
                    self.assertNotIn("keeper", saved_source)
                else:
                    self.assertEqual(saved_source["keeper"], keeper)

    def test_restart_recovers_an_old_question_through_mortys_list_link(self) -> None:
        status = change_page.DeliveryStatus(self.root / "reviews")
        folder = the_list.folder(MORTY.id)
        linked = the_list.folder(OTHER.id)
        linked.mkdir(parents=True)
        folder.parent.mkdir(parents=True)
        folder.symlink_to(linked, target_is_directory=True)
        item = self.add("Check the linked List folder")
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        with patch.object(session_pane, "session_place", return_value=HerdrPlace("native", "w1:p1")):
            asked = list_page.submit(status.root, urlencode(self.fields(
                item, "question", words="Which check passed?")), status)
        assert isinstance(asked, list_page.ListQuestionDelivery)
        delivery = asked.delivery
        assert isinstance(delivery, change_page.PageQuestionSend)
        assert isinstance(delivery.job, change_page.QuestionJob)
        claimed = delivery.job.record
        self.assertEqual(claimed.source.path, str(linked / f"{item.id}.md"))
        self.assertIn(f" --keeper {MORTY.id} ", claimed.message)
        path = status.path(delivery.page, delivery.row)
        with status.locked():
            change_page.advance_question_attempt(path, claimed, replace(
                claimed, source=replace(claimed.source, keeper=delivery_state.NoSavedListKeeper()),
                state=delivery_state.QuestionSendState.READY, owner=delivery_state.NoQuestionSendOwner()))
        _ = delivery.job.release()

        with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
            change_page.recover_review_messages(change_page.DeliveryStatus(status.root))
        send.assert_called_once_with(DWIGHT.id, claimed.message, claimed.message_id)
        self.assertIs(change_page.read_question_attempt(path).state, delivery_state.QuestionSendState.SENT)

    def test_item_action_notices_go_to_the_keeper_when_the_producer_differs(self) -> None:
        root = self.root / "reviews"
        item = self.add("Check the notice recipient")
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        for action, until in (("park", "2026-12-03"), ("unpark", ""), ("remove", "")):
            with self.subTest(action=action):
                fields = self.fields(item, action, until=until)
                with patch.object(change_page, "send_to_staff") as send:
                    list_page.deliver(list_page.submit(root, urlencode(fields)))
                item = the_list.load(MORTY.id, item.id)
                notice = the_list.feedback_requests(item)[fields["request"]]
                send.assert_called_once()
                self.assertEqual(send.call_args.args[0], MORTY.id)
                self.assertEqual(notice.recipient_id(MORTY.id), MORTY.id)
                self.assertEqual(notice.state, "sent")

    def test_signed_actions_use_the_keeper_of_each_visible_item(self) -> None:
        root = self.root / "reviews"
        for keeper in (DWIGHT.id, OTHER.id, staff.StaffId("missing-keeper")):
            with self.subTest(keeper=keeper):
                # Only morty's folder allocates numbers, so move the new record into the legacy keeper's folder.
                # A legacy record names its keeper as the producer.
                item = the_list.add(MORTY.id, "Read the legacy List item", owner=keeper, source="nate", changes={})
                legacy = the_list.folder(keeper)
                legacy.mkdir(parents=True, exist_ok=True)
                created = the_list.folder(MORTY.id) / f"{item.id}.md"
                _ = (legacy / f"{item.id}.md").write_text(created.read_text().replace(MORTY.id, keeper))
                created.unlink()
                markup = list_page.render(root, include_counts=False).decode()
                self.assertIn(f'data-key="{keeper}/{item.id}"', markup)
                member = staff.local_snapshot().by_id(keeper)
                keeper_name = member.name if member is not None else keeper
                self.assertIn(f'Tell {keeper_name}</button>', markup)

                def submit(action: str, *, words: str = "", until: str = "") -> list_page.ListActionSubmission:
                    current = the_list.load(keeper, item.id)
                    request, token = list_page.signed_item_request(
                        list_page.ListRecord(keeper, current), list_page.form_secret(root))
                    return list_page.submit(root, urlencode(dict(
                        keeper=keeper, id=item.id, request=request, token=token, action=action, words=words,
                        until=until)))

                self.assertIsInstance(submit("read"), change_page.NoMessageDelivery)
                self.assertIs(the_list.load(keeper, item.id).unread_since, the_list.NoTimestamp.UNSET)
                told = submit("tell", words="Read this result.")
                assert isinstance(told, list_page.ListFeedbackDelivery)
                self.assertEqual(told.keeper, keeper)
                parked = submit("park", until="2026-12-03")
                assert isinstance(parked, list_page.ListFeedbackDelivery)
                self.assertEqual(parked.keeper, keeper)
                unparked = submit("unpark")
                assert isinstance(unparked, list_page.ListUnparkSubmission)
                self.assertIs(unparked.outcome, list_page.UnparkOutcome.CLEARED)
                asked = submit("question", words="Which check passed?")
                assert isinstance(asked, list_page.ListQuestionDelivery)
                self.assertEqual(asked.delivery.recipients, (keeper,))
                self.assertEqual(the_list.load(keeper, item.id).questions[0].words, "Which check passed?")
                if member is not None and member.kind == "producer":
                    with patch.object(change_page, "send_question", return_value=question_outcome()) as send:
                        list_page.deliver(asked)
                    attempt = change_page.read_question_attempt(asked.status.path(asked.delivery.page, asked.delivery.row))
                    send.assert_called_once_with(keeper, attempt.message, attempt.message_id)
                    command = attempt.message.split("Answer with this command: ", 1)[1].splitlines()[0]
                    arguments = ["The smoke test passed." if word == "<answer>" else word
                                 for word in shlex.split(command)[2:]]
                    with (patch.object(staff, "caller_identity", return_value=member.current_session),
                          redirect_stdout(io.StringIO())):
                        self.assertEqual(the_list.main(arguments), 0)
                    answered = the_list.load(keeper, item.id)
                    answer = answered.questions[0].answer
                    assert isinstance(answer, question_record.Answer)
                    self.assertEqual(answer.text, "The smoke test passed.")
                    self.assertEqual(answered.log[-1].source, member.name)
                    self.assertEqual(answered.unread_since, NOW)
                removed = submit("remove")
                assert isinstance(removed, list_page.ListFeedbackDelivery)
                self.assertEqual(removed.keeper, keeper)
                self.assertIs(the_list.load(keeper, item.id).state, the_list.ItemState.CLOSED)

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
                    ("park", {"until": "2026-12-03"}, "Studio: nate parked item 106 until 2026-12-03."),
                    ("tell", {"words": "Explain this simply.\nKeep these words."}, "Explain this simply.\nKeep these words."),
                    ("remove", {}, "Studio: nate removed item 106.")):
                with self.subTest(action=action):
                    fields = self.fields(item, action, **extra)
                    delivery = list_page.submit(root, urlencode(fields))
                    item = the_list.load(MORTY.id, item.id)
                    feedback = the_list.feedback_requests(item)[fields["request"]]
                    self.assertEqual(feedback.words, words)
                    if action == "park":
                        self.assertEqual(item.parked_until, the_list.time_value("2026-12-03"))
                    if action == "tell":
                        self.assertEqual(item.log[-1].what, the_list.MarkedRead())
                    self.assertEqual(item.log[-2 if action == "tell" else -1].what, feedback)
                    self.assertEqual(the_list.feedback_notice_time(item), NOW)
                    body = list_page.render(root).decode()
                    if action == "remove":
                        self.assertNotIn(f'data-key="{MORTY.id}/{item.id}"', body)
                    else:
                        update = body.split('<td data-label="Update">', 1)[1].split('</td>', 1)[0]
                        self.assertNotIn('<small>', update)
                        self.assertIn('data-feedback-form', body)
                        log = body.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]
                        self.assertIn("Feedback for morty", without_role_colors(log))
                        self.assertIn(html.escape(words.splitlines()[0]), log)
                        self.assertNotIn(fields["request"], log)
                    list_page.deliver(delivery)
                    self.assertEqual(send.call_args.args[0], MORTY.id)
                    expected = "Studio: nate's words for item 106\n" + words if action == "tell" else words
                    self.assertEqual(send.call_args.args[2], expected)
                    item = the_list.load(MORTY.id, item.id)
                    if action != "remove":
                        body = list_page.render(root).decode()
                        update = body.split('<td data-label="Update">', 1)[1].split('</td>', 1)[0]
                        self.assertNotIn('<small>', update)
                        log = body.split('<ol class="list-log">', 1)[1].split("</ol>", 1)[0]
                        self.assertIn("Feedback sent to morty", without_role_colors(log))
                    before = send.call_count, item.log
                    list_page.deliver(list_page.submit(root, urlencode(fields)))
                    self.assertEqual((send.call_count, the_list.load(MORTY.id, item.id).log), before)
            self.assertEqual(item.state, the_list.ItemState.CLOSED)
            self.assertIs(item.parked_until, the_list.NoTimestamp.UNSET)
            self.assertEqual((item.title, item.owner, item.next_step), (initial.title, initial.owner, initial.next_step))
            _ = the_list.reopen(MORTY.id, item.id, source="morty")
            _ = the_list.update(MORTY.id, item.id, {"latest_status": "Morty acts."}, source="morty")
            good = self.fields(item, "tell", words="Keep it.")
            for body in (urlencode({**good, "token": "bad"}), urlencode(good) + '&id=L2',
                         urlencode({**good, "words": ""}), urlencode({**good, "words": " \t\n "}),
                         urlencode({**good, "action": "unknown"}),
                         urlencode({**good, "action": "park", "until": "2026-02-30"})):
                with self.subTest(body=body), self.assertRaises(ValueError):
                    _ = list_page.submit(root, body)

    def test_tell_accepts_a_stored_producer_id_with_a_period(self) -> None:
        root = self.root / "reviews"
        producer = staff.StaffId("producer." + "a" * 32)
        item = self.add("The assigned item")
        _ = the_list.update(MORTY.id, item.id, {"producer": producer}, source="morty")
        stored = the_list.load(MORTY.id, item.id)
        self.assertEqual(stored.producer, producer)
        fields = self.fields(stored, "tell", words="Keep these words.")
        for suffix in ("", "." + "b" * 32):
            with self.subTest(suffix=suffix):
                request = fields["request"] + suffix
                delivery = list_page.submit(root, urlencode({**fields, "request": request}))
                self.assertEqual(delivery, list_page.ListFeedbackDelivery(MORTY.id, item.id, request))
                saved = the_list.feedback_requests(the_list.load(MORTY.id, item.id))[request]
                self.assertEqual(saved.recipient, producer)

    def test_tell_reloads_and_delivers_for_a_stored_producer_id_with_a_space(self) -> None:
        root = self.root / "reviews"
        producer = staff.StaffId("old producer")
        item = self.add("The assigned item")
        item = the_list.update(MORTY.id, item.id, {"producer": producer}, source="morty")
        fields = self.fields(item, "tell", words="Keep these words.")
        delivery = list_page.submit(root, urlencode(fields))
        request = fields["request"]
        pending = the_list.feedback_requests(the_list.load(MORTY.id, item.id))[request]
        self.assertEqual((pending.state, pending.recipient), ("pending", producer))
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(delivery)
            send.assert_called_once()
            self.assertEqual(send.call_args.args,
                             (producer, change_page.SENDER, f"Studio: nate's words for item {item.number}\nKeep these words."))
        saved = the_list.feedback_requests(the_list.load(MORTY.id, item.id))[request]
        self.assertEqual((saved.state, saved.recipient, saved.words), ("sent", producer, "Keep these words."))

    def test_tell_uses_one_request_for_equivalent_recipient_encodings(self) -> None:
        root = self.root / "reviews"
        producer = staff.StaffId("old.producer")
        item = self.add("The assigned item")
        item = the_list.update(MORTY.id, item.id, {"producer": producer}, source="morty")
        fields = self.fields(item, "tell", words="Keep these words.")
        request = fields["request"] + "." + "b" * 32
        alternate = request.replace("%2E", "%2e")
        self.assertNotEqual(alternate, request)
        first = list_page.submit(root, urlencode({**fields, "request": alternate}))
        second = list_page.submit(root, urlencode({**fields, "request": request}))
        self.assertEqual(first, list_page.ListFeedbackDelivery(MORTY.id, item.id, request))
        self.assertEqual(first, second)
        feedback = the_list.feedback_requests(the_list.load(MORTY.id, item.id))
        self.assertEqual(list(feedback), [request])
        self.assertEqual(feedback[request].recipient, producer)

    def test_tell_uses_the_signed_recipients_current_session_after_reassignment(self) -> None:
        root = self.root / "reviews"
        item = self.add("The assigned item")
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        body = list_page.render(root, include_counts=False).decode()
        self.assertIn('>Tell dwight</button>', body)
        self.assertIn('placeholder="Your words for dwight about this item"', body)
        fields = self.fields(item, "tell", words="Keep these words.")
        _ = the_list.update(MORTY.id, item.id, {"producer": MORTY.id}, source="morty")
        changed_request = fields["request"].replace(DWIGHT.id, MORTY.id)
        with self.assertRaisesRegex(ValueError, "the List token does not match this page"):
            _ = list_page.submit(root, urlencode({**fields, "request": changed_request}))
        delivery = list_page.submit(root, urlencode(fields))
        moved = replace(DWIGHT, current_session=staff.StaffId("083debfa-b0a3-460e-807a-92b45a8692eb"))
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            row.to_json() for row in (MORTY, moved, OWNER, OTHER)]}))
        with patch.object(change_page, "send_message") as send:
            list_page.deliver(delivery)
            send.assert_called_once()
            self.assertEqual(send.call_args.args,
                             (f"session:{moved.current_session}", change_page.SENDER,
                              f"Studio: nate's words for item {item.number}\nKeep these words."))
            self.assertEqual(send.call_args.kwargs["recipient_key"], DWIGHT.id)
        saved = the_list.feedback_requests(the_list.load(MORTY.id, item.id))[fields["request"]]
        self.assertEqual((saved.state, saved.words, saved.recipient), ("sent", "Keep these words.", DWIGHT.id))
        body = without_role_colors(list_page.render(root, include_counts=False).decode())
        self.assertIn('>Tell morty</button>', body)
        self.assertIn('Feedback sent to dwight', body)
        update = body.split('<td data-label="Update">', 1)[1].split('</td>', 1)[0]
        self.assertNotIn('<small>', update)

    def test_tell_log_keeps_the_recipient_after_reassignment(self) -> None:
        root = self.root / "reviews"
        item = self.add("The assigned item")
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        fields = self.fields(item, "tell", words="Keep these words.")
        with patch.object(change_page, "send_to_staff"):
            list_page.deliver(list_page.submit(root, urlencode(fields)))
        _ = the_list.update(MORTY.id, item.id, {"producer": MORTY.id}, source="morty")
        body = without_role_colors(list_page.render(root, include_counts=False).decode())
        self.assertIn('>Tell morty</button>', body)
        self.assertIn('Feedback sent to dwight', body)
        self.assertNotIn('Feedback sent to morty', body)

    def test_restart_recovers_legacy_tell_to_the_keeper(self) -> None:
        root = self.root / "reviews"
        item = self.add("The assigned item")
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        request = uuid4().hex
        _ = the_list.save_feedback(MORTY.id, item.id, the_list.PageFeedback(request, "Recover these words."))
        with patch.object(change_page, "send_to_staff") as send:
            list_page.recover_pending(root)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0], MORTY.id)
            self.assertEqual(send.call_args.args[2], f"List page: nate's words for item {item.number}\nRecover these words.")
        feedback = the_list.feedback_requests(the_list.load(MORTY.id, item.id))[request]
        self.assertEqual((feedback.state, feedback.recipient_id(MORTY.id)), ("sent", MORTY.id))
        body = without_role_colors(list_page.render(root, include_counts=False).decode())
        self.assertIn('Feedback sent to morty', body)

    def test_tell_keeps_words_and_alerts_the_showrunner_if_the_producer_is_missing(self) -> None:
        root = self.root / "reviews"
        item = self.add("The assigned item")
        item = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        _ = (self.root / "staff.json").write_text(json.dumps({"schema": 1, "rows": [
            row.to_json() for row in (MORTY, OWNER, OTHER)]}))
        fields = self.fields(item, "tell", words="Keep these words.")
        delivery = list_page.submit(root, urlencode(fields))
        with patch.object(change_page, "send_message") as send, redirect_stderr(io.StringIO()):
            list_page.deliver(delivery)
            send.assert_called_once()
            self.assertEqual(send.call_args.args[0], f"session:{OWNER.current_session}")
            alert = cast(str, send.call_args.args[2])
            self.assertIn("List feedback needs attention", alert)
            self.assertIn("Keep these words.", alert)
        saved = the_list.feedback_requests(the_list.load(MORTY.id, item.id))[fields["request"]]
        self.assertEqual((saved.state, saved.words, saved.alerted_to), ("alerted", "Keep these words.", OWNER.name))

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

        root = self.root / "reviews"
        for keeper, before, after in (
            (MORTY.id, the_list.ItemState.WAITING_FOR_NATE, the_list.ItemState.WORKING),
            (DWIGHT.id, the_list.ItemState.DELIVERED, the_list.ItemState.WORKING),
            (MORTY.id, the_list.ItemState.BLOCKED, the_list.ItemState.BLOCKED),
        ):
            with self.subTest(keeper=keeper, state=before):
                item = self.add("Check the Tell state", before)
                if keeper != MORTY.id:
                    legacy = the_list.folder(keeper)
                    legacy.mkdir(parents=True)
                    _ = (the_list.folder(MORTY.id) / f"{item.id}.md").rename(legacy / f"{item.id}.md")
                request, token = list_page.signed_item_request(
                    list_page.ListRecord(keeper, item), list_page.form_secret(root))
                body = urlencode(dict(keeper=keeper, id=item.id, request=request, token=token,
                                      action="tell", words="Keep this state change.", until=""))
                delivery, state = list_page.save_action(root, body, include_detail=True)
                assert state is not None
                saved = the_list.load(keeper, item.id)
                self.assertIs(saved.state, after)
                self.assertEqual((state["item_state"], state["unread"]), (after.value, "false"))
                self.assertEqual(saved.questions, ())
                self.assertEqual(delivery, list_page.ListFeedbackDelivery(keeper, item.id, request))
                self.assertEqual(the_list.feedback_requests(saved)[request].words, "Keep this state change.")
                _ = list_page.submit(root, body)
                self.assertEqual(the_list.load(keeper, item.id), saved)

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
        item = self.add("Check the update")
        _ = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        with patch.object(list_page, "counts_strip", return_value=""):
            markup = list_page.render(self.root / "reviews").decode()
        setup = '''<script>
window.fetch=async(_path,options={})=>{
 if(options.method!=='POST') throw new Error('Fixture refresh refused');
 return {headers:new Headers(),ok:true,json:async()=>({unread:'UNREAD'})};
};
</script>'''
        probe = '''<script>
(async()=>{
 const row=document.querySelector('[data-list-item]');
 document.getElementById('list-unread').click();
 row.querySelector('.list-toggle').click();
 const form=recordDetails(row).querySelector('form');
 const before=row.dataset.unread==='true' && row.querySelector('.list-dot') && form.querySelector('[value="read"]') && !row.hidden;
 form.elements.words.value='Keep this result.';form.elements.words.dispatchEvent(new Event('input'));
 form.requestSubmit(form.querySelector('[value="ACTION"]'));
 await new Promise(resolve=>setTimeout(resolve,0));
 const cleared=row.dataset.unread==='false' && !row.querySelector('.list-dot') && !form.querySelector('[value="read"]') && row.hidden;
 const retained=row.dataset.unread==='true' && row.querySelector('.list-dot') && form.querySelector('[value="read"]') && !row.hidden;
 const after=EXPECTED_UNREAD ? retained : cleared;
 const notice=form.querySelector('[data-send-status]').textContent;
 const result=document.createElement('p');result.id='probe-result';
 result.textContent=before && after && notice==='NOTICE' ? 'PASS' : 'FAIL';document.body.append(result);
})();
</script>'''
        for action, unread, notice in (("read", "false", ""), ("read", "true", ""),
                                      ("tell", "false", "sent to dwight"),
                                      ("tell", "true", "sent to dwight; update remains unread")):
            with self.subTest(action=action, unread=unread):
                page = self.root / f"{action}-{unread}.html"
                _ = page.write_text(markup.replace('<head>', '<head>' + setup.replace('UNREAD', unread), 1).replace('</body>',
                    probe.replace('ACTION', action).replace('EXPECTED_UNREAD', unread).replace('NOTICE', notice) + '</body>'))
                result = self.chrome().dom(page, "probe-result", reduced_motion=False, window_size=None)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue('<p id="probe-result">PASS</p>' in result.stdout,
                                f"The {action} action must show the saved unread state and apply the Unread filter.")

        with self.subTest(action="tell", view="needs-me"):
            item = self.add("Check the waiting result", the_list.ItemState.WAITING_FOR_NATE)
            browser = self.chrome()
            root = self.root / "reviews"
            with (patch.object(serve, "tailscale_ip", return_value="127.0.0.1"),
                  patch.object(change_page, "send_to_staff", return_value=None),
                  serve.ReviewLinksServer(("127.0.0.1", 0), root) as server):
                thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
                thread.start()
                try:
                    browser.navigate(f"http://127.0.0.1:{server.server_port}{studio_page.PAGE_PATH}#show=needs-me")
                    result = browser.command("Runtime.evaluate", {
                        "expression": r"""(async () => {
 const row=document.querySelector('[data-number="ITEM_NUMBER"]');
 const chip=document.getElementById('filter-show-needs-me');
 const before=[row.hidden,Number(chip.dataset.filterCount)];
 row.querySelector('.list-toggle').click();
 const form=recordDetails(row).querySelector('form');
 const words=form.elements.words;
 words.value='Use the waiting result.';
 words.dispatchEvent(new Event('input',{bubbles:true}));
 const finished=new Promise(resolve=>{
  const observer=new MutationObserver(()=>{
   if(form.dataset.sending) return;
   observer.disconnect();
   resolve();
  });
  observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
 });
 form.querySelector('[value="tell"]').click();
 if(!form.dataset.sending) throw new Error('Tell did not submit.');
 await finished;
 return {before,after:[row.hidden,Number(chip.dataset.filterCount),row.dataset.state],
  sameRow:row===document.querySelector('[data-number="ITEM_NUMBER"]')};
})()""".replace("ITEM_NUMBER", str(item.number)),
                        "awaitPromise": True, "returnByValue": True}, browser.session)
                    self.assertNotIn("exceptionDetails", result)
                    value = cast(dict[str, object], result["result"])
                    self.assertEqual(value.get("value"), {
                        "before": [False, 1], "after": [True, 0, "Working"], "sameRow": True})
                    self.assertIs(the_list.load(MORTY.id, item.id).state, the_list.ItemState.WORKING)
                finally:
                    server.shutdown()
                    thread.join()

    @unittest.skipUnless(shutil.which("google-chrome"), "Chrome is required for this browser test")
    def test_browser_tell_uses_the_producer_in_the_saved_form(self) -> None:
        root = self.root / "reviews"
        item = self.add("Check the reassigned item")
        markup = list_page.render(root, include_counts=False).decode()
        read = self.fields(item, "read")
        _ = the_list.update(MORTY.id, item.id, {"producer": DWIGHT.id}, source="morty")
        _ = list_page.submit(root, urlencode(read))
        saved = list_page.saved_action_state(MORTY.id, item.id, include_detail=True)
        steps = r'''
 const saved=SAVED_STATE;
 const template=document.createElement('template');
 template.innerHTML=saved.detail_html;
 const signed=template.content.querySelector('[data-feedback-form]').elements;
 const listFetch=window.fetch,holds=[];
 window.fetch=(path,options={})=>{
  if(options.method==='POST') holds.push(new Headers(options.headers).get('X-Undo-Hold'));
  return listFetch(path,options);
 };
 check(form.querySelector('[value="tell"]').textContent==='Tell morty',
  'The original form names morty.');
 submit('read');finishPost(saved);await flush();
 const current=recordDetails(row).querySelector('[data-feedback-form]');
 check(current!==form,'Mark read installs the saved form.');
 check(current.querySelector('[value="tell"]').textContent==='Tell dwight',
  'The Tell button names dwight.');
 const words=current.elements.words,label='Your words for dwight about this item';
 check(words.placeholder===label && words.getAttribute('aria-label')===label,
  'The text box names dwight.');
 words.value='Check this result.';words.dispatchEvent(new Event('input'));
 const tell=submit('tell',current);
 check(holds.length===2 && holds[0]===null && holds[1]==='1',
  'With Undo off, Mark read sends directly and Tell requests a hold.');
 check(tell.id===signed.request.value && tell.id.endsWith('~'+RECIPIENT_ID) && tell.token===signed.token.value,
  'Tell uses the request and token for dwight.');
 finishPost({unread:'false'});await flush();
 check(current.querySelector('[data-send-status]').textContent==='sent to dwight',
  'The success message names dwight.');
'''
        steps = steps.replace("SAVED_STATE", json.dumps(saved)).replace("RECIPIENT_ID", json.dumps(DWIGHT.id))
        self.browser_action_sequence(steps, add_item=False, markup=markup, reduced_motion=True)

        refreshed = list_page.render(root, include_counts=False).decode()
        retry = r'''
 const words=form.elements.words,tell=form.querySelector('[value="tell"]'),label=tell.textContent;
 words.value='Retry the same words.';words.dispatchEvent(new Event('input'));
 const first=submit('tell');losePost();await flush();
 refreshMarkup=REFRESHED;
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await flush();
 const item=document.querySelector('[data-number="1"]');
 const current=recordDetails(item).querySelector('[data-feedback-form]');
 check(current!==form && current.querySelector('[value="tell"]').textContent===label,
       'The refreshed form keeps the server label for the signed Tell retry.');
 const second=submit('tell',current);
 check(second.id===first.id && second.token===first.token,'Tell keeps its original signed route.');
 finishPost({unread:'false'});await flush();
'''
        refresh_markup = json.dumps(refreshed).replace('</', '<\\/')
        self.browser_action_sequence(retry.replace('REFRESHED', refresh_markup), add_item=False,
                                     markup=markup, reduced_motion=True)

    def test_feedback_recovers_after_restart_and_alerts_after_failed_tries(self) -> None:
        root = self.root / "reviews"
        item = self.add("Recover the message")
        _ = list_page.submit(root, urlencode(self.fields(item, "tell", words="Ask sdlc for an ETA.")))
        saved = the_list.load(MORTY.id, item.id)
        with (patch.object(change_page, "send_to_staff") as send,
              patch.object(change_review, "lists_directory", return_value=self.root / "lists")):
            list_page.recover_pending(self.root / "reviews")
            send.assert_called_once()
            self.assertIn("Ask sdlc for an ETA.", cast(str, send.call_args.args[2]))
            list_page.recover_pending(self.root / "reviews")
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
            list_page.recover_pending(self.root / "reviews")
            list_page.deliver(delivery)
            self.assertEqual(send.call_count, tries + 1)
            alerted = the_list.load(MORTY.id, item.id)
            latest = tuple(the_list.feedback_requests(alerted).values())[-1]
            self.assertEqual((latest.state, latest.alerted_to), ("alerted", "sdlc"))
            self.assertNotIn("not sent to morty; sdlc told", without_role_colors(list_page.render(root).decode()))
            self.assertEqual(the_list.mark_read(MORTY.id, item.id, source="morty"), alerted)
            self.assertNotIn("not sent to morty; sdlc told", without_role_colors(list_page.render(root).decode()))
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
            self.assertNotIn("not sent to morty; sdlc told", without_role_colors(list_page.render(root).decode()))
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            self.assertEqual(send.call_count, 2 * (tries + 1))
            list_page.recover_pending(self.root / "reviews")
            self.assertEqual(send.call_count, 3 * (tries + 1))
            self.assertEqual(the_list.load(MORTY.id, item.id), pending)
        with patch.object(change_page, "send_to_staff") as send:
            list_page.deliver(list_page.submit(root, urlencode(fields)))
            send.assert_called_once()
            list_page.recover_pending(self.root / "reviews")
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
                self.assertEqual(isinstance(the_list.feedback_notice_time(saved), datetime), published)
                self.assertEqual(fields["request"] in the_list.feedback_requests(saved), published)
                with patch.object(change_page, "send_to_staff") as send:
                    if published:
                        list_page.deliver(list_page.submit(root, urlencode(fields)))
                        send.assert_called_once()
                    list_page.recover_pending(self.root / "reviews")
                    list_page.recover_pending(self.root / "reviews")
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
        self.assertIs(the_list.feedback_notice_time(current), the_list.NoTimestamp.UNSET)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_shared_page_status_uses_the_real_list_loader(self) -> None:
        _ = self.add("Check the List loader")

        def screenshot(arguments: list[str], **_options: object) -> subprocess.CompletedProcess[str]:
            path = Path(arguments[arguments.index("--screenshot") + 1])
            width, _, height = arguments[arguments.index("--window-size") + 1].partition(",")
            return self.firefox_view(arguments[-1], path, int(width), int(height))

        with patch.object(browser_test_support, "run_process_group", side_effect=screenshot):
            assert_status_states(self, list_page.render(self.root / "reviews", include_counts=False).decode(), 15)

    @unittest.skipUnless(shutil.which("firefox"), "Firefox is required for browser checks")
    def test_browser_sort_filters_hash_refresh_open_rows_and_phone_cards(self) -> None:
        if self._chrome is not None:
            self._chrome.close()
        _ = self.fixture_items()
        self.browser(list_page.render(self.root / "reviews").decode())

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
const rows=['owner','producer','show'].map(name=>document.querySelector('[data-filter-row="'+name+'"]'));
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
const rowLeft=row=>row.getBoundingClientRect().left;
check(rows.every(row=>Math.abs(rowLeft(row)-rowLeft(rows[0]))<1) && Math.abs(left(rows[2])-left(rows[0]))<1
 && Math.abs(left(rows[1])-rowLeft(rows[1]))<1,'The filter rows align and the picture tabs start at the row edge.');
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
const producerBounds=()=>rows[1].getBoundingClientRect();
const wordsBounds=()=>document.getElementById('list-search').getBoundingClientRect();
const producerBox=producerBounds(),words=wordsBounds();
check(innerWidth===VIEWPORT_WIDTH,'The page uses the requested viewport width.');
check(words.left>=producerBox.right || words.top>=producerBox.bottom,'The words box does not overlap the Producer tabs.');
if(innerWidth===1440){
 check(words.top>=producerBox.top && words.top<producerBox.bottom,'The words box shares the picture row.');
 check(Math.abs(words.top+words.bottom-producerBox.top-producerBox.bottom)<=2,
  'The words box is vertically centered with the pictures.');
}
const allChips=rows.slice(0,2).map(row=>row.querySelector('[data-filter-value="all"]'));
const staffChips=rows.slice(0,2).map(row=>row.querySelector('[data-filter-value="staff:all"]'));
const chipText=chip=>chip?.querySelector('.studio-name') ? chip.querySelector('.studio-name').textContent+' '+chip.querySelector('b').textContent : chip?.textContent;
check(allChips.every(chip=>chipText(chip)==='All 16') && staffChips.every(chip=>chipText(chip)==='all 1'),
 'Each staff row has distinct All and all chips with separate counts.');
check(ownerChips[2].textContent==='builds 1' && ownerChips[3].textContent==='builds-support 1',
 'The owner chips omit the suffix and sort by the displayed name.');
const matchesAllStaff=()=>[...document.querySelectorAll('[data-list-item]:not([hidden])')].map(row=>row.dataset.number).join()==='13';
staffChips.forEach(chip=>chip.click());
const band=document.querySelector('[data-top-band="none"]').getBoundingClientRect();
const search=wordsBounds();
check(band.height>0 && search.bottom<=band.top,'The words box stays above the visible Top priority band.');
check(Math.abs(search.right-band.right)<=1,'The words box and the Top priority band align at the right edge.');
if(innerWidth===390){
 check(search.top>=producerBounds().bottom && Math.abs(search.left-band.left)<=1,
  'The words box takes the full width below the tabs on a phone.');
}
const selectedPlace=new URLSearchParams(location.hash.slice(1));
check(matchesAllStaff() && selectedPlace.get('owner')==='all' && selectedPlace.get('producer')==='all',
 'Selecting the staff ID all filters the items and stores the raw IDs.');
allChips.forEach(chip=>chip.click());
const restored=new Promise(done=>addEventListener('hashchange',done,{once:true}));
location.hash='show=all&owner=all&producer=all';
await restored;
check(matchesAllStaff() && staffChips.every(chip=>chip.getAttribute('aria-pressed')==='true'),
 'An existing address with the staff ID all selects both staff chips.');
if(innerWidth===1440){
 const tabs=rows[1].querySelector('div');
 tabs.append(...Array.from({length:16},()=>document.createElement('button')));
 const bounds=producerBounds();
 check([...tabs.children].every(tab=>{
  const box=tab.getBoundingClientRect();
  return box.width===88 && box.left>=bounds.left && box.right<=bounds.right+1;
 }), 'Each tab keeps its full width when many tabs wrap.');
 check(wordsBounds().top>=bounds.bottom && document.documentElement.scrollWidth<=innerWidth,
  'Many tabs move the words box to the next line without horizontal scrolling.');
}
}catch(error){failures.push(String(error));}
const result=document.createElement('p');result.id='probe-results';
result.textContent=JSON.stringify(failures);document.body.append(result);
})();
</script>"""
        for width in (1700, 1440, 1280, 1024, 390):
            with self.subTest(width=width):
                self.browser_actions(markup, "", probe.replace("VIEWPORT_WIDTH", str(width)), window_size=(width, 900))

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
  const tab=chip.querySelector('.studio-name');
  const text=tab ? tab.textContent+' '+chip.querySelector('b').textContent : chip.textContent;
  if(text!==label+' 1') failures.push('The '+name+' chip shows '+label+' and its count.');
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

    def browser(self, markup: str) -> None:
        setup = r'''<script>
const nativeFetch=fetch, nativeTimeout=setTimeout, errors=[], reads=[],posts=[];
let original=MARKUP,holdPost=false,finishPost,failRefresh=false,realFeedback=false,lostReplies=0;
window.setInterval=()=>0;window.clearInterval=()=>{};
window.fetch=async(_path,options={})=>{
 if(options.method==='POST'){
  const fields=new URLSearchParams(options.body);posts.push(fields);
  if(realFeedback){
   const result=await nativeFetch('/feedback',options);
   if(lostReplies>0){lostReplies--;throw new Error('fixture lost response');}
   return result;
  }
  if(fields.get('action')==='read'){
   const page=new DOMParser().parseFromString(original,'text/html');
   const row=[...page.querySelectorAll('[data-list-item]')].find(row=>row.dataset.number==='106');
   row.dataset.unread='false';row.querySelector('.list-dot')?.remove();recordDetails(row).querySelector('[value="read"]')?.remove();
   original=page.documentElement.outerHTML;
  }
  const result={headers:new Headers(),ok:true,status:200,json:async()=>({state:'sent',unread:'false',
   ...(fields.get('action')==='remove' ? {item_state:'Closed'} : {})})};
  if(holdPost) return await new Promise(resolve=>finishPost=(reply=result)=>resolve(reply));
  return result;
 }
 reads.push(1);if(failRefresh) throw new Error('fixture refresh failed');
 if(realFeedback) return await nativeFetch('/fresh');
 return {ok:true,status:200,text:async()=>original};
};
addEventListener('error',event=>errors.push(event.message));
addEventListener('unhandledrejection',event=>errors.push(String(event.reason)));
</script>'''
        probe = r'''<script>
(async()=>{
const failed=[],check=(ok,text)=>{if(!ok) failed.push(text);};
const flush=()=>new Promise(resolve=>nativeTimeout(resolve,50));
const submitted=(form,button)=>new Promise((resolve,reject)=>{
 if(form.dataset.sending){reject(new Error('The test submission did not start.'));return;}
 const observer=new MutationObserver(()=>{
  if(!form.dataset.sending){observer.disconnect();resolve();}
 });
 observer.observe(form,{attributes:true,attributeFilter:['data-sending']});
 form.requestSubmit(button);
 if(!form.dataset.sending){observer.disconnect();reject(new Error('The test submission did not start.'));}
});
const until=async (ready,label)=>{
 const deadline=performance.now()+8000;
 while(!ready()){
  if(performance.now()>deadline) throw new Error('Timed out: '+label);
  await flush();
 }
};
const rows=()=>[...document.querySelectorAll('[data-list-item]')];
const visible=()=>rows().filter(row=>!row.hidden);
const shownNumbers=()=>visible().map(row=>row.dataset.number).join(',');
const refreshOrder=async label=>{
 const first=visible()[0],order=shownNumbers();
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await until(()=>!first.isConnected,'the table refresh');
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
 await flush();
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
 searchField().value='auto';
 dispatchEvent(new Event('pageshow'));await flush();
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
 const tab=pressed('producer');
 check(pressed('owner').textContent==='sdlc 1' && tab.querySelector('.studio-name').textContent==='morty'
  && tab.querySelector('b').textContent==='1','The selected staff controls show the names and counts.');
 for(const themeName of ['light','dark']){
  document.documentElement.dataset.theme=themeName;
  for(const view of ['working','blocked']){
   show(view);
   const producer=pressed('producer'),initial=producer.querySelector('.studio-initial');
   const scrim=getComputedStyle(producer,'::after').backgroundImage;
   check(getComputedStyle(producer).opacity==='1' && scrim.startsWith('linear-gradient(')
    && [...producer.querySelectorAll('.studio-name,b')].every(label=>getComputedStyle(label).color==='rgb(255, 255, 255)'),
    'The selected picture tab keeps its label and count over the scrim.');
   for(const control of [chip(view),pressed('owner'),initial]){
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
 check(recordIsOpen(selected),'a row opens');
 const words=recordDetails(selected).querySelector('textarea'),tell=recordDetails(selected).querySelector('[value="tell"]');
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
 words.focus();words.setSelectionRange(1,3);
 const before=rows();
 document.body.style.minHeight='2400px';scrollTo(0,100);
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));await flush();await flush();
 const refreshed=rows().find(row=>row.dataset.number==='106');
 check(reads.length===3,'Each shared status event requests a fresh page.');
 check(document.querySelector('#list-search').value==='console','refresh keeps the query');
 check(document.activeElement===recordDetails(refreshed).querySelector('textarea'),'refresh keeps focus');
 check(document.activeElement.selectionStart===1 && document.activeElement.selectionEnd===3,'refresh keeps the selection');
 check(recordIsOpen(refreshed),'Refresh keeps the open row.');
 check(recordDetails(refreshed).querySelector('textarea').value==='Keep my draft','refresh keeps feedback words');
 check(!recordDetails(refreshed).querySelector('[value="tell"]').disabled,'a restored draft enables Tell morty');
 check(scrollY===100,'refresh keeps scroll');
 check(refreshed===selected && rows().every(row=>row===selected || !before.includes(row)),
  'Refresh applies the snapshot and keeps the open row node.');
 const refusedForm=recordDetails(refreshed).querySelector('form');
 holdPost=true;
 const refused=submitted(refusedForm,refusedForm.querySelector('[value="tell"]'));
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 check(reads.length===3,'The shared status event makes no List request during a row save.');
 finishPost({headers:new Headers(),ok:false,status:409,json:async()=>({reason:'The row save was refused.'})});
 holdPost=false;finishPost=undefined;
 await refused;
 check(reads.length===3 && refreshed.isConnected,'A refused row save makes no List request after the event.');
 check(refreshed.dataset.unread==='true' && refusedForm.elements.words.value==='Keep my draft' &&
       refusedForm.querySelector('[data-send-status]').textContent==='Not saved: The row save was refused.',
       'A refusal restores the unread state and shows the reason beside the draft.');
 realFeedback=true;lostReplies=2;
 const tellRow=rows().find(row=>row.dataset.number==='105');tellRow.querySelector('.list-toggle').click();
 check(visible().length===2 && !recordIsOpen(refreshed) &&
  recordIsOpen(tellRow),'Opening the second List row closes the first row.');
 check(recordDetails(refreshed).querySelector('textarea').value==='Keep my draft','Closing the first row keeps its draft.');
 check(tellRow.dataset.unread==='true' && recordDetails(tellRow).querySelector('[value="read"]'),'Tell starts with a fresh unread row');
 let retryForm=recordDetails(tellRow).querySelector('form');
 retryForm.elements.words.value='Deliver this feedback once';retryForm.elements.words.dispatchEvent(new Event('input'));
 const retryId=retryForm.elements.request.value,retryToken=retryForm.elements.token.value,startPosts=posts.length;
 await submitted(retryForm,retryForm.querySelector('[value="tell"]'));
 const beforeRetryRefresh=document.querySelector('[data-list]');
 dispatchEvent(new MessageEvent('message',{source:parent,origin:location.origin,data:'console-shown'}));
 await until(()=>!beforeRetryRefresh.isConnected,'the refreshed List after the lost response');
 retryForm=recordDetails(rows().find(row=>row.dataset.number==='105')).querySelector('form');
 check(retryForm.elements.request.value===retryId && retryForm.elements.token.value===retryToken,'refresh keeps the failed request identity');
 await submitted(retryForm,retryForm.querySelector('[value="tell"]'));
 check(posts.length===startPosts+3,'the lost response has one transport retry and one manual retry');
 check(posts.slice(startPosts).every(post=>post.get('request')===retryId && post.get('token')===retryToken),
       'each attempt uses the original signature');
 retryForm=recordDetails(rows().find(row=>row.dataset.number==='105')).querySelector('form');
 check(retryForm.elements.request.value!==retryId,'success clears the saved retry identity');
 check(retryForm.elements.words.value==='' && retryForm.querySelector('[value="tell"]').disabled,'a successful send clears the box and disables Tell morty');
 realFeedback=false;failRefresh=true;
 rows().find(row=>row.dataset.number==='106').querySelector('.list-toggle').click();
 const requestIds=[];
 for(const action of ['read','park','tell','remove']){
  const row=rows().find(row=>row.dataset.number==='106');
  if(!recordIsOpen(row)) row.querySelector('.list-toggle').click();
  const form=recordDetails(row).querySelector('form');
  form.elements.until.value='2026-12-03';form.elements.words.value='Explain this simply';
  requestIds.push(form.elements.request.value);
  holdPost=action==='tell';
  form.requestSubmit(form.querySelector('[value="'+action+'"]'));
  if(holdPost){await until(()=>typeof finishPost==='function','the held response');
   form.elements.words.value='New draft during send';form.elements.words.dispatchEvent(new Event('input'));
   check(form.querySelector('[value="tell"]').disabled,'editing words during delivery keeps Tell morty disabled');finishPost();}
  check(form.dataset.sending && !form.querySelector('[data-send-status]').textContent,
   'The pending action disables submission without a status message: '+action);
  await until(()=>!form.dataset.sending,'the '+action+' response');
  check(posts.at(-1).get('action')===action && posts.at(-1).get('id')==='L106','signed feedback '+action);
  check(posts.at(-1).get('token').length===64,'the form carries its token');
  check(form.elements.request.value!==requestIds.at(-1),'success renews the request without refresh');
  const motions=[...document.querySelectorAll('[data-record-motion]')].flatMap(node=>node.getAnimations({subtree:true}));
  await Promise.allSettled(motions.map(animation=>animation.finished));
  if(action==='tell'){
   check(form.elements.words.value==='New draft during send','a newer draft survives the send');
   check(!form.querySelector('[value="tell"]').disabled,'the newer draft enables Tell morty after delivery');
  }
  const savedStatus=form.querySelector('[data-send-status]').textContent;
  if(action==='tell') check(savedStatus==='sent to morty','The saved Tell names its producer: '+savedStatus);
  else check(!savedStatus,'The saved action shows no status message: '+action);
  if(action==='remove' || action==='park'){
   check(!recordIsOpen(row) && row.querySelector('[data-detail]').hidden,
    'The saved action collapses the row: '+action);
   if(action==='remove') check(row.hidden,'Remove hides the item after the save.');
  }else check(recordIsOpen(row) &&
   !recordIsOpen(rows().find(row=>row.dataset.number==='105')),
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
 location.hash=new URLSearchParams({show:'closed',sort:'#',dir:'desc'});await flush();
 check(visible().map(row=>row.dataset.number).join(',')==='106,105,63,58,41' && selectedShow()==='all',
  'An old Closed address opens All in the requested sort order.');
 location.hash=new URLSearchParams({show:'done'});await flush();
 check(selectedShow()==='all' && visible().map(row=>row.dataset.number).sort().join()==='105,106,41,58,63',
  'An old Done address opens All.');
 location.hash=new URLSearchParams({show:'all',producer:DWIGHT});await flush();
 check(selectedFilter('producer')==='staff:'+DWIGHT && visible().map(row=>row.dataset.number).join(',')==='105,41',
   'an address restores the producer filter');
 location.hash=new URLSearchParams({show:'all',producer:'missing'});await flush();
 check(selectedFilter('producer')==='all' && visible().length===5,'An unknown producer clears the Producer filter.');
 const restoredOldAddress=new Promise(resolve=>addEventListener('hashchange',resolve,{once:true}));
 location.hash=new URLSearchParams({show:'unread',state:'Working',owner:OWNER,q:'106'});
 await restoredOldAddress;
 check(selectedShow()==='working' && selectedFilter('owner')==='staff:'+OWNER && unread().checked &&
  visible().map(row=>row.dataset.number).join(',')==='106','An old address restores the Show and Owner chips, switch and search.');

 for(const target of rows().filter(row=>row.dataset.state==='Done' || row.dataset.parked==='true')){
  location.hash=new URLSearchParams({item:target.dataset.key});await flush();
  check(!target.hidden && recordIsOpen(target),'an item link shows '+target.dataset.state);
  check(selectedShow()==='all' &&
    selectedFilter('owner')==='all' && !unread().checked && selectedFilter('producer')==='all' &&
    document.getElementById('list-search').value==='','An item link selects Show and clears the other filters.');
 }
 const retained=rows().find(row=>row.dataset.number==='106');
 location.hash=new URLSearchParams({item:retained.dataset.key,owner:retained.dataset.owner,show:'working',q:'106',sort:'#',dir:'desc'});await flush();
 check(selectedFilter('owner')==='staff:'+retained.dataset.owner && selectedShow()==='working' &&
   document.getElementById('list-search').value==='106','an address with an open item keeps its filters');
 check(!retained.hidden && recordIsOpen(retained),'an address restores its open item');
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
await nativeFetch('/results',{method:'POST',body:JSON.stringify([...failed,...errors])});
})();</script>'''
        for name, identity in (("OWNER", OWNER.id), ("DWIGHT", DWIGHT.id), ("MORTY", MORTY.id), ("OTHER", OTHER.id)):
            probe = probe.replace(name, json.dumps(identity))
        for width in (1440, 961, 390):
            with self.subTest(width=width):
                for identity in ("L105", "L106"):
                    _ = the_list.update(MORTY.id, identity,
                        {"latest_status": f"The fixture uses a width of {width} pixels."}, source="morty")
                markup = list_page.render(self.root / "reviews").decode()
                setup_for_width = setup.replace('MARKUP', json.dumps(markup).replace('</', '<\\/'))
                probe_for_width = probe.replace('FIXTURE_WIDTH', str(width))
                page = markup.replace('<head>', '<head>' + setup_for_width, 1).replace('</body>', probe_for_width + '</body>').encode()
                root = self.root / "reviews"
                results: list[list[str]] = []
                complete = threading.Event()

                class Probe(BaseHTTPRequestHandler):
                    def do_GET(self) -> None:
                        self.send_response(200)
                        self.send_header('Content-Type', 'text/html; charset=utf-8')
                        self.end_headers()
                        if self.path == '/fresh':
                            _ = self.wfile.write(list_page.render(root).decode().encode())
                        elif self.path == '/complete':
                            _ = complete.wait(40)
                        else:
                            _ = self.wfile.write(page if self.path == '/page' else b'<iframe style="border:0;width:100vw;height:100vh" src="/page"></iframe><img hidden src="/complete">')

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
                        results.append(cast(list[str], json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                        complete.set()
                        self.send_response(204)
                        self.end_headers()

                    @override
                    def log_message(self, format: str, *args: object) -> None:
                        pass

                with ThreadingHTTPServer(('127.0.0.1', 0), Probe) as server:
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        with patch.object(change_page, "send_to_staff") as sends:
                            done = self.firefox_view(f'http://127.0.0.1:{server.server_port}/',
                                                     None, width, 900, complete)
                            self.assertEqual(done.returncode, 0, done.stderr)
                            self.assertEqual(results, [[]])
                            sends.assert_called_once()
                    finally:
                        server.shutdown()
                        thread.join(5)


if __name__ == '__main__':
    _ = unittest.main()

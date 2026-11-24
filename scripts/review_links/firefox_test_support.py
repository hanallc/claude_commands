"""Run browser probes in one Firefox process per test module."""
from __future__ import annotations

import base64
import json
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
from uuid import uuid4

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


if __name__ == "__main__":
    reap_firefox(Path(sys.argv[1]))

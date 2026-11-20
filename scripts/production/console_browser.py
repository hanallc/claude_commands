"""Inspect a console page with Chrome's debugger pipe."""
from __future__ import annotations

from collections.abc import Callable, Generator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from enum import Enum, auto
from pathlib import Path
from typing import BinaryIO, IO, Protocol, cast
import re
import json
import os
import select
import subprocess
import time

from scripts.lib import process_supervisor

SETTLE_SECONDS = 0.25

PAGE_FACTS = r"""(() => {
 const documents = [document];
 for (const doc of documents) {
   for (const frame of doc.querySelectorAll('iframe')) {
     if (!frame.closest('[hidden]') && frame.getClientRects().length && frame.contentDocument)
       documents.push(frame.contentDocument);
   }
 }
 const errors = new Map();
 const notices = new Set();
 const notice = detail => notices.add(detail);
 const components = new Set();
 const identityFaults = new Set();
 let renderSeconds = 0;
 for (const doc of documents) {
   for (const node of doc.querySelectorAll('[data-page-status]')) {
     if (node.closest('[hidden]') || !node.getClientRects().length) continue;
     const {statusComponent: component, state} = node.dataset;
     if (!/^[a-z][a-z0-9_-]*\.[a-z][a-z0-9_-]*$/.test(component || '')) {
       identityFaults.add('Page status has no valid component key: ' + node.innerText.trim());
       continue;
     }
     if (components.has(component)) {
       identityFaults.add('Page status component key is ambiguous: ' + component);
       continue;
     }
     components.add(component);
     if (state === 'Failed') {
       const reason = node.querySelector('[data-status-reason]')?.textContent.trim() || '';
       errors.set(component, {state, reason, detail: node.innerText.trim()});
     }
   }
   for (const node of doc.querySelectorAll(
     '[role=alert], [data-error-notice], .error-notice, [data-review-source-notices] li, #page-unavailable')) {
     const detail = node.innerText.trim();
     if (node.closest('[hidden]') || !node.getClientRects().length || !detail) continue;
     if (!node.matches('[data-page-status]')) notice(detail);
   }
   for (const node of doc.querySelectorAll('[data-page-facts]')) {
     if (node.dataset.readError) notice(node.dataset.readError);
     renderSeconds = Math.max(renderSeconds, Number(node.dataset.renderSeconds) || 0);
   }
   for (const line of (doc.body?.innerText || '').split('\n')) {
     if (/^\s*(could not load|error\s*:)/i.test(line)) notice(line.trim());
   }
 }
 const shell = document.querySelector('#console-shell');
 const selected = shell?.querySelector('[data-page][aria-current=page]')?.dataset.page;
 if (shell && documents.length < 2) notice('The console did not load its page frame.');
 for (const {detail} of errors.values()) notices.delete(detail);
 return {errors:[...errors], notices:[...notices], identityFaults:[...identityFaults],
         renderSeconds, selected, shell: Boolean(shell), frameReady: !shell || documents.length >= 2,
         path: location.pathname};
})()"""


@dataclass(frozen=True)
class Viewport:
    width: int
    height: int
    dark: bool


class FaultKind(Enum):
    NOTICE = auto()
    SCRIPT = auto()
    HTTP_SERVER = auto()
    HTTP_DOCUMENT = auto()
    NAVIGATION = auto()
    PAGE_SETUP = auto()
    RENDER_LIMIT = auto()
    STATUS_IDENTITY = auto()


@dataclass(frozen=True)
class FailedPageStatus:
    component: str
    state: str
    reason: str


@dataclass(frozen=True)
class PageFault:
    kind: FaultKind
    detail: str
    source: str | FailedPageStatus = ""

    def identity(self) -> tuple[FaultKind, str | FailedPageStatus]:
        if isinstance(self.source, FailedPageStatus):
            return self.kind, self.source
        if "<console-config>/" in self.detail:
            return self.kind, self.detail
        if self.kind is FaultKind.RENDER_LIMIT:
            return self.kind, ""
        if self.kind in (FaultKind.NOTICE, FaultKind.HTTP_DOCUMENT) and self.source:
            return self.kind, self.source
        return self.kind, self.detail


def notice_fault(detail: str) -> PageFault:
    # Review can list several entries from the same source.
    match = re.search(r"(?<!\w)(/[^\n]+?)(?: \([^\n]*\))?\.?$", detail)
    return PageFault(FaultKind.NOTICE, detail, match[1] if match else "")


class VisitCompletion(Enum):
    COMPLETED = "completed"
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class PageResult:
    seconds: float
    faults: tuple[PageFault, ...]
    completion: VisitCompletion = VisitCompletion.COMPLETED


@contextmanager
def managed_process(command: Sequence[str], *, cwd: Path, env: Mapping[str, str],
                    stderr: BinaryIO) -> Generator[process_supervisor.ManagedProcess[bytes]]:
    """Keep the workload session alive only while the checker owns it."""
    with process_supervisor.launch(
            command, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr) as child:
        yield child


class Pipe:
    """Read complete records with a deadline on each read."""

    def __init__(self, source: BinaryIO, separator: bytes) -> None:
        self.source: BinaryIO = source
        self.separator: bytes = separator
        self.buffer: bytes = b""

    def read(self, deadline: float) -> bytes:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("No response before the deadline.")
            if self.separator in self.buffer:
                break
            if not select.select([self.source], [], [], remaining)[0]:
                raise TimeoutError("No response before the deadline.")
            data = os.read(self.source.fileno(), 65536)
            if not data:
                raise ValueError("The process closed its output.")
            self.buffer += data
        line, separator, self.buffer = self.buffer.partition(self.separator)
        assert separator == self.separator
        return line


def json_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("Expected a JSON object.")
    return cast(dict[str, object], value)


def message_object(data: bytes) -> dict[str, object]:
    return json_object(cast(object, json.loads(data)))


class DebuggerPipes(Protocol):
    @property
    def stdin(self) -> IO[bytes] | None: ...

    @property
    def stdout(self) -> IO[bytes] | None: ...


class Browser:
    """Inspect each navigation in a new Chrome tab."""

    def __init__(self, child: DebuggerPipes, origin: str) -> None:
        if child.stdin is None or child.stdout is None:
            raise ValueError("Chrome needs a debugger pipe.")
        self.output: BinaryIO = cast(BinaryIO, child.stdin)
        self.input: Pipe = Pipe(cast(BinaryIO, child.stdout), b"\0")
        self.origin: str = origin
        self.serial: int = 0
        self.session: str = ""
        self.faults: list[PageFault] = []
        self.loading: set[object] = set()
        self.loaded: bool = False
        self.server_timed_out: bool = False
        self.attempt_id: str = ""
        self.activity: float = 0
        self.before_navigation: Callable[[], object] = lambda: None

    def send(self, method: str, params: Mapping[str, object], session: str = "") -> int:
        self.serial += 1
        payload: dict[str, object] = {"id": self.serial, "method": method, "params": dict(params)}
        if session:
            payload["sessionId"] = session
        _ = self.output.write(json.dumps(payload).encode() + b"\0")
        self.output.flush()
        return self.serial

    def event(self, message: dict[str, object]) -> None:
        if not self.session or message.get("sessionId") != self.session or "method" not in message:
            return
        params = json_object(message.get("params", {}))
        match message["method"]:
            case "Fetch.requestPaused":
                url = str(json_object(params["request"])["url"])
                request = {"requestId": params["requestId"]}
                if url.startswith((self.origin + "/", "about:", "data:", "blob:")):
                    _ = self.send("Fetch.continueRequest", request, self.session)
                else:
                    _ = self.send("Fetch.failRequest", {**request, "errorReason": "BlockedByClient"}, self.session)
            case "Network.requestWillBeSent":
                self.loading.add(params["requestId"])
                self.activity = time.monotonic()
            case "Network.loadingFinished" | "Network.loadingFailed":
                self.loading.discard(params["requestId"])
                self.activity = time.monotonic()
            case "Network.responseReceived":
                response = json_object(params["response"])
                status = response["status"]
                if isinstance(status, int | float) and (status >= 500 or params.get("type") == "Document" and status >= 400):
                    url = str(response["url"]).removeprefix(self.origin)
                    headers = json_object(response.get("headers", {}))
                    accounts_timeout = status == 504 and any(
                        key.lower() == "x-console-render-timeout" and value == "accounts"
                        for key, value in headers.items())
                    if accounts_timeout:
                        if not self.server_timed_out:
                            self.faults.append(PageFault(FaultKind.RENDER_LIMIT, f"Render timed out at {url}."))
                        self.server_timed_out = True
                    else:
                        kind = FaultKind.HTTP_SERVER if status >= 500 else FaultKind.HTTP_DOCUMENT
                        self.faults.append(PageFault(kind, f"HTTP {status} {url}", url))
            case "Page.loadEventFired":
                self.loaded = True
                self.activity = time.monotonic()
            case "Runtime.exceptionThrown":
                details = json_object(params["exceptionDetails"])
                exception = json_object(details.get("exception", {}))
                description = str(exception.get("description") or details.get("text", "Script error"))
                line = description.partition("\n")[0]
                url = str(details.get("url", "")).removeprefix(self.origin)
                self.faults.append(PageFault(FaultKind.SCRIPT, f"{line} ({url}:{details.get('lineNumber', '?')})"))
            case "Runtime.consoleAPICalled" if params.get("type") in ("error", "assert"):
                args = cast(list[object], params.get("args", []))
                parts = [str(arg.get("description") or arg.get("value", "")) for arg in map(json_object, args)]
                detail = "Script error. " + " ".join(parts).replace(self.origin + "/", "/")
                self.faults.append(PageFault(FaultKind.SCRIPT, detail))
            case _:
                return

    def call(self, method: str, deadline: float, params: Mapping[str, object], *, session: str = "") -> dict[str, object]:
        identity = self.send(method, params, session)
        while True:
            message = message_object(self.input.read(deadline))
            if message.get("id") == identity:
                if "error" in message:
                    raise ValueError(f"{method}: {message['error']}")
                return json_object(message.get("result", {}))
            self.event(message)

    def visit(self, path: str, viewport: Viewport, limit: float, page_key: str, direct: str = "") -> PageResult:
        setup = time.monotonic() + 10
        target = self.call("Target.createTarget", setup, {"url": "about:blank"})["targetId"]
        try:
            self.session = str(self.call("Target.attachToTarget", setup, {"targetId": target, "flatten": True})["sessionId"])
            self.faults.clear()
            self.loading.clear()
            self.loaded = False
            self.server_timed_out = False
            for domain in ("Page", "Runtime", "Network"):
                _ = self.call(f"{domain}.enable", setup, {}, session=self.session)
            if self.attempt_id:
                _ = self.call("Network.setExtraHTTPHeaders", setup, {"headers": {"X-Console-Attempt": self.attempt_id}},
                              session=self.session)
            _ = self.call("Fetch.enable", setup, {"patterns": [{"urlPattern": "*", "requestStage": "Request"}]},
                          session=self.session)
            _ = self.call("Emulation.setDeviceMetricsOverride", setup, {
                "width": viewport.width, "height": viewport.height, "deviceScaleFactor": 1, "mobile": False,
            }, session=self.session)
            _ = self.call("Emulation.setEmulatedMedia", setup, {
                "features": [{"name": "prefers-color-scheme", "value": "dark" if viewport.dark else "light"}],
            }, session=self.session)
            _ = self.before_navigation()
            return self.navigate(path, limit, page_key, direct)
        finally:
            self.session = ""
            with suppress(ValueError, OSError):
                _ = self.call("Target.closeTarget", time.monotonic() + 2, {"targetId": target})

    def navigate(self, path: str, limit: float, page_key: str, direct: str = "") -> PageResult:
        started = self.activity = time.monotonic()
        deadline = started + limit + SETTLE_SECONDS
        try:
            result = self.call("Page.navigate", deadline, {"url": self.origin + path}, session=self.session)
            if result.get("errorText"):
                self.faults.append(PageFault(FaultKind.NAVIGATION, str(result["errorText"])))
                return PageResult(time.monotonic() - started, tuple(self.faults), VisitCompletion.INCOMPLETE)
            while True:
                done = self.loaded and not self.loading
                quiet_at = self.activity + SETTLE_SECONDS
                try:
                    self.event(message_object(self.input.read(min(deadline, quiet_at) if done else deadline)))
                except TimeoutError:
                    if done and quiet_at <= deadline:
                        break
                    raise
            observed = self.activity - started
            result = self.call("Runtime.evaluate", time.monotonic() + 2, {
                "expression": PAGE_FACTS, "returnByValue": True,
            }, session=self.session)
            facts = json_object(json_object(result["result"])["value"])
            origin = self.origin + "/"
            self.faults.extend(notice_fault(detail.replace(origin, "/"))
                               for detail in cast(list[str], facts["notices"]))
            for component, status in cast(list[tuple[str, dict[str, str]]], facts["errors"]):
                reason = status["reason"].replace(origin, "/")
                self.faults.append(PageFault(FaultKind.NOTICE, status["detail"].replace(origin, "/"),
                                             FailedPageStatus(component, status["state"], reason)))
            self.faults.extend(PageFault(FaultKind.STATUS_IDENTITY, detail)
                               for detail in cast(list[str], facts["identityFaults"]))
            direct_match = direct and not facts["shell"] and facts["path"] == direct
            if facts.get("selected") != page_key and not direct_match:
                self.faults.append(PageFault(FaultKind.NAVIGATION, "The console did not select the requested page."))
            seconds = max(observed, cast(float, facts["renderSeconds"]))
            if seconds > limit and not self.server_timed_out:
                self.faults.append(PageFault(FaultKind.RENDER_LIMIT, f"Render took {seconds:.2f} s. Limit is {limit:g} s."))
            completion = (VisitCompletion.INCOMPLETE if self.server_timed_out or not facts.get("frameReady")
                          else VisitCompletion.COMPLETED)
            return PageResult(seconds, tuple(dict.fromkeys(self.faults)), completion)
        except TimeoutError:
            if not self.server_timed_out:
                self.faults.append(PageFault(FaultKind.RENDER_LIMIT, f"Render exceeded {limit:g} s."))
            return PageResult(time.monotonic() - started, tuple(dict.fromkeys(self.faults)), VisitCompletion.INCOMPLETE)

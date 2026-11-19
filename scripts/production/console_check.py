"""Check the candidate console before promotion."""
from __future__ import annotations

from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, replace
from enum import Enum, auto
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import BinaryIO, TypedDict, cast
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit
from uuid import uuid4
import shutil
import os
import signal
import subprocess
import sys
import tempfile
from threading import Barrier, BrokenBarrierError
import time

from scripts.lib import cancellation, conf
from scripts.production.console_browser import (
    Browser, FailedPageStatus, FaultKind, PageFault, PageResult, Pipe, Viewport, VisitCompletion, json_object, managed_process,
    message_object,
)

from scripts.production.console_settings import PAGE_SETTINGS, InstalledSettings
from scripts.production.console_write_scope import BARRIER_LOG

CONSOLE_CASES = Path("scripts/review_links/console_cases")
DEFAULT_VIEWPORT = Viewport(1280, 800, False)
STARTUP_SECONDS = 45
SERVER_MODULE = Path("scripts/production/console_server.py")
WRITE_SCOPE = SERVER_MODULE.with_name("console_write_scope.py")
CONSOLE_REQUIRED_PATHS = (
    "config/console.conf",
    "config/review_links.conf",
    "scripts/review_links/console.py",
    "scripts/review_links/serve.py",
)
CONSOLE_MARKERS = (*CONSOLE_REQUIRED_PATHS, SERVER_MODULE, WRITE_SCOPE)


class VisitKind(Enum):
    REGISTRY = auto()
    SAVED_CASE = auto()


@dataclass(frozen=True)
class PageVisit:
    name: str
    path: str
    page_key: str
    kind: VisitKind
    viewport: Viewport = DEFAULT_VIEWPORT


def case_visits(directory: Path) -> list[PageVisit]:
    """Read only the case files committed with the candidate tree."""
    if directory.is_symlink():
        raise ValueError(f"{directory}: the case folder must be in the candidate tree.")
    visits: list[PageVisit] = []
    for path in sorted(directory.glob("*.json")):
        try:
            if path.is_symlink():
                raise ValueError("A case must be a regular file.")
            record = message_object(path.read_bytes())
            if record.get("item_key") != path.stem:
                raise ValueError("The file name must match the Review key.")
            page, url = record.get("page"), record.get("url")
            if not isinstance(page, str) or not page or not isinstance(url, str):
                raise ValueError("The case needs a page name and a URL.")
            parsed = urlsplit(url)
            decoded = unquote(parsed.path)
            if (parsed.scheme not in ("http", "https") or not parsed.hostname or not decoded.startswith("/")
                    or decoded.startswith("//") or "\\" in decoded or ".." in decoded.split("/")):
                raise ValueError("The case needs a console URL with a path under its origin.")
            viewport = json_object(record.get("viewport"))
            width, height, dark = viewport.get("width"), viewport.get("height"), viewport.get("dark")
            if (not isinstance(width, int) or isinstance(width, bool) or width < 1
                    or not isinstance(height, int) or isinstance(height, bool) or height < 1
                    or not isinstance(dark, bool)):
                raise ValueError("The case needs positive viewport dimensions and a dark setting.")
            visits.append(PageVisit(f"{page} ({path.stem})", urlunsplit(("", "", parsed.path, parsed.query, parsed.fragment)),
                                    page, VisitKind.SAVED_CASE, Viewport(width, height, dark)))
        except (OSError, ValueError) as error:
            raise ValueError(f"{path.name}: {error}") from error
    return visits


@contextmanager
def check_signals() -> Generator[None]:
    """Convert a termination signal to an exception so the processes close."""
    def interrupt(number: int, _frame: object) -> None:
        raise KeyboardInterrupt(signal.Signals(number).name)

    previous = {number: signal.signal(number, interrupt) for number in cancellation.CANCELLATION_SIGNALS}
    try:
        yield
    finally:
        for number, handler in previous.items():
            _ = signal.signal(number, handler)


def browser_command() -> list[str]:
    browser = next((path for name in ("google-chrome", "chromium", "chromium-browser")
                    if (path := shutil.which(name))), "")
    if not browser and sys.platform == "darwin":
        chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
        if os.access(chrome, os.X_OK):
            browser = str(chrome)
    if not browser:
        raise ValueError("No supported browser found. Install Chrome or Chromium.")
    return [
        "bash", "-c", 'exec "$@" 3<&0 4>&1 0</dev/null 1>&2', "chrome",
        browser, "--headless", "--no-sandbox", "--disable-gpu", "--no-first-run",
        "--disable-breakpad", "--disable-crash-reporter", "--disable-background-networking",
        "--disable-extensions", "--remote-debugging-pipe",
    ]


def last_line(path: Path) -> str:
    lines = path.read_text(encoding="utf-8", errors="replace").strip().splitlines()
    return lines[-1] if lines else ""


def server_error(path: Path, offset: int) -> str:
    with path.open("rb") as stream:
        _ = stream.seek(offset)
        lines = stream.read().decode(errors="replace").splitlines()
    return next((line for line in reversed(lines) if line.startswith("review-links ")), "")


class ReadyPage(TypedDict):
    identity: str
    label: str
    path: str


class PageSetupFault(TypedDict):
    identity: str
    label: str
    fault: str


class SavedCasePage(Enum):
    UNREGISTERED = auto()


type VisitSetup = ReadyPage | PageSetupFault | SavedCasePage


def ready_pages(value: object) -> list[ReadyPage | PageSetupFault]:
    """Read the server's available pages and page setup faults."""
    if not isinstance(value, list):
        raise ValueError("The console server must list its pages.")
    pages: list[ReadyPage | PageSetupFault] = []
    for entry in cast(list[object], value):
        page = json_object(entry)
        identity, label = page.get("identity"), page.get("label")
        path, fault = page.get("path"), page.get("fault")
        if not isinstance(identity, str) or not isinstance(label, str):
            raise ValueError("The console server must name each page.")
        if isinstance(path, str) and path and "fault" not in page:
            pages.append(ReadyPage(identity=identity, label=label, path=path))
        elif isinstance(fault, str) and fault and "path" not in page:
            pages.append(PageSetupFault(identity=identity, label=label, fault=fault))
        else:
            raise ValueError(f"{label}: the console server must give a path or a setup fault.")
    return pages


class Baseline(Enum):
    ALL_FAULTS_ARE_NEW = auto()


def fault_details(faults: tuple[PageFault, ...] | list[PageFault]) -> str:
    return " | ".join(" ".join(fault.detail.splitlines()) for fault in faults)


def status_components(faults: tuple[PageFault, ...] | list[PageFault]) -> str:
    keys = dict.fromkeys(fault.source.component for fault in faults if isinstance(fault.source, FailedPageStatus))
    return f" [components: {', '.join(keys)}]" if keys else ""


def print_result(label: str, name: str, result: PageResult, attempt: int) -> None:
    status = "red" if result.faults else "ok"
    detail = fault_details(result.faults)
    print(f"{label} console {name}: {status} ({result.seconds:.2f} s)"
          + f" [{result.completion.value}, attempt {attempt}]"
          + (f". {detail}" if detail else "") + status_components(result.faults), flush=True)


def server_command(script: Path, assets: Path, settings: Path) -> list[str]:
    scope = Path(__file__).with_name(WRITE_SCOPE.name).resolve()
    return [sys.executable, "-I", str(scope), str(script), str(assets), str(settings)]


@dataclass(frozen=True)
class ConsoleSession:
    tree: Path
    label: str
    pages: dict[str, ReadyPage | PageSetupFault]
    browser: Browser
    server_log: Path
    chrome_log: Path

    def prepare(self, visit: PageVisit, attempt: int) -> VisitSetup:
        self.browser.attempt_id = uuid4().hex
        if attempt == 1:
            return self.pages.get(visit.page_key, SavedCasePage.UNREGISTERED)
        connection = HTTPConnection(urlsplit(self.browser.origin).netloc, timeout=STARTUP_SECONDS)
        try:
            path = "/__console_check/resolve?" + urlencode({"page": visit.page_key})
            connection.request("GET", path, headers={"X-Console-Attempt": self.browser.attempt_id})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError(f"Retry setup returned HTTP {response.status}.")
            page, = ready_pages([message_object(response.read())])
            if page["identity"] != visit.page_key:
                raise ValueError("Retry setup returned another page.")
            return page
        except (OSError, ValueError, HTTPException) as error:
            raise ValueError(f"{self.label} console {visit.name} retry setup: {error}") from error
        finally:
            connection.close()

    def render(self, visit: PageVisit, limit: float, page: VisitSetup) -> PageResult:
        offset = self.server_log.stat().st_size
        if visit.kind is VisitKind.REGISTRY and isinstance(page, dict) and "fault" in page:
            result = PageResult(0, (PageFault(FaultKind.PAGE_SETUP, page["fault"]),))
        else:
            direct = urlsplit(page["path"]).path if visit.kind is VisitKind.SAVED_CASE and isinstance(page, dict) and "path" in page else ""
            try:
                result = self.browser.visit(visit.path, visit.viewport, limit, visit.page_key, direct)
            except (TimeoutError, BrokenBarrierError):
                result = PageResult(0, (PageFault(FaultKind.RENDER_LIMIT, "Browser setup did not complete."),),
                                    VisitCompletion.INCOMPLETE)
            except (OSError, ValueError) as error:
                raise ValueError(f"{self.label} console {visit.name}: {last_line(self.chrome_log) or error}") from error
        faults = result.faults
        if any(fault.kind is FaultKind.HTTP_SERVER for fault in faults):
            if reason := server_error(self.server_log, offset):
                faults = (*faults, PageFault(FaultKind.HTTP_SERVER, reason))

        def normalize(detail: str) -> str:
            for stem in PAGE_SETTINGS:
                detail = detail.replace(str(self.tree / "config" / f"{stem}.conf"), f"<console-config>/{stem}.conf")
            return detail

        return replace(result, faults=tuple(
            replace(fault, detail=normalize(fault.detail),
                    source=replace(fault.source, reason=normalize(fault.source.reason))
                    if isinstance(fault.source, FailedPageStatus) else fault.source)
            for fault in faults))


def start_console(stack: ExitStack, tree: Path, label: str, folder: Path, settings: Path,
                  chrome: list[str]) -> ConsoleSession:
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "GIT_OPTIONAL_LOCKS": "0"}
    # Use the same adapter with trees that predate the checker.
    script = Path(__file__).with_name(SERVER_MODULE.name).resolve()
    command = server_command(script, folder / "pages", settings)
    if label == "base":
        command.append("--legacy-status")
    server_log = folder / "server.log"
    chrome_log = folder / "chrome.log"
    server_errors = stack.enter_context(server_log.open("wb"))
    chrome_errors = stack.enter_context(chrome_log.open("wb"))
    server = stack.enter_context(managed_process(command, cwd=tree, env=environment, stderr=server_errors))
    if server.stdout is None:
        raise ValueError("The console server has no ready pipe.")
    try:
        ready = message_object(Pipe(cast(BinaryIO, server.stdout), b"\n").read(time.monotonic() + STARTUP_SECONDS))
        pages = {page["identity"]: page for page in ready_pages(ready.get("pages"))}
    except (OSError, ValueError) as error:
        raise ValueError(f"{label} console server startup. {last_line(server_log) or error}") from error
    browser_environment = {**os.environ, "XDG_CONFIG_HOME": str(folder), "XDG_CACHE_HOME": str(folder)}
    child = stack.enter_context(managed_process([*chrome, f"--user-data-dir={folder / 'profile'}", "about:blank"],
                                                cwd=tree, env=browser_environment, stderr=chrome_errors))
    return ConsoleSession(tree, label, pages, Browser(child, f"http://127.0.0.1:{ready['port']}"), server_log, chrome_log)


def result_keys(result: PageResult) -> set[tuple[FaultKind, str | FailedPageStatus]]:
    return {fault.identity() for fault in result.faults}


def check_page(sessions: list[ConsoleSession], visit: PageVisit, limit: float,
               workers: ThreadPoolExecutor) -> list[str]:
    """Retain candidate faults unless every completed base attempt shares them."""
    attempts: list[tuple[PageResult, ...]] = []
    has_base = len(sessions) == 2 or sessions[0].label == "base"
    has_candidate = sessions[-1].label == "candidate"
    for attempt in range(1, 4):
        preparations = [workers.submit(session.prepare, visit, attempt) for session in sessions]
        pages = [prepared.result() for prepared in preparations]
        if len(sessions) == 2 and all(isinstance(page, dict) and "path" in page for page in pages):
            gate = Barrier(2, timeout=15)
            for session in sessions:
                session.browser.before_navigation = gate.wait
            try:
                futures = [workers.submit(session.render, visit, limit, page)
                           for session, page in zip(sessions, pages, strict=True)]
                results = tuple(future.result() for future in futures)
            finally:
                for session in sessions:
                    session.browser.before_navigation = lambda: None
        else:
            results = tuple(session.render(visit, limit, page)
                            for session, page in zip(sessions, pages, strict=True))
        attempts.append(results)
        for session, result in zip(sessions, results, strict=True):
            print_result(session.label, visit.name, result, attempt)
            identity_faults = [fault for fault in result.faults if fault.kind is FaultKind.STATUS_IDENTITY]
            if identity_faults:
                raise ValueError(f"{session.label} console {visit.name}: {fault_details(identity_faults)}")
        if visit.kind is VisitKind.SAVED_CASE:
            break
        if attempt == 1 and all(result.completion is VisitCompletion.COMPLETED for result in results):
            if not results[0].faults and (len(results) == 1 or not results[-1].faults):
                break

    # An incomplete visit can supply a fault, but it cannot clear another fault.
    completed = any(all(result.completion is VisitCompletion.COMPLETED for result in pair) for pair in attempts)
    transient_timeout = visit.kind is VisitKind.REGISTRY and completed
    base_results = [pair[0] for pair in attempts] if has_base else []
    candidate_results = [pair[-1] for pair in attempts] if has_candidate else []
    shared: set[tuple[FaultKind, str | FailedPageStatus]] = set()
    if base_results and all(result.completion is VisitCompletion.COMPLETED for result in base_results):
        shared = result_keys(base_results[0]).intersection(*(result_keys(result) for result in base_results[1:]))

    def observed(results: list[PageResult]) -> dict[tuple[FaultKind, str | FailedPageStatus], PageFault]:
        faults: dict[tuple[FaultKind, str | FailedPageStatus], PageFault] = {}
        for result in results:
            for fault in result.faults:
                if (transient_timeout and result.completion is VisitCompletion.INCOMPLETE
                        and fault.kind is FaultKind.RENDER_LIMIT):
                    continue
                _ = faults.setdefault(fault.identity(), fault)
        return faults

    candidate = observed(candidate_results)
    baseline = observed(base_results)
    new = [fault for fault in candidate.values() if fault.identity() not in shared]
    known = [fault for fault in candidate.values() if fault.identity() in shared]
    known.extend(fault for key, fault in baseline.items() if key not in candidate
                 and any(result.completion is VisitCompletion.COMPLETED for result in base_results))
    if known:
        print(f"live console faults (not held) {visit.name}: {fault_details(known)}"
              + status_components(known), flush=True)
    if new and (keys := status_components(new)):
        print(f"new console faults {visit.name}: {fault_details(new)}{keys}", flush=True)
    failures = [f"{visit.name}: {fault_details(new)}"] if new else []
    if not completed and (len(sessions) == 2 or not failures):
        sides = "base and candidate" if len(sessions) == 2 else sessions[0].label
        reason = next((fault.detail for pair in attempts for result in pair
                       for fault in result.faults if fault.kind is FaultKind.RENDER_LIMIT), "No completed page facts.")
        failures.append(f"{visit.name}: {sides} did not complete a render pair. {reason}")
    return failures


def check_sessions(sessions: list[ConsoleSession], cases: list[PageVisit], limit: float) -> list[str]:
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=2) as workers:
        for key in dict.fromkeys(key for session in sessions for key in session.pages):
            selected = [session for session in sessions if key in session.pages]
            visit = PageVisit(selected[-1].pages[key]["label"], "/console/?" + urlencode({"page": key}),
                              key, VisitKind.REGISTRY)
            failures.extend(check_page(selected, visit, limit, workers))
        if sessions and sessions[-1].label == "candidate":
            for visit in cases:
                failures.extend(check_page(sessions[-1:], visit, limit, workers))
    return failures


def has_console(tree: Path, label: str) -> bool:
    """Check the paths that identify a console and require its main files."""
    locations = [tree / marker for marker in CONSOLE_MARKERS]
    if not any(path.exists() or path.is_symlink() for path in locations):
        return False
    missing = [name for name in CONSOLE_REQUIRED_PATHS if not (tree / name).is_file()]
    if missing:
        raise ValueError(f"{label} console is missing required files: {', '.join(missing)}")
    return True


def run_check(candidate: Path, *, base: Path | Baseline = Baseline.ALL_FAULTS_ARE_NEW) -> str:
    """Compare paired renders and replay the candidate's saved cases."""
    started = time.monotonic()
    failures: list[str] = []
    barrier_faults: list[str] = []
    try:
        base_has_console = isinstance(base, Path) and has_console(base, "base")
        if not has_console(candidate, "candidate"):
            if base_has_console:
                raise ValueError("The candidate removed the console.")
            print("Console check: no console.", flush=True)
            return ""
        chrome = browser_command()
        limit = conf.read(candidate / "config/merge_gate.conf").number("console_render_seconds")
        cases = case_visits(candidate / CONSOLE_CASES)
        with check_signals(), tempfile.TemporaryDirectory(prefix="console-check-") as temporary:
            workspace = Path(temporary)
            settings = workspace / "installed.json"
            InstalledSettings.read().save(settings)
            print(f"Console preparation time {time.monotonic() - started:.2f} s.", flush=True)
            trees = [(base, "base")] if isinstance(base, Path) and base_has_console else []
            trees.append((candidate, "candidate"))
            try:
                with ExitStack() as stack:
                    sessions: list[ConsoleSession] = []
                    for tree, label in trees:
                        folder = workspace / label
                        folder.mkdir()
                        try:
                            sessions.append(start_console(stack, tree, label, folder, settings, chrome))
                        except (OSError, ValueError, subprocess.SubprocessError) as error:
                            failures.append(str(error))
                    failures.extend(check_sessions(sessions, cases, limit))
            finally:
                for _, label in trees:
                    with suppress(FileNotFoundError):
                        records = (workspace / label / BARRIER_LOG).read_text(encoding="utf-8").splitlines()
                        barrier_faults.extend(f"{label} console filesystem barrier. {record}"
                                              for record in dict.fromkeys(records))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        failures.append(str(error))
    finally:
        print(f"Console comparison time {time.monotonic() - started:.2f} s.", flush=True)
    return "RED: console check. " + " | ".join([*barrier_faults, *failures]) if barrier_faults or failures else ""


def export_tree(repo: Path, revision: str, directory: Path) -> None:
    archive = subprocess.run(["git", "archive", "--format=tar", revision], cwd=repo,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
    _ = subprocess.run(["tar", "-x", "-C", str(directory)], input=archive,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


def compare_to_main(repo: Path, base: str, candidate: Path) -> str:
    with tempfile.TemporaryDirectory(prefix="console-base-") as temporary:
        tree = Path(temporary)
        try:
            export_tree(repo, base, tree)
        except (OSError, subprocess.SubprocessError) as error:
            return f"RED: console check. Cannot export the base {base}. {error}"
        return run_check(candidate, base=tree)


def main(arguments: list[str]) -> int:
    if arguments:
        print("Usage: python3 -m scripts.production.console_check", file=sys.stderr)
        return 2
    try:
        repo = Path.cwd()
        started = time.monotonic()
        failure = compare_to_main(repo, "origin/main", repo)
        print(f"Console check wall time {time.monotonic() - started:.2f} s.", flush=True)
    except KeyboardInterrupt as error:
        print(f"Console check interrupted. {error}", file=sys.stderr)
        return 1
    if failure:
        print(failure, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

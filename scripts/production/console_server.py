"""Serve the console code from the tree under test."""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum, auto
from functools import partial
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager, suppress
from contextvars import ContextVar
from pathlib import Path
from threading import Lock
from typing import BinaryIO, TypedDict, cast, override
from urllib.parse import parse_qs, quote, urlsplit
import fcntl
import importlib
import json
import re
import subprocess
import sys
import time

from scripts.disks import disks_page, measure
from scripts.lib import conf, file_lock
from scripts.production import change_review
from scripts.review_links import accounts_page, change_browser, change_page, console, review_items, review_page, serve


class ReadyPage(TypedDict):
    identity: str
    label: str
    path: str


class PageSetupFault(TypedDict):
    identity: str
    label: str
    fault: str


type FileRoutes = dict[str, Callable[[], Path]]


@dataclass
class PageFiles:
    directory: Path
    routes: FileRoutes = field(default_factory=dict)


@dataclass(frozen=True)
class ResolvedPage:
    destination: console.AvailablePage
    routes: FileRoutes


@dataclass(frozen=True)
class SetupFailure:
    reason: str


type PageSetup = ResolvedPage | SetupFailure


def describe_setup(setup: PageSetup, identity: str, label: str) -> ReadyPage | PageSetupFault:
    if isinstance(setup, ResolvedPage):
        return ReadyPage(identity=identity, label=label, path=setup.destination.path)
    return PageSetupFault(identity=identity, label=label, fault=setup.reason)


def file_routes(setups: tuple[PageSetup, ...]) -> FileRoutes:
    return {url: serve_file for setup in reversed(setups) if isinstance(setup, ResolvedPage)
            for url, serve_file in setup.routes.items()}


class ReviewDocument(Enum):
    NOT_RENDERED = auto()


@dataclass
class ComparisonAttempt:
    pages: tuple[console.ConsolePage, ...]
    rebuilds: console.AvailablePage | console.UnavailablePage
    routes: FileRoutes
    review: review_page.ReviewSnapshot | ReviewDocument = ReviewDocument.NOT_RENDERED
    review_lock: Lock = field(default_factory=Lock)


@contextmanager
def read_delivery_lock(root: Path) -> Generator[None]:
    """Read under the delivery lock when it exists."""
    with ExitStack() as stack:
        with suppress(FileNotFoundError):
            lock = stack.enter_context((root / ".review-link-delivery.lock").open("rb"))
            fcntl.flock(lock.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
        yield


def configure_legacy_reads() -> None:
    """Keep the base's reads safe during the first promotion of the checker."""
    def console_folder(root: Path) -> Path:
        folder = root / change_page.CONSOLE_FOLDER
        target = folder / change_browser.MANIFEST
        if not target.is_file():
            raise FileNotFoundError(f"The console has no browser manifest at {target}.")
        return folder

    def read_measure(now: float, launch: Callable[[int, file_lock.ExclusiveLock], None] = disks_page.launch_measure,
                     *, force: bool = False) -> disks_page.MeasureRequest:
        del now, launch
        if force:
            raise ValueError("This console is read-only.")
        generation = disks_page.published_generation(disks_page.output_folder())
        return disks_page.MeasureRequest(disks_page.MeasureStart.CURRENT, generation)

    change_page.console_folder = console_folder
    review_items.read = partial(review_items.read, assign_ids=False)
    disks_page.start_measure = read_measure


def comparison_server(root: Path, pages: tuple[console.ConsolePage, ...],
                      rebuilds: console.AvailablePage | console.UnavailablePage,
                      setups: dict[str, PageSetup],
                      retry: Callable[[str, str], PageSetup]) -> serve.ReviewLinksServer:
    """Keep each attempt's setup and Review snapshot with its requests."""
    attempts: dict[str, ComparisonAttempt] = {}
    attempts_lock = Lock()
    initial_routes = file_routes(tuple(setups.values()))
    active_attempt = ContextVar[ComparisonAttempt]("active_attempt")

    class AccountsRenderExpired(Exception):
        """The Accounts child did not complete before its deadline."""

    class Handler(serve.ReviewLinksHandler):
        @override
        def send_head(self) -> BinaryIO | None:
            route = urlsplit(self.path)
            attempt_id = self.headers.get("X-Console-Attempt", "")
            if route.path == "/__console_check/resolve":
                key = parse_qs(route.query).get("page", [""])[0]
                labels = {page.identity: page.label for page in pages} | {"rebuilds": "Rebuilds"}
                if re.fullmatch("[0-9a-f]{32}", attempt_id) is None or key not in setups:
                    self.send_error(400, "The console retry needs an attempt and a registered page.")
                    return None
                setup = retry(key, attempt_id)
                destination = setup.destination if isinstance(setup, ResolvedPage) else console.UnavailablePage()
                selected = tuple(replace(page, destination=destination) if page.identity == key else page
                                 for page in pages)
                routes = file_routes((setup, *(other for identity, other in setups.items() if identity != key)))
                with attempts_lock:
                    attempts[attempt_id] = ComparisonAttempt(
                        selected, destination if key == "rebuilds" else rebuilds, routes)
                self.json_response(200, describe_setup(setup, key, labels[key]))
                return None

            with attempts_lock:
                if attempt_id not in attempts:
                    attempts[attempt_id] = ComparisonAttempt(pages, rebuilds, initial_routes)
                attempt = attempts[attempt_id]
            scope = active_attempt.set(attempt)
            try:
                return self.send_attempt(route.path, route.query, attempt)
            finally:
                active_attempt.reset(scope)

        def send_attempt(self, path: str, query: str, attempt: ComparisonAttempt) -> BinaryIO | None:
            if path == review_page.PAGE_PATH and not parse_qs(query).keys() & {"body", "index", "version"}:
                try:
                    with attempt.review_lock:
                        if isinstance(attempt.review, ReviewDocument):
                            attempt.review = review_page.build_snapshot(root)
                except (OSError, ValueError) as error:
                    print(f"review-links review: {error}", file=sys.stderr)
                    self.html_response(503, change_page.document("<p>The Review page is not available.</p>"))
                    return None
            if serve_file := attempt.routes.get(path):
                try:
                    file = serve_file()
                    content = file.read_bytes()
                except Exception as error:
                    print(f"review-links generated page: {error}", file=sys.stderr)
                    self.html_response(503, change_page.document("<p>The generated page is not available.</p>"))
                else:
                    self.review_response(content, self.guess_type(str(file)))
                return None
            try:
                return super().send_head()
            except AccountsRenderExpired as error:
                print(f"console accounts: {error}", file=sys.stderr)
                content = change_page.document("<p>The Accounts render timed out.</p>")
                self.send_response(504)
                self.send_header("X-Console-Render-Timeout", "accounts")
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                _ = self.wfile.write(content)
                return None

        @override
        def do_POST(self) -> None:
            if hasattr(serve, "ReadOnlyConsole"):
                super().do_POST()
            elif self.headers.get("Content-Length", "0") == "0" and self.is_disks_measure():
                self.disks_measure_post()
            else:
                self.send_error(405, "This console is read-only")

    class Server(serve.ReviewLinksServer):
        @override
        def service_actions(self) -> None:
            pass

    server = Server(("127.0.0.1", 0), root, mode=serve.ServerMode.LIST_FIXTURE)
    try:
        server.RequestHandlerClass = Handler
        server.mode = serve.ReadOnlyConsole(pages, rebuilds) if hasattr(serve, "ReadOnlyConsole") else serve.ServerMode.LIVE
        server.root = server.store_folder
        server.delivery_status = change_page.DeliveryStatus(server.root)
        def read_accounts() -> bytes:
            try:
                rendered = accounts_page.render_with_facts()
            except OSError as error:
                if isinstance(error.__cause__, subprocess.TimeoutExpired):
                    raise AccountsRenderExpired(str(error)) from error
                raise
            return rendered.with_recent_times((rendered.seconds,))

        def read_review() -> review_page.ReviewSnapshot:
            attempt = active_attempt.get()
            with attempt.review_lock:
                if isinstance(attempt.review, ReviewDocument):
                    raise ValueError("The Review render has no completed snapshot.")
                return attempt.review

        render_shell = console.render

        def render_attempt_shell(
                _pages: tuple[console.ConsolePage, ...],
                _rebuilds: console.AvailablePage | console.UnavailablePage) -> bytes:
            attempt = active_attempt.get()
            return render_shell(attempt.pages, attempt.rebuilds)

        console.render = render_attempt_shell
        server.accounts_cache.get = read_accounts
        server.review_cache.get = read_review
    except BaseException:
        server.server_close()
        raise
    return server


def serve_console(assets: Path, *, installed_settings: Callable[[str], conf.ConfigRead]) -> int:
    """Serve this tree on loopback with private copies of generated pages."""
    from scripts.aws_target import aws_page
    from scripts.build_queue import memory_feed, memory_files
    from scripts.production import change_routing

    change_review.review_delivery_lock = read_delivery_lock
    root = change_routing.review_root().resolve(strict=True)
    setups: dict[str, PageSetup] = {}
    references: dict[str, str] = {}
    publishing = ContextVar[PageFiles]("publishing")
    resolve_page = console.resolve
    publish_disks = disks_page.publish
    published_generation = disks_page.published_generation
    output_folder = disks_page.output_folder
    read_data = measure.read_data
    use_installed_disks = ContextVar("use_installed_disks", default=False)

    def live_disks_folder() -> Path:
        folder = Path(installed_settings("disks").text("output_folder")).expanduser().resolve()
        _ = folder.relative_to(root)
        return folder

    def read_live_measure() -> measure.DisksData | None:
        scope = use_installed_disks.set(True)
        try:
            return read_data()
        finally:
            use_installed_disks.reset(scope)

    disks_page.output_folder = lambda: live_disks_folder() if use_installed_disks.get() else output_folder()
    measure.read_data = read_live_measure

    def live_disks_generation(_page_folder: Path) -> int:
        return published_generation(live_disks_folder())

    def private_file(pages: PageFiles, live: Path) -> Path:
        private = pages.directory / live.relative_to(root)
        private.parent.mkdir(parents=True, exist_ok=True)
        return private

    def publish_private_disks(folder: Path, settings: disks_page.PageSettings) -> Path:
        try:
            pages = publishing.get()
        except LookupError:
            return publish_disks(folder, settings)
        # Keep the resolver's URL and the browser's report paths under the live root.
        live = folder.resolve() / disks_page.PAGE_NAME
        private = private_file(pages, live)
        data_url = "/" + quote(live.with_name(disks_page.DATA_FILE).relative_to(root).as_posix())
        data_file = (live_disks_folder() / disks_page.DATA_FILE).resolve()
        _ = data_file.relative_to(root)
        pages.routes[data_url] = lambda: data_file
        pages.routes["/" + quote(live.relative_to(root).as_posix())] = lambda: publish_disks(private.parent, settings)
        return live

    def detail(error: Exception) -> str:
        return str(error).replace(str(assets), str(root)) or type(error).__name__

    def resolve(key: str, reference: str, pages: PageFiles) -> PageSetup:
        fault = "The console has no valid page URL."
        scope = publishing.set(pages)
        try:
            match = console.REFERENCE.fullmatch(reference)
            if match is None:
                raise ValueError("Expected a Python URL function.")
            module = importlib.import_module(match[1])
            resolver = cast(object, getattr(module, match[2]))
            if not callable(resolver):
                raise ValueError("The configured URL function is not callable.")

            def record_failure() -> str:
                nonlocal fault
                try:
                    return cast(Callable[[], str], resolver)()
                except Exception as error:
                    fault = detail(error)
                    raise

            # The registry validates the URL from the configured function.
            setattr(module, match[2], record_failure)
            try:
                destination = resolve_page(key, reference)
            finally:
                setattr(module, match[2], resolver)
            if isinstance(destination, console.UnavailablePage):
                return SetupFailure(fault)
            if reference == "python:scripts.aws_target.aws_page:page_url":
                from scripts.build_targets.target_config import ssh_target_state_directory

                live = aws_page.output_folder().resolve() / aws_page.PAGE_NAME
                folder = private_file(pages, live).parent
                pages.routes[urlsplit(destination.path).path] = lambda: aws_page.publish(
                    ssh_target_state_directory("aws"), int(time.time()), folder, settings_path=conf.path("aws_target"))
            if reference == "python:scripts.build_queue.memory_feed:page_url":
                config = memory_feed.read_configuration()
                private = private_file(pages, config.output_folder.resolve() / memory_files.PAGE_NAME)

                def publish_builds() -> Path:
                    memory_feed.publish_page(replace(config, output_folder=private.parent))
                    return private

                pages.routes[urlsplit(destination.path).path] = publish_builds
            return ResolvedPage(destination, pages.routes)
        except Exception as error:
            return SetupFailure(detail(error))
        finally:
            publishing.reset(scope)

    def initial_resolution(key: str, reference: str) -> console.AvailablePage | console.UnavailablePage:
        identity = key.split(".")[1] if key.startswith("page.") else "rebuilds"
        references[identity] = reference
        setup = resolve(key, reference, PageFiles(assets))
        setups[identity] = setup
        return setup.destination if isinstance(setup, ResolvedPage) else console.UnavailablePage()

    def retry_page(identity: str, attempt_id: str) -> PageSetup:
        key = "rebuilds.url" if identity == "rebuilds" else f"page.{identity}.url"
        return resolve(key, references[identity], PageFiles(assets / f"retry-{attempt_id}"))

    console.resolve = initial_resolution
    disks_page.publish = publish_private_disks
    disks_page.published_generation = live_disks_generation
    try:
        if not hasattr(serve, "ReadOnlyConsole"):
            configure_legacy_reads()
        pages = console.read_pages()
        rebuilds = console.read_rebuilds()
    finally:
        console.resolve = resolve_page

    if not hasattr(serve, "ReadOnlyConsole"):
        # The legacy handler reads the registry on each shell request.
        def fixed_pages(**_settings: object) -> tuple[console.ConsolePage, ...]:
            return pages

        def fixed_rebuilds(**_settings: object) -> console.AvailablePage | console.UnavailablePage:
            return rebuilds

        console.read_pages = fixed_pages
        console.read_rebuilds = fixed_rebuilds

    server = comparison_server(root, pages, rebuilds, setups, retry_page)

    with server:
        listed = [describe_setup(setups[page.identity], page.identity, page.label) for page in pages]
        listed.append(describe_setup(setups["rebuilds"], "rebuilds", "Rebuilds"))
        print(json.dumps({"port": server.server_port, "pages": listed}), flush=True)
        server.serve_forever()
    return 0


if __name__ == "__main__":
    installed_settings = cast(Callable[[str], conf.ConfigRead], globals().get("read_installed", conf.read))
    try:
        raise SystemExit(serve_console(Path(sys.argv[1]), installed_settings=installed_settings))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"console server: {error}", file=sys.stderr)
        raise SystemExit(1) from error

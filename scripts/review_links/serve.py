#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Serve review snapshots only on this machine's Tailscale IPv4 address."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass, field, replace
from functools import partial
import html
import gzip
import ipaddress
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import threading
from collections.abc import Callable, Generator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager, nullcontext
from enum import Enum
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast, override
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

# Detect file changes before the first stamp, including changes during imports.
STARTED_NS = time.time_ns()

from scripts.disks import disks_page, sessions, slots
from scripts.aws_target import aws_page, machines_page
from scripts.build_queue import memory_files
from scripts.lib import conf, scratch
from scripts.lib.file_lock import exclusive
from scripts.production import change_routing, change_review, staff
from scripts.review_links import accounts_page, branch_page, change_page, change_browser, console, for_you, image_store, images, investigations_page, list_page, review_items, review_page
from scripts.review_links import console_tune, new_item_dialog, page_frame, question_record, review_actions, review_link, review_submissions, staff_names
from scripts.review_links.review_link import private_name
from scripts.whoami import usage_store

BRANCHES_DELETING = "Deleting these branches now. The result comes by message."
# A restart passes the listening socket to the new process in this variable.
LISTEN_FD = "REVIEW_LINKS_LISTEN_FD"
# Allow page state and rows in addition to the existing words and image budget.
MAX_NEW_ITEM_BODY = (4 * ((new_item_dialog.MAX_TOTAL_IMAGE_BYTES + 2) // 3) + 256 * 1024
                     + new_item_dialog.MAX_PAGE_DATA_BYTES + new_item_dialog.MAX_BUG_PAGE_STATE_BYTES)
SCREENSHOT_RELOAD = "Reload the page to send the screenshot."
STALE_NEW_ITEM_TOKEN = "The New item token expired. Try Send again."
ATTACHMENT_MEDIA = {f"image.{extension}": media for media, extension in new_item_dialog.IMAGE_FORMATS.items()}


def published_console_page(root: Path, path: str) -> str:
    """Recognize the decoded path of a published console page."""
    leaf = path.rsplit("/", 1)[-1]
    try:
        match leaf:
            case aws_page.PAGE_NAME:
                page, folder = "aws", aws_page.output_folder()
            case machines_page.PAGE_NAME:
                page, folder = "aws_machines", machines_page.output_folder()
            case disks_page.PAGE_NAME:
                page, folder = "disks", disks_page.output_folder()
            case memory_files.PAGE_NAME:
                page, folder = "builds", memory_files.configured_output_folder(conf.read("memory_feed"))
            case _:
                return ""
        relative = (folder.resolve() / leaf).relative_to(root.resolve())
    except (OSError, ValueError):
        return ""
    return page if path == f"/{relative.as_posix()}" else ""


CONSOLE_DOCUMENTS = {
    accounts_page.PAGE_PATH: "accounts", images.PAGE_PATH: "images", list_page.PAGE_PATH: "list",
    review_page.PAGE_PATH: "review", f"/console/{change_page.REBUILDS_NAME}": "rebuilds",
}


def capture_page_key(root: Path, place: str) -> str:
    """Identify a console document that Firefox can open."""
    route = urlsplit(place)
    if route.netloc or route.scheme:
        return ""
    key = CONSOLE_DOCUMENTS.get(route.path) or published_console_page(root, unquote(route.path))
    if key == "review" and {"body", "version", "index"} & parse_qs(route.query, keep_blank_values=True).keys():
        return ""
    return key


def owned_document(content: bytes, page: str) -> bytes:
    """Read the current owner when serving a console document."""
    try:
        members = staff.local_snapshot()
        owners = review_submissions.page_owners(page, conf.read_or_repository("console"), members)
        if isinstance(owners.route, staff.StaffRow):
            recipient = owners.route.name
        elif review_submissions.has_recipient_choice(members):
            recipient = page_frame.OwnerNewItem.CHOOSE_RECIPIENT
        elif isinstance(owners.shown, staff.StaffRow):
            recipient = page_frame.OwnerNewItem.UNAVAILABLE
        else:
            return content
        shown = owners.shown if isinstance(owners.shown, staff.StaffRow) else page_frame.NoShownOwner()
        return page_frame.with_owner(content, page, shown, members, new_item_dialog.render, recipient=recipient)
    except (OSError, ValueError):
        pass
    return content


def page_screenshot(firefox: str, url: str, request: new_item_dialog.PageCaptureRequest) -> bytes:
    """Capture the console page in a clean Firefox profile."""
    with tempfile.TemporaryDirectory(prefix="console-capture-") as folder:
        profile = Path(folder)
        output = profile / "page.png"
        theme = int(request.dark)
        _ = (profile / "user.js").write_text(
            f'user_pref("ui.systemUsesDarkTheme", {theme});\n'
            + 'user_pref("layout.css.devPixelsPerPx", "1.0");\n'
            + 'user_pref("ui.prefersReducedMotion", 1);\n'
            + 'user_pref("network.proxy.type", 0);\n', encoding="utf-8")
        _ = subprocess.run([firefox, "--headless", "--no-remote", "--profile", folder,
                            "--window-size", f"{request.width},{request.height}", "--screenshot", str(output), url],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=15)
        with output.open("rb") as stream:
            image = stream.read(new_item_dialog.MAX_IMAGE_BYTES + 1)
    if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > new_item_dialog.MAX_IMAGE_BYTES:
        raise OSError("Firefox did not return a PNG within the attachment limit.")
    return image


PAGE_CAPTURE_TTL_SECONDS = 600


@dataclass(frozen=True)
class PageCapturePending:
    pass


@dataclass(frozen=True)
class PageCaptureForSavedItem:
    saved: review_submissions.SavedSubmission


@dataclass
class PageCapture:
    expires: float
    state: (PageCapturePending | PageCaptureForSavedItem | new_item_dialog.PageScreenshot
            | new_item_dialog.FiledPageScreenshot) = field(default_factory=PageCapturePending)
    completed: threading.Event = field(default_factory=threading.Event)
    filing: bool = False
    discard_after_filing: bool = False


class PageCaptures:
    def __init__(self) -> None:
        self._lock: threading.Lock = threading.Lock()
        self._sessions: dict[str, PageCapture] = {}

    def _remove(self, key: str) -> None:
        self._sessions.pop(key).completed.set()

    def _active(self, key: str) -> PageCapture | None:
        capture = self._sessions.get(key)
        if capture is not None and not capture.filing and capture.expires <= time.monotonic():
            self._remove(key)
            return None
        return capture

    def start(self) -> str:
        """Reserve space for a dialog capture."""
        with self._lock:
            now = time.monotonic()
            for key in tuple(self._sessions):
                _ = self._active(key)
            if len(self._sessions) >= 8:
                filed = next((key for key, capture in self._sessions.items()
                              if not capture.filing and isinstance(capture.state, new_item_dialog.FiledPageScreenshot)), "")
                if not filed:
                    return ""
                self._remove(filed)
            key = secrets.token_hex(16)
            self._sessions[key] = PageCapture(now + PAGE_CAPTURE_TTL_SECONDS)
            return key

    def discard(self, key: str) -> None:
        with self._lock:
            capture = self._sessions.get(key)
            if capture is None or isinstance(capture.state, PageCaptureForSavedItem):
                return
            if capture.filing:
                capture.discard_after_filing = True
            else:
                self._remove(key)

    def mark_filed(self, key: str, image: new_item_dialog.PageScreenshot) -> None:
        """Keep only the digest for a retry of Send."""
        filed = new_item_dialog.FiledPageScreenshot(image.digest)
        with self._lock:
            capture = self._sessions.get(key)
            if capture is not None and capture.state is image:
                capture.state = filed

    def file(self, key: str, saved: review_submissions.SavedSubmission, *, allow_pending: bool = False
             ) -> new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot:
        """Claim a finished image or reserve the pending image for this item."""
        with self._lock:
            capture = self._active(key)
            if capture is None or not capture.filing or (allow_pending and capture.discard_after_filing):
                return new_item_dialog.NO_PAGE_SCREENSHOT
            state = capture.state
            if isinstance(state, new_item_dialog.PageScreenshot):
                capture.state = new_item_dialog.FiledPageScreenshot(state.digest)
                return state
            if allow_pending and isinstance(state, PageCapturePending):
                capture.state = PageCaptureForSavedItem(saved)
            return new_item_dialog.NO_PAGE_SCREENSHOT

    def preview(self, key: str) -> new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot:
        """Return the held PNG after the capture completes."""
        with self._lock:
            capture = self._active(key)
            if capture is None:
                return new_item_dialog.NO_PAGE_SCREENSHOT
        _ = capture.completed.wait(max(0, capture.expires - time.monotonic()))
        with self._lock:
            current = self._active(key)
            if current is not None and isinstance(current.state, new_item_dialog.PageScreenshot):
                return current.state
        return new_item_dialog.NO_PAGE_SCREENSHOT

    @contextmanager
    def for_filing(self, key: str, *, allow_pending: bool = False
                   ) -> Generator[new_item_dialog.SubmissionScreenshot | PageCapturePending | PageCaptureForSavedItem]:
        """Reserve the capture while Send files the item."""
        screenshot: (new_item_dialog.SubmissionScreenshot | PageCapturePending
                     | PageCaptureForSavedItem) = new_item_dialog.NO_PAGE_SCREENSHOT
        with self._lock:
            capture = self._active(key)
            if capture is not None and (
                    isinstance(capture.state, new_item_dialog.PageScreenshot)
                    or (allow_pending and isinstance(capture.state, PageCapturePending))):
                if capture.filing:
                    raise new_item_dialog.SubmissionConflict("This screenshot is in use. Try Send again.")
                screenshot = capture.state
                capture.filing = True
            else:
                if capture is not None and isinstance(capture.state, (
                        new_item_dialog.FiledPageScreenshot, PageCaptureForSavedItem)):
                    screenshot = capture.state
                capture = None

        if capture is None:
            yield screenshot
            return

        try:
            yield screenshot
        finally:
            with self._lock:
                if capture.discard_after_filing:
                    self._remove(key)
                else:
                    capture.expires = time.monotonic() + PAGE_CAPTURE_TTL_SECONDS
                    capture.filing = False

    def complete(self, key: str, image: new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot
                 ) -> review_submissions.SavedSubmission | None:
        """Hold the finished image or return the item that needs it."""
        with self._lock:
            capture = self._active(key)
            if capture is None:
                return None
            if isinstance(image, new_item_dialog.NoPageScreenshot):
                if capture.filing:
                    capture.discard_after_filing = True
                    capture.completed.set()
                else:
                    self._remove(key)
                return None
            if isinstance(capture.state, PageCaptureForSavedItem):
                saved = capture.state.saved
                capture.state = new_item_dialog.FiledPageScreenshot(image.digest)
                capture.completed.set()
                return saved
            capture.state = image
            capture.completed.set()
            return None


@dataclass(frozen=True)
class UsageStoreRevision:
    device: int
    inode: int
    modified_ns: int
    size: int


class MissingUsageStore(Enum):
    ABSENT = "usage store absent"


class UsageStoreRevisionUnavailable(OSError):
    """The server cannot check for a change in the usage store."""


def _usage_store_revision() -> UsageStoreRevision | MissingUsageStore:
    try:
        info = usage_store.store_file().stat()
    except FileNotFoundError:
        return MissingUsageStore.ABSENT
    except OSError as error:
        raise UsageStoreRevisionUnavailable("Cannot check the usage store file.") from error
    return UsageStoreRevision(info.st_dev, info.st_ino, info.st_mtime_ns, info.st_size)


@dataclass(frozen=True)
class AccountsRenderAttempt:
    revision: UsageStoreRevision | MissingUsageStore
    response: Future[bytes]
    following_response: Future[bytes] = field(default_factory=Future[bytes])


class NoAccountsRender(Enum):
    YET = "no render started"


@dataclass
class AccountsPageCache:
    """Keep the last good page and allow one render at a time."""

    page: bytes = b""
    rendered_at: float = 0.0
    page_revision: UsageStoreRevision | MissingUsageStore = MissingUsageStore.ABSENT
    render_failed: bool = False
    recent_times: deque[float] = field(default_factory=lambda: deque(maxlen=10))
    rendering: bool = False
    render_attempt: AccountsRenderAttempt | NoAccountsRender = NoAccountsRender.YET
    condition: threading.Condition = field(default_factory=threading.Condition)

    def get(self) -> bytes:
        try:
            refresh_seconds = conf.read("review_links").number("page_refresh_seconds")
        except (OSError, ValueError) as error:
            with self.condition:
                return self._read_error(error, "The Accounts refresh setting could not be read.")
        try:
            return self._get_for_period(refresh_seconds)
        except UsageStoreRevisionUnavailable as error:
            with self.condition:
                return self._read_error(error, "The Accounts usage store could not be checked.")

    def _get_for_period(self, refresh_seconds: float) -> bytes:
        with self.condition:
            revision = _usage_store_revision()
            previous = self.render_attempt
            current = (bool(self.page) and self.page_revision == revision
                       and time.monotonic() - self.rendered_at <= refresh_seconds)
            if current:
                if self.rendering:
                    return self._response()
                background = self._begin_render(revision)
                try:
                    threading.Thread(target=self._render, args=(background,), daemon=True).start()
                except RuntimeError:
                    self.rendering = False
                    self.render_attempt = previous
                    raise
                return self._response()
            attempt = (previous if self.rendering and isinstance(previous, AccountsRenderAttempt)
                       else self._begin_render(revision))

        if attempt is not previous:
            return self._render(attempt)
        if attempt.revision == revision:
            page = attempt.response.result()
            if revision == _usage_store_revision():
                return page
        return self._render_after(attempt)

    def _render_after(self, previous: AccountsRenderAttempt) -> bytes:
        with self.condition:
            _ = self.condition.wait_for(lambda: not self.rendering or self.render_attempt is not previous)
            attempt = (self._begin_render(_usage_store_revision()) if self.render_attempt is previous
                       else NoAccountsRender.YET)
        if isinstance(attempt, AccountsRenderAttempt):
            return self._render(attempt)
        return previous.following_response.result()

    def _begin_render(self, revision: UsageStoreRevision | MissingUsageStore) -> AccountsRenderAttempt:
        previous = self.render_attempt
        # Give requests that waited for the previous attempt this render's response.
        response = previous.following_response if isinstance(previous, AccountsRenderAttempt) else Future[bytes]()
        attempt = AccountsRenderAttempt(revision, response)
        self.render_attempt = attempt
        self.rendering = True
        return attempt

    def _render(self, attempt: AccountsRenderAttempt) -> bytes:
        try:
            page = self._replace(attempt.revision)
        except BaseException as error:
            attempt.response.set_exception(error)
            raise
        else:
            attempt.response.set_result(page)
            return page
        finally:
            with self.condition:
                self.rendering = False
                self.condition.notify_all()

    def _response(self) -> bytes:
        if self.render_failed:
            return self._page_with_error("The Accounts render failed.")
        return self.page

    def _page_with_error(self, message: str) -> bytes:
        mark = f'data-page-facts data-read-error="{html.escape(message, quote=True)}"'
        return self.page.replace(b'data-page-facts', mark.encode(), 1)

    def _read_error(self, error: OSError | ValueError, message: str) -> bytes:
        if not self.page:
            raise error
        print(f"review-links accounts: {error}", file=sys.stderr)
        return self._page_with_error(message)

    def _replace(self, revision: UsageStoreRevision | MissingUsageStore) -> bytes:
        try:
            rendered = accounts_page.render_with_facts()
            with self.condition:
                self.render_failed = False
                self.recent_times.append(rendered.seconds)
                self.page = rendered.with_recent_times(tuple(self.recent_times))
                self.rendered_at = time.monotonic()
                self.page_revision = revision
                return self.page
        except (OSError, ValueError) as error:
            with self.condition:
                if not self.page:
                    raise
                self.render_failed = True
                print(f"review-links accounts: {error}", file=sys.stderr)
                return self._response()


class TailscaleAddressLost(RuntimeError):
    """The server's bound address is no longer this machine's Tailscale address."""


class CodeChanged(RuntimeError):
    """A loaded module changed and the server must restart."""


class FixtureStoreRemoved(Exception):
    """The List fixture folder was removed."""


class ServerMode(Enum):
    LIVE = "live console"
    LIST_FIXTURE = "List fixture"


class ModuleStampUnavailable(Enum):
    UNREADABLE = "unreadable"


def loaded_module_stamps() -> dict[Path, tuple[int, int] | ModuleStampUnavailable]:
    checkout = Path(__file__).resolve().parents[2]
    stamps: dict[Path, tuple[int, int] | ModuleStampUnavailable] = {}
    resolved_directories: dict[Path, Path] = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str):
            continue
        module_path = Path(filename)
        directory = module_path.parent
        if directory not in resolved_directories:
            resolved_directories[directory] = directory.resolve()
        path = resolved_directories[directory] / module_path.name
        try:
            if module_path.name == ".." or path.is_symlink():
                path = path.resolve()
        except OSError:
            path = module_path.resolve()
        if path.is_relative_to(checkout):
            try:
                stat = path.stat()
                stamps[path] = (stat.st_mtime_ns, stat.st_size)
            except OSError:
                stamps[path] = ModuleStampUnavailable.UNREADABLE
    return stamps


def inherited_listener(address: tuple[str, int]) -> socket.socket | None:
    """Take the socket a restart passed on when it still listens on this address."""
    descriptor = os.environ.pop(LISTEN_FD, None)
    if descriptor is None:
        return None
    try:
        listener = socket.socket(fileno=int(descriptor))
    except (ValueError, OSError) as error:
        print(f"review-links: inherited socket {descriptor}: {error}", file=sys.stderr)
        return None
    if tuple(cast(tuple[str, int], listener.getsockname())[:2]) != address:
        listener.close()
        return None
    return listener


def restart(server: ReviewLinksServer) -> None:
    """Run the changed code in this process and keep the listening socket open, so no request is refused."""
    descriptor = server.socket.fileno()
    os.set_inheritable(descriptor, True)
    os.execve(sys.executable, sys.orig_argv, os.environ | {LISTEN_FD: str(descriptor)})


def tailscale_ip() -> str:
    result = subprocess.run(["tailscale", "ip", "-4"], capture_output=True,
                            text=True, check=True, timeout=conf.read("review_links").number("tailscale_timeout_seconds"))
    return str(ipaddress.IPv4Address(result.stdout.strip()))


def sweep_snapshots(root: Path, now: float) -> int:
    """Expire flat snapshots and legacy session snapshots without following symlinks."""
    retention = conf.read("review_links").number("snapshot_retention_seconds")
    removed = sweep_change_images(root, now)
    if not root.exists():
        return removed
    try:
        entries = list(root.iterdir())
    except OSError as error:
        print(f"review-links sweep: {root}: {error}", file=sys.stderr)
        return removed
    for entry in entries:
        try:
            # The console folder holds the browser token; the images folder holds registered images until a delete.
            if entry.name in (change_page.CONSOLE_FOLDER, image_store.IMAGES_FOLDER, image_store.CHANGE_FOLDER) or entry.is_symlink() or not entry.is_dir():
                continue
            children = list(entry.iterdir())
            # New snapshots contain regular files. Check symlinks before their
            # targets; a legacy session may contain only inaccessible links.
            flat = re.fullmatch(r"[a-z0-9]{6}", entry.name) is not None and not any(
                child.is_symlink() or child.is_dir() for child in children
            )
        except OSError as error:
            print(f"review-links sweep: {entry}: {error}", file=sys.stderr)
            continue
        for folder in [entry] if flat else children:
            try:
                if folder.is_symlink() or not folder.is_dir():
                    continue
                if folder.stat().st_mtime < now - retention:
                    shutil.rmtree(folder)
                    removed += 1
            except FileNotFoundError:
                continue
            except OSError as error:
                print(f"review-links sweep: {folder}: {error}", file=sys.stderr)
    return removed


def sweep_change_images(root: Path, now: float) -> int:
    """Remove old change images that no To review row names."""
    folder = root / image_store.CHANGE_FOLDER
    if folder.is_symlink() or not folder.is_dir():
        return 0
    retention = conf.read("review_links").number("change_image_unclaimed_days") * 86400
    try:
        claimed = {row.get(change_review.IMAGE, "") for path in change_review.lists_directory().glob(change_review.PREFIX + "*.md")
                   for row in change_review.pending(path)}
        entries = list(folder.iterdir())
    except (OSError, change_review.ChangeReviewError) as error:
        print(f"review-links sweep: {error}", file=sys.stderr)
        return 0
    removed = 0
    for entry in entries:
        try:
            if (image_store.IDENTITY.fullmatch(entry.name) and entry.name not in claimed
                    and not entry.is_symlink() and entry.is_dir() and entry.stat().st_mtime < now - retention):
                image_store.remove_change(root, entry.name)
                removed += 1
        except FileNotFoundError:
            continue
        except OSError as error:
            print(f"review-links sweep: {entry}: {error}", file=sys.stderr)
    return removed


def old_console_link(root: Path, path: str) -> str:
    """Give the fixed console address for an older console, browser or rebuild link."""
    parts = urlsplit(path)
    match = re.fullmatch(r"/([a-z0-9]{6})/(" + "|".join(map(re.escape, (
        console.PAGE_NAME, change_page.PAGE_NAME, change_page.REBUILDS_NAME))) + ")", parts.path)
    # A selected-row page keeps its own signed link.
    if match is None or (match[2] == change_page.PAGE_NAME and (root / match[1] / change_page.MANIFEST).exists()):
        raise FileNotFoundError("not an older console link")
    page = "" if match[2] == console.PAGE_NAME else match[2]
    return f"/{change_page.CONSOLE_FOLDER}/{page}" + (f"?{parts.query}" if parts.query else "")


def accepts_gzip(header: str, *, allow_wildcard: bool = False) -> bool:
    """Accept gzip when its encoding token has a positive quality."""
    wildcard = False
    for part in header.split(','):
        encoding, *parameters = part.split(';')
        encoding = encoding.strip().lower()
        if encoding != 'gzip' and not (allow_wildcard and encoding == '*'):
            continue
        accepted = True
        for parameter in parameters:
            key, _, value = parameter.partition('=')
            if key.strip().lower() == 'q':
                accepted = re.fullmatch(r'(?:0(?:\.\d{0,3})?|1(?:\.0{0,3})?)', value.strip()) is not None and float(value) > 0
                break
        if encoding == 'gzip':
            return accepted
        wildcard = accepted
    return wildcard


class ReviewLinksHandler(SimpleHTTPRequestHandler):
    """Serve regular files within the snapshot root, without directory indexes."""

    @override
    def send_head(self) -> BinaryIO | None:
        server = cast(ReviewLinksServer, self.server)
        root = server.root
        route = urlsplit(self.path)
        fixture_options = route.path == review_page.NEW_ITEM_PATH and route.query == "page=list"
        if server.mode is ServerMode.LIST_FIXTURE and route.path != list_page.PAGE_PATH and not fixture_options:
            self.send_error(404)
            return None
        if route.path == f"/{change_page.CONSOLE_FOLDER}":
            self.redirect(f"/{change_page.CONSOLE_FOLDER}/" + (f"?{route.query}" if route.query else ""))
            return None
        if route.path == f"/{change_page.CONSOLE_FOLDER}/":
            try:
                with server.staff_read_scope():
                    content = console.render(console.read_pages(), console.read_rebuilds())
            except (OSError, ValueError, ImportError) as error:
                print(f"review-links console: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>The console is not available.</p>"))
                return None
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                             + "style-src 'unsafe-inline'; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'")
            self.end_headers()
            return BytesIO(content)
        if route.path == "/console/for-you.json":
            extra: dict[str, object] = {}
            if route.query == "view=console":
                extra["accounts_quota"] = {"level": accounts_page.QuotaLevel.NONE, "name": ""}
                try:
                    extra["accounts_quota"] = accounts_page.nav_quota(root)
                except (accounts_page.UnreadableQuotaState, accounts_page.InvalidQuotaThresholds) as error:
                    print(f"review-links accounts: {error}", file=sys.stderr)
                except (OSError, ValueError):
                    print("review-links accounts: could not read the quota alerts", file=sys.stderr)
            try:
                with server.staff_read_scope():
                    payload = (console.for_you_count(for_you.read()) if route.query == "view=count"
                               else console.for_you_payload())
            except (OSError, ValueError) as error:
                print(f"review-links actions: {error}", file=sys.stderr)
                self.json_response(503, {"available": False, **extra})
            else:
                self.json_response(200, {**payload, **extra})
            return None
        if route.path == accounts_page.PAGE_PATH:
            try:
                content = cast(ReviewLinksServer, self.server).accounts_cache.get()
            except (OSError, ValueError) as error:
                print(f"review-links accounts: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>The Accounts page is not available.</p>"))
                return None
            self.html_response(200, owned_document(content, "accounts"))
            return None
        if route.path in (investigations_page.PAGE_PATH, "/console/questions.html", "/console/changes.html"):
            self.redirect(review_page.legacy_url(route.path, route.query))
            return None
        if route.path == console_tune.PAGE_PATH:
            self.tune_token(route.query)
            return None
        if route.path == review_page.NEW_ITEM_PATH:
            return self.new_item_options()
        if route.path == review_page.ATTACHMENT_PATH:
            return self.review_attachment(route.query)
        if route.path == review_page.PAGE_PATH:
            try:
                fields = parse_qs(route.query)
                delivery = server.item_delivery_pending(fields.get('delivery', [''])[0])
                snapshot = server.review_cache.get()
                if 'body' in fields:
                    view = (review_page.ReviewBodyView.CARD if fields.get('view') == ['card']
                            else review_page.ReviewBodyView.TABLE)
                    try:
                        key = fields['body'][0]
                        include_done = view is review_page.ReviewBodyView.CARD or fields.get('saved') == ['1']
                        content = (snapshot.linked_card(root, key, view=view) if include_done else snapshot.body(key))
                    except LookupError as error:
                        message = html.escape(str(error))
                        content = (change_page.document(f'<p role="status">{message}</p>', 'Review card')
                                   if view is review_page.ReviewBodyView.CARD else message.encode())
                        self.review_response(content, 'text/html; charset=utf-8', version=snapshot.version, status=404)
                    else:
                        self.review_response(content, 'text/html; charset=utf-8', version=snapshot.version)
                    return None
                elif 'version' in fields and 'index' not in fields:
                    self.json_response(200, {'version':snapshot.version, 'facts':snapshot.facts(),
                                             'delivery_pending':delivery})
                    return None
                elif 'index' in fields:
                    content = snapshot.index_response()
                    self.review_response(content, 'application/json; charset=utf-8',snapshot.payload_gzip)
                    return None
                else:
                    content = snapshot.render(show=fields.get('show',['open'])[0])
            except (OSError, ValueError) as error:
                print(f"review-links review: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>The Review page is not available.</p>"))
                return None
            prepared = snapshot.page_gzip if fields.get('show',['open'])[0]=='open' else b''
            owned = owned_document(content, "review")
            self.review_response(owned, 'text/html; charset=utf-8', prepared if owned is content else b'')
            return None
        if route.path == list_page.PAGE_PATH:
            if route.query in (list_page.COUNTS_QUERY, list_page.COUNTS_AND_LINKS_QUERY):
                self.list_counts(include_linked_reviews=route.query == list_page.COUNTS_AND_LINKS_QUERY)
                return None
            fields = parse_qs(route.query)
            if "item-state" in fields:
                self.list_action_state(fields)
                return None
            delivery = server.item_delivery_pending(fields.get('delivery', [''])[0])
            try:
                with server.list_store_access(), server.staff_read_scope():
                    live = server.mode is ServerMode.LIVE
                    reviews = server.list_review_records()
                    content = list_page.render(root, include_counts=live, defer_counts=True,
                                               status=server.delivery_status,
                                               review_records=(list_page.LinkedReviewRead.AFTER_RENDER
                                                               if live and reviews is list_page.LinkedReviewRead.FROM_DISK
                                                               else reviews))
                    content = owned_document(content, "list")
            except (OSError, ValueError) as error:
                print(f"review-links list: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>The List page is not available.</p>"))
                return None
            self.html_response(200, content, delivery_pending=delivery)
            return None
        if route.path == images.PAGE_PATH:
            try:
                with server.staff_read_scope():
                    content = images.render(server.delivery_status)
            except (OSError, ValueError) as error:
                print(f"review-links images: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>The Images page is not available.</p>"))
                return None
            self.html_response(200, owned_document(content, "images"))
            return None
        try:
            location = old_console_link(root, self.path)
        except FileNotFoundError:
            pass
        else:
            self.redirect(location)
            return None
        try:
            branches = branch_page.page_at(root, self.path)
        except FileNotFoundError:
            pass
        except branch_page.PageSubmitted:
            self.html_response(200, change_page.document(f"<p>{branch_page.SUBMITTED}</p>"))
            return None
        except (OSError, ValueError) as error:
            print(f"review-links branches: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>This branch page is not available.</p>"))
            return None
        else:
            with server.branch_deletions_lock:
                if branches.folder in server.branch_deletions:
                    self.html_response(200, change_page.document(f"<p>{BRANCHES_DELETING}</p>"))
                    return None
            with server.staff_read_scope():
                content = branch_page.render(branches)
            self.html_response(200, content)
            return None
        try:
            change_browser.rebuilds_at(root, self.path)
            with server.staff_read_scope():
                content = change_browser.render_rebuilds()
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links rebuilds: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>The rebuild list is not available.</p>"))
            return None
        else:
            self.html_response(200, owned_document(content, "rebuilds"))
            return None
        try:
            browser = change_browser.page_at(root, self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links browser: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>This review page is not available.</p>"))
            return None
        else:
            try:
                with server.staff_read_scope():
                    content = change_browser.render(browser, server.delivery_status)
            except (OSError, ValueError, change_page.change_review.ChangeReviewError, change_page.Refusal) as error:
                print(f"review-links browser: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>This review page is not available.</p>"))
                return None
            self.html_response(200, content)
            return None
        try:
            page = change_page.page_at(root, self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links page: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>This review page is not available.</p>"))
            return None
        else:
            try:
                with server.staff_read_scope():
                    content = change_page.render(page, server.delivery_status)
            except (OSError, ValueError, change_page.change_review.ChangeReviewError, change_page.Refusal) as error:
                print(f"review-links render: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>This review page is not available.</p>"))
                return None
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            return BytesIO(content)
        try:
            decoded = unquote(urlsplit(self.path).path)
            if ".." in decoded.split("/"):
                raise FileNotFoundError("parent traversal")
            if any(private_name(part) for part in decoded.split("/")):
                raise FileNotFoundError("private publisher file")
            file = (root / decoded.lstrip("/")).resolve()
            if not file.is_relative_to(root) or not file.is_file():
                raise FileNotFoundError("outside root or not a file")
            if any(private_name(part) for part in file.relative_to(root).parts):
                raise FileNotFoundError("private publisher file")
            stream: BinaryIO = file.open("rb")
        except (OSError, ValueError, RuntimeError):
            self.html_response(404, change_page.document("<p>This review page is not available.</p>"))
            return None
        identity = published_console_page(root, decoded)
        if identity:
            with stream:
                content = owned_document(stream.read(), identity)
            self.html_response(200, content)
            return None
        try:
            stat = os.fstat(stream.fileno())
            json_file = file.suffix.lower() == ".json"
            accepts = ",".join(self.headers.get_all("Accept-Encoding", []))
            compressed = json_file and accepts_gzip(accepts, allow_wildcard=True)
            encoding = "-gzip" if compressed else ""
            etag = (f'"{stat.st_dev:x}-{stat.st_ino:x}-{stat.st_size:x}'
                    f'-{stat.st_mtime_ns:x}-{stat.st_ctime_ns:x}{encoding}"')
            modified = stat.st_mtime_ns // 1_000_000_000
            unchanged = self.static_file_unchanged(etag)
            size = stat.st_size
            if compressed and not unchanged:
                with stream:
                    wire = gzip.compress(stream.read(), compresslevel=1, mtime=0)
                stream = BytesIO(wire)
                size = len(wire)
            self.send_response(304 if unchanged else 200)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", self.date_time_string(modified))
            if json_file:
                self.send_header("Vary", "Accept-Encoding")
            if not unchanged:
                self.send_header("Content-Type", self.guess_type(str(file)))
                self.send_header("Content-Length", str(size))
                self.send_header("X-Content-Type-Options", "nosniff")
                if compressed:
                    self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            if unchanged:
                stream.close()
                return None
        except BaseException:
            stream.close()
            raise
        return stream

    def static_file_unchanged(self, etag: str) -> bool:
        """Check If-None-Match for the selected static representation."""
        return any(
            value.strip() == "*"
            or any(tag.strip().removeprefix("W/") == etag for tag in value.split(","))
            for value in self.headers.get_all("If-None-Match", [])
        )

    def html_response(self, status: int, content: bytes, *, delivery_pending: bool | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if delivery_pending is not None:
            self.send_header("X-Item-Delivery-Pending", str(delivery_pending).lower())
        self.end_headers()
        _ = self.wfile.write(content)

    def review_response(self, content: bytes, content_type: str, prepared: bytes = b'', *,
                        version: str = '', status: int = 200) -> None:
        """Compress the retained Review index and shell for transfer."""
        accepts = self.headers.get('Accept-Encoding','')
        compressed = accepts_gzip(accepts)
        if compressed:
            content = prepared or gzip.compress(content,compresslevel=1,mtime=0)
        self.send_response(status)
        self.send_header('Content-Type',content_type)
        self.send_header('Content-Length',str(len(content)))
        self.send_header('Cache-Control','no-store')
        self.send_header('Vary','Accept-Encoding')
        self.send_header('X-Content-Type-Options','nosniff')
        if version:
            self.send_header('X-Review-Version', version)
        if compressed:
            self.send_header('Content-Encoding','gzip')
        self.end_headers()
        _ = self.wfile.write(content)

    def failed_answer(self, page: change_page.ReviewPage, body: str, status: int, reason: str,
                      error: Exception) -> None:
        print(f"review-links answer: {error}", file=sys.stderr)
        server = cast(ReviewLinksServer, self.server)
        try:
            content = change_page.render(page, server.delivery_status, change_page.answer_notice(page, body, reason))
        except (OSError, ValueError, change_page.change_review.ChangeReviewError, change_page.Refusal) as render_error:
            print(f"review-links error page: {render_error}", file=sys.stderr)
            content = change_page.fallback_answer(page, change_page.answer_notice(page, body, reason))
        self.html_response(status, content)

    def redirect(self, location: str) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.wfile.flush()

    def do_POST(self) -> None:
        server = cast(ReviewLinksServer, self.server)
        route = urlsplit(self.path)
        if server.mode is ServerMode.LIST_FIXTURE and route.path != list_page.PAGE_PATH:
            self.send_error(404)
            return
        refusal = self.tailscale_refusal()
        if refusal:
            if route.path == console_tune.PAGE_PATH:
                self.json_response(403, {"reason": refusal})
            elif route.path == review_page.NEW_ITEM_PATH:
                self.json_response(403, {"error": refusal})
            else:
                self.html_response(403, change_page.document(f"<p>{html.escape(refusal)}</p>"))
            return
        if route.path == review_page.NEW_ITEM_PATH:
            self._write_post(self.new_item_post)
            return
        if route.path == review_page.ATTACHMENT_PATH:
            self.send_error(405)
            return
        if route.path == console_tune.PAGE_PATH:
            self.tune_post(route.query)
            return
        if not self.same_origin():
            self.send_error(403)
            return
        if route.query or self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.html_response(400, change_page.document("<p>Use the answer form on the review page.</p>"))
            return
        if route.path == review_page.PAGE_PATH:
            self._write_post(self.review_post)
            return
        if route.path == accounts_page.PAGE_PATH:
            self.accounts_post()
            return
        if route.path == images.PAGE_PATH:
            self._write_post(self.image_post)
            return
        if route.path == investigations_page.PAGE_PATH:
            self._write_post(self.investigation_post)
            return
        if route.path == list_page.PAGE_PATH:
            self._write_post(self.list_post)
            return
        if route.path == f"/{change_page.CONSOLE_FOLDER}/{console.NEWS_NAME}":
            self.news_post()
            return
        if self.is_disks_measure():
            self.disks_measure_post()
            return
        try:
            branches = branch_page.page_at(server.root, self.path)
        except FileNotFoundError:
            pass
        except branch_page.PageSubmitted:
            self.html_response(200, change_page.document(f"<p>{branch_page.SUBMITTED}</p>"))
            return
        except (OSError, ValueError) as error:
            print(f"review-links branches: {error}", file=sys.stderr)
            self.html_response(404, change_page.document("<p>This branch page is not available.</p>"))
            return
        else:
            self._write_post(partial(self.delete_branches, branches))
            return
        try:
            browser = change_browser.page_at(server.root, self.path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links browser: {error}", file=sys.stderr)
            self.html_response(404, change_page.document("<p>This review page is not available.</p>"))
            return
        else:
            self._write_post(partial(self.browser_answer, browser))
            return
        try:
            page = change_page.page_at(server.root, self.path)
        except (OSError, ValueError) as error:
            print(f"review-links page: {error}", file=sys.stderr)
            self.html_response(404, change_page.document("<p>This review page is not available.</p>"))
            return
        self._write_post(partial(self._change_post, page))

    def _write_post(self, action: Callable[[], None]) -> None:
        server = cast(ReviewLinksServer, self.server)
        try:
            action()
        finally:
            server.clear_staff_snapshot()

    def _change_post(self, page: change_page.ReviewPage) -> None:
        server = cast(ReviewLinksServer, self.server)
        body = ""
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("Empty answer")
            body = self.rfile.read(length).decode("utf-8")
            if parse_qs(body).get("outcome") == ["next"]:
                location = change_page.publish_next(page, body)
                self.redirect(location)
                return
            delivery = change_page.submit(page, body, server.delivery_status)
        except change_review.VerdictDurabilityUnconfirmed as error:
            try:
                self.failed_answer(page, body, 503, "The verdict was written. Durability is unconfirmed.", error)
            finally:
                change_page.recover_pending(server.delivery_status, only=error.cleanup)
            return
        except (ValueError, change_page.change_review.ChangeReviewError) as error:
            self.failed_answer(page, body, 400, "Check the answer and submit it again.", error)
            return
        except change_page.FeatureLogFailed as error:
            self.failed_answer(page, body, 503, "The feature log could not be updated.", error)
            return
        except (OSError, RuntimeError, subprocess.SubprocessError, change_page.Refusal) as error:
            self.failed_answer(page, body, 503, "The answer could not be saved. Try again.", error)
            return
        try:
            self.redirect(urlsplit(self.path).path)
        finally:
            # The request thread sends messages after it flushes the response.
            try:
                change_page.deliver(change_page.finish_verdict_cleanup(delivery, server.delivery_status), server.delivery_status)
            except OSError as error:
                print(f"review-links delivery status: {error}", file=sys.stderr)

    def delete_branches(self, page: branch_page.BranchPage) -> None:
        server = cast(ReviewLinksServer, self.server)
        with server.branch_deletions_lock:
            if page.folder in server.branch_deletions:
                self.html_response(200, change_page.document(f"<p>{BRANCHES_DELETING}</p>"))
                return
            if (page.folder / branch_page.USED).exists():
                self.html_response(200, change_page.document(f"<p>{branch_page.SUBMITTED}</p>"))
                return
            server.branch_deletions.add(page.folder)
        try:
            self.run_branch_deletion(page)
        finally:
            with server.branch_deletions_lock:
                server.branch_deletions.remove(page.folder)

    def run_branch_deletion(self, page: branch_page.BranchPage) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("empty branch selection")
            body = self.rfile.read(length).decode("utf-8")
            deletion = branch_page.submit(page, body)
        except ValueError as error:
            self.html_response(400, change_page.document(f"<p>{html.escape(str(error))}</p>"))
            return
        except OSError as error:
            print(f"review-links branch delete: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>The request could not be read. No branches were deleted.</p>"))
            return
        notice = (f"Deleting {len(deletion.selected)} branches. The result and a new link go to "
                  + f"{deletion.publisher_name} by message.")
        try:
            self.html_response(200, change_page.document(f"<p>{html.escape(notice)}</p>"))
            self.wfile.flush()
        finally:
            try:
                branch_page.deliver(deletion)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                print(f"review-links branch delivery: {error}", file=sys.stderr)

    def browser_answer(self, browser: change_browser.ReviewBrowser) -> None:
        server = cast(ReviewLinksServer, self.server)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("empty answer")
            body = self.rfile.read(length).decode("utf-8")
            delivery, answer = change_browser.submit(browser, body, server.delivery_status)
        except change_review.VerdictDurabilityUnconfirmed as error:
            try:
                self.browser_failure(503, "The verdict was written. Durability is unconfirmed.", error)
            finally:
                change_page.recover_pending(server.delivery_status, only=error.cleanup)
            return
        except (ValueError, change_page.change_review.ChangeReviewError) as error:
            self.browser_failure(400, str(error) if isinstance(error, change_page.QuestionAnswerRefused) else "Check the answer and submit it again.", error)
            return
        except change_page.FeatureLogFailed as error:
            self.browser_failure(503, "The feature log could not be updated.", error)
            return
        except (OSError, RuntimeError, subprocess.SubprocessError, change_page.Refusal) as error:
            self.browser_failure(503, "The answer could not be saved. Try again.", error)
            return
        try:
            self.json_response(200, {"answer": answer})
        finally:
            try:
                change_page.deliver(change_page.finish_verdict_cleanup(delivery, server.delivery_status), server.delivery_status)
            except OSError as error:
                print(f"review-links delivery status: {error}", file=sys.stderr)

    def tailscale_refusal(self) -> str:
        server = cast(ReviewLinksServer, self.server)
        try:
            if server.bound_address != server.request_tailscale_address():
                return "Open this page through this machine's Tailscale address."
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"review-links address: {error}", file=sys.stderr)
            return "The Tailscale address is not available."
        return ""

    def accepts_host(self, host: str) -> bool:
        """Accept one Host for the bound or published address on this port."""
        if not host or self.headers.get_all("Host", []) != [host]:
            return False
        server = cast(ReviewLinksServer, self.server)
        address, port = server.bound_address, server.server_port
        if host == f"{address}:{port}" or (port == 80 and host == address):
            return True
        published = review_link.link_host(bound_address=address).lower()
        host = host.lower()
        return bool(published) and (host == f"{published}:{port}" or (port == 80 and host == published))

    def same_origin(self, *, required: bool = False) -> bool:
        origins = self.headers.get_all("Origin", [])
        host = self.headers.get("Host", "")
        return self.accepts_host(host) and ((not required and not origins) or origins == [f"http://{host}"])

    def valid_request_token(self, token: object) -> bool:
        server = cast(ReviewLinksServer, self.server)
        return isinstance(token, str) and token.isascii() and secrets.compare_digest(token, server.new_item_token)

    def new_item_options(self) -> BinaryIO | None:
        """Return a New item token and current recipients."""
        server = cast(ReviewLinksServer, self.server)
        if self.command != "GET":
            self.send_error(405)
            return None
        query = parse_qs(urlsplit(self.path).query, keep_blank_values=True)
        if set(query) == {"capture_key"} and len(query["capture_key"]) == 1:
            return self.new_item_preview(query["capture_key"][0])
        selected = query.get("page", [])
        if query and (set(query) != {"page"} or len(selected) != 1 or selected[0] not in page_frame.OWNER_PAGES):
            self.json_response(400, {"error": "Open the dialog from a console page owner."})
            return None
        refusal = self.tailscale_refusal()
        if refusal or not self.same_origin():
            self.json_response(403, {"error": refusal or "Open the dialog from a console page owner."})
            return None
        try:
            fields: dict[str, object] = {"token": server.new_item_token}
            with server.list_store_access(), server.staff_read_scope():
                members = staff.snapshot()
                if selected:
                    owner = review_submissions.page_owner(selected[0], conf.read_or_repository("console"), members)
                    fields["page_owner"] = owner.id if isinstance(owner, staff.StaffRow) else ""
                    recipients = review_submissions.recipient_options(members, owner=owner)
                else:
                    recipients = review_submissions.recipient_options(members)
        except (OSError, ValueError) as error:
            print(f"review-links New item recipients: {error}", file=sys.stderr)
            self.json_response(503, {"error": "The recipients are unavailable."})
            return None
        if server.mode is ServerMode.LIST_FIXTURE:
            fields.update(token="", send_disabled="Sending is off on a practice List.")
        self.json_response(200, {**fields, "recipients": recipients})
        return None

    def new_item_preview(self, capture_key: str) -> BinaryIO | None:
        tokens = self.headers.get_all("X-Review-Token", [])
        if (len(tokens) != 1 or not self.valid_request_token(tokens[0])
                or not self.same_origin() or self.tailscale_refusal()):
            self.json_response(403, {"error": "The capture preview is not authorized."})
            return None
        try:
            key = new_item_dialog.read_capture_key(capture_key)
        except new_item_dialog.SubmissionError:
            self.json_response(404, {"error": "The page screenshot is unavailable."})
            return None
        capture = cast(ReviewLinksServer, self.server).page_captures.preview(key)
        if isinstance(capture, new_item_dialog.PageScreenshot):
            return self.attachment_response(capture.png, "image/png")
        self.json_response(404, {"error": "The page screenshot is unavailable."})
        return None

    def new_item_post(self) -> None:
        """File and publish a native record before sending its message."""
        server = cast(ReviewLinksServer, self.server)
        if self.path != review_page.NEW_ITEM_PATH or self.headers.get_content_type() != "application/json":
            self.json_response(400, {"error": "Use the New item dialog on Review."})
            return
        if not self.same_origin():
            self.json_response(403, {"error": "Use the New item dialog on Review."})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if len(lengths) != 1 or self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
                raise new_item_dialog.SubmissionError("Invalid New item request size.")
            size = int(lengths[0])
            if not 0 < size <= MAX_NEW_ITEM_BODY:
                raise new_item_dialog.SubmissionError("Invalid New item request size.")
            body = self.rfile.read(size)
            if len(body) != size:
                raise new_item_dialog.SubmissionError("Incomplete New item request.")
            payload = cast(object, json.loads(body.decode("utf-8")))
            if not isinstance(payload, dict):
                raise new_item_dialog.SubmissionError("Invalid New item request.")
            fields = cast(dict[str, object], payload)
            token = fields.get("token")
            valid_token = self.valid_request_token(token)
            draft = fields.get("request_id")
            list_draft_id = (draft if fields.get("page") == "list" and isinstance(draft, str)
                             and review_submissions.is_draft_id(draft) else "")
            browser_draft = fields.get("page") != "list" and review_submissions.is_draft_id(draft)
            # The browser draft ID can recover an existing Send after a restart.
            if not valid_token and (
                    "capture_key" not in fields or "page_capture" in fields or "discard_capture" in fields
                    or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
                    or not browser_draft):
                self.json_response(403, {"code": "stale_token", "error": STALE_NEW_ITEM_TOKEN})
                return
            if "page_capture" in fields:
                self.new_item_capture(new_item_dialog.read_page_capture(fields["page_capture"]))
                return
            if "discard_capture" in fields:
                server.page_captures.discard(new_item_dialog.read_capture_key(fields["discard_capture"]))
                self.json_response(200, {})
                return
            key = ""
            refusal: new_item_dialog.SubmissionError | None = None
            if not valid_token:
                refusal = new_item_dialog.SubmissionScreenshotExpired()
            request: new_item_dialog.NewItemRequest | new_item_dialog.UnverifiedDraftRetry | None = None

            def read_submission() -> new_item_dialog.NewItemRequest:
                nonlocal key
                try:
                    key = new_item_dialog.read_capture_key(fields["capture_key"]) if "capture_key" in fields else ""
                    item = new_item_dialog.read_request(fields, has_capture=bool(key))
                except new_item_dialog.SubmissionError:
                    raise
                except (ValueError, RecursionError) as error:
                    raise new_item_dialog.SubmissionError("Invalid New item JSON.") from error
                if key and (not item.page or not isinstance(
                        item.evidence, (new_item_dialog.PageDataEvidence, new_item_dialog.NoEvidence))):
                    raise new_item_dialog.SubmissionError("Open the New item dialog from a page owner.")
                return item

            if not list_draft_id:
                try:
                    request = read_submission()
                    if not key and request.page_state is not None and request.page_state.include_screenshot:
                        if browser_draft:
                            refusal = new_item_dialog.SubmissionScreenshotExpired()
                        elif not review_submissions.DRAFT_ID.fullmatch(request.request_id):
                            refusal = new_item_dialog.SubmissionScreenshotReload()
                except new_item_dialog.SubmissionError as error:
                    if not browser_draft or not isinstance(draft, str):
                        raise
                    request = new_item_dialog.UnverifiedDraftRetry(draft, error)
                    key = ""
        except (ValueError, RecursionError) as error:
            reason = str(error) if isinstance(error, new_item_dialog.SubmissionError) else "Invalid New item JSON."
            self.json_response(400, {"error": reason})
            return
        except OSError:
            self.json_response(400, {"error": "Could not read the New item request."})
            return
        legacy_page_draft = (isinstance(request, new_item_dialog.NewItemRequest) and request.page != "list"
                             and review_submissions.DRAFT_ID.fullmatch(request.request_id) is not None)
        try:
            with ExitStack() as captures:
                screenshot: new_item_dialog.SubmissionScreenshot = new_item_dialog.NO_PAGE_SCREENSHOT
                pending = False
                if list_draft_id:
                    def prepare_list_draft() -> review_submissions.ListDraftFiling:
                        nonlocal request, screenshot
                        item = read_submission()
                        request = item
                        if not key and item.page_state is not None and item.page_state.include_screenshot:
                            raise new_item_dialog.SubmissionScreenshotExpired()
                        capture = captures.enter_context(server.page_captures.for_filing(key))
                        if isinstance(capture, (new_item_dialog.PageScreenshot, new_item_dialog.FiledPageScreenshot)):
                            screenshot = capture
                        return review_submissions.ListDraftFiling(item, screenshot, bool(key))

                    created = server.review_cache.create_submission(
                        review_submissions.ListDraftSend(list_draft_id, prepare_list_draft))
                else:
                    assert request is not None
                    capture = captures.enter_context(server.page_captures.for_filing(
                        key if valid_token else "", allow_pending=legacy_page_draft))
                    pending = isinstance(capture, PageCapturePending)
                    claimed = isinstance(capture, PageCaptureForSavedItem)
                    if not isinstance(capture, (PageCapturePending, PageCaptureForSavedItem)):
                        screenshot = capture
                    capture_requested = bool(key) and (not legacy_page_draft or claimed)
                    created = server.review_cache.create_submission(
                        request, screenshot=screenshot, capture_requested=capture_requested, refusal=refusal)
                saved = created.saved
                details = saved.details
                edited = isinstance(saved, review_submissions.EditedSubmission)
                response = {"id": saved.id, "key": created.key, "version": created.version}
                on_list = bool(list_draft_id) or isinstance(request, new_item_dialog.NewItemRequest) and request.page == "list"
                if on_list:
                    response["list_key"] = list_page.add_submission(created).key
                if list_draft_id and edited:
                    request = None
                    screenshot = new_item_dialog.NO_PAGE_SCREENSHOT
                if (key and not edited and isinstance(screenshot, new_item_dialog.PageScreenshot)
                        and review_submissions.PAGE_SCREENSHOT in details.images):
                    server.page_captures.mark_filed(key, screenshot)
                main_request = request if isinstance(request, new_item_dialog.NewItemRequest) and (
                    request.page == "list" or review_submissions.DRAFT_ID.fullmatch(request.request_id) is not None) else None
                evidence_url = ""
                if ((legacy_page_draft and key) or details.images or details.page_state is not None
                        or (main_request is not None and main_request.page_state is not None)
                        or isinstance(details.evidence, new_item_dialog.PageDataEvidence)):
                    site = f"http://{server.bound_address}:{server.server_port}"
                    evidence_url = site + review_page.PAGE_PATH + "#" + urlencode({"item": created.key})
                status = server.delivery_status
                # Concurrent Sends wait for the delivery of the same item.
                with exclusive(review_submissions.image_folder(saved).parent / ".lock"):
                    delivery = review_actions.prepare_submission_delivery(
                        saved, saved.words, status, evidence_url=evidence_url)
                    if not on_list and main_request is None:
                        self.deliver_review(server, delivery)
                        path, row = saved.original_path, saved.request_id
                        message = status.metadata_page(change_page.ReviewPage(path, path.parent, (row,), ""))
                        if not isinstance(message, change_page.ReviewMessagePage) or not message.saved.get("submission_delivered"):
                            self.json_response(503, {"error": "Could not deliver the item. Try Send again."})
                            return
                if pending or (main_request is not None and main_request.page == "list"
                               and isinstance(screenshot, new_item_dialog.PageScreenshot)):
                    if review_submissions.PAGE_SCREENSHOT not in details.images:
                        screenshot = server.page_captures.file(key, saved, allow_pending=pending)
                    elif pending:
                        server.page_captures.discard(key)
        except new_item_dialog.SubmissionScreenshotReload:
            self.json_response(409, {"code": "reload_page", "error": SCREENSHOT_RELOAD})
            return
        except new_item_dialog.SubmissionScreenshotExpired as error:
            self.json_response(409, {"code": "screenshot_expired", "error": str(error)})
            return
        except new_item_dialog.SubmissionConflict as error:
            self.json_response(409, {"error": str(error)})
            return
        except new_item_dialog.SubmissionError as error:
            self.json_response(400, {"error": str(error)})
            return
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            print(f"review-links New item Send: {error}", file=sys.stderr)
            self.json_response(503, {"error": "Could not publish the item. Try Send again."})
            return
        if edited:
            response.update(code="already_filed",
                            error=f"This item was already filed as {saved.id}. Any change after that Send was not sent.")
        respond_first = on_list or main_request is not None
        try:
            if respond_first:
                self.json_response(409 if edited else 200, response)
        finally:
            if respond_first:
                self.deliver_review(server, delivery)
                if ((pending or on_list) and isinstance(screenshot, new_item_dialog.PageScreenshot)
                        and review_submissions.PAGE_SCREENSHOT not in details.images):
                    server.attach_page_screenshot(saved, screenshot.png)
            if saved.kind == new_item_dialog.SubmissionKind.BUG and (
                    details.page_state is not None or (main_request is not None and main_request.page_state is not None)):
                server.finish_bug_evidence(created, main_request, self.headers.get("Host", ""), screenshot)
        if not respond_first:
            self.json_response(409 if edited else 200, response)

    def new_item_capture(self, capture: new_item_dialog.PageCaptureRequest) -> None:
        """Start a screenshot only for a known page on this server."""
        server = cast(ReviewLinksServer, self.server)
        if not capture_page_key(server.root, capture.place):
            self.json_response(400, {"error": "Select a console document."})
            return
        firefox = shutil.which("firefox")
        if not firefox or not server.capture_slot.acquire(blocking=False):
            self.json_response(503, {"error": "The page screenshot is unavailable."})
            return
        key = server.page_captures.start()
        if not key:
            server.capture_slot.release()
            self.json_response(503, {"error": "The page screenshot is unavailable."})
            return
        url = f"http://{server.bound_address}:{server.server_port}{capture.place}"
        try:
            threading.Thread(target=server.capture_page, args=(key, firefox, url, capture), daemon=True).start()
        except RuntimeError:
            server.page_captures.discard(key)
            server.capture_slot.release()
            raise
        self.json_response(202, {"capture_key": key})

    def review_attachment(self, query: str) -> BinaryIO | None:
        """Serve an image named by a retained Review record."""
        server = cast(ReviewLinksServer, self.server)
        if self.tailscale_refusal() or not self.same_origin():
            self.send_error(403)
            return None
        fields = parse_qs(query, keep_blank_values=True)
        if set(fields) != {"key", "id"} or any(len(values) != 1 for values in fields.values()):
            self.send_error(404)
            return None
        try:
            item = server.review_cache.get().records[fields["key"][0]]
            origin = review_items.submission_origin(item)
            if not isinstance(origin, new_item_dialog.ReviewSubmission):
                raise FileNotFoundError()
            image = next((image for image in origin.images if image.id == fields["id"][0]), None)
            if image is None:
                raise FileNotFoundError()
            folder = review_submissions.image_folder(origin)
            path = folder / image.id / image.file
            media = ATTACHMENT_MEDIA[image.file]
            if path.resolve() != path:
                raise FileNotFoundError()
            with path.open("rb") as stream:
                content = stream.read(new_item_dialog.MAX_IMAGE_BYTES + 1)
            if not 0 < len(content) <= new_item_dialog.MAX_IMAGE_BYTES:
                raise FileNotFoundError()
        except (KeyError, OSError, ValueError):
            self.send_error(404)
            return None
        return self.attachment_response(content, media)

    def attachment_response(self, content: bytes, media: str) -> BinaryIO:
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.end_headers()
        return BytesIO(content)

    def review_post(self) -> None:
        """Save the review answer before its shared message delivery."""
        server = cast(ReviewLinksServer, self.server)
        try:
            body = self.rfile.read(int(self.headers.get("Content-Length", "0"))).decode("utf-8")
            key = review_page.review_fields(body)['row'][0]
            saved = server.review_cache.save(body, server.delivery_status)
        except review_items.ReviewRecordChanged as error:
            self.browser_failure(409, str(error), error)
            return
        except change_review.VerdictDurabilityUnconfirmed as error:
            server.review_cache.invalidate()
            try:
                self.browser_failure(503, "The verdict was written. Durability is unconfirmed. Refresh Review.", error)
            finally:
                change_page.recover_pending(server.delivery_status, only=error.cleanup)
                server.review_cache.invalidate()
            return
        except ValueError as error:
            self.browser_failure(400, str(error), error)
            return
        except (OSError, RuntimeError, change_page.Refusal) as error:
            self.browser_failure(503, "The answer could not be saved.", error)
            return
        pending = isinstance(saved.delivery, change_page.MessageDelivery)
        result: dict[str, object] = {'answer':saved.answer, 'version':saved.version, 'delivery_pending':pending}
        if isinstance(saved.detail, review_page.ConfirmedReviewDetail):
            result.update(detail_html=saved.detail.body, detail_revision=saved.detail.revision)
        elif isinstance(saved.detail, review_page.ClosedReviewDetail):
            result['detail_closed'] = True
        else:
            result['detail_unavailable'] = True
        if isinstance(saved.item, review_items.ReviewItem):
            later = saved.item.times.later
            result.update(state=saved.item.state, type=saved.item.type,
                          later=0 if isinstance(later, review_items.NoLaterTime) else later.timestamp()*1000)
        with server.item_delivery(pending) as check:
            if check:
                result['delivery'] = check
            try:
                self.json_response(200, result)
            finally:
                self.deliver_review(server, change_page.finish_verdict_cleanup(saved.delivery, server.delivery_status))
                if pending:
                    try:
                        server.review_cache.publish_delivery_result(key)
                    except (OSError, ValueError, RuntimeError) as error:
                        print(f"review-links Review delivery refresh {key}: {error}", file=sys.stderr)

    def deliver_review(self, server: ReviewLinksServer,
                       delivery: change_page.MessageDelivery | change_page.NoMessageDelivery) -> None:
        """Retry a failure before sending and leave the saved intent available to recovery."""
        status = server.delivery_status
        try:
            try:
                change_page.deliver(delivery, status)
            except Exception as error:
                print(f"review-links review delivery: {error}", file=sys.stderr)
                if isinstance(delivery, change_page.MessageDelivery):
                    ownership = status.request_attempts.get(delivery.attempt)
                    if ownership is not None and ownership.state == "claimed":
                        change_page.deliver(delivery, status)
        except Exception as error:
            print(f"review-links review delivery retry: {error}", file=sys.stderr)
        finally:
            if isinstance(delivery, change_page.MessageDelivery):
                try:
                    with status.locked():
                        _ = status.request_attempts.pop(delivery.attempt, None)
                except Exception as error:
                    print(f"review-links review delivery release: {error}", file=sys.stderr)
                server.review_cache.invalidate()

    def tune_token(self, query: str) -> None:
        """Return the console request token for a Go confirmation."""
        if self.command != "GET" or query:
            self.json_response(400, {"reason": "Use the Go button on the console page."})
            return
        refusal = self.tailscale_refusal()
        if refusal or not self.same_origin():
            self.json_response(403, {"reason": refusal or "Open the Go button on this console."})
            return
        server = cast(ReviewLinksServer, self.server)
        self.json_response(200, {"token": server.new_item_token})

    def tune_post(self, query: str) -> None:
        """Apply one setting from the shared Go button."""
        if not self.same_origin(required=True):
            self.json_response(403, {"reason": "Open the Go button on this console."})
            return
        try:
            if query or self.headers.get_content_type() != "application/x-www-form-urlencoded":
                raise ValueError("Use the Go button on the console page.")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid tune request size.")
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
        except ValueError as error:
            self.json_response(400, {"reason": str(error)})
            return
        tokens = fields.pop("token", [])
        if len(tokens) != 1 or not self.valid_request_token(tokens[0]):
            self.json_response(403, {"reason": "The Go token expired. Confirm again."})
            return
        if set(fields) != {"page", "key", "value"} or any(len(values) != 1 for values in fields.values()):
            self.json_response(400, {"reason": "Send one page, key and value."})
            return
        outcome = console_tune.save(fields["page"][0], fields["key"][0], fields["value"][0])
        result = outcome.result
        response: dict[str, object] = {
            "live_value": outcome.live.value if isinstance(outcome.live, conf.UsableLiveValue) else None,
        }
        if isinstance(result, accounts_page.Refused):
            self.json_response(400, {**response, "reason": result.reason})
        elif isinstance(result, accounts_page.SaveFailed):
            self.json_response(503, {**response, "reason": result.reason})
        else:
            self.json_response(200, {**response, "state": "saved"})

    def accounts_post(self) -> None:
        """Apply one Accounts page action."""
        quota_fields = {"quota_account", "quota_level", "quota_reset"}
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                raise ValueError("Invalid accounts request size.")
            fields = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            if (set(fields) not in ({"key", "value"}, {"group", "to"}, {"group", "clear"}, quota_fields)
                    or any(len(values) != 1 for values in fields.values())
                    or ("clear" in fields and fields["clear"] != ["first"])):
                raise ValueError("Send one Accounts action with its required fields.")
        except (ValueError, UnicodeError) as error:
            self.json_response(400, {"reason": str(error)})
            return
        server = cast(ReviewLinksServer, self.server)
        result = (accounts_page.clear_quota(server.root, fields["quota_account"][0], fields["quota_level"][0],
                                            fields["quota_reset"][0]) if set(fields) == quota_fields
                  else accounts_page.save_color(fields["key"][0], fields["value"][0]) if "key" in fields
                  else accounts_page.clear_next_move(fields["group"][0]) if "clear" in fields
                  else accounts_page.save_next_move(fields["group"][0], fields["to"][0]))
        if isinstance(result, accounts_page.Refused):
            self.json_response(400, {"reason": result.reason})
        elif isinstance(result, accounts_page.SaveFailed):
            self.json_response(503, {"reason": result.reason})
        else:
            cast(ReviewLinksServer, self.server).accounts_cache = AccountsPageCache()
            self.json_response(200, {"state": "saved"})

    def list_counts(self, *, include_linked_reviews: bool) -> None:
        """Serve the counts and any pending links after the List rows."""
        server = cast(ReviewLinksServer, self.server)
        if server.mode is not ServerMode.LIVE:
            self.send_error(404)
            return
        with server.staff_read_scope():
            try:
                members = staff.snapshot()
                content = list_page.counts_strip(members, staff_names.StaffNames(members))
            except (OSError, ValueError) as error:
                print(f"review-links List counts: {error}", file=sys.stderr)
                self.html_response(503, b"")
                return
            if include_linked_reviews:
                try:
                    content += list_page.render_review_updates(server.root, members, server.list_review_records())
                except (OSError, ValueError, change_review.ChangeReviewError) as error:
                    print(f"review-links List links: {error}", file=sys.stderr)
                    content += '<template data-list-review-read-error></template>'
            self.html_response(200, content.encode())

    def list_action_state(self, fields: dict[str, list[str]]) -> None:
        """Read the current item after an action response is lost."""
        server = cast(ReviewLinksServer, self.server)
        try:
            (identity,) = fields["item-state"]
            (keeper,) = fields.get("keeper", [])
            (park_request,) = fields.get("park-request", [""])
            with server.list_store_access(), server.staff_read_scope():
                current = list_page.saved_action_state(keeper, identity, status=server.delivery_status,
                                                       park_request=park_request, include_detail=True,
                                                       review_records=server.list_review_records())
        except ValueError as error:
            self.browser_failure(400, "The List item is unavailable.", error)
        except OSError as error:
            self.browser_failure(503, "The List item cannot be read. Try again.", error)
        else:
            self.json_response(200, current)

    def list_post(self) -> None:
        """Save List feedback before the shared sender starts."""
        server = cast(ReviewLinksServer, self.server)
        fixture = server.mode is ServerMode.LIST_FIXTURE
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100000:
                raise ValueError("invalid List request size")
            body = self.rfile.read(length).decode("utf-8")
            fields = parse_qs(body)
            if fixture and fields.get("action") != ["remove"]:
                raise ValueError("Only Remove is available in this demo.")
            with server.list_store_access():
                submission, current = list_page.save_action(
                    server.root, body, server.delivery_status,
                    review_records=server.list_review_records(), include_detail=True)
        except ValueError as error:
            self.browser_failure(400, str(error), error)
            return
        except OSError as error:
            self.browser_failure(503, "The List request could not be saved. Try again.", error)
            return
        saved_fields: dict[str, object] = {"detail_unavailable": True} if current is None else dict(current)
        if fixture:
            self.json_response(200, {"state": "saved", "delivery_pending": False, **saved_fields})
            return
        action = fields["action"][0]
        delivery = submission.delivery if isinstance(submission, list_page.ListUnparkSubmission) else submission
        pending = not isinstance(delivery, change_page.NoMessageDelivery)
        with server.item_delivery(pending) as check:
            try:
                state = {"read": "read", "question": "saved"}.get(action, "sent")
                reply = {"state": state, "delivery_pending": pending, **saved_fields}
                if check:
                    reply["delivery"] = check
                if current is not None and isinstance(submission, list_page.ListUnparkSubmission):
                    reply["unpark_outcome"] = submission.response_outcome(current).value
                self.json_response(200, reply)
            finally:
                try:
                    list_page.deliver(submission)
                except OSError as error:
                    print(f"review-links delivery status: {error}", file=sys.stderr)

    def investigation_post(self) -> None:
        """Save Mark read and return an error for the card when the save fails."""
        root = cast(ReviewLinksServer, self.server).root
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("empty investigation request")
            investigations_page.submit(root, self.rfile.read(length).decode("utf-8"))
        except ValueError as error:
            self.browser_failure(400, str(error), error)
            return
        except OSError as error:
            self.browser_failure(503, "The investigation could not be changed. Try again.", error)
            return
        self.json_response(200, {"state": "read"})

    def image_post(self) -> None:
        """Answer the browser before the shared sender delivers an image question."""
        server = cast(ReviewLinksServer, self.server)
        root = server.root
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("empty image request")
            delivery = images.submit(root, self.rfile.read(length).decode("utf-8"), server.delivery_status)
        except question_record.AnswerRefused as error:
            self.browser_failure(400, str(error), error)
            return
        except images.AnswerNotSent as error:
            self.browser_failure(503, f"the owner did not get the message ({error}). Try again.", error)
            return
        except ValueError as error:
            self.browser_failure(400, "Use the form on this page.", error)
            return
        except OSError as error:
            self.browser_failure(503, "The image could not be changed. Try again.", error)
            return
        try:
            self.json_response(200, {"state": "done"})
        finally:
            change_page.deliver(delivery, server.delivery_status)

    def news_post(self) -> None:
        """Record the page the console shows, and name the other pages that hold new items."""
        server = cast(ReviewLinksServer, self.server)

        def waiting_review(request: console.NewsRequest) -> frozenset[str]:
            return review_page.waiting_items(request, snapshot=server.review_cache.get())

        try:
            length = int(self.headers.get("Content-Length", "0"))
            opened = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True).get("open", [])
            pages: list[console.ConsolePage] = []
            with server.staff_read_scope():
                for page in console.read_pages():
                    if page.items is review_page.waiting_items:
                        page = replace(page, items=waiting_review)
                    elif page.items is images.waiting_images:
                        page = replace(page, items=partial(images.waiting_images, status=server.delivery_status))
                    pages.append(page)
                # Rebuilds has no navigation entry, so its request uses an empty name.
                if len(opened) != 1 or opened[0] not in {"", *(page.identity for page in pages)}:
                    raise ValueError("name one console page or none")
                fresh = console.news(server.root, tuple(pages), opened[0])
        except ValueError as error:
            self.browser_failure(400, "Name one console page.", error)
            return
        except (OSError, ImportError) as error:
            self.browser_failure(503, "The console is not available.", error)
            return
        self.json_response(200, {"new": list(fresh)})

    def is_disks_measure(self) -> bool:
        """Whether the request posts to the measure name beside the Disks page."""
        path = urlsplit(self.path).path
        if not path.endswith("/" + disks_page.MEASURE_NAME):
            return False
        try:
            return path == disks_page.measure_path(cast(ReviewLinksServer, self.server).root)
        except (OSError, ValueError) as error:
            print(f"review-links disks: {error}", file=sys.stderr)
            return False

    def disks_measure_post(self) -> None:
        """Start a Disks page measure in the background and answer at once with the slots and sessions in use now."""
        now = time.time()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            started = disks_page.start_measure(now, force=fields.get("force") == ["1"])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.browser_failure(503, "The measure could not start.", error)
            return
        try:
            report = slots.slot_report(now)
        except (OSError, ValueError) as error:
            print(f"review-links disks slots: {error}", file=sys.stderr)
            report = None
        try:
            folders = sessions.session_report()
        except (OSError, ValueError) as error:
            print(f"review-links disks sessions: {error}", file=sys.stderr)
            folders = None
        self.json_response(200, {"state": started.state.value, "target": started.target, "slots": report, "sessions": folders})

    def browser_failure(self, status: int, reason: str, error: Exception) -> None:
        print(f"review-links browser answer: {error}", file=sys.stderr)
        self.json_response(status, {"reason": reason})

    def json_response(self, status: int, fields: Mapping[str, object]) -> None:
        content = json.dumps(fields).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        _ = self.wfile.write(content)
        self.wfile.flush()

    @override
    def guess_type(self, path: str | os.PathLike[str]) -> str:
        if Path(path).suffix.lower() in (".html", ".htm"):
            return "text/html; charset=utf-8"
        guessed = super().guess_type(path)
        if guessed.startswith("image/"):
            return guessed
        return "text/plain; charset=utf-8"


class ConsoleReviewSnapshotCache(review_page.ReviewSnapshotCache):
    def linked_records(self) -> list_page.ImmediateReviewRecords:
        """Return the published records without waiting for a build."""
        with self._condition:
            snapshot = self._snapshot
        if isinstance(snapshot, review_page.SnapshotUnavailable):
            return list_page.LinkedReviewRead.FROM_DISK
        return snapshot.records


@dataclass(frozen=True)
class StaffSnapshotUnloaded:
    """The next reader must load staff from its authority."""


@dataclass(frozen=True)
class StaffSnapshotAttempt:
    """Keep the result and completion time of the last cache read."""

    finished_at: float
    result: staff.StaffSnapshot | staff.ReadStaffAuthority


class ReviewLinksServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], root: Path, *, mode: ServerMode = ServerMode.LIVE,
                 initialize_review: bool = False) -> None:
        settings = conf.read("review_links")
        self.mode: ServerMode = mode
        self.store_folder: Path = root.resolve()
        # Use relative paths in the fixture process so a request cannot recreate the store after removal.
        self.root: Path = Path(".") if mode is ServerMode.LIST_FIXTURE else self.store_folder
        self.bound_address: str = address[0]
        self.verified_tailscale_address: str = ""
        self._tailscale_address_lock: threading.Lock = threading.Lock()
        self.delivery_status: change_page.DeliveryStatus = change_page.DeliveryStatus(self.root)
        self._item_deliveries: set[str] = set()
        self._item_delivery_lock: threading.Lock = threading.Lock()
        self.branch_deletions: set[Path] = set()
        self.branch_deletions_lock: threading.Lock = threading.Lock()
        self._staff_snapshot_lock: threading.Lock = threading.Lock()
        self._staff_snapshot: StaffSnapshotAttempt | StaffSnapshotUnloaded = StaffSnapshotUnloaded()
        self.accounts_cache: AccountsPageCache = AccountsPageCache()
        self.review_cache: review_page.ReviewSnapshotCache = ConsoleReviewSnapshotCache(
            self.root, start_worker=False, initialize_on_start=initialize_review and mode is ServerMode.LIVE,
            staff_scope=self.staff_read_scope)
        self.new_item_token: str = secrets.token_urlsafe()
        self.capture_slot: threading.BoundedSemaphore = threading.BoundedSemaphore()
        self.page_captures: PageCaptures = PageCaptures()
        self.next_tailscale_check: float = time.monotonic() + settings.number("tailscale_check_seconds")
        self.next_sweep: float = time.monotonic() + settings.number("snapshot_sweep_seconds")
        self.question_recovery: threading.Thread | None = None
        self.alert_recovery: threading.Thread | None = None
        self.next_code_check: float = time.monotonic() + settings.number("code_check_seconds")
        self.module_stamps: dict[Path, tuple[int, int] | ModuleStampUnavailable] = loaded_module_stamps()
        if mode is ServerMode.LIVE:
            _ = sweep_snapshots(self.root, time.time())
        super().__init__(address, ReviewLinksHandler)
        if mode is ServerMode.LIST_FIXTURE:
            return
        recover_changes = change_page.recover_pending
        recover_list = list_page.recover_pending
        self.delivery_status.startup_recovery_pending.set()

        def recover_pending() -> None:
            try:
                recover_changes(self.delivery_status)
            finally:
                self.delivery_status.startup_recovery_pending.clear()
            recover_list()
        threading.Thread(target=recover_pending, daemon=True).start()
        self.review_cache.start()

    def staff_read_scope(self) -> AbstractContextManager[None]:
        """Supply one remote staff snapshot for a request or a Review build."""
        if staff.is_owner():
            return nullcontext()
        seconds = conf.read("review_links").number("staff_snapshot_seconds")
        with self._staff_snapshot_lock:
            attempt = self._staff_snapshot
            if isinstance(attempt, StaffSnapshotUnloaded) or time.monotonic() - attempt.finished_at >= seconds:
                result: staff.StaffSnapshot | staff.ReadStaffAuthority
                try:
                    result = staff.authority_snapshot()
                except staff.StaffUnreadable:
                    result = staff.ReadStaffAuthority()
                attempt = self._staff_snapshot = StaffSnapshotAttempt(time.monotonic(), result)
            if isinstance(attempt.result, staff.StaffSnapshot):
                return staff.supplied_snapshot(attempt.result)
            return nullcontext()

    def clear_staff_snapshot(self) -> None:
        with self._staff_snapshot_lock:
            self._staff_snapshot = StaffSnapshotUnloaded()

    @contextmanager
    def item_delivery(self, pending: bool) -> Generator[str]:
        """Return a temporary key for this delivery's status."""
        if not pending:
            yield ''
            return
        check = secrets.token_urlsafe()
        with self._item_delivery_lock:
            self._item_deliveries.add(check)
        try:
            yield check
        finally:
            with self._item_delivery_lock:
                self._item_deliveries.remove(check)

    def item_delivery_pending(self, check: str) -> bool:
        with self._item_delivery_lock:
            return check in self._item_deliveries

    def list_review_records(self) -> list_page.ImmediateReviewRecords:
        cache = self.review_cache
        if isinstance(cache, ConsoleReviewSnapshotCache) and cache.root == self.root:
            return cache.linked_records()
        return list_page.LinkedReviewRead.FROM_DISK

    def capture_page(self, key: str, firefox: str, url: str, capture: new_item_dialog.PageCaptureRequest) -> None:
        image: new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot = new_item_dialog.NO_PAGE_SCREENSHOT
        try:
            png = page_screenshot(firefox, url, capture)
            image = new_item_dialog.PageScreenshot(png, new_item_dialog.PageViewport(capture.width, capture.height, capture.dark))
        except (OSError, subprocess.SubprocessError):
            pass
        finally:
            self.capture_slot.release()
            saved = self.page_captures.complete(key, image)
        if saved is not None and isinstance(image, new_item_dialog.PageScreenshot):
            self.attach_page_screenshot(saved, image.png)

    def finish_bug_evidence(self, created: review_page.PublishedSubmission, request: new_item_dialog.NewItemRequest | None,
                            host: str, screenshot: new_item_dialog.SubmissionScreenshot) -> None:
        """Save the evidence after the response and delivery."""
        saved = created.saved
        if (request is not None and request.page_state is not None
                and isinstance(screenshot, new_item_dialog.PageScreenshot)):
            request = replace(request, page_state=replace(
                request.page_state, viewport=screenshot.viewport, include_screenshot=True))

        def capture(page: str, state: new_item_dialog.BugPageState) -> bool:
            if isinstance(screenshot, new_item_dialog.PageScreenshot):
                return self.review_cache.attach_page_screenshot(saved, screenshot.png)
            return request is not None and self.capture_bug_screenshot(saved, page, state, host)

        try:
            if review_submissions.save_bug_page_state(saved, created.key, request, capture):
                self.review_cache.publish_submission_evidence(saved.request_id)
        except Exception as error:
            self.review_cache.invalidate()
            print(f"review-links Bug evidence for {saved.id}: {' '.join(str(error).splitlines())}", file=sys.stderr)

    def capture_bug_screenshot(self, saved: review_submissions.SavedSubmission, page: str,
                               state: new_item_dialog.BugPageState, host: str) -> bool:
        """Capture the reported viewport before the case record becomes visible."""
        if state.viewport is None:
            return False
        try:
            address = urlsplit(state.url)
            if (address.scheme not in ("http", "https") or not address.hostname or address.username is not None
                    or address.netloc.casefold() != host.casefold() or re.search(r"[\\\x00-\x20\x7f]", state.url)):
                return False
            place = urlunsplit(("", "", address.path, address.query, address.fragment))
            capture = new_item_dialog.read_page_capture({
                "place": place, "width": state.viewport.width, "height": state.viewport.height,
                "dark": state.viewport.dark,
            })
        except ValueError:
            return False
        if capture_page_key(self.root, capture.place) != page:
            return False
        firefox = shutil.which("firefox")
        if not firefox or not self.capture_slot.acquire(blocking=False):
            return False
        try:
            url = f"http://{self.bound_address}:{self.server_port}{capture.place}"
            image = page_screenshot(firefox, url, capture)
        except (OSError, subprocess.SubprocessError):
            return False
        finally:
            self.capture_slot.release()
        return self.review_cache.attach_page_screenshot(saved, image)

    def attach_page_screenshot(self, saved: review_submissions.SavedSubmission, image: bytes) -> None:
        try:
            _ = self.review_cache.attach_page_screenshot(saved, image)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"review-links screenshot for {saved.id}: {error}", file=sys.stderr)

    def request_tailscale_address(self) -> str:
        """Reuse the startup address for the request guard."""
        with self._tailscale_address_lock:
            if self.verified_tailscale_address:
                return self.verified_tailscale_address
            address = tailscale_ip()
            if address == self.bound_address:
                self.verified_tailscale_address = address
            return address

    @override
    def server_close(self) -> None:
        self.review_cache.close()
        super().server_close()

    @contextmanager
    def list_store_access(self) -> Generator[None]:
        """Keep fixture file access separate from scratch removal."""
        if self.mode is ServerMode.LIVE:
            yield
            return
        try:
            with scratch.directory_record_lock(self.store_folder, wait=False) as locked:
                if not locked or not self.root.samefile(self.store_folder):
                    raise OSError("The List fixture folder is unavailable.")
                yield
        except scratch.ScratchError as error:
            raise OSError(str(error)) from error

    @override
    def server_bind(self) -> None:
        host, port = cast(tuple[str, int], self.server_address)
        listener = inherited_listener((host, port))
        if listener is None:
            super().server_bind()
            return
        # Point the server's own socket object at the inherited listening socket.
        _ = os.dup2(listener.fileno(), self.socket.fileno(), inheritable=False)
        listener.close()
        self.server_name: str = socket.getfqdn(host)
        self.server_port: int = port

    @override
    def service_actions(self) -> None:
        if self.mode is ServerMode.LIST_FIXTURE:
            try:
                present = self.root.samefile(self.store_folder)
            except FileNotFoundError:
                present = False
            if not present:
                raise FixtureStoreRemoved()
            return
        now = time.monotonic()
        if now >= self.next_tailscale_check:
            settings = conf.read("review_links")
            self.next_tailscale_check = now + settings.number("tailscale_check_seconds")
            try:
                address = tailscale_ip()
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                raise TailscaleAddressLost(f"tailscale address gone (was {self.bound_address})") from error
            if address != self.bound_address:
                raise TailscaleAddressLost(f"tailscale address changed (was {self.bound_address}, now {address})")
        if time.monotonic() >= self.next_sweep:
            settings = conf.read("review_links")
            self.next_sweep = time.monotonic() + settings.number("snapshot_sweep_seconds")
            try:
                _ = sweep_snapshots(self.root, time.time())
            except OSError as error:
                print(f"review-links sweep: {error}", file=sys.stderr)
        if now >= self.next_code_check:
            settings = conf.read("review_links")
            self.next_code_check = now + settings.number("code_check_seconds")
            stamps = loaded_module_stamps()
            for path, stamp in stamps.items():
                if (stamp is ModuleStampUnavailable.UNREADABLE or stamp[0] >= STARTED_NS
                        or (path in self.module_stamps and self.module_stamps[path] != stamp)):
                    with self.delivery_status.locked():
                        if self.delivery_status.request_attempts or self.delivery_status.question_jobs:
                            return
                    with self.branch_deletions_lock:
                        if self.branch_deletions:
                            return
                    raise CodeChanged(f"code changed: {path}")
            self.module_stamps.update(stamps)
            if (self.delivery_status.has_deferred_questions()
                    and (self.question_recovery is None or not self.question_recovery.is_alive())):
                self.question_recovery = threading.Thread(
                    target=change_page.recover_deferred_questions, args=(self.delivery_status,), daemon=True)
                self.question_recovery.start()
            if (self.delivery_status.has_deferred_alerts()
                    and (self.alert_recovery is None or not self.alert_recovery.is_alive())):
                self.alert_recovery = threading.Thread(
                    target=self.delivery_status.recover_deferred_alerts, daemon=True)
                self.alert_recovery.start()


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--port", type=int, default=conf.read("review_links").whole("server_port"), help="override the configured server port")
    _ = parser.add_argument("--list-fixture", type=Path, help="serve a private List store and exit when its folder is removed")
    args = parser.parse_args(arguments)
    fixture = cast(Path | None, args.list_fixture)
    try:
        if fixture is not None:
            fixture = fixture.expanduser().absolute()
            if not fixture.is_dir():
                raise ValueError(f"List fixture folder is missing or is not a directory: {fixture}")
            fixture = fixture.resolve(strict=True)
        address = tailscale_ip(), cast(int, args.port)
        if fixture is None:
            root = change_routing.review_root()
            root.mkdir(parents=True, exist_ok=True)
            service = ReviewLinksServer(address, root, initialize_review=True)
        else:
            root = fixture
            os.chdir(root)
            os.environ.update(REVIEW_LINKS_ROOT=".", SHOWRUNNER_STATE_DIR="records", STAFF_FILE="staff.json",
                              CHANGE_REVIEW_DIR="reviews", REBUILD_NEEDS_DIR="rebuild-needs")
            service = ReviewLinksServer(address, root, mode=ServerMode.LIST_FIXTURE)
        with service as server:
            server.verified_tailscale_address = address[0]
            print(f"review-links: serving {root} on {address[0]}:{server.server_port}", flush=True)
            try:
                server.serve_forever()
            except CodeChanged as error:
                print(f"review-links: {error}", file=sys.stderr)
                restart(server)
    except (KeyboardInterrupt, FixtureStoreRemoved):
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, TailscaleAddressLost) as error:
        print(f"review-links: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

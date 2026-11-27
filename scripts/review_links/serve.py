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
from dataclasses import asdict, dataclass, field, replace
from functools import lru_cache, partial
import html
import gzip
import hashlib
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
from urllib.parse import SplitResult, parse_qs, unquote, urlsplit, urlunsplit

# Detect file changes before the first stamp, including changes during imports.
STARTED_NS = time.time_ns()

from scripts.disks import disks_page, sessions
from scripts.aws_target import aws_page, machines_page
from scripts.build_queue import admission_client, admission_protocol as admission, kill_action, memory_feed, memory_files
from scripts.lib import atomic_file, conf, scratch
from scripts.lib.file_lock import exclusive
from scripts.lib.process_group import run_process_group
from scripts.production import change_routing, change_review, staff, the_list, tidy_wrapped
from scripts.review_links import accounts_page, branch_page, change_page, change_browser, console, for_you, image_store, images, investigations_page, list_page, review_items, review_page
from scripts.review_links import console_tune, go_ledger, new_item, new_item_dialog, page_frame, placement_disk, question_record, reply_tokens, review_actions, review_link, review_submissions, send_hold, staff_names, studio_page
from scripts.review_links.review_link import private_name, tailscale_ip as tailscale_ip
from scripts.whoami import usage_store

# A restart passes the listening socket to the new process in this variable.
LISTEN_FD = "REVIEW_LINKS_LISTEN_FD"
# Allow page state and rows in addition to the existing words and image budget.
MAX_NEW_ITEM_BODY = (4 * ((new_item_dialog.MAX_TOTAL_IMAGE_BYTES + 2) // 3) + 256 * 1024
                     + new_item_dialog.MAX_PAGE_DATA_BYTES + new_item_dialog.MAX_BUG_PAGE_STATE_BYTES)
QUEUE_MOVE_PATH = "/console/builds/queue-move"
# A send to one of these pages, or to a change browser, can wait for Undo. The largest is a New item with its images.
HELD_SEND_PATHS = frozenset((review_page.PAGE_PATH, list_page.PAGE_PATH, studio_page.PAGE_PATH,
                            images.PAGE_PATH, review_page.NEW_ITEM_PATH))
HELD_SEND_LIMIT = MAX_NEW_ITEM_BODY
SCREENSHOT_RELOAD = "Reload the page to send the screenshot."
STALE_NEW_ITEM_TOKEN = "The New item token expired. Try Send again."
ATTACHMENT_MEDIA = {f"image.{extension}": media for media, extension in new_item_dialog.IMAGE_FORMATS.items()}
GZIP_FILE_SUFFIXES = frozenset({".html", ".htm", ".css", ".js", ".json", ".svg", ".txt"})


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
    placement_disk.PAGE_PATH: "placement_disk", studio_page.PAGE_PATH: "list",
}

# Each List view answers its page, its item-state reads and its item actions at its own path.
LIST_VIEWS = {view.path: view for view in (list_page.LIST_VIEW, studio_page.STUDIO_VIEW)}


def capture_page_key(root: Path, place: str) -> str:
    """Identify a console document that Firefox can open."""
    route = urlsplit(place)
    if route.netloc or route.scheme:
        return ""
    key = CONSOLE_DOCUMENTS.get(route.path) or published_console_page(root, unquote(route.path))
    if key == "review" and {"body", "version", "index"} & parse_qs(route.query, keep_blank_values=True).keys():
        return ""
    return key


def deferred_capture_request(root: Path, page: str, value: object
                             ) -> new_item_dialog.PageCaptureRequest | new_item_dialog.UnavailableCapture:
    """Check if the saved item can get a screenshot."""
    try:
        request = new_item_dialog.read_page_capture(value)
        if capture_page_key(root, request.place) == page:
            return request
    except ValueError:
        pass
    return new_item_dialog.UnavailableCapture.UNAVAILABLE


def owned_document(content: bytes, page: str) -> bytes:
    """Read the current owner when serving a console document."""
    try:
        members = staff.local_snapshot()
        owners = review_submissions.page_owners(page, conf.read_or_repository("console"), members)
        if isinstance(owners.route, review_submissions.PageOwnerRoute):
            recipient = owners.route.row.name
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


def gzip_content(content: bytes) -> bytes:
    return gzip.compress(content, compresslevel=1, mtime=0)


_cached_gzip_content = lru_cache(maxsize=8)(gzip_content)


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
        run_process_group([firefox, "--headless", "--no-remote", "--profile", folder,
                           "--window-size", f"{request.width},{request.height}", "--screenshot", str(output), url],
                          timeout=15).check_returncode()
        with output.open("rb") as stream:
            image = stream.read(new_item_dialog.MAX_IMAGE_BYTES + 1)
    if not image.startswith(b"\x89PNG\r\n\x1a\n") or len(image) > new_item_dialog.MAX_IMAGE_BYTES:
        raise OSError("Firefox did not return a PNG within the attachment limit.")
    return image


PAGE_CAPTURE_TTL_SECONDS = 600
PAGE_CAPTURE_PREVIEW_SECONDS = 20
PAGE_CAPTURE_FOLDER = ".review-link-page-captures"
MAX_PAGE_CAPTURES = 8
PAGE_ATTACHMENT_RETRY_SECONDS = 5


@dataclass(frozen=True)
class PageCapturePending:
    request: new_item_dialog.PageCaptureRequest


@dataclass(frozen=True)
class PageCaptureToResume:
    request: new_item_dialog.PageCaptureRequest


class NoPageCaptureToResume(Enum):
    ABSENT = "no capture to resume"


class PagePreviewPending(Enum):
    PENDING = "preview pending"


@dataclass(frozen=True)
class PageCaptureForSavedItem:
    saved: review_submissions.SavedSubmission


class UnavailableBoundCapture(Enum):
    UNAVAILABLE = "screenshot unavailable"


class CaptureReservation(Enum):
    STARTED = "new capture"
    PRESENT = "capture already exists"
    FULL = "capture store is full"


@dataclass
class PageCapture:
    expires: float
    state: (PageCapturePending | PageCaptureToResume | PageCaptureForSavedItem
            | new_item_dialog.PageScreenshot | new_item_dialog.FiledPageScreenshot | UnavailableBoundCapture)
    target: (new_item_dialog.ScopedSubmissionKey
             | new_item_dialog.NoBoundCaptureTarget) = new_item_dialog.NoBoundCaptureTarget.UNBOUND
    completed: threading.Event = field(default_factory=threading.Event)
    filing: bool = False
    discard_after_filing: bool = False
    attaching: bool = False


class PageCaptures:
    def __init__(self, folder: Path) -> None:
        self._folder: Path = folder
        self._lock: threading.Lock = threading.Lock()
        self._sessions: dict[str, PageCapture] = {}
        self._restore()

    def _restore(self) -> None:
        try:
            paths = sorted(self._folder.iterdir())
        except FileNotFoundError:
            return
        except OSError as error:
            print(f"review-links page captures: {error}", file=sys.stderr)
            return
        for path in paths:
            if not new_item_dialog.PAGE_CAPTURE_KEY.fullmatch(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                with path.open("rb") as stream:
                    header = cast(object, json.loads(stream.readline(32 * 1024)))
                    if not isinstance(header, dict):
                        raise ValueError("Invalid capture record.")
                    values = cast(dict[str, object], header)
                    expires = values.get("expires")
                    target = values.get("target")
                    now = time.time()
                    if type(expires) not in (int, float) or (
                            not isinstance(target, str)
                            and not now < cast(float, expires) <= now + PAGE_CAPTURE_TTL_SECONDS):
                        raise ValueError("Expired capture record.")
                    state: (PageCaptureToResume | new_item_dialog.PageScreenshot
                            | new_item_dialog.FiledPageScreenshot | UnavailableBoundCapture)
                    match values.get("kind"):
                        case "pending":
                            state = PageCaptureToResume(new_item_dialog.read_page_capture(values.get("request")))
                        case "ready":
                            viewport = new_item_dialog.read_capture_viewport(values.get("viewport"))
                            png = stream.read(new_item_dialog.MAX_IMAGE_BYTES + 1)
                            if not png.startswith(b"\x89PNG\r\n\x1a\n") or len(png) > new_item_dialog.MAX_IMAGE_BYTES:
                                raise ValueError("Invalid saved screenshot.")
                            state = new_item_dialog.PageScreenshot(png, viewport)
                        case "filed" | "attached":
                            digest = values.get("digest")
                            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                                raise ValueError("Invalid screenshot digest.")
                            state = new_item_dialog.FiledPageScreenshot(digest)
                        case "unavailable" if isinstance(target, str):
                            state = UnavailableBoundCapture.UNAVAILABLE
                        case _:
                            raise ValueError("Invalid capture state.")
                capture = PageCapture(cast(float, expires), state)
                if isinstance(target, str):
                    capture.target = new_item_dialog.read_submission_key(target)
                if not isinstance(state, PageCaptureToResume):
                    capture.completed.set()
                self._sessions[path.name] = capture
            except (OSError, ValueError, RecursionError):
                self._forget(path.name)

    def _forget(self, key: str) -> None:
        try:
            (self._folder / key).unlink()
            atomic_file.sync_folder(self._folder)
        except FileNotFoundError:
            pass
        except OSError as error:
            print(f"review-links page capture cleanup: {error}", file=sys.stderr)

    def _save(self, key: str, capture: PageCapture) -> None:
        state = capture.state
        header: dict[str, object] = {"expires": capture.expires}
        if isinstance(capture.target, new_item_dialog.ScopedSubmissionKey):
            target = capture.target
            header["target"] = new_item_dialog.submission_key(
                target.kind, target.recipient_id, target.item_id, target.request_id)
        png = b""
        match state:
            case PageCapturePending(request) | PageCaptureToResume(request):
                header.update(kind="pending", request=asdict(request))
            case new_item_dialog.PageScreenshot(png, viewport):
                header.update(kind="ready", viewport=asdict(viewport))
            case new_item_dialog.FiledPageScreenshot(digest):
                header.update(kind="filed", digest=digest)
            case UnavailableBoundCapture.UNAVAILABLE:
                header.update(kind="unavailable")
            case PageCaptureForSavedItem():
                self._forget(key)
                return
        try:
            self._folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            atomic_file.write_bytes_atomic(self._folder / key, json.dumps(header).encode() + b"\n" + png, mode=0o600)
        except OSError as error:
            print(f"review-links page capture save: {error}", file=sys.stderr)

    def _remove(self, key: str) -> None:
        self._sessions.pop(key).completed.set()
        self._forget(key)

    def _active(self, key: str) -> PageCapture | None:
        capture = self._sessions.get(key)
        if (capture is not None and capture.target is new_item_dialog.NoBoundCaptureTarget.UNBOUND
                and not capture.filing and capture.expires <= time.time()):
            self._remove(key)
            return None
        return capture

    def _expire(self) -> None:
        for key in tuple(self._sessions):
            _ = self._active(key)

    def expire(self) -> None:
        with self._lock:
            self._expire()

    def start(self, request: new_item_dialog.PageCaptureRequest) -> str:
        """Reserve space for a dialog capture."""
        key = secrets.token_hex(16)
        return key if self.reserve(key, request) is CaptureReservation.STARTED else ""

    def reserve(self, key: str, request: new_item_dialog.PageCaptureRequest) -> CaptureReservation:
        """Reserve one worker for the key from the dialog."""
        with self._lock:
            self._expire()
            if key in self._sessions:
                return CaptureReservation.PRESENT
            active = sum(1 for capture in self._sessions.values() if not isinstance(
                capture.state, (new_item_dialog.FiledPageScreenshot, UnavailableBoundCapture)))
            if active >= MAX_PAGE_CAPTURES:
                return CaptureReservation.FULL
            capture = self._sessions[key] = PageCapture(time.time() + PAGE_CAPTURE_TTL_SECONDS, PageCapturePending(request))
            self._save(key, capture)
            return CaptureReservation.STARTED

    def target(self, key: str) -> new_item_dialog.ScopedSubmissionKey | new_item_dialog.NoBoundCaptureTarget:
        """Read the saved target before Send can file another item."""
        with self._lock:
            capture = self._active(key)
            return capture.target if capture is not None else new_item_dialog.NoBoundCaptureTarget.UNBOUND

    def bind(self, key: str, submitted_draft_id: str,
             target: new_item_dialog.ScopedSubmissionKey) -> new_item_dialog.SubmissionScreenshot:
        """Keep the item with the capture so the worker can attach the screenshot."""
        if not new_item_dialog.capture_key_matches_draft(key, submitted_draft_id):
            raise new_item_dialog.SubmissionConflict("The screenshot belongs to another draft.")
        with self._lock:
            capture = self._active(key)
            if capture is None:
                return new_item_dialog.NO_PAGE_SCREENSHOT
            if capture.target not in (new_item_dialog.NoBoundCaptureTarget.UNBOUND, target):
                raise new_item_dialog.SubmissionConflict("This screenshot belongs to another item.")
            if capture.target is new_item_dialog.NoBoundCaptureTarget.UNBOUND:
                capture.target = target
                self._save(key, capture)
            if isinstance(capture.state, new_item_dialog.PageScreenshot):
                return capture.state
            return new_item_dialog.NO_PAGE_SCREENSHOT

    def unavailable(self, key: str, target: new_item_dialog.ScopedSubmissionKey) -> None:
        """Keep the outcome when the committed capture cannot produce an image."""
        with self._lock:
            capture = self._sessions.get(key)
            if capture is None:
                capture = self._sessions[key] = PageCapture(time.time(), UnavailableBoundCapture.UNAVAILABLE, target)
            elif capture.target in (target, new_item_dialog.NoBoundCaptureTarget.UNBOUND):
                if isinstance(capture.state, new_item_dialog.FiledPageScreenshot):
                    return
                capture.target = target
                capture.state = UnavailableBoundCapture.UNAVAILABLE
            else:
                raise new_item_dialog.SubmissionConflict("This screenshot belongs to another item.")
            capture.completed.set()
            self._save(key, capture)

    def restore_ready(self, key: str, target: new_item_dialog.ScopedSubmissionKey,
                      image: new_item_dialog.PageScreenshot) -> None:
        """Restore a missing lookup from the screenshot on the native record."""
        with self._lock:
            if key not in self._sessions:
                capture = self._sessions[key] = PageCapture(time.time(), image, target)
                capture.completed.set()
                self._save(key, capture)

    def bound_work(self) -> tuple[tuple[str, PageCaptureToResume | new_item_dialog.PageScreenshot,
                                        new_item_dialog.ScopedSubmissionKey], ...]:
        """Find unfinished attachments after a restart."""
        with self._lock:
            self._expire()
            return tuple((key, capture.state, capture.target) for key, capture in self._sessions.items()
                         if isinstance(capture.target, new_item_dialog.ScopedSubmissionKey)
                         and not capture.attaching
                         and isinstance(capture.state, (PageCaptureToResume, new_item_dialog.PageScreenshot)))

    @contextmanager
    def for_attachment(self, key: str, image: new_item_dialog.PageScreenshot) -> Generator[bool]:
        """Allow one worker at a time to attach this image."""
        with self._lock:
            capture = self._active(key)
            if capture is not None and capture.state is image and not capture.attaching:
                capture.attaching = True
            else:
                capture = None
        try:
            yield capture is not None
        finally:
            if capture is not None:
                with self._lock:
                    capture.attaching = False

    def resume(self, key: str) -> PageCaptureToResume | NoPageCaptureToResume:
        """Reserve an unfinished capture from the previous server."""
        with self._lock:
            capture = self._active(key)
            if capture is None or not isinstance(capture.state, PageCaptureToResume):
                return NoPageCaptureToResume.ABSENT
            resume = capture.state
            capture.state = PageCapturePending(resume.request)
            return resume

    def discard(self, key: str) -> None:
        with self._lock:
            capture = self._sessions.get(key)
            if (capture is None or isinstance(capture.state, PageCaptureForSavedItem)
                    or isinstance(capture.target, new_item_dialog.ScopedSubmissionKey)):
                return
            if capture.filing:
                capture.discard_after_filing = True
            else:
                self._remove(key)

    def mark_filed(self, key: str, image: new_item_dialog.PageScreenshot) -> None:
        """Keep the screenshot digest for Send retries."""
        filed = new_item_dialog.FiledPageScreenshot(image.digest)
        with self._lock:
            capture = self._sessions.get(key)
            if capture is not None and capture.state is image:
                capture.state = filed
                self._save(key, capture)

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
                self._save(key, capture)
                return state
            if allow_pending and isinstance(state, PageCapturePending):
                capture.state = PageCaptureForSavedItem(saved)
                self._save(key, capture)
            return new_item_dialog.NO_PAGE_SCREENSHOT

    def queued(self, key: str) -> bool:
        """Check whether a queued capture still has a destination."""
        with self._lock:
            capture = self._active(key)
            return capture is not None and isinstance(capture.state, (PageCapturePending, PageCaptureForSavedItem))

    def preview(self, key: str) -> new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot | PagePreviewPending:
        """Wait briefly for a preview and report whether it is still pending."""
        with self._lock:
            capture = self._active(key)
            if capture is None or isinstance(capture.state, PageCaptureToResume):
                return new_item_dialog.NO_PAGE_SCREENSHOT
        _ = capture.completed.wait(max(0, min(PAGE_CAPTURE_PREVIEW_SECONDS, capture.expires - time.time())))
        with self._lock:
            current = self._active(key)
            if current is not None and isinstance(current.state, new_item_dialog.PageScreenshot):
                return current.state
            if current is capture and not capture.completed.is_set():
                return PagePreviewPending.PENDING
        return new_item_dialog.NO_PAGE_SCREENSHOT

    @contextmanager
    def for_filing(self, key: str, *, allow_pending: bool = False
                   ) -> Generator[new_item_dialog.SubmissionScreenshot | PageCapturePending | PageCaptureForSavedItem]:
        """Reserve the capture while Send files the item."""
        if not key:
            yield new_item_dialog.NO_PAGE_SCREENSHOT
            return
        screenshot: (new_item_dialog.SubmissionScreenshot | PageCapturePending
                     | PageCaptureForSavedItem) = new_item_dialog.NO_PAGE_SCREENSHOT
        with self._lock:
            capture = self._active(key)
            if capture is not None and (
                    (isinstance(capture.state, new_item_dialog.PageScreenshot)
                     and capture.target is new_item_dialog.NoBoundCaptureTarget.UNBOUND)
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
                    capture.expires = time.time() + PAGE_CAPTURE_TTL_SECONDS
                    capture.filing = False
                    self._save(key, capture)

    def complete(self, key: str, image: new_item_dialog.PageScreenshot | new_item_dialog.NoPageScreenshot
                 ) -> review_submissions.SavedSubmission | new_item_dialog.ScopedSubmissionKey | None:
        """Hold the finished image or return the item that needs it."""
        with self._lock:
            capture = self._active(key)
            if capture is None or not isinstance(capture.state, (PageCapturePending, PageCaptureForSavedItem)):
                return None
            if isinstance(image, new_item_dialog.NoPageScreenshot):
                if isinstance(capture.target, new_item_dialog.ScopedSubmissionKey):
                    capture.state = UnavailableBoundCapture.UNAVAILABLE
                    capture.completed.set()
                    self._save(key, capture)
                elif capture.filing:
                    capture.discard_after_filing = True
                    capture.completed.set()
                else:
                    self._remove(key)
                return None
            if isinstance(capture.state, PageCaptureForSavedItem):
                saved = capture.state.saved
                capture.state = new_item_dialog.FiledPageScreenshot(image.digest)
                self._save(key, capture)
                capture.completed.set()
                return saved
            capture.state = image
            self._save(key, capture)
            capture.completed.set()
            return capture.target if isinstance(capture.target, new_item_dialog.ScopedSubmissionKey) else None


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


def accepts_gzip(header: str) -> bool:
    """Use the gzip quality if present. Otherwise, use the wildcard quality."""
    wildcard = False
    for part in header.split(','):
        encoding, *parameters = part.split(';')
        encoding = encoding.strip().lower()
        if encoding not in ('gzip', '*'):
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


def builds_operator(peer: tuple[str, int]) -> bool:
    """Authenticate the TCP peer through Tailscale."""
    def read(timeout: float, *args: str) -> dict[str, object]:
        result = subprocess.run(["tailscale", *args], capture_output=True, text=True,
                                timeout=timeout, check=True)
        return cast(dict[str, object], json.loads(result.stdout))

    try:
        settings = conf.read_or_repository("review_links")
        timeout = settings.number("tailscale_timeout_seconds")
        login = settings.value("operator_login", str, allow_blank=True)
        if not login:
            return False
        address, port = peer
        source = f"[{address}]:{port}" if ":" in address else f"{address}:{port}"
        identity = read(timeout, "whois", "--json", "--proto=tcp", source)
        node = cast(dict[str, object], identity["Node"])
        user = cast(dict[str, object], identity["UserProfile"])
        if node.get("Tags"):
            return False
        if login != "local-node-owner":
            return user.get("LoginName") == login
        local = cast(dict[str, object], read(timeout, "status", "--json")["Self"])
        owner = local.get("UserID")
        return not local.get("Tags") and type(owner) is int and owner > 0 and user.get("ID") == owner
    except (OSError, KeyError, TypeError, AttributeError, ValueError, subprocess.SubprocessError):
        return False


def held_delivery_result(outcome: change_page.DeliveryOutcome, no_message: bool = False) -> dict[str, object]:
    """Report the transport result without changing the status of the saved action."""
    if no_message:
        return {"no_message": True}
    if isinstance(outcome, change_page.FailedDelivery):
        return {"send_failure": "Delivery failed: " + outcome.last_error}
    if isinstance(outcome, change_page.DeliveryUnconfirmed):
        return {"send_failure": "The sender has not confirmed delivery."}
    return {}


class ReviewLinksHandler(SimpleHTTPRequestHandler):
    """Serve regular files within the snapshot root, without directory indexes."""

    @override
    def send_head(self) -> BinaryIO | None:
        server = cast(ReviewLinksServer, self.server)
        root = server.root
        route = urlsplit(self.path)
        fixture_options = route.path == review_page.NEW_ITEM_PATH and route.query == "page=list"
        held = route.path.startswith(send_hold.HOLD_PATH + "/")
        if server.mode is ServerMode.LIST_FIXTURE and route.path != list_page.PAGE_PATH and not fixture_options and not held:
            self.send_error(404)
            return None
        if held:
            # A result read is used once by the page, so HEAD gets no answer.
            if self.command == "GET":
                self.hold_result(route.path.removeprefix(send_hold.HOLD_PATH + "/"))
            else:
                self.send_error(405)
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
            self.html_response(200, content, content_security_policy="default-src 'self'; script-src 'unsafe-inline'; "
                               + "style-src 'unsafe-inline'; frame-src 'self'; frame-ancestors 'self'; base-uri 'none'")
            return None
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
                    if route.query == "view=count":
                        payload = console.for_you_count_view(for_you.read_for_status_line(include_reasons=True))
                    else:
                        payload = console.for_you_payload()
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
            self.html_response(200, owned_document(content, "accounts"), cache_gzip=True)
            return None
        if route.path in (investigations_page.PAGE_PATH, "/console/questions.html", "/console/changes.html"):
            self.redirect(review_page.legacy_url(route.path, route.query))
            return None
        if route.path == QUEUE_MOVE_PATH:
            self.queue_move_token(route.query)
            return None
        if route.path == kill_action.PATH:
            self.build_kill_options(route.query)
            return None
        if route.path == go_ledger.GO_CHECK_PATH:
            self.go_check(route.query)
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
                        message = str(error)
                        content = (change_page.document(page_frame.action_failure("review-card-failure", message), 'Review card')
                                   if view is review_page.ReviewBodyView.CARD else html.escape(message).encode())
                        self.review_response(content, 'text/html; charset=utf-8', version=snapshot.version, status=404)
                    else:
                        self.review_response(content, 'text/html; charset=utf-8', version=snapshot.version)
                    return None
                elif 'version' in fields and 'index' not in fields:
                    payload = {'version': snapshot.version, 'facts': snapshot.facts(), 'delivery_pending': delivery}
                    self.review_response(json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8")
                    return None
                elif 'index' in fields:
                    response = snapshot.full_index if fields.get('for-you') == ['1'] else snapshot.legacy_index
                    self.review_response(response.body, 'application/json; charset=utf-8', response.gzip_body)
                    return None
                else:
                    filters = parse_qs(route.query, keep_blank_values=True)
                    show = filters.get('show', filters.get('state', ['open']))[0]
                    kind = filters.get('type', [''])[0]
                    content = snapshot.render(show=show or 'open', kind=kind)
            except (OSError, ValueError) as error:
                print(f"review-links review: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>The Review page is not available.</p>"))
                return None
            owned = owned_document(content, "review")
            prepared = snapshot.page_gzip if content is snapshot.page and owned is content else b""
            self.encoded_response(200, owned, "text/html; charset=utf-8", {}, prepared, cache_gzip=True)
            return None
        if route.path in LIST_VIEWS:
            self.list_document(route.query, LIST_VIEWS[route.path])
            return None
        if route.path.startswith(f"{studio_page.AVATAR_PATH}/"):
            return self.studio_avatar(route.path.removeprefix(f"{studio_page.AVATAR_PATH}/"))
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
        if route.path == placement_disk.PAGE_PATH:
            try:
                content = placement_disk.render()
            except (OSError, ValueError) as error:
                print(f"review-links placement disk: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>The Placement disk POC page is not available.</p>"))
                return None
            self.html_response(200, owned_document(content, "placement_disk"))
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
            self.html_response(200, branch_page.empty_page())
            return None
        except (OSError, ValueError) as error:
            print(f"review-links branches: {error}", file=sys.stderr)
            self.html_response(503, branch_page.unavailable_page())
            return None
        else:
            with server.staff_read_scope():
                self.branch_response(branches)
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
            self.html_response(200, content)
            return None
        try:
            decoded = unquote(urlsplit(self.path).path)
            if ".." in decoded.split("/"):
                raise FileNotFoundError("parent traversal")
            if any(part == reply_tokens.KEY_NAME or private_name(part) for part in decoded.split("/")):
                raise FileNotFoundError("private publisher file")
            file = (root / decoded.lstrip("/")).resolve()
            if not file.is_relative_to(root) or not file.is_file():
                raise FileNotFoundError("outside root or not a file")
            if any(part == reply_tokens.KEY_NAME or private_name(part) for part in file.relative_to(root).parts):
                raise FileNotFoundError("private publisher file")
            stream: BinaryIO = file.open("rb")
        except (OSError, ValueError, RuntimeError):
            self.html_response(404, change_page.document("<p>This review page is not available.</p>"))
            return None
        identity = published_console_page(root, decoded)
        if identity:
            version = ""
            with stream:
                content = stream.read()
                if identity == "builds":
                    stat = os.fstat(stream.fileno())
                    version = f"{stat.st_ino:x}-{stat.st_mtime_ns:x}-{stat.st_ctime_ns:x}-{stat.st_size:x}"
            if version:
                marker = f'<meta name="builds-page-version" content="{version}">\n'
                content = content.replace(b"</head>", marker.encode() + b"</head>", 1)
            self.html_response(200, owned_document(content, identity), builds_version=version)
            return None
        try:
            stat = os.fstat(stream.fileno())
            headers = self.gzip_headers() if file.suffix.lower() in GZIP_FILE_SUFFIXES else {}
            compressed = "Content-Encoding" in headers
            encoding = "-gzip" if compressed else ""
            etag = (f'"{stat.st_dev:x}-{stat.st_ino:x}-{stat.st_size:x}'
                    f'-{stat.st_mtime_ns:x}-{stat.st_ctime_ns:x}{encoding}"')
            modified = stat.st_mtime_ns // 1_000_000_000
            unchanged = self.static_file_unchanged(etag)
            size = stat.st_size
            if compressed and not unchanged:
                with stream:
                    wire = gzip_content(stream.read())
                stream = BytesIO(wire)
                size = len(wire)
            self.send_response(304 if unchanged else 200)
            self.send_header("ETag", etag)
            self.send_header("Last-Modified", self.date_time_string(modified))
            if unchanged:
                _ = headers.pop("Content-Encoding", None)
            else:
                self.send_header("Content-Type", self.guess_type(str(file)))
                self.send_header("Content-Length", str(size))
                self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in headers.items():
                self.send_header(name, value)
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

    def gzip_headers(self) -> dict[str, str]:
        """Select the encoding headers for a response that can use gzip."""
        headers = {"Vary": "Accept-Encoding"}
        accepts = ",".join(self.headers.get_all("Accept-Encoding", []))
        if accepts_gzip(accepts):
            headers["Content-Encoding"] = "gzip"
        return headers

    def html_response(self, status: int, content: bytes, *, delivery_pending: bool | None = None,
                      builds_version: str = "", content_security_policy: str = "", cache_gzip: bool = False) -> None:
        headers: dict[str, str] = {}
        if delivery_pending is not None:
            headers["X-Item-Delivery-Pending"] = str(delivery_pending).lower()
        if builds_version:
            headers["X-Builds-Page-Version"] = builds_version
        if content_security_policy:
            headers["Content-Security-Policy"] = content_security_policy
        self.encoded_response(status, content, "text/html; charset=utf-8", headers, cache_gzip=cache_gzip)

    def review_response(self, content: bytes, content_type: str, prepared: bytes = b'', *,
                        version: str = '', status: int = 200) -> None:
        """Serve the Review response with any prepared gzip bytes."""
        headers = {"X-Review-Version": version} if version else {}
        self.encoded_response(status, content, content_type, headers, prepared)

    def encoded_response(self, status: int, content: bytes, content_type: str,
                         headers: dict[str, str], prepared: bytes = b"", *, cache_gzip: bool = False) -> None:
        headers.update(self.gzip_headers())
        if "Content-Encoding" in headers:
            compress = _cached_gzip_content if cache_gzip else gzip_content
            content = prepared or compress(content)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
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
        if (server.mode is ServerMode.LIST_FIXTURE and route.path != list_page.PAGE_PATH
                and not route.path.startswith(send_hold.HOLD_PATH + "/")):
            self.send_error(404)
            return
        if route.path == QUEUE_MOVE_PATH:
            self.queue_move_post(route.query)
            return
        if route.path == kill_action.PATH:
            self.build_kill_post(route.query)
            return
        refusal = self.tailscale_refusal()
        if refusal:
            if route.path == console_tune.PAGE_PATH or route.path.startswith(send_hold.HOLD_PATH + "/"):
                self.json_response(403, {"reason": refusal})
            elif route.path == review_page.NEW_ITEM_PATH:
                self.json_response(403, {"error": refusal})
            else:
                self.html_response(403, change_page.document(f"<p>{html.escape(refusal)}</p>"))
            return
        if route.path.startswith(send_hold.HOLD_PATH + "/"):
            self.undo_post(route)
            return
        if self.headers.get(send_hold.HOLD_HEADER) == "1" and self.held_send_path(route.path):
            request_ids = self.headers.get_all(send_hold.REQUEST_HEADER, [])
            if request_ids and (len(request_ids) != 1 or not send_hold.valid_hold_id(request_ids[0])):
                self.json_response(400, {"reason": "The send has no valid request ID."})
                return
            request_id = request_ids[0] if request_ids else ""
            seconds = conf.read("review_links").whole("undo_seconds")
            server = cast(ReviewLinksServer, self.server)
            if seconds or (request_id and server.send_holds.has_hold(request_id)):
                self.hold_post(route, seconds, request_id)
                return
        if route.path == review_page.NEW_ITEM_PATH:
            self._write_post(self.new_item_post)
            return
        if route.path == review_page.ATTACHMENT_PATH:
            self.html_response(405, change_page.document("<p>Use GET to open this attachment.</p>", "Review attachment"))
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
        if route.path in LIST_VIEWS:
            view = LIST_VIEWS[route.path]
            self._write_post(lambda: self.list_post(view))
            return
        if route.path == go_ledger.GO_PATH:
            self._write_post(self.go_post)
            return
        if route.path == f"/{change_page.CONSOLE_FOLDER}/{console.NEWS_NAME}":
            self.news_post()
            return
        if self.is_disks_measure():
            self.disks_measure_post()
            return
        try:
            _ = branch_page.page_at(server.root, self.path)
        except FileNotFoundError:
            pass
        except branch_page.PageSubmitted:
            self.html_response(200, branch_page.empty_page())
            return
        except (OSError, ValueError) as error:
            print(f"review-links branches: {error}", file=sys.stderr)
            self.html_response(404, branch_page.unavailable_page())
            return
        else:
            self._write_post(self.delete_branches)
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
            with reply_tokens.serving(server.store_folder):
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
            reason = str(error) if isinstance(error, change_page.ChangeHandoffRefused) else "Check the answer and submit it again."
            self.failed_answer(page, body, 400, reason, error)
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

    def branch_response(self, page: branch_page.BranchPage, *, status: int = 200, failure: str = "") -> None:
        action = f"/{page.folder.name}/{branch_page.PAGE_NAME}"
        self.html_response(status, branch_page.render(page, failure=failure, action=action))

    def delete_branches(self) -> None:
        server = cast(ReviewLinksServer, self.server)
        try:
            with server.branch_deletions_lock:
                page = branch_page.page_at(server.root, self.path)
                active = page.folder in server.branch_deletions
                if not active:
                    server.branch_deletions.add(page.folder)
        except branch_page.PageSubmitted:
            self.html_response(200, branch_page.empty_page())
            return
        except (OSError, ValueError) as error:
            print(f"review-links branches: {error}", file=sys.stderr)
            self.html_response(503, branch_page.unavailable_page())
            return
        if active:
            self.branch_response(page, status=409, failure="A branch deletion is already in progress.")
            return
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
            self.branch_response(page, status=400, failure=str(error))
            return
        except OSError as error:
            print(f"review-links branch delete: {error}", file=sys.stderr)
            self.branch_response(page, status=503, failure="The branch request could not be read.")
            return
        deleted, failure = branch_page.finish_deletion(deletion)
        server = cast(ReviewLinksServer, self.server)
        try:
            remaining = branch_page.page_at(server.root, self.path)
        except (OSError, ValueError) as error:
            print(f"review-links branch results: {error}", file=sys.stderr)
            self.html_response(503, branch_page.unavailable_page())
            return
        self.branch_response(branch_page.without_branches(remaining, deleted), failure=failure)

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
        if self.headers.get(send_hold.REPLAY_HEADER) == "1":
            delivery = change_page.finish_verdict_cleanup(delivery, server.delivery_status)
            result = self.deliver_review(server, delivery, report_outcome=True)
            self.json_response(200, {"answer": answer, **held_delivery_result(result, isinstance(delivery, change_page.NoMessageDelivery))})
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

    def build_kill_options(self, query: str) -> None:
        if query or self.command != "GET":
            self.json_response(400, {"reason": "Open Kill from the Builds page."})
            return
        if not self.build_kill_allowed():
            self.json_response(403, {"reason": "The Kill control requires the operator's Tailscale account."})
            return
        server = cast(ReviewLinksServer, self.server)
        try:
            refresh_seconds = conf.read_or_repository("review_links").number("page_refresh_seconds")
            offers, results = server.kill_actions.options(refresh_seconds)
        except (OSError, ValueError, admission.AdmissionUnavailable) as error:
            print(f"review-links build kill: {error}", file=sys.stderr)
            self.json_response(503, {"reason": "The current runs are unavailable."})
        else:
            self.json_response(200, {"token": server.new_item_token, "offers": offers, "results": results,
                                     "refresh_seconds": refresh_seconds})

    def build_kill_allowed(self, *, required_origin: bool = False) -> bool:
        if not self.same_origin(required=required_origin) or self.tailscale_refusal():
            return False
        server = cast(ReviewLinksServer, self.server)
        return server.mode is ServerMode.LIVE and builds_operator(self.client_address)

    def build_kill_post(self, query: str) -> None:
        if not self.same_origin(required=True):
            self.json_response(403, {"reason": "Open Kill from the Builds page."})
            return
        try:
            lengths = self.headers.get_all("Content-Length", [])
            if (query or self.headers.get_content_type() != "application/x-www-form-urlencoded"
                    or self.headers.get_all("Transfer-Encoding") or len(lengths) != 1):
                raise ValueError
            limit = conf.read_or_repository("review_links").whole("kill.request_bytes")
            length = int(lengths[0])
            if not 0 < length <= limit:
                raise ValueError
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            fields = parse_qs(body.decode(), keep_blank_values=True, max_num_fields=4, errors="strict")
            if (set(fields) != {"token", "reference", "handle", "words"}
                    or any(len(values) != 1 for values in fields.values())):
                raise ValueError
            reference, handle = fields["reference"][0], fields["handle"][0]
            if any(re.fullmatch(r"[0-9a-f]{64}", value) is None for value in (reference, handle)):
                raise ValueError
            words = fields["words"][0]
            if "\0" in words:
                raise ValueError
        except (OSError, ValueError):
            self.json_response(400, {"reason": "The Kill request is invalid or too large."})
            return
        if not self.valid_request_token(fields["token"][0]) or not self.build_kill_allowed(required_origin=True):
            self.json_response(403, {"reason": "The Kill request is not authorized. Confirm again."})
            return
        server = cast(ReviewLinksServer, self.server)
        try:
            outcome = server.kill_actions.stop(reference, handle, words)
        except (OSError, ValueError, admission.AdmissionUnavailable) as error:
            print(f"review-links build kill: {error}", file=sys.stderr)
            self.json_response(503, {"reason": "The current run is unavailable."})
            return
        if isinstance(outcome, kill_action.NoStopRoute):
            self.json_response(409, {"reason": "The run or its stop route changed."})
        else:
            self.json_response(200, {"result": outcome})

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
                if self.headers.get("X-New-Item-Route") == "producer":
                    fields.update(review_submissions.new_item_routing(
                        selected[0] if selected else "", conf.read_or_repository("console"), members))
                elif selected:
                    route = review_submissions.page_owners(selected[0], conf.read_or_repository("console"), members).route
                    if isinstance(route, review_submissions.PageOwnerRoute):
                        fields.update(page_owner=route.row.id, page_owner_reason=route.reason)
                        fields["recipients"] = review_submissions.recipient_options(members, owner=route.row)
                    else:
                        fields.update(page_owner="", page_owner_reason="")
                        fields["recipients"] = review_submissions.recipient_options(members, owner=route)
                else:
                    fields["recipients"] = review_submissions.recipient_options(members)
        except (OSError, ValueError) as error:
            print(f"review-links New item recipients: {error}", file=sys.stderr)
            self.json_response(503, {"error": "The recipients are unavailable."})
            return None
        if server.mode is ServerMode.LIST_FIXTURE:
            fields.update(token="", send_disabled="Sending is off on a practice List.")
        self.json_response(200, fields)
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
        server = cast(ReviewLinksServer, self.server)
        server.resume_page_capture(key)
        capture = server.page_captures.preview(key)
        if isinstance(capture, new_item_dialog.PageScreenshot):
            return self.attachment_response(capture.png, "image/png")
        if capture is PagePreviewPending.PENDING:
            self.json_response(202, {})
        else:
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
            if fields.get("kind") == new_item_dialog.SCHEDULED_KIND:
                if not valid_token:
                    self.json_response(403, {"code": "stale_token", "error": STALE_NEW_ITEM_TOKEN})
                    return
                scheduled = new_item_dialog.read_scheduled_request(fields)
                self.scheduled_item_post(server, scheduled)
                return
        except new_item_dialog.ScheduledDraftRequired as error:
            self.json_response(409, {"code": "wrong_draft_kind", "error": str(error)})
            return
        except (ValueError, RecursionError) as error:
            reason = str(error) if isinstance(error, new_item_dialog.SubmissionError) else "Invalid New item JSON."
            self.json_response(400, {"error": reason})
            return
        except OSError:
            self.json_response(400, {"error": "Could not read the New item request."})
            return
        with ExitStack() as claims:
            self.review_item_post(server, fields, token, valid_token, claims)

    def review_item_post(self, server: ReviewLinksServer, fields: dict[str, object], token: object,
                         valid_token: bool, claims: ExitStack) -> None:
        try:
            draft = fields.get("request_id")
            list_draft_id = (draft if fields.get("page") == "list" and isinstance(draft, str)
                             and review_submissions.is_draft_id(draft) else "")
            browser_draft = fields.get("page") != "list" and review_submissions.is_draft_id(draft)
            # A restart retry can use the token saved with the draft.
            if not valid_token and (
                    "capture_key" not in fields or "page_capture" in fields or "discard_capture" in fields
                    or "defer_capture" in fields
                    or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token)
                    or not browser_draft):
                self.json_response(403, {"code": "stale_token", "error": STALE_NEW_ITEM_TOKEN})
                return
            if "page_capture" in fields:
                key = new_item_dialog.read_capture_key(fields["capture_key"]) if "capture_key" in fields else ""
                self.new_item_capture(new_item_dialog.read_page_capture(fields["page_capture"]), key)
                return
            if "discard_capture" in fields:
                server.page_captures.discard(new_item_dialog.read_capture_key(fields["discard_capture"]))
                self.json_response(200, {})
                return
            has_capture_metadata = "defer_capture" in fields
            deferred: new_item_dialog.SubmissionCaptureIntent = new_item_dialog.NoDeferredCapture.ABSENT
            request: new_item_dialog.NewItemRequest | new_item_dialog.UnverifiedDraftRetry | None = None
            refusal: new_item_dialog.SubmissionError | None = None

            def read_submission() -> new_item_dialog.NewItemRequest:
                try:
                    item = new_item_dialog.read_request(
                        fields, has_capture="capture_key" in fields and not has_capture_metadata)
                except new_item_dialog.SubmissionError:
                    raise
                except (ValueError, RecursionError) as error:
                    raise new_item_dialog.SubmissionError("Invalid New item JSON.") from error
                if "capture_key" in fields and (not item.page or not isinstance(
                        item.evidence, (new_item_dialog.PageDataEvidence, new_item_dialog.NoEvidence))):
                    raise new_item_dialog.SubmissionError("Open the New item dialog from a page owner.")
                return item

            if not list_draft_id:
                try:
                    if not valid_token:
                        raise new_item_dialog.SubmissionScreenshotExpired()
                    request = read_submission()
                    if ("capture_key" not in fields and request.page_state is not None
                            and request.page_state.include_screenshot):
                        if browser_draft:
                            refusal = new_item_dialog.SubmissionScreenshotExpired()
                        elif not review_submissions.DRAFT_ID.fullmatch(request.request_id):
                            refusal = new_item_dialog.SubmissionScreenshotReload()
                except new_item_dialog.SubmissionError as error:
                    if not browser_draft or not isinstance(draft, str):
                        raise
                    request = new_item_dialog.UnverifiedDraftRetry(draft, error)
            try:
                key = new_item_dialog.read_capture_key(fields["capture_key"]) if "capture_key" in fields else ""
                if len(key) > 32 or has_capture_metadata:
                    page = fields.get("page")
                    if (not isinstance(draft, str)
                            or not (review_submissions.is_draft_id(draft) or page == "list"
                                    and review_submissions.DRAFT_ID.fullmatch(draft))
                            or not new_item_dialog.capture_key_matches_draft(key, draft)):
                        raise new_item_dialog.SubmissionError("The screenshot belongs to another draft.")
                    if has_capture_metadata and isinstance(page, str) and page in page_frame.OWNER_PAGES:
                        deferred = new_item_dialog.DeferredCaptureIntent(
                            key, draft, deferred_capture_request(server.root, page, fields["defer_capture"]))
            except new_item_dialog.SubmissionError as error:
                if not list_draft_id:
                    raise
                # Check for a saved List draft before validating a new capture.
                key, refusal = "", error
        except (ValueError, RecursionError) as error:
            reason = str(error) if isinstance(error, new_item_dialog.SubmissionError) else "Invalid New item JSON."
            self.json_response(400, {"error": reason})
            return
        except OSError:
            self.json_response(400, {"error": "Could not read the New item request."})
            return
        legacy_page_draft = (isinstance(request, new_item_dialog.NewItemRequest) and request.page != "list"
                             and review_submissions.DRAFT_ID.fullmatch(request.request_id) is not None)
        authorization = None
        if browser_draft and isinstance(token, str):
            digest = hashlib.sha256(token.encode("ascii")).hexdigest()
            authorization = (review_submissions.CurrentBrowserDraftToken(digest) if valid_token
                             else review_submissions.BrowserDraftRecoveryToken(digest))
        try:
            if request is not None and new_item_dialog.SCHEDULED_DRAFT_ID.fullmatch(request.request_id):
                with server.list_store_access(), server.staff_read_scope():
                    new_item.refuse_scheduled_draft_reuse(request.request_id)
            if key and valid_token and legacy_page_draft:
                server.resume_page_capture(key)
            has_deferred = isinstance(deferred, new_item_dialog.DeferredCaptureIntent)
            preview_key = key if valid_token and not has_deferred else ""
            capture_target = (server.page_captures.target(key) if valid_token and len(key) > 32
                              else new_item_dialog.NoBoundCaptureTarget.UNBOUND)
            site = f"http://{server.bound_address}:{server.server_port}"
            evidence = fields.get("evidence")
            has_evidence = (key or fields.get("images") or fields.get("page_state")
                            or isinstance(evidence, dict) and cast(dict[str, object], evidence).get("kind") == "page-data")
            list_assignee: review_submissions.ListCommandAssignee | review_submissions.ListWindowAssignee
            list_assignee = review_submissions.ListWindowAssignee.RECIPIENT
            if "list_assignee" in fields:
                if not list_draft_id or fields.get("page_recipient") == new_item_dialog.PageRecipient.PRODUCER:
                    raise new_item_dialog.SubmissionError("Send a List assignee with a List draft.")
                list_assignee = review_submissions.list_command_assignee(fields["list_assignee"])
            initial_delivery = review_submissions.InitialDelivery(site if has_evidence else "", list_assignee)
            with ExitStack() as captures:
                screenshot: new_item_dialog.SubmissionScreenshot = new_item_dialog.NO_PAGE_SCREENSHOT
                pending = False
                if list_draft_id:
                    def prepare_list_draft() -> review_submissions.ListDraftFiling:
                        nonlocal request, screenshot
                        if refusal is not None:
                            raise refusal
                        item = read_submission()
                        request = item
                        if not key and item.page_state is not None and item.page_state.include_screenshot:
                            raise new_item_dialog.SubmissionScreenshotExpired()
                        capture = captures.enter_context(server.page_captures.for_filing(preview_key))
                        if isinstance(capture, (new_item_dialog.PageScreenshot, new_item_dialog.FiledPageScreenshot)):
                            screenshot = capture
                        return review_submissions.ListDraftFiling(item, screenshot, bool(preview_key))

                    preview = (review_submissions.RequestedListPreview(preview_key) if preview_key
                               else review_submissions.NoRequestedListPreview.ABSENT)
                    submission = review_submissions.ListDraftSend(list_draft_id, prepare_list_draft, preview)
                    created = server.review_cache.create_submission(
                        submission, capture_intent=deferred, capture_target=capture_target,
                        initial_delivery=initial_delivery)
                else:
                    assert request is not None
                    capture = captures.enter_context(
                        server.page_captures.for_filing(preview_key, allow_pending=legacy_page_draft))
                    pending = isinstance(capture, PageCapturePending)
                    claimed = isinstance(capture, PageCaptureForSavedItem)
                    if not isinstance(capture, (PageCapturePending, PageCaptureForSavedItem)):
                        screenshot = capture
                    capture_requested = bool(preview_key) and (not legacy_page_draft or claimed)
                    with (review_submissions.browser_draft_token(authorization)
                          if authorization is not None else nullcontext()):
                        created = server.review_cache.create_submission(
                            request, screenshot=screenshot, capture_requested=capture_requested, refusal=refusal,
                            capture_intent=deferred, capture_target=capture_target, initial_delivery=initial_delivery)
                saved = created.saved
                details = saved.details
                edited = isinstance(saved, review_submissions.EditedSubmission)
                response: dict[str, object] = {"id": saved.id, "key": created.key, "version": created.version}
                if isinstance(saved.attachments, review_submissions.UnavailableAttachments):
                    if not valid_token:
                        self.json_response(403, {"code": "stale_token", "error": STALE_NEW_ITEM_TOKEN})
                        return
                    warnings = list(dict.fromkeys(
                        "The saved screenshot is missing or damaged." if reference == review_submissions.PAGE_SCREENSHOT
                        else "A saved pasted image is missing or damaged."
                        for reference in saved.attachments.references))
                    if edited:
                        warnings.append("The item keeps the values from the first Send.")
                    response["attachment_warning"] = "\n".join(warnings)
                on_list = bool(list_draft_id) or isinstance(request, new_item_dialog.NewItemRequest) and request.page == "list"
                if list_draft_id and edited:
                    request = None
                    screenshot = new_item_dialog.NO_PAGE_SCREENSHOT
                if (preview_key and not edited and isinstance(screenshot, new_item_dialog.PageScreenshot)
                        and review_submissions.PAGE_SCREENSHOT in details.images):
                    server.page_captures.mark_filed(key, screenshot)
                main_request = request if isinstance(request, new_item_dialog.NewItemRequest) and (
                    request.page == "list" or review_submissions.DRAFT_ID.fullmatch(request.request_id) is not None) else None
                has_saved_evidence = ((legacy_page_draft and key) or details.images or details.page_state is not None
                                      or (main_request is not None and main_request.page_state is not None)
                                      or isinstance(details.evidence, new_item_dialog.PageDataEvidence)
                                      or isinstance(details.capture_intent, new_item_dialog.DeferredCaptureIntent))
                evidence_page = site + review_page.PAGE_PATH if has_saved_evidence else ""
                status = server.delivery_status
                with exclusive(review_submissions.image_folder(saved).parent / ".lock"):
                    try:
                        delivery = review_actions.prepare_submission_delivery(
                            saved, saved.words, status, evidence_page=evidence_page, members=created.members,
                            claims=claims)
                    except review_actions.SubmissionDeliveryPending:
                        delivery = change_page.NoMessageDelivery()
                    if on_list or isinstance(request, new_item_dialog.NewItemRequest) and (
                            request.page_recipient == new_item_dialog.PageRecipient.PRODUCER):
                        number = the_list.reserved_review_number(review_submissions.list_filing_reservation(saved))
                        if isinstance(number, int):
                            response["list_number"] = number
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
            if valid_token:
                self.json_response(409, {"code": "screenshot_expired", "error": str(error)})
            else:
                # The dialog refreshes the token and retries the same draft.
                self.json_response(403, {"code": "stale_token", "error": STALE_NEW_ITEM_TOKEN})
            return
        except review_submissions.DraftAlreadyFiled as error:
            self.new_item_saved({"id": error.id}, error.id, True)
            return
        except review_submissions.DraftPending:
            self.pending_draft_response()
            return
        except new_item_dialog.SubmissionConflict as error:
            self.json_response(409, {"error": str(error)})
            return
        except new_item_dialog.SubmissionError as error:
            self.json_response(400, {"error": str(error)})
            return
        except (reply_tokens.UnavailableReplyToken, review_actions.SubmissionDeliveryPending) as error:
            print(f"review-links New item Send: {error}", file=sys.stderr)
            self.json_response(503, {"error": f"{error} Try Send again."})
            return
        except (OSError, ValueError, RuntimeError, TypeError) as error:
            print(f"review-links New item Send: {error}", file=sys.stderr)
            self.json_response(503, {"error": "Could not publish the item. Try Send again."})
            return
        respond_first = on_list or main_request is not None
        claim = delivery if isinstance(delivery, change_page.MessageDelivery) else None
        send = partial(self.deliver_review, server, delivery, report_outcome=True) if claim is not None else None
        try:
            try:
                if respond_first:
                    self.new_item_saved(response, saved.id, edited, deliver=send, claim=claim)
            finally:
                if respond_first:
                    if ((pending or on_list) and isinstance(screenshot, new_item_dialog.PageScreenshot)
                            and review_submissions.PAGE_SCREENSHOT not in details.images):
                        server.attach_page_screenshot(saved, screenshot.png)
                if saved.kind == new_item_dialog.SubmissionKind.BUG and (
                        details.page_state is not None or (main_request is not None and main_request.page_state is not None)):
                    server.finish_bug_evidence(created, main_request, self.headers.get("Host", ""), screenshot)
            if not respond_first:
                self.new_item_saved(response, saved.id, edited, deliver=send, claim=claim)
        finally:
            if isinstance(details.capture_intent, new_item_dialog.DeferredCaptureIntent):
                server.process_committed_capture(new_item_dialog.read_submission_key(saved.key), details.capture_intent,
                                                 attached=review_submissions.PAGE_SCREENSHOT in details.images)

    def scheduled_item_post(self, server: ReviewLinksServer, request: new_item_dialog.ScheduledSubmission) -> None:
        """File or recover a Scheduled item under the shared draft lock."""
        sent_at = server.send_holds.replay_started_at(self.headers.get(send_hold.REPLAY_TICKET_HEADER, ""),
                                                      received_at=the_list.now())
        try:
            with server.list_store_access(), server.staff_read_scope():
                saved = new_item.submit_scheduled(request, sent_at=sent_at)
        except review_submissions.DraftAlreadyFiled as error:
            self.new_item_saved({"id": error.id}, error.id, True)
            return
        except review_submissions.DraftPending:
            self.pending_draft_response()
            return
        except new_item.FilingRefused as error:
            status = 400 if error.reason is new_item.FilingRefusal.INVALID_SCHEDULE else 409
            self.json_response(status, {"error": str(error)})
            return
        except new_item_dialog.SubmissionConflict as error:
            self.json_response(409, {"error": str(error)})
            return
        except new_item_dialog.SubmissionError as error:
            self.json_response(400, {"error": str(error)})
            return
        except (OSError, ValueError, TypeError, staff.StaffRefused) as error:
            print(f"review-links Scheduled Send: {error}", file=sys.stderr)
            self.json_response(503, {"error": "Could not confirm the Scheduled save. Try again to recover this draft."})
            return
        claim = saved.claim
        response: dict[str, object] = {"id": saved.id, "list_key": f"{claim.keeper}/{saved.id}",
                                       "receipt": new_item.filing_receipt(claim.assignment, saved.id),
                                       "readback": claim.readback, "notice_warning": claim.notice_warning}
        send = (partial(new_item.deliver_scheduled_notices, review_link.review_root(), request.request_id)
                if saved.pending_notices else None)
        self.new_item_saved(response, saved.id, False, deliver=send)

    def pending_draft_response(self) -> None:
        self.json_response(409, {"code": "draft_pending", "error": "Retry the original Send for this draft."})

    def new_item_saved(self, response: Mapping[str, object], identity: str, edited: bool, *,
                       deliver: Callable[[], object] | None = None,
                       claim: change_page.MessageDelivery | None = None) -> None:
        try:
            if edited and "attachment_warning" not in response:
                self.json_response(409, {
                    **response, "code": "already_filed",
                    "error": f"This item was already filed as {identity}. Any change after that Send was not sent.",
                })
            else:
                self.json_response(200, response)
        finally:
            if deliver is not None:
                server = cast(ReviewLinksServer, self.server)
                _ = server.start_new_item_delivery(deliver, claim=claim)

    def new_item_capture(self, capture: new_item_dialog.PageCaptureRequest, key: str) -> None:
        """Start a screenshot only for a known page on this server."""
        server = cast(ReviewLinksServer, self.server)
        if not capture_page_key(server.root, capture.place):
            self.json_response(400, {"error": "Select a console document."})
            return
        if key:
            if len(key) <= 32:
                raise new_item_dialog.SubmissionError("Use a key for this draft.")
            if server.ensure_page_capture(key, capture):
                self.json_response(202, {"capture_key": key})
            else:
                self.json_response(503, {"error": "The page screenshot is unavailable."})
            return
        firefox = shutil.which("firefox")
        if not firefox or not server.capture_slot.acquire(blocking=False):
            self.json_response(503, {"error": "The page screenshot is unavailable."})
            return
        key = server.page_captures.start(capture)
        if not key:
            server.capture_slot.release()
            self.json_response(503, {"error": "The page screenshot is unavailable."})
            return
        if server.launch_page_capture(key, firefox, capture):
            self.json_response(202, {"capture_key": key})
        else:
            self.json_response(503, {"error": "The page screenshot is unavailable."})

    def review_attachment(self, query: str) -> BinaryIO | None:
        """Serve an image named by a retained Review record."""
        server = cast(ReviewLinksServer, self.server)
        if self.tailscale_refusal() or not self.same_origin():
            self.html_response(403, change_page.document("<p>Access to this attachment is forbidden.</p>", "Review attachment"))
            return None
        fields = parse_qs(query, keep_blank_values=True)
        try:
            if set(fields) != {"key", "id"} or any(len(values) != 1 for values in fields.values()):
                raise ValueError("Invalid attachment query.")
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
            self.html_response(404, change_page.document("<p>This attachment is not available.</p>", "Review attachment"))
            return None
        return self.attachment_response(content, media)

    def list_document(self, query: str, view: list_page.ListView) -> None:
        """Answer a List view: the page, or the List counts and item state it reads."""
        server = cast(ReviewLinksServer, self.server)
        if query in (list_page.COUNTS_QUERY, list_page.COUNTS_AND_LINKS_QUERY):
            self.list_counts(include_linked_reviews=query == list_page.COUNTS_AND_LINKS_QUERY)
            return
        fields = parse_qs(query)
        if "item-state" in fields:
            self.list_action_state(fields, view)
            return
        delivery = server.item_delivery_pending(fields.get('delivery', [''])[0])
        try:
            with server.list_store_access(), server.staff_read_scope():
                live = server.mode is ServerMode.LIVE
                reviews = server.list_review_records()
                content = list_page.render(server.root, include_counts=live, defer_counts=True,
                                           status=server.delivery_status, query=query,
                                           review_records=(list_page.LinkedReviewRead.AFTER_RENDER
                                                           if live and reviews is list_page.LinkedReviewRead.FROM_DISK
                                                           else reviews),
                                           view=view)
                content = owned_document(content, "list")
        except (OSError, ValueError) as error:
            print(f"review-links {view.log_name}: {error}", file=sys.stderr)
            self.html_response(503, change_page.document(f"<p>{html.escape(view.title)} page is not available.</p>"))
            return
        self.html_response(200, content, delivery_pending=delivery)

    def studio_avatar(self, relative: str) -> BinaryIO | None:
        """Serve one Studio picture from the avatar root."""
        if self.tailscale_refusal() or not self.same_origin():
            self.send_error(403)
            return None
        try:
            content = studio_page.avatar(relative)
        except (OSError, ValueError):
            self.send_error(404)
            return None
        return self.attachment_response(content, "image/webp", cache="max-age=86400")

    def attachment_response(self, content: bytes, media: str, *, cache: str = "no-store") -> BinaryIO:
        self.send_response(200)
        self.send_header("Content-Type", media)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache)
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
        if self.headers.get(send_hold.REPLAY_HEADER) == "1":
            delivery = change_page.finish_verdict_cleanup(saved.delivery, server.delivery_status)
            outcome = self.deliver_review(server, delivery, report_outcome=True)
            result.update(held_delivery_result(outcome, not pending))
            result['delivery_pending'] = False
            if pending:
                self.publish_review_delivery(server, key)
            self.json_response(200, result)
            return
        with server.item_delivery(pending) as check:
            if check:
                result['delivery'] = check
            try:
                self.json_response(200, result)
            finally:
                _ = self.deliver_review(server, change_page.finish_verdict_cleanup(saved.delivery, server.delivery_status))
                if pending:
                    self.publish_review_delivery(server, key)

    def publish_review_delivery(self, server: ReviewLinksServer, key: str) -> None:
        try:
            server.review_cache.publish_delivery_result(key)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"review-links Review delivery refresh {key}: {error}", file=sys.stderr)

    def deliver_review(self, server: ReviewLinksServer,
                       delivery: change_page.MessageDelivery | change_page.NoMessageDelivery, *,
                       report_outcome: bool = False) -> change_page.DeliveryOutcome:
        """Retry a failure before sending and leave the saved intent available to recovery."""
        status = server.delivery_status
        send = change_page.deliver_with_outcome if report_outcome else change_page.deliver
        try:
            try:
                return send(delivery, status) or change_page.DeliveryUnconfirmed()
            except Exception as error:
                print(f"review-links review delivery: {error}", file=sys.stderr)
                if isinstance(delivery, change_page.MessageDelivery):
                    ownership = status.request_attempts.get(delivery.attempt)
                    if ownership is not None and ownership.state == "claimed":
                        return send(delivery, status) or change_page.DeliveryUnconfirmed()
        except Exception as error:
            print(f"review-links review delivery retry: {error}", file=sys.stderr)
        finally:
            if isinstance(delivery, change_page.MessageDelivery):
                status.release_owned_claims((delivery.attempt,))
                server.review_cache.invalidate()
        return change_page.DeliveryUnconfirmed()

    def queue_move_allowed(self, *, origin_required: bool) -> bool:
        if self.tailscale_refusal() or not self.same_origin(required=origin_required):
            return False
        server = cast(ReviewLinksServer, self.server)
        return server.mode is ServerMode.LIVE and builds_operator(self.client_address)

    def queue_move_token(self, query: str) -> None:
        if self.command != "GET" or query:
            self.send_error(400)
            return
        settings = conf.read_or_repository("build_queue")
        try:
            allowed = self.queue_move_allowed(origin_required=False)
            highlight = settings.number("move_highlight_seconds") if allowed else 0
        except ValueError:
            allowed = False
            highlight = 0
        if not allowed:
            self.json_response(403, {"reason": "Queue moves require the operator's Tailscale account."})
            return
        server = cast(ReviewLinksServer, self.server)
        self.json_response(200, {"token": server.new_item_token, "highlight_seconds": highlight})

    def queue_move_post(self, query: str) -> None:
        settings = conf.read_or_repository("build_queue")
        try:
            if not self.queue_move_allowed(origin_required=True):
                self.json_response(403, {"reason": "Queue moves require the operator's Tailscale account."})
                return
            if query or self.headers.get_content_type() != "application/json":
                raise ValueError("Send a queue move as JSON.")
            lengths = self.headers.get_all("Content-Length", [])
            limit = settings.whole("move_request_bytes")
            if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
                raise ValueError("Invalid queue move request size.")
            length = int(lengths[0])
            if not 0 < length <= limit or self.headers.get_all("Transfer-Encoding"):
                raise ValueError("Invalid queue move request size.")
            timeout = settings.number("move_timeout_seconds")
            connection = cast(socket.socket, self.connection)
            previous_timeout = connection.gettimeout()
            connection.settimeout(timeout)
            try:
                payload = cast(object, json.loads(self.rfile.read(length)))
            finally:
                connection.settimeout(previous_timeout)
            if not isinstance(payload, dict):
                raise ValueError("Send one queue move.")
            fields = cast(dict[str, object], payload)
            if not self.valid_request_token(fields.pop("token", None)):
                self.json_response(403, {"reason": "The queue move token expired. Refresh Builds."})
                return
            kind = fields.get("kind")
            if "code_version" in fields or kind not in ("QueueMoveRequest", "QueueUndoRequest"):
                raise ValueError("Send one queue move or Undo request.")
            if kind == "QueueUndoRequest":
                request = admission.parse_record(json.dumps(fields), admission.QueueUndoRequest)
                other_ids = (request.move_request_id,)
            else:
                request = admission.parse_record(json.dumps(fields), admission.QueueMoveRequest)
                other_ids = (request.destination.neighbor,) if isinstance(request.destination, admission.QueueDrop) else ()
            identities = (request.request_id, request.row_id, request.revision, *other_ids)
            if any(not re.fullmatch(r"[0-9a-f]{32}", key) for key in identities):
                raise ValueError("Invalid queue move identity.")
        except (ValueError, TypeError, TimeoutError, RecursionError) as error:
            self.json_response(400, {"reason": str(error)})
            return
        try:
            result = admission_client.move_queue(request, timeout)
        except (OSError, ValueError) as error:
            self.browser_failure(503, "The queue could not confirm the move. Refresh Builds.", error)
            return
        if isinstance(result, admission.Refused):
            self.json_response(503, {"reason": result.reason})
        else:
            content = cast(dict[str, object], json.loads(admission.encode_record(result)))
            self.json_response(409 if isinstance(result, admission.QueueMoveConflict) else 200, content)

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

    def list_action_state(self, fields: dict[str, list[str]], view: list_page.ListView) -> None:
        """Read the current item after an action response is lost."""
        server = cast(ReviewLinksServer, self.server)
        try:
            (identity,) = fields["item-state"]
            (keeper,) = fields.get("keeper", [])
            (park_request,) = fields.get("park-request", [""])
            with server.list_store_access(), server.staff_read_scope():
                current = list_page.saved_action_state(keeper, identity, status=server.delivery_status,
                                                       park_request=park_request, include_detail=True,
                                                       review_records=server.list_review_records(),
                                                       top_priority=view.top_priority)
        except ValueError as error:
            self.browser_failure(400, "The List item is unavailable.", error)
        except OSError as error:
            self.browser_failure(503, "The List item cannot be read. Try again.", error)
        else:
            self.json_response(200, current)

    def list_post(self, view: list_page.ListView) -> None:
        """Save a List view's item action before the shared sender starts."""
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
                    review_records=server.list_review_records(), include_detail=True,
                    top_priority=view.top_priority)
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
        state = {"read": "read", "question": "saved", "top": "saved"}.get(action, "sent")
        reply = {"state": state, "delivery_pending": pending, **saved_fields}
        if current is not None and isinstance(submission, list_page.ListUnparkSubmission):
            reply["unpark_outcome"] = submission.response_outcome(current).value
        if self.headers.get(send_hold.REPLAY_HEADER) == "1":
            try:
                outcome = list_page.deliver_with_outcome(submission)
            except OSError as error:
                print(f"review-links delivery status: {error}", file=sys.stderr)
                outcome = change_page.DeliveryUnconfirmed()
            reply.update(held_delivery_result(outcome, not pending))
            reply["delivery_pending"] = False
            self.json_response(200, reply)
            return
        with server.item_delivery(pending) as check:
            try:
                if check:
                    reply["delivery"] = check
                self.json_response(200, reply)
            finally:
                try:
                    list_page.deliver(submission)
                except OSError as error:
                    print(f"review-links delivery status: {error}", file=sys.stderr)

    def go_post(self) -> None:
        """Refuse a Go from natedev; else write the ledger record, close the ask and send the Go."""
        server = cast(ReviewLinksServer, self.server)
        client = self.client_address[0]
        try:
            refused = go_ledger.refused_address(client)
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.browser_failure(503, "Could not check natedev's addresses. Try again.", error)
            return
        if refused:
            self.json_response(403, {"reason": "Press Go from your own device. A Go from natedev is refused."})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 100000:
                raise ValueError("invalid List Go size")
            body = self.rfile.read(length).decode("utf-8")
            with server.list_store_access():
                go_id, current = list_page.save_go(server.root, server.store_folder, body, client, views=LIST_VIEWS,
                                                   review_records=server.list_review_records())
        except ValueError as error:
            self.browser_failure(400, str(error), error)
            return
        except list_page.GoSavedUnsent as error:
            self.browser_failure(503, "The Go is saved. It is sent at the next server start.", error)
            return
        except OSError as error:
            self.browser_failure(503, "The Go could not be saved. Try again.", error)
            return
        try:
            self.json_response(200, {"state": "sent", **({"detail_unavailable": True} if current is None else current)})
        finally:
            try:
                list_page.deliver_go(server.store_folder, go_id)
            except (OSError, ValueError) as error:
                print(f"review-links List Go delivery: {error}", file=sys.stderr)

    def go_check(self, query: str) -> None:
        """Answer go_check.py: accept one use of a Go by its target session."""
        server = cast(ReviewLinksServer, self.server)
        fields = parse_qs(query, keep_blank_values=True)
        if set(fields) != {"id", "caller"} or any(len(values) != 1 for values in fields.values()):
            self.json_response(400, {"ok": False, "text": "give one go id and one caller."})
            return
        try:
            answer = go_ledger.check(server.store_folder, fields["id"][0], fields["caller"][0])
        except (OSError, ValueError) as error:
            self.browser_failure(503, "The go ledger is not available.", error)
            return
        self.json_response(200 if answer["ok"] else 403, answer)

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
            self.browser_failure(503, str(error), error)
            return
        except ValueError as error:
            self.browser_failure(400, "Use the form on this page.", error)
            return
        except OSError as error:
            self.browser_failure(503, "Could not save the picture change. Try again.", error)
            return
        if isinstance(delivery, images.ImageAnswerSent):
            result: dict[str, object] = {"state": "done"}
            if delivery.notice:
                result["notice"] = delivery.notice
            if delivery.draft_confirmed:
                result["draft_confirmed"] = True
            self.json_response(200, result)
            return
        if self.headers.get(send_hold.REPLAY_HEADER) == "1" and isinstance(delivery, change_page.MessageDelivery):
            outcome = self.deliver_review(server, delivery, report_outcome=True)
            self.json_response(200, {"state": "done", **held_delivery_result(outcome)})
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
        """Start a Disks page measure and return its state and the current sessions."""
        now = time.time()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            fields = parse_qs(self.rfile.read(length).decode("utf-8"))
            started = disks_page.start_measure(now, force=fields.get("force") == ["1"])
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.browser_failure(503, "The measure could not start.", error)
            return
        try:
            folders = sessions.session_report()
        except (OSError, ValueError) as error:
            print(f"review-links disks sessions: {error}", file=sys.stderr)
            folders = None
        self.json_response(200, {"state": started.state.value, "target": started.target, "sessions": folders})

    def browser_failure(self, status: int, reason: str, error: Exception) -> None:
        print(f"review-links browser answer: {error}", file=sys.stderr)
        self.json_response(status, {"reason": reason})

    def held_send_path(self, path: str) -> bool:
        """Say whether a send to this path can wait for Undo: a console send page or a change browser."""
        server = cast(ReviewLinksServer, self.server)
        if path in HELD_SEND_PATHS:
            return True
        try:
            _ = change_browser.page_at(server.root, path)
        except (OSError, ValueError):
            return False
        return True

    def undo_post(self, route: SplitResult) -> None:
        """Undo a held send, or send it at once because its page closed."""
        server = cast(ReviewLinksServer, self.server)
        if not self.same_origin(required=True) or route.query:
            self.json_response(403, {"reason": "Send from the console page."})
            return
        hold_id, _, action = route.path.removeprefix(send_hold.HOLD_PATH + "/").partition("/")
        if action not in ("undo", "send") or not send_hold.valid_hold_id(hold_id):
            self.json_response(404, {"reason": "No such send."})
            return
        if action == "send":
            try:
                sent = server.send_holds.send_now(hold_id, expect_post=self.headers.get(send_hold.REQUEST_HEADER) == hold_id)
            except OSError as error:
                print(f"review-links send hold {hold_id}: {error}", file=sys.stderr)
                self.json_response(503, {"reason": "The send could not release early."})
                return
            self.json_response(200 if sent else 409, {})
            return
        try:
            outcome = server.send_holds.undo(hold_id)
        except OSError as error:
            print(f"review-links send hold {hold_id}: {error}", file=sys.stderr)
            self.json_response(503, {"reason": "The undo failed. The send can still leave."})
            return
        status = {send_hold.UndoResult.UNDONE: 200, send_hold.UndoResult.ALREADY_SENT: 409}.get(outcome, 404)
        self.json_response(status, {"outcome": outcome.value})

    def hold_post(self, route: SplitResult, seconds: int, request_id: str) -> None:
        """Hold a marked send for Undo."""
        server = cast(ReviewLinksServer, self.server)
        if not self.same_origin(required=True) or route.query:
            self.json_response(403, {"reason": "Send from the console page."})
            return
        lengths = self.headers.get_all("Content-Length", [])
        if len(lengths) != 1 or self.headers.get("Transfer-Encoding") or not re.fullmatch(r"[0-9]{1,10}", lengths[0]):
            self.json_response(400, {"reason": "The send has no valid size."})
            return
        size = int(lengths[0])
        if not 0 < size <= HELD_SEND_LIMIT:
            self.json_response(400, {"reason": "The send is empty or too large."})
            return
        body = self.rfile.read(size)
        if len(body) != size:
            self.json_response(400, {"reason": "The send is incomplete."})
            return
        try:
            # A restart waits until the browser has the hold id, so it can read the result from the next server.
            with server.send_holds.answering():
                held = server.send_holds.hold(route.path, dict(self.headers.items()), body, seconds, request_id=request_id)
                self.json_response(202, held, hold=send_hold.HoldHeader.HELD)
        except send_hold.HoldsStopped:
            self.json_response(503, {"reason": "The console is restarting. Send again."})

    def hold_result(self, hold_id: str) -> None:
        """Answer with the response of a released send, or say that it is still held."""
        server = cast(ReviewLinksServer, self.server)
        refusal = self.tailscale_refusal()
        if refusal or not self.same_origin():
            self.json_response(403, {"reason": refusal or "Read the send from the console page."})
            return
        result = server.send_holds.result(hold_id) if send_hold.valid_hold_id(hold_id) else send_hold.HoldState.MISSING
        if result is send_hold.HoldState.PENDING:
            self.json_response(202, {"pending": True}, hold=send_hold.HoldHeader.PENDING)
        elif result is send_hold.HoldState.UNDONE:
            self.json_response(404, {"reason": "This send was undone."})
        elif result is send_hold.HoldState.MISSING:
            self.json_response(404, {"reason": "This send was undone or its result expired."})
        else:
            self.send_response(result.status)
            self.send_header("Content-Type", result.content_type)
            self.send_header("Content-Length", str(len(result.body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            _ = self.wfile.write(result.body)

    def json_response(self, status: int, fields: Mapping[str, object], *,
                      hold: send_hold.HoldHeader | None = None) -> None:
        content = json.dumps(fields).encode("utf-8")
        self.send_response(status)
        if hold is not None:
            self.send_header("X-Send-Hold", hold.value)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
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
        if mode is ServerMode.LIVE:
            try:
                tidy_wrapped.recover_review_moves(self.root)
            except (OSError, ValueError) as error:
                print(f"review-links move recovery: {error}", file=sys.stderr)
        self.accounts_cache: AccountsPageCache = AccountsPageCache()
        self.review_cache: review_page.ReviewSnapshotCache = ConsoleReviewSnapshotCache(
            self.root, start_worker=False, initialize_on_start=initialize_review and mode is ServerMode.LIVE,
            staff_scope=self.staff_read_scope)
        self.new_item_token: str = secrets.token_urlsafe()
        self.kill_actions: kill_action.KillActions = kill_action.KillActions(
            self.store_folder, memory_feed.live_outside_kill_targets)
        self.capture_slot: threading.BoundedSemaphore = threading.BoundedSemaphore()
        self.page_captures: PageCaptures = PageCaptures(self.root / PAGE_CAPTURE_FOLDER)
        self.next_attachment_retry: float = time.monotonic() + PAGE_ATTACHMENT_RETRY_SECONDS
        self.next_tailscale_check: float = time.monotonic() + settings.number("tailscale_check_seconds")
        self.next_sweep: float = time.monotonic() + settings.number("snapshot_sweep_seconds")
        self.question_recovery: threading.Thread | None = None
        self.alert_recovery: threading.Thread | None = None
        self.next_code_check: float = time.monotonic() + settings.number("code_check_seconds")
        self.module_stamps: dict[Path, tuple[int, int] | ModuleStampUnavailable] = loaded_module_stamps()
        if mode is ServerMode.LIVE:
            _ = sweep_snapshots(self.root, time.time())
        super().__init__(address, ReviewLinksHandler)
        self.send_holds: send_hold.Holds = send_hold.Holds(
            self.store_folder / send_hold.HOLD_FOLDER, send_hold.replay_to(self.bound_address, self.server_port))
        self.send_holds.recover()
        if mode is ServerMode.LIST_FIXTURE:
            return
        self.kill_actions.resume()
        recover_changes = change_page.recover_pending
        recover_list = list_page.recover_pending
        recover_goes = list_page.recover_goes
        self.delivery_status.startup_recovery_pending.set()

        def recover_pending() -> None:
            def recover(name: str, operation: Callable[[], None]) -> None:
                try:
                    operation()
                except Exception as error:
                    print(f"review-links {name} recovery: {error}", file=sys.stderr)

            with reply_tokens.serving(self.store_folder):
                try:
                    recover("delivery", partial(recover_changes, self.delivery_status))
                    recover("first message", partial(
                        review_actions.recover_submission_deliveries, self.delivery_status, review_page.PAGE_PATH))
                finally:
                    self.delivery_status.startup_recovery_pending.clear()
                # Recover Go first because its message includes the producer's next-step notice.
                recover("List Go", partial(recover_goes, self.store_folder))
                recover("List feedback", partial(recover_list, self.store_folder))
                recover("Scheduled notice", partial(new_item.recover_scheduled_notices, self.root))
        threading.Thread(target=recover_pending, daemon=True).start()
        self.review_cache.start()
        try:
            threading.Thread(target=self.recover_page_captures, daemon=True).start()
        except (OSError, RuntimeError) as error:
            print(f"review-links capture recovery worker: {error}", file=sys.stderr)

    def start_new_item_delivery(self, deliver: Callable[[], object], *,
                                claim: change_page.MessageDelivery | None) -> threading.Thread | None:
        """Start delivery after the saved reply leaves the handler."""
        def send() -> None:
            try:
                with reply_tokens.serving(self.store_folder):
                    _ = deliver()
            except (OSError, ValueError, RuntimeError) as error:
                print(f"review-links New item notices: {error}", file=sys.stderr)

        try:
            thread = threading.Thread(target=send, daemon=True)
            if claim is None:
                thread.start()
                return thread
            status = self.delivery_status
            # Keep the claim live while ownership moves to the delivery thread.
            with status.locked():
                owner = status.request_attempts.get(claim.attempt)
                if owner is None or owner.request_thread is not threading.current_thread():
                    return None
                thread.start()
                status.request_attempts[claim.attempt] = replace(owner, request_thread=thread)
            return thread
        except (OSError, RuntimeError) as error:
            print(f"review-links New item notices could not start: {error}", file=sys.stderr)
            return None

    def recover_page_captures(self) -> None:
        """Recover capture work from committed native submissions."""
        try:
            for target, intent, attached in review_submissions.committed_page_captures():
                self.process_committed_capture(target, intent, attached=attached)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"review-links capture recovery: {error}", file=sys.stderr)
        self.retry_page_attachments()

    def retry_page_attachments(self) -> None:
        """Schedule unfinished captures and attachments."""
        for key, capture, target in self.page_captures.bound_work():
            if isinstance(capture, PageCaptureToResume):
                self.resume_page_capture(key)
            else:
                self.launch_bound_attachment(key, target, capture)

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

    def ensure_page_capture(self, key: str, request: new_item_dialog.PageCaptureRequest) -> bool:
        """Start the capture once, even if Send arrives first."""
        firefox = shutil.which("firefox")
        if not firefox:
            return False
        match self.page_captures.reserve(key, request):
            case CaptureReservation.STARTED:
                return self.launch_page_capture(key, firefox, request, queued=not self.capture_slot.acquire(blocking=False))
            case CaptureReservation.PRESENT:
                self.resume_page_capture(key)
            case CaptureReservation.FULL:
                return False
        return True

    def process_committed_capture(self, target: new_item_dialog.ScopedSubmissionKey,
                                  intent: new_item_dialog.DeferredCaptureIntent, *, attached: bool) -> None:
        """Process capture work after Send publishes and responds."""
        key, draft, request = intent.capture_key, intent.submitted_draft_id, intent.request
        try:
            if not new_item_dialog.capture_key_matches_draft(key, draft):
                raise new_item_dialog.SubmissionConflict("The screenshot belongs to another draft.")
            if not isinstance(request, new_item_dialog.PageCaptureRequest):
                self.page_captures.unavailable(key, target)
                return
            if attached and self.page_captures.target(key) is new_item_dialog.NoBoundCaptureTarget.UNBOUND:
                image = review_submissions.read_attached_page_screenshot(target, request)
                self.page_captures.restore_ready(key, target, image)
            match self.page_captures.reserve(key, request):
                case CaptureReservation.STARTED:
                    _ = self.page_captures.bind(key, draft, target)
                    firefox = shutil.which("firefox")
                    if not firefox or not capture_page_key(self.root, request.place):
                        self.page_captures.unavailable(key, target)
                        return
                    queued = not self.capture_slot.acquire(blocking=False)
                    _ = self.launch_page_capture(key, firefox, request, queued=queued)
                case CaptureReservation.PRESENT:
                    image = self.page_captures.bind(key, draft, target)
                    if isinstance(image, new_item_dialog.PageScreenshot):
                        self.launch_bound_attachment(key, target, image)
                    else:
                        self.resume_page_capture(key)
                case CaptureReservation.FULL:
                    self.page_captures.unavailable(key, target)
        except (OSError, ValueError, RuntimeError) as error:
            print(f"review-links screenshot for {target.item_id}: {error}", file=sys.stderr)

    def resume_page_capture(self, key: str) -> None:
        """Restart an unfinished capture when the dialog requests its key."""
        capture = self.page_captures.resume(key)
        if isinstance(capture, PageCaptureToResume):
            firefox = shutil.which("firefox")
            if firefox and capture_page_key(self.root, capture.request.place):
                _ = self.launch_page_capture(key, firefox, capture.request, queued=True)
            else:
                _ = self.page_captures.complete(key, new_item_dialog.NO_PAGE_SCREENSHOT)

    def launch_page_capture(self, key: str, firefox: str, capture: new_item_dialog.PageCaptureRequest, *,
                            queued: bool = False) -> bool:
        """Start the capture worker with a reserved slot or wait for a slot."""
        url = f"http://{self.bound_address}:{self.server_port}{capture.place}"
        worker = self.capture_queued_page if queued else self.capture_page
        try:
            threading.Thread(target=worker, args=(key, firefox, url, capture), daemon=True).start()
        except (OSError, RuntimeError) as error:
            if not queued:
                self.capture_slot.release()
            _ = self.page_captures.complete(key, new_item_dialog.NO_PAGE_SCREENSHOT)
            print(f"review-links capture worker: {error}", file=sys.stderr)
            return False
        return True

    def capture_queued_page(self, key: str, firefox: str, url: str,
                            capture: new_item_dialog.PageCaptureRequest) -> None:
        """Use the capture slot when Firefox becomes free."""
        if not self.capture_slot.acquire(timeout=PAGE_CAPTURE_TTL_SECONDS):
            _ = self.page_captures.complete(key, new_item_dialog.NO_PAGE_SCREENSHOT)
            return
        if not self.page_captures.queued(key):
            self.capture_slot.release()
            return
        self.capture_page(key, firefox, url, capture)

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
        if not isinstance(image, new_item_dialog.PageScreenshot):
            return
        if isinstance(saved, new_item_dialog.ScopedSubmissionKey):
            self.finish_bound_capture(key, saved, image)
        elif isinstance(saved, review_submissions.SavedSubmission):
            self.attach_page_screenshot(saved, image.png)

    def launch_bound_attachment(self, key: str, target: new_item_dialog.ScopedSubmissionKey,
                                image: new_item_dialog.PageScreenshot) -> None:
        """Attach a ready screenshot outside the Send request."""
        try:
            threading.Thread(target=self.finish_bound_capture, args=(key, target, image), daemon=True).start()
        except (OSError, RuntimeError) as error:
            print(f"review-links attachment worker: {error}", file=sys.stderr)

    def finish_bound_capture(self, key: str, target: new_item_dialog.ScopedSubmissionKey,
                             image: new_item_dialog.PageScreenshot) -> None:
        """Attach the screenshot to the item that Send bound to its key."""
        with self.page_captures.for_attachment(key, image) as claimed:
            if not claimed:
                return
            try:
                attached = self.review_cache.attach_deferred_page_screenshot(target, image)
                if attached is review_submissions.ScreenshotAttachment.UNAVAILABLE:
                    self.page_captures.unavailable(key, target)
                    return
                review_submissions.record_deferred_bug_screenshot(target, image.viewport)
                self.page_captures.mark_filed(key, image)
            except review_submissions.SavedSubmissionMissing:
                self.page_captures.unavailable(key, target)
            except (OSError, ValueError, RuntimeError) as error:
                print(f"review-links screenshot for {target.item_id}: {error}", file=sys.stderr)

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
        if now >= self.next_attachment_retry:
            self.next_attachment_retry = now + PAGE_ATTACHMENT_RETRY_SECONDS
            self.retry_page_attachments()
            self.kill_actions.retry_pending()
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
                self.page_captures.expire()
                self.send_holds.expire()
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
                    # A restart waits for each replay, and no replay starts after this stop.
                    if not self.send_holds.stop_if_idle():
                        return
                    raise CodeChanged(f"code changed: {path}")
            self.module_stamps.update(stamps)
            if (self.delivery_status.has_deferred_questions()
                    and (self.question_recovery is None or not self.question_recovery.is_alive())):
                def recover_questions() -> None:
                    with reply_tokens.serving(self.store_folder):
                        change_page.recover_deferred_questions(self.delivery_status)

                self.question_recovery = threading.Thread(
                    target=recover_questions, daemon=True)
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

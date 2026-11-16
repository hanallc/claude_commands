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
from dataclasses import dataclass, field
import html
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import threading
from collections.abc import Mapping
from enum import Enum
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
from typing import BinaryIO, cast, override
from urllib.parse import parse_qs, unquote, urlsplit

# Detect file changes before the first stamp, including changes during imports.
STARTED_NS = time.time_ns()

from scripts.disks import disks_page, sessions, slots
from scripts.lib import conf
from scripts.production import change_routing, change_review
from scripts.review_links import accounts_page, branch_page, change_page, change_browser, console, image_store, images
from scripts.review_links.review_link import private_name

BRANCHES_DELETING = "Deleting these branches now. The result comes by message."
# A restart passes the listening socket to the new process in this variable.
LISTEN_FD = "REVIEW_LINKS_LISTEN_FD"


@dataclass
class AccountsPageCache:
    """Keep the last good page and allow one render at a time."""

    page: bytes = b""
    recent_times: deque[float] = field(default_factory=lambda: deque(maxlen=10))
    rendering: bool = False
    first_render: Future[bytes] = field(default_factory=Future[bytes])
    condition: threading.Condition = field(default_factory=threading.Condition)

    def get(self) -> bytes:
        render_first = False
        with self.condition:
            if self.page:
                if not self.rendering:
                    self.rendering = True
                    try:
                        threading.Thread(target=self.replace, daemon=True).start()
                    except RuntimeError:
                        self.rendering = False
                        raise
                return self.page
            if not self.rendering:
                self.first_render = Future[bytes]()
                self.rendering = True
                render_first = True
            attempt = self.first_render
        if render_first:
            try:
                page = self.replace()
            except BaseException as error:
                attempt.set_exception(error)
                raise
            attempt.set_result(page)
            return page
        return attempt.result()

    def replace(self) -> bytes:
        try:
            rendered = accounts_page.render_with_facts()
            with self.condition:
                self.recent_times.append(rendered.seconds)
                self.page = rendered.with_recent_times(tuple(self.recent_times))
                return self.page
        except (OSError, ValueError) as error:
            with self.condition:
                if not self.page:
                    raise
                print(f"review-links accounts: {error}", file=sys.stderr)
                return self.page
        finally:
            with self.condition:
                self.rendering = False
                self.condition.notify_all()


class TailscaleAddressLost(RuntimeError):
    """The server's bound address is no longer this machine's Tailscale address."""


class CodeChanged(RuntimeError):
    """A loaded module changed and the server must restart."""


class ModuleStampUnavailable(Enum):
    UNREADABLE = "unreadable"


def loaded_module_stamps() -> dict[Path, tuple[int, int] | ModuleStampUnavailable]:
    checkout = Path(__file__).resolve().parents[2]
    stamps: dict[Path, tuple[int, int] | ModuleStampUnavailable] = {}
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not isinstance(filename, str):
            continue
        path = Path(filename).resolve()
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


class ReviewLinksHandler(SimpleHTTPRequestHandler):
    """Serve regular files within the snapshot root, without directory indexes."""

    @override
    def send_head(self) -> BinaryIO | None:
        root = cast(ReviewLinksServer, self.server).root
        route = urlsplit(self.path)
        if route.path == f"/{change_page.CONSOLE_FOLDER}":
            self.redirect(f"/{change_page.CONSOLE_FOLDER}/" + (f"?{route.query}" if route.query else ""))
            return None
        if route.path == f"/{change_page.CONSOLE_FOLDER}/":
            try:
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
        if route.path == accounts_page.PAGE_PATH:
            try:
                content = cast(ReviewLinksServer, self.server).accounts_cache.get()
            except (OSError, ValueError) as error:
                print(f"review-links accounts: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>The Accounts page is not available.</p>"))
                return None
            self.html_response(200, content)
            return None
        if route.path == images.PAGE_PATH:
            try:
                content = images.render(root)
            except (OSError, ValueError) as error:
                print(f"review-links images: {error}", file=sys.stderr)
                self.html_response(503, change_page.document("<p>The Images page is not available.</p>"))
                return None
            self.html_response(200, content)
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
            server = cast(ReviewLinksServer, self.server)
            with server.branch_deletions_lock:
                if branches.folder in server.branch_deletions:
                    self.html_response(200, change_page.document(f"<p>{BRANCHES_DELETING}</p>"))
                    return None
            self.html_response(200, branch_page.render(branches))
            return None
        try:
            change_browser.rebuilds_at(root, self.path)
            content = change_browser.render_rebuilds()
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as error:
            print(f"review-links rebuilds: {error}", file=sys.stderr)
            self.html_response(503, change_page.document("<p>The rebuild list is not available.</p>"))
            return None
        else:
            self.html_response(200, content)
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
                content = change_browser.render(browser, cast(ReviewLinksServer, self.server).delivery_status)
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
                content = change_page.render(page, cast(ReviewLinksServer, self.server).delivery_status)
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
            stream = file.open("rb")
        except (OSError, ValueError, RuntimeError):
            self.html_response(404, change_page.document("<p>This review page is not available.</p>"))
            return None
        try:
            stat = os.fstat(stream.fileno())
            self.send_response(200)
            self.send_header("Content-Type", self.guess_type(str(file)))
            self.send_header("Content-Length", str(stat.st_size))
            self.send_header("Last-Modified", self.date_time_string(stat.st_mtime))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
        except BaseException:
            stream.close()
            raise
        return stream

    def html_response(self, status: int, content: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
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
        try:
            if server.bound_address != tailscale_ip():
                self.html_response(403, change_page.document("<p>Open this page through this machine's Tailscale address.</p>"))
                return
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print(f"review-links address: {error}", file=sys.stderr)
            self.html_response(403, change_page.document("<p>The Tailscale address is not available.</p>"))
            return
        if urlsplit(self.path).query or self.headers.get_content_type() != "application/x-www-form-urlencoded":
            self.html_response(400, change_page.document("<p>Use the answer form on the review page.</p>"))
            return
        if urlsplit(self.path).path == images.PAGE_PATH:
            self.image_post()
            return
        if urlsplit(self.path).path == f"/{change_page.CONSOLE_FOLDER}/{console.NEWS_NAME}":
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
            self.delete_branches(branches)
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
            self.browser_answer(browser)
            return
        try:
            page = change_page.page_at(server.root, self.path)
        except (OSError, ValueError) as error:
            print(f"review-links page: {error}", file=sys.stderr)
            self.html_response(404, change_page.document("<p>This review page is not available.</p>"))
            return
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
                change_page.deliver(delivery, server.delivery_status)
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
                change_page.deliver(delivery, server.delivery_status)
            except OSError as error:
                print(f"review-links delivery status: {error}", file=sys.stderr)

    def image_post(self) -> None:
        """Delete an image or send its feedback; the page shows the reason of a failure on the card."""
        root = cast(ReviewLinksServer, self.server).root
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0:
                raise ValueError("empty image request")
            images.submit(root, self.rfile.read(length).decode("utf-8"))
        except change_page.AnswerRefused as error:
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
        self.json_response(200, {"state": "done"})

    def news_post(self) -> None:
        """Record the page the console shows, and name the other pages that hold new items."""
        root = cast(ReviewLinksServer, self.server).root
        try:
            length = int(self.headers.get("Content-Length", "0"))
            opened = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True).get("open", [])
            pages = console.read_pages()
            # An empty name means the open page is not a navigation entry, such as the Rebuilds page.
            if len(opened) != 1 or opened[0] not in {"", *(page.identity for page in pages)}:
                raise ValueError("name one console page or none")
            fresh = console.news(root, pages, opened[0])
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
            started = disks_page.start_measure(now)
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
        self.json_response(200, {"state": started.value, "slots": report, "sessions": folders})

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


class ReviewLinksServer(ThreadingHTTPServer):
    def __init__(self, address: tuple[str, int], root: Path) -> None:
        settings = conf.read("review_links")
        self.root: Path = root.resolve()
        self.bound_address: str = address[0]
        self.delivery_status: change_page.DeliveryStatus = change_page.DeliveryStatus(self.root)
        self.branch_deletions: set[Path] = set()
        self.branch_deletions_lock: threading.Lock = threading.Lock()
        self.accounts_cache: AccountsPageCache = AccountsPageCache()
        self.next_tailscale_check: float = time.monotonic() + settings.number("tailscale_check_seconds")
        self.next_sweep: float = time.monotonic() + settings.number("snapshot_sweep_seconds")
        self.next_code_check: float = time.monotonic() + settings.number("code_check_seconds")
        self.module_stamps: dict[Path, tuple[int, int] | ModuleStampUnavailable] = loaded_module_stamps()
        _ = sweep_snapshots(self.root, time.time())
        super().__init__(address, ReviewLinksHandler)
        threading.Thread(target=change_page.recover_pending, args=(self.delivery_status,), daemon=True).start()

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
                        if self.delivery_status.request_attempts:
                            return
                    with self.branch_deletions_lock:
                        if self.branch_deletions:
                            return
                    raise CodeChanged(f"code changed: {path}")
            self.module_stamps.update(stamps)


def main(arguments: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--port", type=int, default=conf.read("review_links").whole("server_port"), help="override the configured server port")
    args = parser.parse_args(arguments)
    try:
        address = tailscale_ip()
        root = change_routing.review_root()
        root.mkdir(parents=True, exist_ok=True)
        with ReviewLinksServer((address, cast(int, args.port)), root) as server:
            print(f"review-links: serving {root} on {address}:{server.server_port}", flush=True)
            try:
                server.serve_forever()
            except CodeChanged as error:
                print(f"review-links: {error}", file=sys.stderr)
                restart(server)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError, subprocess.SubprocessError, TailscaleAddressLost) as error:
        print(f"review-links: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

"""Hold one console send for a short time so the user can undo it, as an email client does.

The browser marks a send with the hold header. The server saves the request and answers 202 at once.
When the hold time ends, the server replays the saved request to itself; the normal handler then saves and sends.
An undo before that time replaces the saved request with an undone mark, so nothing is saved and nothing is sent.
Each hold is a file, so a server restart replays the holds that it finds, and a browser that lost its
connection reads the same result again. A replay first writes a "may have left" result, so a server that stops
during a replay never sends that request again.
"""
from __future__ import annotations

import base64
import http.client
import json
import secrets
import sys
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast

from scripts.lib.atomic_file import write_text_atomic

HOLD_PATH = "/send-hold"
HOLD_HEADER = "X-Undo-Hold"
REQUEST_HEADER = "X-Undo-Request"
REPLAY_HEADER = "X-Undo-Replay"
REPLAY_TICKET_HEADER = "X-Undo-Replay-Ticket"
HOLD_FOLDER = ".review-link-holds"
# The request headers that a replay needs. The handlers read the body, its type, and the origin checks.
REPLAY_HEADERS = frozenset(("content-type", "origin", "host", "accept"))
# The server waits this long after the browser's Undo time ends, so a late Undo click still wins.
RELEASE_GRACE_SECONDS = 1.0
# A browser reads the result of a released send within this time; the sweep then deletes the result file.
RESULT_KEEP_SECONDS = 600.0
RESULT_WAIT_SECONDS = 25.0
# A handler that runs longer than this gives the "may have left" result.
REPLAY_TIMEOUT_SECONDS = 300.0
MAY_HAVE_LEFT = "The send may have left. Check before you send again."

Replay = Callable[[str, Mapping[str, str], bytes, str], "HeldResponse"]


class PendingFile(TypedDict):
    state: Literal["pending"]
    path: str
    headers: dict[str, str]
    body: str
    due: float
    started_at: NotRequired[float]


class ReleasedFile(TypedDict):
    state: Literal["released"]
    status: int
    content_type: str
    body: str
    released_at: float


class UndoneFile(TypedDict):
    state: Literal["undone"]
    undone_at: float


class ClosingFile(TypedDict):
    state: Literal["closing"]
    closed_at: float


HoldFile = PendingFile | ReleasedFile | UndoneFile | ClosingFile


class BadFile(Enum):
    """A file in the hold folder that is not a readable hold."""
    UNREADABLE = "unreadable"


class HoldHeader(Enum):
    """The X-Send-Hold answer header: the send waits for Undo, or its result is not ready."""
    HELD = "held"
    PENDING = "pending"


class HeldFields(TypedDict):
    hold: str
    seconds: float


class HoldsStopped(Exception):
    """The server restarts, so it takes no new hold."""


@dataclass(frozen=True)
class HeldResponse:
    status: int
    content_type: str
    body: bytes


class HoldState(Enum):
    PENDING = "pending"
    UNDONE = "undone"
    MISSING = "missing"


class UndoResult(Enum):
    UNDONE = "undone"
    ALREADY_SENT = "already sent"
    MISSING = "missing"


def new_hold_id() -> str:
    return secrets.token_hex(12)


def valid_hold_id(text: str) -> bool:
    return len(text) == 24 and all(character in "0123456789abcdef" for character in text)


def failed(reason: str) -> HeldResponse:
    return HeldResponse(503, "application/json; charset=utf-8", json.dumps({"reason": reason}).encode("utf-8"))


def released_file(response: HeldResponse) -> ReleasedFile:
    return {"state": "released", "status": response.status, "content_type": response.content_type,
            "body": base64.b64encode(response.body).decode("ascii"), "released_at": time.time()}


class Holds:
    """Keep the saved sends of one server and release each one when its time ends.

    The pending ids, their timers and the releases in progress stay in memory, so an undo or a result wait for a
    pending send reads no file. A hold without a timer is read from its file: a result, an undone mark, or a send
    that the next server releases.
    """

    def __init__(self, folder: Path, replay: Replay) -> None:
        self._folder: Path = folder
        self._replay: Replay = replay
        self._lock: threading.Condition = threading.Condition()
        self._timers: dict[str, threading.Timer] = {}
        self._releasing: set[str] = set()
        self._replay_starts: dict[str, datetime] = {}
        # Results that the server could not write; the page reads them from memory.
        self._failed: dict[str, HeldResponse] = {}
        self._answering: int = 0
        self._stopping: bool = False

    def _file(self, hold_id: str) -> Path:
        return self._folder / f"{hold_id}.json"

    def _write(self, hold_id: str, saved: HoldFile) -> None:
        # A hold keeps the user's words and images, so only the owner can read it.
        self._folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_text_atomic(self._file(hold_id), json.dumps(saved), new_file_mode=0o600)

    def _saved(self, path: Path) -> HoldFile | BadFile:
        """Read one hold file, or name it on stderr when it is not a hold."""
        try:
            saved = cast(HoldFile, json.loads(path.read_text(encoding="utf-8")))
            if saved["state"] in ("pending", "released", "undone", "closing"):
                return saved
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"review-links send hold {path.name}: {error}", file=sys.stderr)
            return BadFile.UNREADABLE
        print(f"review-links send hold {path.name}: unknown state", file=sys.stderr)
        return BadFile.UNREADABLE

    @contextmanager
    def answering(self) -> Generator[None]:
        """Keep the server from a restart while a request saves a hold or answers it.

        Raise HoldsStopped after a stop, because the browser could then lose the answer of a saved hold.
        """
        with self._lock:
            if self._stopping:
                raise HoldsStopped
            self._answering += 1
        try:
            yield
        finally:
            with self._lock:
                self._answering -= 1

    def has_hold(self, hold_id: str) -> bool:
        """Check for a saved request when the current Undo time is zero."""
        with self._lock:
            if hold_id in self._timers or hold_id in self._releasing or hold_id in self._failed:
                return True
            saved = self._existing(hold_id)
            return saved is not BadFile.UNREADABLE and saved["state"] != "closing"

    def _existing(self, hold_id: str) -> HoldFile | BadFile:
        path = self._file(hold_id)
        return self._saved(path) if path.exists() else BadFile.UNREADABLE

    def hold(self, path: str, headers: Mapping[str, str], body: bytes, seconds: float, *,
             request_id: str = "") -> HeldFields:
        """Save a send or return its existing hold with the remaining Undo time."""
        hold_id = request_id or new_hold_id()
        if not valid_hold_id(hold_id):
            raise ValueError("Invalid hold id")
        started_at = time.time()
        saved: PendingFile = {
            "state": "pending", "path": path, "body": base64.b64encode(body).decode("ascii"),
            "headers": {name: value for name, value in headers.items() if name.lower() in REPLAY_HEADERS},
            "started_at": started_at, "due": started_at + seconds + RELEASE_GRACE_SECONDS}
        with self._lock:
            if hold_id in self._releasing or hold_id in self._failed:
                return {"hold": hold_id, "seconds": 0}
            timer = self._timers.get(hold_id)
            existing = self._existing(hold_id)
            if timer is not None:
                remaining = (max(0.0, existing["due"] - RELEASE_GRACE_SECONDS - time.time())
                             if existing is not BadFile.UNREADABLE and existing["state"] == "pending"
                             and not timer.finished.is_set() else 0.0)
                return {"hold": hold_id, "seconds": remaining}
            if existing is not BadFile.UNREADABLE:
                if existing["state"] != "closing":
                    return {"hold": hold_id, "seconds": 0}
                saved["due"] = time.time()
                seconds = 0
            self._write(hold_id, saved)
            self._schedule(hold_id, saved["due"])
        return {"hold": hold_id, "seconds": seconds}

    def _schedule(self, hold_id: str, due: float) -> None:
        timer = threading.Timer(max(0.0, due - time.time()), self._release, (hold_id,))
        timer.daemon = True
        self._timers[hold_id] = timer
        timer.start()

    def undo(self, hold_id: str) -> UndoResult:
        """Cancel a send that the server has not released.

        An undone mark replaces the saved words, so a repeated undo, after a lost answer, still says undone.
        The mark is written before the timer stops, so a failed write raises OSError and the send still leaves.
        """
        with self._lock:
            timer = self._timers.get(hold_id)
            if timer is not None:
                undone: UndoneFile = {"state": "undone", "undone_at": time.time()}
                self._write(hold_id, undone)
                del self._timers[hold_id]
                timer.cancel()
                return UndoResult.UNDONE
            if hold_id in self._releasing or hold_id in self._failed:
                return UndoResult.ALREADY_SENT
            saved = self._existing(hold_id)
        if saved is BadFile.UNREADABLE or saved["state"] == "closing":
            return UndoResult.MISSING
        return UndoResult.UNDONE if saved["state"] == "undone" else UndoResult.ALREADY_SENT

    def send_now(self, hold_id: str, *, expect_post: bool = False) -> bool:
        """Release a pending send or record that its POST must send at once."""
        with self._lock:
            timer = self._timers.get(hold_id)
            if timer is None:
                if not expect_post or hold_id in self._releasing or hold_id in self._failed:
                    return False
                saved = self._existing(hold_id)
                if saved is BadFile.UNREADABLE:
                    self._write(hold_id, {"state": "closing", "closed_at": time.time()})
                    return True
                return saved["state"] == "closing"
            timer.cancel()
        threading.Thread(target=self._release, args=(hold_id,), daemon=True).start()
        return True

    def stop_if_idle(self) -> bool:
        """Stop all holds when no replay runs and no hold request is answered. Return whether they stopped.

        A timer that fires after the stop leaves its file pending, and the next server sends it.
        """
        with self._lock:
            self._stopping = not self._releasing and not self._answering
            return self._stopping

    def _release(self, hold_id: str) -> None:
        with self._lock:
            if self._stopping or self._timers.pop(hold_id, None) is None:
                return
            self._releasing.add(hold_id)
        try:
            self._send(hold_id)
        finally:
            with self._lock:
                self._releasing.discard(hold_id)
                self._lock.notify_all()

    def replay_started_at(self, ticket: str, *, received_at: datetime) -> datetime:
        """Get the server's Send time for an active replay."""
        if not ticket:
            return received_at
        with self._lock:
            return self._replay_starts.get(ticket, received_at)

    def _send(self, hold_id: str) -> None:
        try:
            path = self._file(hold_id)
            saved = cast(PendingFile, json.loads(path.read_text(encoding="utf-8")))
            body = base64.b64decode(saved["body"])
            # Older pending files record the Send time in their file timestamp.
            started_at = saved["started_at"] if "started_at" in saved else path.stat().st_mtime
            sent_at = datetime.fromtimestamp(started_at, UTC)
            # A server that stops during the replay finds this result, and never sends the request again.
            self._write(hold_id, released_file(failed(MAY_HAVE_LEFT)))
        except (OSError, OverflowError, ValueError, KeyError, TypeError) as error:
            # A pending file left here would leave with the next server, after the page said it failed.
            print(f"review-links send hold {hold_id}: {error}", file=sys.stderr)
            self._file(hold_id).unlink(missing_ok=True)
            with self._lock:
                self._failed[hold_id] = failed(f"The held send failed: {error}")
            return
        # Only the replay request receives this ticket. The browser receives the hold ID.
        ticket = new_hold_id()
        with self._lock:
            self._replay_starts[ticket] = sent_at
        try:
            response = self._replay(saved["path"], saved["headers"], body, ticket)
        except TimeoutError:
            # The handler can still finish after the replay stops waiting.
            response = failed(MAY_HAVE_LEFT)
        except (OSError, http.client.HTTPException, ValueError) as error:
            response = failed(f"The held send failed: {error}")
        finally:
            with self._lock:
                del self._replay_starts[ticket]
        try:
            self._write(hold_id, released_file(response))
        except OSError as error:
            print(f"review-links send hold {hold_id}: {error}", file=sys.stderr)
            with self._lock:
                self._failed[hold_id] = response

    def result(self, hold_id: str, *, wait: float = RESULT_WAIT_SECONDS) -> HeldResponse | HoldState:
        """Return the response of a released send, waiting a short time for a pending one.

        A read keeps the result, so a browser that lost the answer can read it again until the sweep deletes it.
        """
        deadline = time.monotonic() + wait
        with self._lock:
            while hold_id in self._timers or hold_id in self._releasing:
                left = deadline - time.monotonic()
                if left <= 0:
                    return HoldState.PENDING
                _ = self._lock.wait(left)
            if hold_id in self._failed:
                return self._failed[hold_id]
            path = self._file(hold_id)
            saved = self._saved(path) if path.exists() else BadFile.UNREADABLE
        if saved is BadFile.UNREADABLE:
            return HoldState.MISSING
        match saved["state"]:
            case "pending":
                # Only a stopped server keeps a pending file without its timer; the next server sends it.
                # The wait keeps the page from asking again at once.
                time.sleep(max(0.0, deadline - time.monotonic()))
                return HoldState.PENDING
            case "undone":
                return HoldState.UNDONE
            case "closing":
                return HoldState.MISSING
            case "released":
                return HeldResponse(saved["status"], saved["content_type"], base64.b64decode(saved["body"]))

    def recover(self) -> None:
        """Schedule each saved send after a restart, and delete old results."""
        if not self._folder.is_dir():
            return
        with self._lock:
            for path in sorted(self._folder.glob("*.json")):
                saved = self._saved(path) if valid_hold_id(path.stem) else BadFile.UNREADABLE
                if saved is not BadFile.UNREADABLE and saved["state"] == "pending":
                    self._schedule(path.stem, saved["due"])
        self.expire()

    def expire(self) -> None:
        """Delete each result and undone mark older than RESULT_KEEP_SECONDS, and each file that is not a hold.

        The folder is private to the holds, so a bad hold, or an old temporary file of a cut write, goes too.
        """
        if not self._folder.is_dir():
            return
        oldest = time.time() - RESULT_KEEP_SECONDS
        with self._lock:
            busy = self._timers.keys() | self._releasing
        # A released or undone file does not change again, so the reads need no lock.
        for path in self._folder.iterdir():
            if path.stem in busy:
                continue
            if path.suffix != ".json" or not valid_hold_id(path.stem):
                if path.is_file() and path.stat().st_mtime < oldest:
                    path.unlink(missing_ok=True)
                continue
            saved = self._saved(path)
            if saved is BadFile.UNREADABLE:
                path.unlink(missing_ok=True)
            elif saved["state"] == "released" and saved["released_at"] < oldest:
                path.unlink(missing_ok=True)
            elif saved["state"] == "undone" and saved["undone_at"] < oldest:
                path.unlink(missing_ok=True)
            elif saved["state"] == "closing" and saved["closed_at"] < oldest:
                with self._lock:
                    current = self._existing(path.stem)
                    if current == saved:
                        path.unlink(missing_ok=True)


def replay_to(address: str, port: int) -> Replay:
    """Return a replay that posts a saved send to this server through its own bound address."""
    def replay(path: str, headers: Mapping[str, str], body: bytes, ticket: str) -> HeldResponse:
        connection = http.client.HTTPConnection(address, port, timeout=REPLAY_TIMEOUT_SECONDS)
        try:
            connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.putheader(REPLAY_HEADER, "1")
            connection.putheader(REPLAY_TICKET_HEADER, ticket)
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            answer = connection.getresponse()
            return HeldResponse(answer.status, answer.getheader("Content-Type") or "text/plain; charset=utf-8",
                                answer.read())
        finally:
            connection.close()
    return replay

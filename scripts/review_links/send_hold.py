"""Save each console Send until release, cancellation, or recovery.

The server replays held requests through the normal handler when Undo ends.
The server records uncertainty before release so a restart cannot replay the same request twice.
After the response expires, the terminal record still reserves the Send ID.
"""
from __future__ import annotations

import base64
import hashlib
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

NEW_ITEM_PATH = "/console/review/new-item"
HOLD_PATH = "/send-hold"
HOLD_HEADER = "X-Undo-Hold"
REQUEST_HEADER = "X-Undo-Request"
RESOLVED_HEADER = "X-Send-Hold-Resolved"
REPLAY_HEADER = "X-Undo-Replay"
REPLAY_TICKET_HEADER = "X-Undo-Replay-Ticket"
HOLD_FOLDER = ".review-link-holds"
# The request headers that a replay needs. The handlers read the body, its type, and the origin checks.
REPLAY_HEADERS = frozenset(("content-type", "origin", "host", "accept"))
# The server waits this long after the browser's Undo time ends, so a late Undo click still wins.
RELEASE_GRACE_SECONDS = 1.0
# Keep the response body for this time. Keep the Send ID after the body expires.
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
    scheduled_token_verified: NotRequired[bool]
    new_item_token_verified: NotRequired[bool]
    binding: NotRequired[str]
    release_now: NotRequired[bool]


class ReleasedFile(TypedDict):
    state: Literal["released"]
    status: int
    content_type: str
    body: NotRequired[str]
    released_at: float
    binding: NotRequired[str]
    started_at: NotRequired[float]
    refresh_token: NotRequired[bool]


class NeedsTokenFile(TypedDict):
    state: Literal["needs_token"]
    binding: str
    started_at: float
    content_type: str
    body: str


class UndoneFile(TypedDict):
    state: Literal["undone"]
    undone_at: float
    binding: NotRequired[str]


class ClosingFile(TypedDict):
    state: Literal["closing"]
    closed_at: float


HoldFile = PendingFile | ReleasedFile | NeedsTokenFile | UndoneFile | ClosingFile


class BadFile(Enum):
    """A file in the hold folder that is not a readable hold."""
    UNREADABLE = "unreadable"
    ABSENT = "no saved operation"


class HoldHeader(Enum):
    """The X-Send-Hold answer header: the send waits for Undo, or its result is not ready."""
    HELD = "held"
    PENDING = "pending"
    UNDONE = "undone"


class HeldFields(TypedDict):
    hold: str
    seconds: float


class HoldsStopped(Exception):
    """The server restarts, so it takes no new hold."""


class OperationConflict(Exception):
    """The Send ID has different content."""


class UnseenOperation(Enum):
    ABSENT = "no operation"


type AdmissionState = UnseenOperation | ReleasedFile | NeedsTokenFile


@dataclass(frozen=True)
class HoldAdmission:
    seconds: float
    new_item_token_verified: bool = False


@dataclass(frozen=True)
class DirectAdmission:
    """Run the normal New item handler on this connection."""

    new_item_token_verified: bool


@dataclass(frozen=True)
class DirectSend:
    hold_id: str
    started_at: float
    binding: str
    new_item_token_verified: bool


@dataclass(frozen=True)
class ActiveReplay:
    started_at: datetime
    scheduled_token_verified: bool
    new_item_token_verified: bool = False


class NoActiveReplay(Enum):
    ABSENT = "no active replay"


@dataclass(frozen=True)
class HeldResponse:
    status: int
    content_type: str
    body: bytes


class HoldState(Enum):
    PENDING = "pending"
    UNDONE = "undone"
    MISSING = "missing"
    EXPIRED = "expired"
    UNCERTAIN = "uncertain"


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


def operation_binding(path: str, body: bytes) -> str:
    if path == NEW_ITEM_PATH:
        try:
            payload = cast(object, json.loads(body))
        except (ValueError, RecursionError):
            payload = None
        if isinstance(payload, dict):
            fields = cast(dict[str, object], payload)
            _ = fields.pop("token", None)
            body = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(path.encode("utf-8") + b"\0" + body).hexdigest()


def released_file(response: HeldResponse, binding: str, started_at: float) -> ReleasedFile:
    return {"state": "released", "status": response.status, "content_type": response.content_type,
            "body": base64.b64encode(response.body).decode("ascii"), "released_at": time.time(),
            "binding": binding, "started_at": started_at}


def _needs_token(response: HeldResponse) -> bool:
    if response.status != 403 or not response.content_type.startswith("application/json"):
        return False
    try:
        fields = cast(object, json.loads(response.body))
    except (ValueError, RecursionError):
        return False
    return isinstance(fields, dict) and cast(dict[str, object], fields).get("code") == "stale_token"


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
        self._active_replays: dict[str, ActiveReplay] = {}
        # Results that the server could not write; the page reads them from memory.
        self._failed: dict[str, HeldResponse] = {}
        self._answering: int = 0
        self._stopping: bool = False

    def _file(self, hold_id: str) -> Path:
        return self._folder / f"{hold_id}.json"

    def _write(self, hold_id: str, saved: HoldFile) -> None:
        # A hold keeps the user's words and images, so only the owner can read it.
        content = json.dumps(saved)
        with self._lock:
            self._folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            write_text_atomic(self._file(hold_id), content, new_file_mode=0o600)

    def _saved(self, path: Path) -> HoldFile | BadFile:
        """Read one hold file, or name it on stderr when it is not a hold."""
        try:
            saved = cast(HoldFile, json.loads(path.read_text(encoding="utf-8")))
            if saved["state"] in ("pending", "released", "needs_token", "undone", "closing"):
                return saved
        except FileNotFoundError:
            return BadFile.ABSENT
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
        """Check for an operation when the current Undo time is zero."""
        with self._lock:
            return self._existing(hold_id) is not BadFile.ABSENT or hold_id in self._failed

    def _existing(self, hold_id: str) -> HoldFile | BadFile:
        return self._saved(self._file(hold_id))

    def hold(self, path: str, headers: Mapping[str, str], body: bytes, seconds: float, *,
             request_id: str = "") -> HeldFields:
        held = self.submit(path, headers, body, request_id=request_id, binding=operation_binding(path, body),
                           admit=lambda _: HoldAdmission(seconds))
        assert isinstance(held, dict)
        return held

    def submit(self, path: str, headers: Mapping[str, str], body: bytes, *, request_id: str, binding: str,
               admit: Callable[[AdmissionState], HoldAdmission | DirectAdmission | HeldResponse]
               ) -> HeldFields | DirectSend | HeldResponse:
        """Resolve an existing operation or admit a new one under the same lock."""
        hold_id = request_id or new_hold_id()
        if not valid_hold_id(hold_id):
            raise ValueError("Invalid hold id")
        while True:
            with self._lock:
                existing = self._existing(hold_id)
                opening: AdmissionState = UnseenOperation.ABSENT
                started_at = time.time()
                if isinstance(existing, dict) and existing["state"] != "closing":
                    if existing.get("binding", binding) != binding:
                        raise OperationConflict
                    if existing["state"] == "pending":
                        if "binding" not in existing:
                            original = operation_binding(existing["path"], base64.b64decode(existing["body"]))
                            if original != binding:
                                raise OperationConflict
                        seconds = (max(0.0, existing["due"] - RELEASE_GRACE_SECONDS - started_at)
                                   if hold_id in self._timers and not existing.get("release_now") else 0.0)
                        return {"hold": hold_id, "seconds": seconds}
                    if (existing["state"] != "released" and existing["state"] != "needs_token"
                            or hold_id in self._releasing or hold_id in self._failed or "binding" not in existing):
                        return {"hold": hold_id, "seconds": 0}
                    if existing["state"] == "released" and "body" in existing and not existing.get("refresh_token"):
                        return {"hold": hold_id, "seconds": 0}
                    opening = existing
                    started_at = existing.get("started_at", started_at)
                elif existing is BadFile.UNREADABLE or hold_id in self._failed:
                    return {"hold": hold_id, "seconds": 0}
            admission = admit(opening)
            with self._lock:
                if existing != self._existing(hold_id) or hold_id in self._failed:
                    continue
                if isinstance(admission, HeldResponse):
                    return admission
                if isinstance(admission, DirectAdmission):
                    self._write(hold_id, released_file(failed(MAY_HAVE_LEFT), binding, started_at))
                    self._releasing.add(hold_id)
                    return DirectSend(hold_id, started_at, binding, admission.new_item_token_verified)
                if isinstance(opening, dict):
                    return {"hold": hold_id, "seconds": 0}
                seconds = admission.seconds
                if isinstance(existing, dict) and existing["state"] == "closing":
                    seconds = 0
                saved: PendingFile = {
                    "state": "pending", "path": path, "body": base64.b64encode(body).decode("ascii"),
                    "headers": {name: value for name, value in headers.items() if name.lower() in REPLAY_HEADERS},
                    "started_at": started_at, "due": started_at + seconds + (RELEASE_GRACE_SECONDS if seconds else 0),
                    "new_item_token_verified": admission.new_item_token_verified, "binding": binding,
                    "release_now": seconds == 0}
                self._write(hold_id, saved)
                self._schedule(hold_id, saved["due"])
                return {"hold": hold_id, "seconds": seconds}

    @contextmanager
    def direct_result(self, send: DirectSend) -> Generator[Callable[[HeldResponse], None]]:
        """Save the normal handler response before it reaches the browser."""
        try:
            yield lambda answer: self._finish(send.hold_id, answer, send.binding, send.started_at,
                                              refreshable_token=True)
        finally:
            with self._lock:
                self._releasing.discard(send.hold_id)
                self._lock.notify_all()

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
            saved = self._existing(hold_id)
            if hold_id in self._releasing or hold_id in self._failed:
                return UndoResult.ALREADY_SENT
            if isinstance(saved, BadFile) or saved["state"] == "closing":
                return UndoResult.MISSING
            timer = self._timers.get(hold_id)
            if timer is not None and saved["state"] == "pending" and not saved.get("release_now"):
                undone: UndoneFile = {"state": "undone", "undone_at": time.time()}
                if "binding" in saved:
                    undone["binding"] = saved["binding"]
                self._write(hold_id, undone)
                del self._timers[hold_id]
                timer.cancel()
                self._lock.notify_all()
                return UndoResult.UNDONE
            return UndoResult.UNDONE if saved["state"] == "undone" else UndoResult.ALREADY_SENT

    def send_now(self, hold_id: str, *, expect_post: bool = False) -> bool:
        """Save the immediate deadline or record page close before the POST."""
        with self._lock:
            saved = self._existing(hold_id)
            timer = self._timers.get(hold_id)
            if timer is None:
                if not expect_post or hold_id in self._releasing or hold_id in self._failed:
                    return False
                if saved is BadFile.ABSENT:
                    self._write(hold_id, {"state": "closing", "closed_at": time.time()})
                    return True
                return isinstance(saved, dict) and saved["state"] == "closing"
            if not isinstance(saved, dict) or saved["state"] != "pending":
                raise OSError("The pending send is unreadable.")
            saved["due"] = time.time()
            saved["release_now"] = True
            # A restart must retain this deadline after the page receives the answer.
            self._write(hold_id, saved)
            timer.cancel()
            self._schedule(hold_id, saved["due"])
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

    def active_replay(self, ticket: str) -> ActiveReplay | NoActiveReplay:
        """Get the Send time and token check for an active replay."""
        if not ticket:
            return NoActiveReplay.ABSENT
        with self._lock:
            return self._active_replays.get(ticket, NoActiveReplay.ABSENT)

    def _send(self, hold_id: str) -> None:
        try:
            path = self._file(hold_id)
            saved = cast(PendingFile, json.loads(path.read_text(encoding="utf-8")))
            body = base64.b64decode(saved["body"])
            # For older files, the modification time can be later than Send.
            started_at = saved["started_at"] if "started_at" in saved else path.stat().st_mtime
            sent_at = datetime.fromtimestamp(started_at, UTC)
            # A server that stops during the replay finds this result, and never sends the request again.
            binding = saved.get("binding") or operation_binding(saved["path"], body)
            self._write(hold_id, released_file(failed(MAY_HAVE_LEFT), binding, started_at))
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
            self._active_replays[ticket] = ActiveReplay(sent_at, saved.get("scheduled_token_verified") is True,
                                                        saved.get("new_item_token_verified") is True)
        try:
            response = self._replay(saved["path"], saved["headers"], body, ticket)
        except TimeoutError:
            # The handler can still finish after the replay stops waiting.
            response = failed(MAY_HAVE_LEFT)
        except (OSError, http.client.HTTPException, ValueError) as error:
            response = failed(f"The held send failed: {error}")
        finally:
            with self._lock:
                del self._active_replays[ticket]
        self._finish(hold_id, response, binding, started_at, refreshable_token=saved["path"] == NEW_ITEM_PATH)

    def _finish(self, hold_id: str, response: HeldResponse, binding: str, started_at: float, *,
                refreshable_token: bool) -> None:
        try:
            if refreshable_token and _needs_token(response):
                self._write(hold_id, {"state": "needs_token", "binding": binding, "started_at": started_at,
                                      "content_type": response.content_type,
                                      "body": base64.b64encode(response.body).decode("ascii")})
            else:
                self._write(hold_id, released_file(response, binding, started_at))
        except OSError as error:
            print(f"review-links send hold {hold_id}: {error}", file=sys.stderr)
            with self._lock:
                self._failed[hold_id] = response

    def result(self, hold_id: str, *, wait: float = RESULT_WAIT_SECONDS) -> HeldResponse | HoldState:
        """Return the response of a released send, waiting a short time for a pending one.

        A read keeps the result so the browser can read it again until the response body expires.
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
            saved = self._existing(hold_id)
        if isinstance(saved, BadFile):
            return HoldState.MISSING if saved is BadFile.ABSENT else HoldState.UNCERTAIN
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
            case "needs_token":
                return HeldResponse(403, saved["content_type"], base64.b64decode(saved["body"]))
            case "released":
                if "body" not in saved:
                    return HoldState.UNCERTAIN if saved["status"] >= 500 else HoldState.EXPIRED
                return HeldResponse(saved["status"], saved["content_type"], base64.b64decode(saved["body"]))

    def recover(self) -> None:
        """Schedule each saved send after a restart and remove expired response bodies."""
        if not self._folder.is_dir():
            return
        with self._lock:
            for path in sorted(self._folder.glob("*.json")):
                saved = self._saved(path) if valid_hold_id(path.stem) else BadFile.UNREADABLE
                if isinstance(saved, dict) and saved["state"] == "pending":
                    self._schedule(path.stem, saved["due"])
        self.expire()

    def expire(self) -> None:
        """Remove expired response bodies and old temporary files."""
        if not self._folder.is_dir():
            return
        oldest = time.time() - RESULT_KEEP_SECONDS
        for path in self._folder.iterdir():
            with self._lock:
                if path.suffix != ".json" or not valid_hold_id(path.stem):
                    try:
                        if path.is_file() and path.stat().st_mtime < oldest:
                            path.unlink(missing_ok=True)
                    except FileNotFoundError:
                        pass
                    continue
                if path.stem in self._timers or path.stem in self._releasing:
                    continue
                saved = self._saved(path)
                if isinstance(saved, BadFile):
                    continue
                if saved["state"] == "released" and "body" in saved and saved["released_at"] < oldest:
                    del saved["body"]
                    self._write(path.stem, saved)


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

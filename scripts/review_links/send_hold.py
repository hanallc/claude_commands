"""Hold one console send for a short time so the user can undo it, as an email client does.

The browser marks a send with the hold header. The server saves the request and answers 202 at once.
When the hold time ends, the server replays the saved request to itself; the normal handler then saves and sends.
An undo before that time deletes the saved request, so nothing is saved and nothing is sent.
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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal, TypedDict, cast

from scripts.lib.atomic_file import write_text_atomic

HOLD_PATH = "/send-hold"
HOLD_HEADER = "X-Undo-Hold"
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

Replay = Callable[[str, Mapping[str, str], bytes], "HeldResponse"]


class PendingFile(TypedDict):
    state: Literal["pending"]
    path: str
    headers: dict[str, str]
    body: str
    due: float


class ReleasedFile(TypedDict):
    state: Literal["released"]
    status: int
    content_type: str
    body: str
    released_at: float


@dataclass(frozen=True)
class HeldResponse:
    status: int
    content_type: str
    body: bytes


class HoldState(Enum):
    PENDING = "pending"
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

    The pending ids, their timers and the releases in progress stay in memory, so an undo or a result wait reads
    no file. Only a release reads the saved request, and only a released result is read back from its file.
    """

    def __init__(self, folder: Path, replay: Replay) -> None:
        self._folder: Path = folder
        self._replay: Replay = replay
        self._lock: threading.Condition = threading.Condition()
        self._timers: dict[str, threading.Timer] = {}
        self._releasing: set[str] = set()
        self._stopping: bool = False

    def _file(self, hold_id: str) -> Path:
        return self._folder / f"{hold_id}.json"

    def _write(self, hold_id: str, saved: PendingFile | ReleasedFile) -> None:
        # A hold keeps the user's words and images, so only the owner can read it.
        self._folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_text_atomic(self._file(hold_id), json.dumps(saved), new_file_mode=0o600)

    def _saved(self, path: Path) -> PendingFile | ReleasedFile | None:
        """Read one hold file, or name it on stderr and skip it when it is not a hold."""
        try:
            saved = cast(PendingFile | ReleasedFile, json.loads(path.read_text(encoding="utf-8")))
            if saved["state"] in ("pending", "released"):
                return saved
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"review-links send hold {path.name}: {error}", file=sys.stderr)
            return None
        print(f"review-links send hold {path.name}: unknown state", file=sys.stderr)
        return None

    def hold(self, path: str, headers: Mapping[str, str], body: bytes, seconds: float) -> str:
        """Save one send and start its timer. Return the hold id."""
        hold_id = new_hold_id()
        saved: PendingFile = {
            "state": "pending", "path": path, "body": base64.b64encode(body).decode("ascii"),
            "headers": {name: value for name, value in headers.items() if name.lower() in REPLAY_HEADERS},
            "due": time.time() + seconds + RELEASE_GRACE_SECONDS}
        # The id is new and the sweep keeps pending files, so the large write needs no lock.
        self._write(hold_id, saved)
        with self._lock:
            self._schedule(hold_id, saved["due"])
        return hold_id

    def _schedule(self, hold_id: str, due: float) -> None:
        timer = threading.Timer(max(0.0, due - time.time()), self._release, (hold_id,))
        timer.daemon = True
        self._timers[hold_id] = timer
        timer.start()

    def undo(self, hold_id: str) -> UndoResult:
        """Delete a send that the server has not released."""
        with self._lock:
            timer = self._timers.pop(hold_id, None)
            if timer is None:
                return UndoResult.ALREADY_SENT if hold_id in self._releasing or self._file(hold_id).exists() \
                    else UndoResult.MISSING
            timer.cancel()
            self._file(hold_id).unlink(missing_ok=True)
            return UndoResult.UNDONE

    def send_now(self, hold_id: str) -> bool:
        """Release a pending send at once, because its page closed. Return whether it was pending."""
        with self._lock:
            timer = self._timers.get(hold_id)
            if timer is None:
                return False
            timer.cancel()
        threading.Thread(target=self._release, args=(hold_id,), daemon=True).start()
        return True

    def stop_if_idle(self) -> bool:
        """Stop all releases when no replay runs, so a restart cannot cut one. Return whether they stopped.

        A timer that fires after the stop leaves its file pending, and the next server sends it.
        """
        with self._lock:
            self._stopping = not self._releasing
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

    def _send(self, hold_id: str) -> None:
        try:
            saved = cast(PendingFile, json.loads(self._file(hold_id).read_text(encoding="utf-8")))
            body = base64.b64decode(saved["body"])
            # A server that stops during the replay finds this result, and never sends the request again.
            self._write(hold_id, released_file(failed(MAY_HAVE_LEFT)))
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"review-links send hold {hold_id}: {error}", file=sys.stderr)
            try:
                self._write(hold_id, released_file(failed(f"The held send failed: {error}")))
            except OSError:
                pass
            return
        try:
            response = self._replay(saved["path"], saved["headers"], body)
        except TimeoutError:
            # The handler can still finish after the replay stops waiting.
            response = failed(MAY_HAVE_LEFT)
        except (OSError, http.client.HTTPException, ValueError) as error:
            response = failed(f"The held send failed: {error}")
        try:
            self._write(hold_id, released_file(response))
        except OSError as error:
            print(f"review-links send hold {hold_id}: {error}", file=sys.stderr)

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
            path = self._file(hold_id)
            saved = self._saved(path) if path.exists() else None
        if saved is None or saved["state"] == "pending":
            return HoldState.MISSING
        return HeldResponse(saved["status"], saved["content_type"], base64.b64decode(saved["body"]))

    def recover(self) -> None:
        """Schedule each saved send after a restart, and delete old results."""
        if not self._folder.is_dir():
            return
        with self._lock:
            for path in sorted(self._folder.glob("*.json")):
                saved = self._saved(path) if valid_hold_id(path.stem) else None
                if saved is not None and saved["state"] == "pending":
                    self._schedule(path.stem, saved["due"])
        self.expire()

    def expire(self) -> None:
        """Delete the results that no browser read within RESULT_KEEP_SECONDS."""
        if not self._folder.is_dir():
            return
        oldest = time.time() - RESULT_KEEP_SECONDS
        with self._lock:
            busy = self._timers.keys() | self._releasing
        # A released file does not change again, so the reads need no lock.
        for path in self._folder.glob("*.json"):
            if not valid_hold_id(path.stem) or path.stem in busy:
                continue
            saved = self._saved(path)
            if saved is not None and saved["state"] == "released" and saved["released_at"] < oldest:
                path.unlink(missing_ok=True)


def replay_to(address: str, port: int) -> Replay:
    """Return a replay that posts a saved send to this server through its own bound address."""
    def replay(path: str, headers: Mapping[str, str], body: bytes) -> HeldResponse:
        connection = http.client.HTTPConnection(address, port, timeout=REPLAY_TIMEOUT_SECONDS)
        try:
            connection.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            answer = connection.getresponse()
            return HeldResponse(answer.status, answer.getheader("Content-Type") or "text/plain; charset=utf-8",
                                answer.read())
        finally:
            connection.close()
    return replay


def held_fields(hold_id: str, seconds: float) -> dict[str, object]:
    """Return the 202 answer that tells the browser to show its Undo for this many seconds."""
    return {"hold": hold_id, "seconds": seconds}

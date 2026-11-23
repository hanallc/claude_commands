"""Hold one console send for a short time so the user can undo it, as an email client does.

The browser marks a send with the hold header. The server saves the request and answers 202 at once.
When the hold time ends, the server replays the saved request to itself; the normal handler then saves and sends.
An undo before that time deletes the saved request, so nothing is saved and nothing is sent.
Each hold is a file, so a server restart replays the holds that it finds, and a browser that lost its
connection reads the same result again.
"""
from __future__ import annotations

import base64
import http.client
import json
import secrets
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
# A handler that runs longer than this gives a 503 result.
REPLAY_TIMEOUT_SECONDS = 300.0

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

    def _file(self, hold_id: str) -> Path:
        return self._folder / f"{hold_id}.json"

    def _write(self, hold_id: str, saved: PendingFile | ReleasedFile) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self._file(hold_id), json.dumps(saved))

    def hold(self, path: str, headers: Mapping[str, str], body: bytes, seconds: float) -> str:
        """Save one send and start its timer. Return the hold id."""
        hold_id = new_hold_id()
        saved: PendingFile = {
            "state": "pending", "path": path, "body": base64.b64encode(body).decode("ascii"),
            "headers": {name: value for name, value in headers.items() if name.lower() in REPLAY_HEADERS},
            "due": time.time() + seconds + RELEASE_GRACE_SECONDS}
        with self._lock:
            self._write(hold_id, saved)
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

    def busy(self) -> bool:
        """Say whether a replay runs now, so a restart must wait for its result."""
        with self._lock:
            return bool(self._releasing)

    def _release(self, hold_id: str) -> None:
        with self._lock:
            if self._timers.pop(hold_id, None) is None:
                return
            self._releasing.add(hold_id)
        try:
            saved = cast(PendingFile, json.loads(self._file(hold_id).read_text(encoding="utf-8")))
            try:
                response = self._replay(saved["path"], saved["headers"], base64.b64decode(saved["body"]))
            except (OSError, http.client.HTTPException) as error:
                response = HeldResponse(503, "application/json; charset=utf-8",
                                        json.dumps({"reason": f"The held send failed: {error}"}).encode("utf-8"))
            released: ReleasedFile = {"state": "released", "status": response.status,
                                      "content_type": response.content_type,
                                      "body": base64.b64encode(response.body).decode("ascii"),
                                      "released_at": time.time()}
            with self._lock:
                self._write(hold_id, released)
        finally:
            with self._lock:
                self._releasing.discard(hold_id)
                self._lock.notify_all()

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
            try:
                saved = cast(PendingFile | ReleasedFile, json.loads(self._file(hold_id).read_text(encoding="utf-8")))
            except FileNotFoundError:
                return HoldState.MISSING
        if saved["state"] == "pending":
            return HoldState.MISSING
        return HeldResponse(saved["status"], saved["content_type"], base64.b64decode(saved["body"]))

    def recover(self) -> None:
        """Schedule each saved send after a restart, and delete old results."""
        if not self._folder.is_dir():
            return
        with self._lock:
            for path in sorted(self._folder.glob("*.json")):
                if not valid_hold_id(path.stem):
                    continue
                saved = cast(PendingFile | ReleasedFile, json.loads(path.read_text(encoding="utf-8")))
                if saved["state"] == "pending":
                    self._schedule(path.stem, saved["due"])
        self.expire()

    def expire(self) -> None:
        """Delete the results that no browser read within RESULT_KEEP_SECONDS."""
        if not self._folder.is_dir():
            return
        oldest = time.time() - RESULT_KEEP_SECONDS
        with self._lock:
            for path in self._folder.glob("*.json"):
                if not valid_hold_id(path.stem) or path.stem in self._timers or path.stem in self._releasing:
                    continue
                saved = cast(PendingFile | ReleasedFile, json.loads(path.read_text(encoding="utf-8")))
                if saved["state"] == "released" and saved["released_at"] < oldest:
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

"""Hold one console send for a short time so the user can undo it, as an email client does.

The browser marks a send with the hold header. The server saves the request and answers 202 at once.
When the hold time ends, the server replays the saved request to itself; the normal handler then saves and sends.
An undo before that time deletes the saved request, so nothing is saved and nothing is sent.
Each hold is a file, so a server restart replays the holds that it finds.
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
from typing import TypedDict, cast

from scripts.lib.atomic_file import write_text_atomic

HOLD_PATH = "/send-hold"
HOLD_HEADER = "X-Undo-Hold"
HOLD_FOLDER = ".review-link-holds"
# The request headers that a replay needs. The handlers read the body, its type, and the origin checks.
REPLAY_HEADERS = ("Content-Type", "Origin", "Host", "Accept")
# The server waits this long after the browser's Undo time ends, so a late Undo click still wins.
RELEASE_GRACE_SECONDS = 1.0
# A browser reads the result of a released send within this time; after it, the result file is deleted.
RESULT_KEEP_SECONDS = 600.0
RESULT_WAIT_SECONDS = 25.0


class HeldFile(TypedDict):
    id: str
    path: str
    headers: dict[str, str]
    body: str
    due: float
    response: HeldResponseFields | None
    released_at: float


class HeldResponseFields(TypedDict):
    status: int
    content_type: str
    body: str


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
    """Keep the saved sends of one server and release each one when its time ends."""

    def __init__(self, folder: Path, replay: Callable[[str, Mapping[str, str], bytes], HeldResponse]) -> None:
        self._folder: Path = folder
        self._replay: Callable[[str, Mapping[str, str], bytes], HeldResponse] = replay
        self._lock: threading.Condition = threading.Condition()
        self._releasing: set[str] = set()
        self._timers: dict[str, threading.Timer] = {}

    def _file(self, hold_id: str) -> Path:
        return self._folder / f"{hold_id}.json"

    def _read(self, hold_id: str) -> HeldFile | None:
        try:
            return cast(HeldFile, json.loads(self._file(hold_id).read_text(encoding="utf-8")))
        except FileNotFoundError:
            return None

    def _write(self, saved: HeldFile) -> None:
        self._folder.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self._file(saved["id"]), json.dumps(saved))

    def hold(self, path: str, headers: Mapping[str, str], body: bytes, seconds: float) -> str:
        """Save one send and start its timer. Return the hold id."""
        hold_id = new_hold_id()
        saved: HeldFile = {"id": hold_id, "path": path, "body": base64.b64encode(body).decode("ascii"),
                           "headers": {name: value for name, value in headers.items() if name in REPLAY_HEADERS},
                           "due": time.time() + seconds + RELEASE_GRACE_SECONDS, "response": None, "released_at": 0.0}
        with self._lock:
            self._write(saved)
            self._schedule(saved)
        return hold_id

    def _schedule(self, saved: HeldFile) -> None:
        timer = threading.Timer(max(0.0, saved["due"] - time.time()), self._release, (saved["id"],))
        timer.daemon = True
        self._timers[saved["id"]] = timer
        timer.start()

    def undo(self, hold_id: str) -> UndoResult:
        """Delete a send that the server has not released."""
        with self._lock:
            saved = self._read(hold_id)
            if saved is None:
                return UndoResult.MISSING
            if hold_id in self._releasing or saved["response"] is not None:
                return UndoResult.ALREADY_SENT
            timer = self._timers.pop(hold_id, None)
            if timer is not None:
                timer.cancel()
            self._file(hold_id).unlink()
            return UndoResult.UNDONE

    def _release(self, hold_id: str) -> None:
        with self._lock:
            _ = self._timers.pop(hold_id, None)
            saved = self._read(hold_id)
            if saved is None or saved["response"] is not None or hold_id in self._releasing:
                return
            self._releasing.add(hold_id)
        try:
            response = self._replay(saved["path"], saved["headers"], base64.b64decode(saved["body"]))
        except (OSError, http.client.HTTPException) as error:
            response = HeldResponse(503, "application/json; charset=utf-8",
                                    json.dumps({"reason": f"The held send failed: {error}"}).encode("utf-8"))
        with self._lock:
            self._releasing.discard(hold_id)
            saved["response"] = {"status": response.status, "content_type": response.content_type,
                                 "body": base64.b64encode(response.body).decode("ascii")}
            saved["released_at"] = time.time()
            saved["body"] = ""
            self._write(saved)
            self._lock.notify_all()

    def result(self, hold_id: str, *, wait: float = RESULT_WAIT_SECONDS) -> HeldResponse | HoldState:
        """Return the response of a released send, waiting a short time for a pending one."""
        deadline = time.monotonic() + wait
        with self._lock:
            while True:
                saved = self._read(hold_id)
                if saved is None:
                    return HoldState.MISSING
                if (response := saved["response"]) is not None:
                    self._file(hold_id).unlink()
                    return HeldResponse(response["status"], response["content_type"], base64.b64decode(response["body"]))
                left = deadline - time.monotonic()
                if left <= 0:
                    return HoldState.PENDING
                _ = self._lock.wait(left)

    def recover(self) -> None:
        """Schedule each saved send after a restart, and delete results that no browser read in time."""
        if not self._folder.is_dir():
            return
        with self._lock:
            for path in sorted(self._folder.glob("*.json")):
                if not valid_hold_id(path.stem):
                    continue
                saved = self._read(path.stem)
                if saved is None:
                    continue
                if saved["response"] is None:
                    self._schedule(saved)
                elif time.time() - saved["released_at"] > RESULT_KEEP_SECONDS:
                    path.unlink()


def replay_to(address: str, port: int) -> Callable[[str, Mapping[str, str], bytes], HeldResponse]:
    """Return a replay that posts a saved send to this server through its own bound address."""
    def replay(path: str, headers: Mapping[str, str], body: bytes) -> HeldResponse:
        connection = http.client.HTTPConnection(address, port, timeout=300)
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

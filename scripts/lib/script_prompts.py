"""Keep slash-command evidence until the prompt hook consumes its hash.

Each submission has one record without time expiry.
The configured count limit removes the oldest evidence and records its cause.
"""
from __future__ import annotations

import hashlib
import math
import os
import json
import socket
import time
import uuid
from datetime import datetime, timezone
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from scripts.agents import worker_lifecycle
from scripts.lib import conf


def root() -> Path:
    state = os.environ.get("CONVERSATION_PAUSE_STATE_DIR") or Path.home() / ".local/state/conversation-pause"
    return Path(state) / "script-prompts"


def _path(pane: str, text: str) -> Path:
    words = " ".join(text.split())
    return root() / hashlib.sha256(f"{pane}\0{words}".encode()).hexdigest()


@contextmanager
def locked(deadline: float = math.inf) -> Generator[None]:
    folder = root()
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".lock").open("a") as stream:
        try:
            worker_lifecycle.acquire_flock(stream, deadline=deadline)
        except worker_lifecycle.OperationDeadlineExpired as error:
            raise TimeoutError("script prompt record lock deadline elapsed") from error
        yield


def _matching(pane: str, text: str) -> list[Path]:
    path = _path(pane, text)
    return [*([path] if path.is_file() else []), *root().glob(path.name + ".*")]


def _oldest_first(paths: list[Path]) -> list[Path]:
    records: list[tuple[int, Path]] = []
    for path in paths:
        try:
            records.append((path.stat().st_mtime_ns, path))
        except FileNotFoundError:
            continue
    return [path for _, path in sorted(records)]


def _evict(path: Path) -> None:
    folder = Path(os.environ.get("NOTIFY_STATE_DIR") or Path.home() / ".local/state/notify")
    folder.mkdir(parents=True, exist_ok=True)
    event = {"event_id": uuid.uuid4().hex, "timestamp_utc": datetime.now(timezone.utc).isoformat(),
             "event": "script_evidence_evicted", "cause": "record_limit", "machine": socket.gethostname(),
             "record": path.name, "result": "oldest_removed"}
    with (folder / "auto-silence-events.jsonl").open("a", encoding="utf-8") as stream:
        _ = stream.write(json.dumps(event) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    path.unlink(missing_ok=True)


def record(pane: str, text: str) -> None:
    if text.lstrip().startswith("/"):
        prepared = prepare_record(pane, text, deadline=math.inf)
        prepared.publish()
        prepared.trim()


@dataclass(frozen=True)
class PreparedRecord:
    path: Path
    limit: int
    deadline: float

    def publish(self) -> None:
        with locked(self.deadline):
            self._check_deadline()
            self.path.touch()

    def trim(self) -> None:
        with locked(self.deadline):
            records = _oldest_first([item for item in root().iterdir() if item.name != ".lock"])
            for old in records[:max(0, len(records) - self.limit)]:
                self._check_deadline()
                _evict(old)

    def _check_deadline(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError("script prompt record deadline elapsed")


def prepare_record(pane: str, text: str, *, deadline: float) -> PreparedRecord:
    """Prepare the record without writing it or evicting another record."""
    path = _path(pane, text)
    limit = conf.read("script_prompts").whole("max_records")
    return PreparedRecord(path.with_name(path.name + "." + uuid.uuid4().hex), limit, deadline)


def typed_by_script(pane: str, text: str) -> bool:
    if not pane:
        return False
    return bool(_matching(pane, text))


def typed_here(text: str) -> bool:
    """Consume matching pane and session evidence under the record lock."""
    session = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    identities = [os.environ.get("HERDR_PANE_ID", ""), "session:" + session if session else ""]
    with locked():
        matched = False
        for identity in set(identities) - {""}:
            for path in _oldest_first(_matching(identity, text)):
                try:
                    path.unlink()
                except FileNotFoundError:
                    continue
                matched = True
                break
        return matched

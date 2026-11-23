"""Record answers to requests for approval of registered push texts."""
from __future__ import annotations

import fcntl
import json
import os
import re
import subprocess
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast

from scripts.lib import conf, script_prompts
from scripts.lib.atomic_file import write_text_atomic

APPROVAL_WORDS = frozenset({"yes", "go", "ok", "approve", "approved"})
NEGATIONS = frozenset({"not", "no", "never"})


@dataclass(frozen=True)
class Ask:
    text_id: str
    fingerprint: str
    session: str
    time: str


@dataclass(frozen=True)
class NoAsk:
    pass


@dataclass(frozen=True)
class Refused:
    reason: str


@dataclass(frozen=True)
class LeftOpen:
    pass


@dataclass(frozen=True)
class Approved:
    text_id: str
    fingerprint: str
    words: str
    session: str
    pane: str
    time: str
    source: str


@dataclass(frozen=True)
class NoUsableApproval:
    pass


AnswerOutcome = NoAsk | Refused | LeftOpen | Approved


def _state() -> Path:
    return Path(os.environ.get("NOTIFY_STATE_DIR") or Path.home() / ".local/state/notify")


def _ask_path(pane: str) -> Path:
    return _state() / "text-asks" / f"{pane}.json"


def _approval_path(text_id: str, fingerprint: str) -> Path:
    return _state() / "text-approvals" / f"{text_id}-{fingerprint}.json"


@contextmanager
def _lock_ask(path: Path) -> Generator[None]:
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def open_ask(text_id: str, fingerprint: str, session_id: str, pane: str, now: datetime) -> None:
    path = _ask_path(pane)
    path.parent.mkdir(parents=True, exist_ok=True)
    ask = Ask(text_id, fingerprint, session_id, now.isoformat())
    with _lock_ask(path):
        try:
            write_text_atomic(path, json.dumps(asdict(ask)) + "\n", new_file_mode=0o600)
        except OSError:
            path.unlink(missing_ok=True)
            raise


def _fields(path: Path) -> dict[str, str]:
    value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict) or not all(isinstance(item, str) for item in cast(dict[str, object], value).values()):
        raise ValueError("Expected a JSON object with text fields.")
    return cast(dict[str, str], value)


def approval_for(text_id: str, fingerprint: str) -> Approved | NoUsableApproval:
    try:
        record = Approved(**_fields(_approval_path(text_id, fingerprint)))
    except (OSError, ValueError, TypeError):
        return NoUsableApproval()
    if (record.text_id, record.fingerprint, record.source) == (text_id, fingerprint, "user-prompt"):
        return record
    return NoUsableApproval()


def read_pane(pane: str) -> dict[str, object]:
    from scripts.herdr import herdr

    client = herdr.Herdr(os.environ.get("HERDR_SESSION") or "default")
    return herdr.json_object(client.result("pane", "get", pane).get("pane"), "herdr pane")


def answer(session_id: str, pane: str, prompt: str, source_typed: bool, now: datetime) -> AnswerOutcome:
    path = _ask_path(pane)
    try:
        _ = path.stat()
    except FileNotFoundError:
        return NoAsk()
    except OSError as error:
        return Refused(f"Cannot check the ask. {error}")

    if not source_typed:
        return Refused("The prompt source must be typed.")
    try:
        with _lock_ask(path):
            return _answer(path, session_id, pane, prompt, now)
    except OSError as error:
        return Refused(f"Cannot update the ask. {error}")


def _answer(path: Path, session_id: str, pane: str, prompt: str, now: datetime) -> AnswerOutcome:
    try:
        ask = Ask(**_fields(path))
        asked_at = datetime.fromisoformat(ask.time)
        age = (now - asked_at).total_seconds()
    except FileNotFoundError:
        return NoAsk()
    except (OSError, ValueError, TypeError) as error:
        return Refused(f"Cannot read the ask. {error}")
    if ask.session != session_id:
        return Refused("The ask belongs to another session.")
    try:
        limit = conf.read("notify").number("TEXT_ASK_MINUTES") * 60
    except conf.Unusable as error:
        return Refused(f"Cannot read the ask time limit. {error}")
    if not 0 <= age < limit:
        return Refused("The ask is outside its time limit.")
    if script_prompts.typed_by_script(pane, prompt):
        return Refused("A script typed the prompt.")
    if script_prompts.keys_since(pane, asked_at - timedelta(seconds=limit)):
        return Refused("A script sent printable keys to this pane during the ask or within one ask time limit before it.")

    from scripts.herdr.herdr_types import HerdrFailure

    try:
        row = read_pane(pane)
    except (HerdrFailure, OSError, subprocess.SubprocessError, ValueError) as error:
        return Refused(f"Cannot read the herdr pane. {error}")
    if row.get("focused") is not True:
        return Refused("The herdr pane must have focus.")
    report = row.get("agent_session")
    if not isinstance(report, dict) or cast(dict[str, object], report).get("value") != session_id:
        return Refused("The herdr pane must report this session.")

    normalized = prompt.casefold()
    words = re.findall(r"[^\W_]+", normalized)
    if (not words or words[0] not in APPROVAL_WORDS or NEGATIONS.intersection(words)
            or re.search(r"n['’]t(?![^\W_])", normalized)):
        return LeftOpen()

    approval = Approved(ask.text_id, ask.fingerprint, prompt, session_id, pane, now.isoformat(), "user-prompt")
    path.unlink()
    write_text_atomic(_approval_path(ask.text_id, ask.fingerprint),
                      json.dumps(asdict(approval)) + "\n", new_file_mode=0o600)
    return approval

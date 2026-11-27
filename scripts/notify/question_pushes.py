"""Retire pushes when their registered Question opening ends."""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from scripts.lib import conf, operation_deadline
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone
from scripts.notify import push_reference, silence
from scripts.notify.push_reference import PushIdentity


@dataclass(frozen=True)
class Binding:
    identity: PushIdentity
    emergency: bool

    @property
    def reference(self) -> push_reference.QuestionReference:
        question = self.identity.question
        assert question is not None
        return question.reference


class RetirementTransport(Protocol):
    def retire_question_copies(self, identity: PushIdentity, *, emergency: bool) -> str: ...


class SettledQuestion(ValueError):
    """The Question opening no longer accepts a push."""


class QuestionOpeningUnreadable(OSError):
    """The Question read could not prove an opening for retirement."""


def directory() -> Path:
    return silence.state_dir() / "question-pushes"


def binding_path(source: str, key: str) -> Path:
    digest = hashlib.sha256(json.dumps([source, key]).encode()).hexdigest()
    return directory() / f"{digest}.json"


def _released_binding_path(identity: PushIdentity) -> Path:
    digest = hashlib.sha256(identity.tag_key.encode()).hexdigest()
    return directory() / f"released-{digest}.json"


def read_binding(path: Path) -> Binding | None:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return None
    if isinstance(value, dict):
        fields = cast(dict[str, object], value)
        reference = push_reference.read_reference(fields.get("reference"))
        source, key, emergency = fields.get("source"), fields.get("key"), fields.get("emergency")
        if (reference["kind"] == "question" and isinstance(source, str) and source and isinstance(key, str)
                and key and isinstance(emergency, bool)):
            identity = PushIdentity(source, key, reference)
            if path == binding_path(source, key) or (emergency and path == _released_binding_path(identity)):
                return Binding(identity, emergency)
    raise ValueError(f"invalid Question push binding {path}")


def opening(reference: push_reference.QuestionReference) -> push_reference.QuestionOpening:
    """Read the Question on the machine that admitted its reference."""
    path = reference["path"]
    if reference.get("machine") != "mac" or sys.platform == "darwin":
        return push_reference.read_question(Path(path))
    command = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/pushover.py" '
    reply = subprocess.run(["ssh", "-o", "BatchMode=yes", "mac", command + shlex.join(["question-opening", path])],
                           capture_output=True, text=True, check=False,
                           timeout=operation_deadline.remaining(conf.read("send").number("remote_transport_seconds")))
    if reply.returncode != 0:
        raise OSError(f"cannot read Question on mac: {reply.stderr.strip() or reply.returncode}")
    data = cast(object, json.loads(reply.stdout))
    if isinstance(data, dict):
        fields = cast(dict[str, object], data)
        status, since = fields.get("status"), fields.get("since")
        if isinstance(status, str) and status in ("open", "closed") and isinstance(since, str) and since:
            return push_reference.QuestionOpening(status, since)
    raise ValueError(f"invalid Question state from mac for {path}")


def settled(reference: push_reference.QuestionReference) -> str:
    current = opening(reference)
    if current.since != reference["since"]:
        return "question reopened"
    return "question closed" if current.status == "closed" else ""


def _retire_binding_locked(path: Path, identity: PushIdentity, *, emergency: bool) -> str:
    """Retire the binding that the caller read under the silence lock."""
    released_path = _released_binding_path(identity)
    released = path != released_path and read_binding(released_path) is not None
    result = retire_copies(identity, emergency=emergency or released)
    if released:
        released_path.unlink()
    path.unlink()
    return result


def retire_copies(identity: PushIdentity, *, emergency: bool) -> str:
    transport = cast(RetirementTransport, cast(object, import_module("scripts.message.send")))
    return transport.retire_question_copies(identity, emergency=emergency)


def _require_opening(reference: push_reference.QuestionReference) -> None:
    try:
        reason = settled(reference)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise QuestionOpeningUnreadable(str(error)) from error
    if reason:
        raise SettledQuestion(reason)


def bind_locked(identity: PushIdentity, *, emergency: bool) -> Binding:
    """Record the active binding before the caller keeps or posts the push."""
    question = identity.question
    assert question is not None
    reference = question.reference
    _require_opening(reference)
    path = binding_path(identity.sender, identity.key)
    previous = read_binding(path)
    if previous is not None and previous.identity != identity:
        _ = _retire_binding_locked(path, previous.identity, emergency=previous.emergency)
        previous = None
        _require_opening(reference)
    released_path = _released_binding_path(identity)
    released = read_binding(released_path) is not None
    bound = Binding(identity, emergency or released or (previous is not None and previous.emergency))
    if bound != previous:
        _write_binding(path, bound)
    if released:
        released_path.unlink()
    return bound


def _write_binding(path: Path, binding: Binding) -> None:
    fields = {"source": binding.identity.sender, "key": binding.identity.key,
              "reference": binding.reference, "emergency": binding.emergency}
    write_text_atomic(path, json.dumps(fields) + "\n")


def bind_released_emergency_locked(identity: PushIdentity, *, confirmed_open: bool) -> None:
    """Record repeat cancellation before posting a released emergency."""
    path = binding_path(identity.sender, identity.key)
    previous = read_binding(path)
    if previous is not None and previous.identity != identity:
        # Keep the active binding for the newer push.
        path = _released_binding_path(identity)
        previous = read_binding(path)
    if previous is None and not confirmed_open:
        return
    binding = Binding(identity, emergency=True)
    if binding != previous:
        _write_binding(path, binding)


def retire_matching(identity: PushIdentity, *, emergency: bool = False) -> str:
    """Retire a binding after its explicit emergency clear check passes."""
    path = binding_path(identity.sender, identity.key)
    with silence.locked():
        binding = read_binding(path)
        if binding is not None and binding.identity == identity:
            return _retire_binding_locked(path, identity, emergency=emergency or binding.emergency)
        released_path = _released_binding_path(identity)
        if read_binding(released_path) is not None:
            return _retire_binding_locked(released_path, identity, emergency=True)
        return retire_copies(identity, emergency=emergency)


def report(line: str) -> None:
    root = Path(os.environ.get("NOTIFIER_STATE_DIR", str(Path.home() / ".local/state/notifier")))
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "notifier.log").open("a", encoding="utf-8") as output:
            _ = output.write(f"{datetime.now(user_zone()).isoformat()} | {line}\n")
    except OSError as error:
        print(f"{line}. Cannot write the notifier log: {error}", file=sys.stderr)


def drain(question: Path | None = None) -> int:
    """Retire current bindings and old kept copies on the notification owner."""
    started = time.monotonic()
    if silence.on_mac() or not (directory().is_dir() or silence.kept_file().exists()):
        return 0
    deadline = started + conf.read("send").number("user_cancel_timeout")
    try:
        with operation_deadline.within(deadline):
            return _drain(question)
    except TimeoutError as error:
        report(f"Question push retirement pending. {error}")
        return 1


def _drain(question: Path | None) -> int:
    selected = str(question.resolve()) if question is not None else None
    failed = 0

    def wanted(reference: push_reference.QuestionReference) -> bool:
        return selected is None or (reference["path"] == selected and reference.get("machine") != "mac")

    pending: dict[str, Path | Binding] = {}
    for path in directory().glob("*.json"):
        operation_deadline.check()
        pending[path.name] = path
    try:
        for push in silence.read_kept(strict=True):
            operation_deadline.check()
            reference = push.get("reference", push_reference.NO_REFERENCE)
            if reference["kind"] == "question" and wanted(reference):
                identity = PushIdentity(push["source"], push["key"], reference)
                pending[identity.tag_key] = Binding(identity, False)
    except TimeoutError:
        raise
    except (OSError, ValueError) as error:
        report(f"Cannot read kept Question pushes. {error}")
        failed = 1
    operation_deadline.check()
    if not pending:
        return failed

    cursor = directory() / "retry-cursor"
    entries = sorted(pending)
    if question is None:
        try:
            turn = bisect_right(entries, cursor.read_text(encoding="utf-8"))
            entries = entries[turn:] + entries[:turn]
        except FileNotFoundError:
            pass
    last_checked: str | None = None
    for key in entries:
        entry = pending[key]
        terminal = question is not None
        label = str(entry) if isinstance(entry, Path) else entry.reference["path"]
        try:
            operation_deadline.check()
            with silence.locked(nonblocking=True):
                last_checked = key
                if isinstance(entry, Path):
                    binding = read_binding(entry)
                    operation_deadline.check()
                    if binding is None or not wanted(binding.reference):
                        continue
                else:
                    binding = entry
                    current = read_binding(binding_path(binding.identity.sender, binding.identity.key))
                    operation_deadline.check()
                    if current is not None and current.identity == binding.identity:
                        continue
                label = binding.reference["path"]
                reason = settled(binding.reference)
                operation_deadline.check()
                if not reason:
                    continue
                terminal = True
                if isinstance(entry, Path):
                    report(_retire_binding_locked(entry, binding.identity, emergency=binding.emergency))
                else:
                    dropped = silence.drop_locked(binding.identity)
                    if dropped:
                        report(f"retired {dropped} kept copies for {binding.identity.sender} / {binding.identity.key}")
        except BlockingIOError:
            continue
        except TimeoutError as error:
            report(f"Question push retirement pending. {label}: {error}")
            failed = 1
            break
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            message = "Answer saved. Push retirement pending." if terminal else "Question push state unavailable."
            report(f"{message} {label}: {error}")
            failed = 1
    if question is None and last_checked is not None:
        write_text_atomic(cursor, last_checked)
    return failed


def answer_saved(path: Path) -> None:
    """Keep the committed answer when notification retirement fails."""
    try:
        _ = drain(path)
    except Exception as error:
        report(f"Answer saved. Push retirement pending. {path}: {error}")

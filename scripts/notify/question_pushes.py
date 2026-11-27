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
from scripts.notify import push_reference, silence


@dataclass(frozen=True)
class Binding:
    source: str
    key: str
    reference: push_reference.QuestionReference
    emergency: bool

    @property
    def tag_key(self) -> str:
        """Give each sender, key and opening its own emergency tag."""
        return json.dumps([self.source, self.key, self.reference], sort_keys=True)


class RetirementTransport(Protocol):
    def retire_question_copies(self, binding: Binding) -> str: ...


class SettledQuestion(ValueError):
    """The Question opening no longer accepts a push."""


def directory() -> Path:
    return silence.state_dir() / "question-pushes"


def binding_path(source: str, key: str) -> Path:
    digest = hashlib.sha256(json.dumps([source, key]).encode()).hexdigest()
    return directory() / f"{digest}.json"


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
                and key and isinstance(emergency, bool) and path == binding_path(source, key)):
            return Binding(source, key, reference, emergency)
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


def retire_locked(path: Path, binding: Binding) -> str:
    """Retire the current binding under the silence lock."""
    result = retire_copies(binding)
    path.unlink()
    return result


def retire_copies(binding: Binding) -> str:
    transport = cast(RetirementTransport, cast(object, import_module("scripts.message.send")))
    return transport.retire_question_copies(binding)


def bind_locked(source: str, key: str, reference: push_reference.QuestionReference, *, emergency: bool) -> Binding:
    """Record the active binding before the caller keeps or posts the push."""
    path = binding_path(source, key)
    previous = read_binding(path)
    if reason := settled(reference):
        raise SettledQuestion(reason)
    if previous is not None and previous.reference != reference:
        _ = retire_locked(path, previous)
        previous = None
        if reason := settled(reference):
            raise SettledQuestion(reason)
    bound = Binding(source, key, reference, emergency or (previous is not None and previous.emergency))
    if bound != previous:
        write_text_atomic(path, json.dumps({"source": source, "key": key, "reference": reference,
                                           "emergency": bound.emergency}) + "\n")
    return bound


def retire_matching(source: str, key: str, reference: push_reference.QuestionReference) -> str:
    """Retire a binding after its explicit emergency clear check passes."""
    path = binding_path(source, key)
    with silence.locked():
        binding = read_binding(path)
        if binding is not None and binding.reference == reference:
            return retire_locked(path, binding)
        return retire_copies(Binding(source, key, reference, False))


def report(line: str) -> None:
    root = Path(os.environ.get("NOTIFIER_STATE_DIR", str(Path.home() / ".local/state/notifier")))
    try:
        root.mkdir(parents=True, exist_ok=True)
        with (root / "notifier.log").open("a", encoding="utf-8") as output:
            _ = output.write(f"{datetime.now().astimezone().isoformat()} | {line}\n")
    except OSError as error:
        print(f"{line}. Cannot write the notifier log: {error}", file=sys.stderr)


def drain(question: Path | None = None) -> int:
    """Retire current bindings and old kept copies on the notification owner."""
    if silence.on_mac() or not (directory().is_dir() or silence.kept_file().exists()):
        return 0
    selected = str(question.resolve()) if question is not None else None
    failed = 0

    def wanted(reference: push_reference.QuestionReference) -> bool:
        return selected is None or (reference["path"] == selected and reference.get("machine") != "mac")

    pending: dict[str, tuple[Binding, Path | None]] = {}
    for path in sorted(directory().glob("*.json")):
        try:
            binding = read_binding(path)
            if binding is not None and wanted(binding.reference):
                pending[binding.tag_key] = (binding, path)
        except (OSError, ValueError) as error:
            report(f"Question push state unavailable. {path.name}: {error}")
            failed = 1
    try:
        for push in silence.read_kept(strict=True):
            reference = push.get("reference", push_reference.NO_REFERENCE)
            if reference["kind"] == "question" and wanted(reference):
                binding = Binding(push["source"], push["key"], reference, False)
                _ = pending.setdefault(binding.tag_key, (binding, None))
    except (OSError, ValueError) as error:
        report(f"Cannot read kept Question pushes. {error}")
        failed = 1
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
    deadline = time.monotonic() + conf.read("send").number("user_cancel_timeout")
    last_checked: str | None = None
    for key in entries:
        if time.monotonic() >= deadline:
            break
        observed, path = pending[key]
        terminal = question is not None
        try:
            with silence.locked(nonblocking=True), operation_deadline.within(deadline):
                last_checked = key
                current = read_binding(path or binding_path(observed.source, observed.key))
                if (path is not None and current != observed
                        or path is None and current is not None and current.reference == observed.reference):
                    continue
                if not settled(observed.reference):
                    continue
                terminal = True
                if path is not None:
                    report(retire_locked(path, observed))
                else:
                    dropped = silence.drop_locked(observed.source, observed.key, observed.reference)
                    if dropped:
                        report(f"retired {dropped} kept copies for {observed.source} / {observed.key}")
        except BlockingIOError:
            continue
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            message = "Answer saved. Push retirement pending." if terminal else "Question push state unavailable."
            report(f"{message} {observed.reference['path']}: {error}")
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

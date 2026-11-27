#!/usr/bin/env python3
"""Keep the user's pushes for a while: `silence.py on [--until "YYYY-MM-DD HH:MM" | --for SPAN] | off | status`.

Silence keeps ordinary pushes. Priority 2 pushes and pushes with --through-silence go out at once.
`off` ends silence and posts
the kept pushes, oldest first, as they would have gone out, and prints how many. A kept push
with the same non-empty key from the same source replaces the older kept one, so `off` posts
only the newest for each Question opening or for pushes without a Question reference.
`on` with no end lasts until `off`; --until (a time in the user's zone,
user_zone.py) or --for (a span: 1h, 45min, 1h30min) makes a transient systemd user timer that
runs `off --timer UNIT` then, ending only the silence that still names it. `on` again replaces the
end. If the end passes without the timer (natedev was off), the next push or silence.py call ends
silence as `off` does.

`drop --source S --key K` removes kept pushes without a Question reference for that source and key.
It prints the number of pushes it removed. `send.py retire --to user` calls it to remove obsolete alerts.

auto_silence.py turns silence on with the auto marker when the user is idle, and ends only that
silence when they come back; `on` replaces the marker, so a silence set by hand stays until `off`.

natedev holds the state: on the Mac every subcommand runs there over ssh, and pushover.py sends
each Mac push to natedev. NOTIFY_STATE_DIR, when set, names this host's state directory and
keeps everything here.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import importlib
import json
import os
import platform
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import NotRequired, Protocol, TypedDict, cast
from uuid import uuid4

from scripts.lib import operation_deadline
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.user_zone import user_zone, zone_label  # noqa: E402
from scripts.notify import push_reference
from scripts.notify.push_reference import PushIdentity

PY = Path.home() / ".claude/scripts/lib/py"
SILENCE = Path.home() / ".claude/scripts/notify/silence.py"
REMOTE = '"$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/notify/silence.py"'
SSH = ["ssh", "-o", "BatchMode=yes", "natedev"]
UNTIL_OFF = "until off"
UNTIL_BACK = "until you are back"
SPAN_UNITS = {"d": 86400, "day": 86400, "days": 86400, "h": 3600, "hour": 3600, "hours": 3600,
              "m": 60, "min": 60, "minute": 60, "minutes": 60, "s": 1, "sec": 1, "second": 1, "seconds": 1}
SPAN_PART = re.compile(r"\s*(\d+)\s*([a-z]+)")

Run = Callable[[Sequence[str]], int]
Ask = Callable[[Sequence[str], str], tuple[int, str]]
Deliver = Callable[["KeptPush"], int]


class SilenceRecord(TypedDict):
    since: str
    until: str | None
    timer: str | None
    auto: bool


class SilencePolicy(str, Enum):
    KEEP_DURING_SILENCE = "keep_during_silence"
    SEND_THROUGH_SILENCE = "send_through_silence"


class KeptPush(TypedDict):
    post_started: NotRequired[bool | str]
    post_rejection: NotRequired[str]
    silence_policy: NotRequired[SilencePolicy]
    reference: NotRequired[push_reference.PushReference]
    due: NotRequired[str]
    need: NotRequired[str]
    source: str
    priority: str
    title: str
    message: str
    detail: str
    key: str
    text_id: str


@dataclass(frozen=True)
class Kept:
    until: str


@dataclass(frozen=True)
class NotSilent:
    pass


def state_dir() -> Path:
    return Path(os.environ.get("NOTIFY_STATE_DIR", str(Path.home() / ".local/state/notify")))


def state_file() -> Path:
    return state_dir() / "silence.json"


def kept_file() -> Path:
    return state_dir() / "silence-kept.jsonl"


def on_mac(system: str | None = None) -> bool:
    return (system or platform.system()) == "Darwin" and "NOTIFY_STATE_DIR" not in os.environ


@contextmanager
def locked(*, nonblocking: bool = False) -> Generator[None]:
    state_dir().mkdir(parents=True, exist_ok=True)
    with (state_dir() / "silence.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0))
        yield


def read_state() -> SilenceRecord | None:
    try:
        value = cast(object, json.loads(state_file().read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    if not isinstance(value, dict):
        return None
    record = cast(dict[str, object], value)
    since, until, timer = record.get("since"), record.get("until"), record.get("timer")
    if not isinstance(since, str) or not isinstance(until, str | None) or not isinstance(timer, str | None):
        return None
    return {"since": since, "until": until, "timer": timer, "auto": record.get("auto") is True}


def write_state(record: SilenceRecord) -> None:
    """The caller holds the lock."""
    _ = state_file().write_text(json.dumps(record), encoding="utf-8")


def read_kept(*, strict: bool = False) -> list[KeptPush]:
    try:
        if strict:
            operation_deadline.check()
        lines = kept_file().read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    except OSError:
        if strict:
            raise
        return []
    kept: list[KeptPush] = []
    for line in lines:
        if strict:
            operation_deadline.check()
        push = parse_push(line)
        if push is not None:
            kept.append(push)
        elif strict:
            raise ValueError(f"invalid kept push in {kept_file()}")
    return kept


def parse_push(text: str) -> KeptPush | None:
    try:
        value = cast(object, json.loads(text))
    except ValueError:
        return None
    if not isinstance(value, dict):
        return None
    record = cast(dict[str, object], value)
    # A line kept before pushes had a key or a text id reads as key "" and text id "" (free text).
    found = {name: record.get(name) for name in KeptPush.__annotations__
             if name not in {"due", "need", "reference", "silence_policy", "post_started", "post_rejection"}} | {
        "key": record.get("key", ""), "text_id": record.get("text_id", "")}
    for name in ("due", "need", "post_rejection"):
        if name in record:
            found[name] = record[name]
    if not all(isinstance(item, str) for item in found.values()):
        return None
    try:
        reference = push_reference.read_reference(record.get("reference"))
    except ValueError:
        return None
    result = cast(KeptPush, cast(object, found))
    if "post_started" in record:
        started = record["post_started"]
        if not isinstance(started, bool) and (not isinstance(started, str) or not started):
            return None
        result["post_started"] = started
    if "silence_policy" in record:
        policy = record["silence_policy"]
        if not isinstance(policy, str):
            return None
        try:
            result["silence_policy"] = SilencePolicy(policy)
        except ValueError:
            return None
    if "reference" in record:
        result["reference"] = reference
    return result


def shown(moment: datetime) -> str:
    """HH:MM and zone in the user's zone; the date too when it is not today."""
    local = moment.astimezone(user_zone())
    day = "" if local.date() == datetime.now(user_zone()).date() else local.strftime("%Y-%m-%d ")
    return f"{day}{local:%H:%M} {zone_label(local)}"


def ends(record: SilenceRecord) -> str:
    if record["auto"]:
        return UNTIL_BACK
    return f"until {shown(datetime.fromisoformat(record['until']))}" if record["until"] else UNTIL_OFF


def span_seconds(span: str) -> int | None:
    """A span such as 1h, 45min or 1h30min in seconds; None when it does not parse."""
    parts = cast(list[tuple[str, str]], SPAN_PART.findall(span.lower()))
    if not parts or "".join(f"{count}{unit}" for count, unit in parts) != re.sub(r"\s+", "", span.lower()):
        return None
    if any(unit not in SPAN_UNITS for _count, unit in parts):
        return None
    return sum(int(count) * SPAN_UNITS[unit] for count, unit in parts)


def timer_argv(until: datetime, unit: str) -> list[str]:
    """The systemd-run command that ends this timer's silence once, at `until`."""
    return ["systemd-run", "--user", "--collect", f"--unit={unit}",
            f"--on-calendar={until.astimezone(timezone.utc):%Y-%m-%d %H:%M:%S} UTC",
            "--timer-property=AccuracySec=1s", str(PY), str(SILENCE), "off", "--timer", unit]


def stop_timer(unit: str | None, runner: Run) -> int:
    return runner(["systemctl", "--user", "stop", f"{unit}.timer"]) if unit else 0


def past_end(record: SilenceRecord) -> bool:
    until = record["until"]
    return until is not None and datetime.fromisoformat(until) <= datetime.now(user_zone())


def active() -> bool:
    with locked():
        return active_locked()


def active_locked() -> bool:
    record = read_state()
    return record is not None and not past_end(record)


def keep_here(push: KeptPush, runner: Run | None = None, deliver: Deliver | None = None) -> Kept | NotSilent:
    _ = end_if_past(runner, deliver)
    with locked():
        return keep_locked(push)


def keep_locked(push: KeptPush) -> Kept | NotSilent:
    """Keep the push while the caller holds the silence lock."""
    record = read_state()
    if record is None or past_end(record):
        return NotSilent()
    _ = drop_locked(push_identity(push), pending_only=True)
    with kept_file().open("a", encoding="utf-8") as out:
        _ = out.write(json.dumps(push) + "\n")
    return Kept(ends(record))


def push_identity(push: KeptPush) -> PushIdentity:
    return PushIdentity(push["source"], push["key"], push.get("reference", push_reference.NO_REFERENCE))


def drop_here(identity: PushIdentity) -> int:
    """Remove kept copies of this push."""
    with locked():
        return drop_locked(identity)


def drop_locked(identity: PushIdentity, *, pending_only: bool = False) -> int:
    """Drop matching kept pushes while the caller holds the silence lock."""
    if not identity.key and (pending_only or identity.question is None):
        return 0
    kept = read_kept(strict=identity.question is not None)
    left = [push for push in kept if push_identity(push) != identity
            or (pending_only and push.get("post_started", False))]
    if len(left) != len(kept):
        write_kept(left)
    return len(kept) - len(left)


def write_kept(kept: list[KeptPush]) -> None:
    if kept:
        write_text_atomic(kept_file(), "".join(json.dumps(push) + "\n" for push in kept))
    else:
        kept_file().unlink(missing_ok=True)


def mark_post_started(push: KeptPush) -> None:
    """Save the attempt before transport while the caller holds the silence lock."""
    kept = read_kept(strict=True)
    if push.get("post_started", False) or push not in kept:
        raise OSError("silence: kept push is no longer pending")
    attempt = uuid4().hex
    kept[kept.index(push)]["post_started"] = attempt
    write_kept(kept)
    push["post_started"] = attempt


def mark_post_rejected(push: KeptPush, reason: str) -> None:
    """Save the rejection and allow a later release to try again."""
    kept = read_kept(strict=True)
    if not push.get("post_started", False) or push not in kept:
        raise OSError("silence: kept push has no saved attempt")
    rejected: KeptPush = {**push, "post_rejection": reason}
    del rejected["post_started"]
    kept[kept.index(push)] = rejected
    write_kept(kept)
    del push["post_started"]
    push["post_rejection"] = reason


def ask(argv: Sequence[str], text: str) -> tuple[int, str]:
    done = subprocess.run(list(argv), input=text, capture_output=True, text=True, check=False)
    return done.returncode, done.stdout


class RemoteDropFailed(OSError):
    """The notification owner did not confirm the drop."""


def drop(identity: PushIdentity, system: str | None = None, asker: Ask = ask) -> int:
    """Remove kept copies on the notification owner."""
    if not on_mac(system):
        return drop_here(identity)
    arguments = ["drop", "--source", identity.sender, "--key", identity.key]
    if identity.question is not None:
        arguments += ["--reference", json.dumps(identity.question.reference)]
    try:
        code, out = asker([*SSH, f"{REMOTE} {shlex.join(arguments)}"], "")
    except (OSError, subprocess.SubprocessError) as error:
        raise RemoteDropFailed(f"silence: natedev could not drop {identity.key}: {error}") from error
    if code != 0 or not out.strip().isdigit():
        raise RemoteDropFailed(f"silence: natedev did not confirm the drop for {identity.key} (exit {code})")
    return int(out.strip())


def on(until: datetime | None, runner: Run) -> int:
    unit = f"silence-off-{time.time_ns()}" if until else None
    if until and unit and runner(timer_argv(until, unit)) != 0:
        print("silence: the end timer could not be made; silence not changed", file=sys.stderr)
        return 1
    with locked():
        old = read_state()
        record: SilenceRecord = {"since": old["since"] if old else datetime.now(user_zone()).isoformat(),
                                 "until": until.isoformat() if until else None, "timer": unit, "auto": False}
        if stop_timer(old["timer"] if old else None, runner) != 0:
            _ = stop_timer(unit, runner)
            print("silence: the old end timer could not be stopped; silence not changed", file=sys.stderr)
            return 1
        if old is not None and old["auto"]:
            try:
                automatic_event("end", "manual_on", old, next_state="manual")
            except OSError as error:
                _ = stop_timer(unit, runner)
                print(f"silence: end event append failed: {error}", file=sys.stderr)
                return 1
        write_state(record)
    print(f"silence on from {shown(datetime.fromisoformat(record['since']))} {ends(record)}")
    return 0


class PushChannel(Protocol):
    def post_kept(self, push: KeptPush) -> int: ...


def post_kept(push: KeptPush) -> int:
    """pushover.py posts it; imported here, as pushover.py imports this module."""
    return cast(PushChannel, cast(object, importlib.import_module('scripts.notify.pushover'))).post_kept(push)


class AutomaticSilenceEvents(Protocol):
    def record_command_event(self, event: str, cause: str, record: SilenceRecord, next_state: str) -> None: ...


def automatic_event(event: str, cause: str, record: SilenceRecord, *, next_state: str = "off") -> None:
    """Record an automatic transition while the caller holds the silence lock."""
    channel = cast(AutomaticSilenceEvents, cast(object, importlib.import_module('scripts.notify.auto_silence')))
    channel.record_command_event(event, cause, record, next_state)


def claim(when: Callable[[SilenceRecord], bool] | None = None, *,
          release_if_off: bool = False) -> tuple[SilenceRecord | None, list[KeptPush]] | None:
    """End silence and read the kept pushes, leaving them saved until release posts them.

    With `when`, only a silence it accepts ends; otherwise nothing changes and the result is None.
    `release_if_off` also claims leftover pushes when no silence is saved.
    """
    with locked():
        record = read_state()
        if when is not None and ((record is None and not release_if_off)
                                 or (record is not None and not when(record))):
            return None
        kept = read_kept()
        if record is not None and record["auto"]:
            automatic_event("end", "manual_off", record)
        state_file().unlink(missing_ok=True)
    return record, kept


def release(record: SilenceRecord | None, kept: list[KeptPush], runner: Run, deliver: Deliver, opening: str, *,
            only_if_off: bool = False) -> int:
    _ = stop_timer(record["timer"] if record else None, runner)
    failed = delivered = dropped = 0
    with locked():
        if only_if_off and read_state() is not None:
            return 0
        pending = read_kept()
        for claimed in kept:
            if claimed.get("post_started", False) or claimed not in pending:
                continue
            index = pending.index(claimed)
            push = pending[index]
            try:
                code = deliver(push)
            except Exception:
                if record is not None and record["auto"]:
                    automatic_event("release_failed", "delivery_exception", record)
                raise
            if code != 0:
                failed += 1
                continue
            if isinstance(code, push_reference.KeptPushDropped):
                dropped += 1
            else:
                delivered += 1
            _ = pending.pop(index)
            write_kept(pending)
        if not pending:
            kept_file().unlink(missing_ok=True)
        if record is not None and record["auto"]:
            try:
                automatic_event("release_failed" if failed else "released", "held_push_release", record)
            except OSError as error:
                print(f"silence: release event append failed: {error}", file=sys.stderr)
                failed += 1
    plural = "" if delivered == 1 else "es"
    note = f"; {failed} failed, see ~/.local/state/notify/pushover.jsonl" if failed else ""
    dropped_note = f"; {dropped} dropped" if dropped else ""
    print(f"{opening}; {delivered} kept push{plural} delivered{dropped_note}{note}", file=sys.stdout if opening == "silence off" else sys.stderr)
    return 1 if failed else 0


def off(runner: Run, deliver: Deliver | None = None, timer: str = "") -> int:
    try:
        claimed = claim(lambda record: record["timer"] == timer, release_if_off=True) if timer else claim()
    except OSError as error:
        print(f"silence: end event append failed: {error}", file=sys.stderr)
        return 1
    if claimed is None:
        return 0
    record, kept = claimed
    return release(record, kept, runner, deliver or post_kept, "silence off")


def end_if_past(runner: Run | None = None, deliver: Deliver | None = None) -> int:
    """End expired silence or retry its pending Question pushes."""
    try:
        claimed = claim(past_end, release_if_off=True)
    except OSError as error:
        print(f"silence: end event append failed: {error}", file=sys.stderr)
        return 1
    if claimed is None:
        return 0
    record, kept = claimed
    if record is not None:
        return release(record, kept, runner or run, deliver or post_kept, "silence ended at its end time")
    questions = [push for push in kept if not push.get("post_started", False) and "post_rejection" not in push
                 and push.get("reference", push_reference.NO_REFERENCE)["kind"] == "question"]
    if not questions:
        return 0
    return release(None, questions, runner or run, deliver or post_kept, "silence release retried", only_if_off=True)


def status() -> int:
    record = read_state()
    kept = read_kept()
    unconfirmed = sum(bool(push.get("post_started", False)) for push in kept)
    count = len(kept) - unconfirmed
    plural = "" if count == 1 else "es"
    attempts = f"; {unconfirmed} unconfirmed" if unconfirmed else ""
    if record is None:
        print("silence off" + (f"; {count} kept push{plural} not yet delivered: run off" if count else "") + attempts)
    else:
        print(f"silence on from {shown(datetime.fromisoformat(record['since']))} {ends(record)}; {count} push{plural} kept"
              + attempts)
    return 0


def run(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False, stdout=subprocess.DEVNULL).returncode


def passthrough(argv: Sequence[str]) -> int:
    return subprocess.run(list(argv), check=False).returncode


def main(argv: Sequence[str] | None = None, runner: Run = run, system: str | None = None,
         remote: Run = passthrough) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("on", help="keep pushes from now")
    end = start.add_mutually_exclusive_group()
    _ = end.add_argument("--until", help='a time in the user\'s zone: "YYYY-MM-DD HH:MM"')
    _ = end.add_argument("--for", dest="span", help="a span: 1h, 45min, 1h30min")
    stop = commands.add_parser("off", help="end silence and post the kept pushes")
    _ = stop.add_argument("--timer", default="", help="end only the silence that names this timer")
    _ = commands.add_parser("status", help="say whether silence is on")
    dropping = commands.add_parser("drop", help="remove matching kept pushes without a Question reference")
    _ = dropping.add_argument("--source", required=True)
    _ = dropping.add_argument("--key", required=True)
    _ = dropping.add_argument("--reference", help=argparse.SUPPRESS)
    options = parser.parse_args(arguments)
    if on_mac(system):
        return remote([*SSH, f"{REMOTE} {shlex.join(arguments)}"])
    command = cast(str, options.command)
    if command not in ("off", "drop"):
        _ = end_if_past(runner)
    if command == "on":
        until_text, span = cast(str | None, options.until), cast(str | None, options.span)
        until: datetime | None = None
        if until_text:
            try:
                until = datetime.strptime(until_text, "%Y-%m-%d %H:%M").replace(tzinfo=user_zone())
            except ValueError:
                parser.error('--until takes "YYYY-MM-DD HH:MM"')
        elif span:
            seconds = span_seconds(span)
            if not seconds:
                parser.error("--for takes a span such as 1h, 45min or 1h30min")
            until = datetime.now(user_zone()) + timedelta(seconds=seconds)
        if until and until <= datetime.now(user_zone()):
            parser.error(f"{until_text} has already passed")
        return on(until, runner)
    if command == "off":
        return off(runner, timer=cast(str, options.timer))
    if command == "drop":
        try:
            encoded = cast(str | None, options.reference)
            reference = push_reference.read_reference(cast(object, json.loads(encoded)) if encoded else None)
        except ValueError as error:
            parser.error(str(error))
        print(drop_here(PushIdentity(cast(str, options.source), cast(str, options.key), reference)))
        return 0
    return status()


if __name__ == "__main__":
    sys.exit(main())

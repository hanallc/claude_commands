#!/usr/bin/env python3
"""Find and fix the cause of a permission stop or refusal.

  rca.py open --session S --machine M --kind prompt|auto_mode [--command C ...] [--transcript P]
              [--production SLUG]
  rca.py start ID --owner UNIT
  rca.py fixed ID --cause TEXT --fix TEXT --commit SHA
  rca.py list
  rca.py remind

A permission alert to the user opens a case: stall_watch.py opens one after its prompt push, and send.py
after an auto_mode.refused push. A new case sends its owner one message with the steps. An open or started
case for the same session and the same commands counts one more alert instead. The owner is the showrunner
of the session's production (--production, else the production mark the session's processes carry), or the
natedev session when there is none or it is not running. The case's transcript belongs to the affected session:
--transcript, else the newest transcript of the live session on this machine that --session names, else none.

A case keeps the permission event time, the commands, and the alert's waits sentence, sender and key.
`start` records the owner. `fixed` records the cause, fix and commit.
`fixed` uses `send.py retire` to retire the alert by its sender and key.
Retirement removes a kept alert and stops an emergency alert from repeating.
`remind` sends the owner one reminder for a case that stays open for 30 minutes.
`stall_watch.py` runs `remind` on each pass.

Owner sends wait for send.py and record its outcome in STATE/owner.log.
The watcher sends new owner notices in its concurrent reminder batch.
The batch uses the time that remains before the watchdog deadline.
Cases keep undelivered notices for the next pass.

Each case is one JSON file in STATE, which is $XDG_STATE_HOME/message/rca or ~/.local/state/message/rca.
`open` never fails its caller: the alert already went out, so an error prints to stderr and it exits 0.
"""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from time import monotonic, sleep
from collections.abc import Generator
from contextlib import contextmanager
from enum import Enum
from datetime import datetime, timedelta, timezone
from pathlib import Path

from typing import Literal, NamedTuple, NotRequired, TypedDict, cast

from scripts.lib import conf

from scripts.notify import push_texts  # noqa: E402
from scripts.message import session_send, sessions  # noqa: E402
from scripts.production import showrunners  # noqa: E402
from scripts.lib.user_zone import user_zone  # noqa: E402
from scripts.production import staff  # noqa: E402

STATE = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "message" / "rca"
SEND = Path(os.environ.get("RCA_SEND") or session_send.SEND_SCRIPT)
CALL = "~/.claude/scripts/lib/py ~/.claude/scripts/message/rca.py"
PROJECTS = Path.home() / ".claude" / "projects"
# The owner when the session has no production, or its showrunner does not run.
NATEDEV = "natedev"
SENDER = "rca"
REMIND_AFTER = timedelta(minutes=30)
# The completed outcome of each owner message and reminder, after a line with its time and key.
OWNER_LOG = "owner.log"
# The first word of a command that changes a file: auto_mode.refused's "write …" and "edit …", and the
# tool names stall_watch.py records for a pending edit or write call.
FILE_WORDS = frozenset({"write", "edit", "multiedit", "notebookedit"})
# Any other Claude Code tool name stall_watch.py records: "WebFetch", "mcp__brp__brp_launch".
TOOL_NAME = re.compile(r"[A-Z][A-Za-z]+|mcp__\S+")

Kind = Literal["prompt", "auto_mode"]
Status = Literal["open", "started", "fixed"]


def send_seconds() -> float:
    """Owner delivery budget in seconds."""
    return conf.read("rca").number("send_seconds")


class Case(TypedDict):
    id: str
    session: str
    machine: str
    production: str
    commands: list[str]
    kind: Kind
    transcript: str
    opened: str
    status: Status
    owner: str
    cause: str
    fix: str
    commit: str
    reminded: bool
    owner_told: NotRequired[bool]
    alerts: int
    # Permission stop or refusal time (UTC ISO time). An older case uses its opened time.
    stopped: str
    # The alert's sentence of what waits on the session, or "".
    waits: str
    # The alert's sender and key, which `send.py retire` retires it by; "" when the alert had no key.
    alert_source: str
    alert_key: str


class Report(NamedTuple):
    """One permission alert that went to the user: the facts a case opens with.

    stopped is a UTC ISO time, "" for the time the case opens; source and key are the alert's sender and key."""
    session: str
    machine: str
    kind: Kind
    commands: tuple[str, ...]
    transcript: str = ""
    production: str = ""
    stopped: str = ""
    waits: str = ""
    source: str = ""
    key: str = ""


def now() -> datetime:
    return datetime.now(timezone.utc)


@contextmanager
def locked(*, deadline: float = math.inf) -> Generator[None]:
    STATE.mkdir(parents=True, exist_ok=True)
    with open(STATE / ".lock", "w", encoding="utf-8") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise OSError("case lock deadline exhausted; retained for the next pass") from None
                sleep(min(0.01, remaining))
        yield


def case_path(case_id: str) -> Path:
    return STATE / f"{case_id}.json"


def read_case(path: Path) -> Case | None:
    """The case in the file; a case saved before a field existed reads that field's default."""
    try:
        found = cast(Case, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return None
    defaults = {"stopped": found["opened"], "waits": "", "alert_source": "", "alert_key": ""}
    return cast(Case, cast(object, defaults | dict(found)))


def cases() -> list[Case]:
    found = [case for path in sorted(STATE.glob("*.json")) if (case := read_case(path)) is not None]
    return sorted(found, key=lambda case: case["opened"])


def save(case: Case) -> None:
    path = case_path(case["id"])
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    _ = temporary.write_text(json.dumps(case, indent=1) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def one_line(text: str) -> str:
    return " ".join(text.split())


def new_id(at: datetime) -> str:
    """The date in the user's zone and a counter: 261023-1 is the first case of 2026-10-23."""
    day = at.astimezone(user_zone()).strftime("%y%m%d")
    return f"{day}-{len(list(STATE.glob(f'{day}-*.json'))) + 1}"


def transcript_of(report: Report) -> str:
    """The given transcript, else the newest transcript of the live session the report names, else "".

    Never the caller's own: a showrunner or a session that sends auto_mode.refused reports another session."""
    if report.transcript:
        return report.transcript
    session = sessions.named(report.session, sessions.live_sessions())
    found = sessions.latest_transcript(session["sessionId"], PROJECTS) if session is not None else None
    return str(found or "")


def production_of(report: Report) -> str:
    if report.production:
        return report.production
    record = staff.snapshot()
    known = sessions.named(report.session, sessions.live_sessions(), membership=record)
    identity = known["sessionId"] if known is not None else ""
    row = record.by_current_session(identity)
    while row is not None and row.kind != "showrunner":
        row = record.parent_of(row)
    if row is None:
        return ""

    return row.name


def owner_address(production: str, *, deadline: float = math.inf) -> str:
    """Where the case's owner gets messages: the production's showrunner, else the natedev session."""
    if not production or monotonic() >= deadline:
        return NATEDEV
    try:
        record = staff.snapshot()
        for row in staff.active(record, "showrunner"):

            name = row.name
            if name == production:
                socket = showrunners.socket_for(f"session:{row.current_session}", deadline=deadline)
                return f"uds:{socket}" if socket else NATEDEV
        return NATEDEV
    except TimeoutError:
        raise
    except (OSError, ValueError):
        return NATEDEV


def kind_of(command: str) -> str:
    words = command.split()
    first = words[0] if words else ""
    if first.lower() in FILE_WORDS:
        return "file change"
    return "action" if len(words) == 1 and TOOL_NAME.fullmatch(first) else "shell command"


def action_of(commands: list[str] | tuple[str, ...]) -> str:
    """The kind of action the commands are, in plain words: "a file change", "2 shell commands", or
    "2 actions" when they are of mixed kinds; "an action" when there are none."""
    kinds = [kind_of(command) for command in commands if command.strip()]
    if not kinds:
        return "an action"
    if len(kinds) == 1:
        return ("an " if kinds[0][0] in "aeiou" else "a ") + kinds[0]
    return f"{len(kinds)} {kinds[0] if len(set(kinds)) == 1 else 'action'}s"


def waits_line(waits: str) -> str:
    """The sentence about work that waits after a permission stop or refusal; "" for none."""
    return f" {waits}." if waits else ""


def owner_message(case: Case, *, reminder: bool = False) -> str:
    stop = "stopped at a permission prompt" if case["kind"] == "prompt" else "had a command refused by auto mode"
    first = (f"rca {case['id']} is still open after 30 minutes. " if reminder else f"rca {case['id']}: ")
    transcript = ["Transcript: " + case["transcript"]] if case["transcript"] else []
    lines = [first + f"{case['session']} on {case['machine']} {stop}.",
             "Commands: " + ("; ".join(case["commands"]) or "none recorded"),
             *transcript,
             f"Find why this happened and fix the source. Run the rca.py commands on {case['machine']}:",
             f"1. Run `{CALL} start {case['id']} --owner <unit you route the fix to>`.",
             "2. Find where the command came from: a work order, a helper prompt, a skill or a script.",
             "3. Route the fix to the unit that owns that source, so the command never needs a prompt "
             + "(an allow rule only when the command is safe and expected).",
             f'4. After the fix merges, run `{CALL} fixed {case["id"]} --cause "<cause>" --fix "<fix>" --commit <sha>`.']
    return "\n".join(lines)


class ConfiguredSendBudget(Enum):
    CURRENT = "current"


def send(argv: list[str], *, seconds: float | ConfiguredSendBudget = ConfiguredSendBudget.CURRENT) -> tuple[bool, str]:
    """Run send.py; whether it exited 0, and what it printed."""
    seconds = send_seconds() if isinstance(seconds, ConfiguredSendBudget) else seconds
    if seconds <= 0:
        return False, "owner delivery budget exhausted; retained for the next pass"
    try:
        result = subprocess.run(session_send.send_argv(*argv, send_script=SEND), capture_output=True, text=True,
                                timeout=seconds, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, str(error)
    return result.returncode == 0, (result.stdout + result.stderr).strip()


def tell_owner(case: Case, *, reminder: bool = False, deadline: float = math.inf) -> str:
    """Wait for the owner delivery before a one-shot caller exits; return its failure, or empty on success."""
    key = f"rca-{case['id']}" + ("-remind" if reminder else "")
    try:
        argv = ["--to", owner_address(case["production"], deadline=deadline), "--from", SENDER, "--key", key,
                "--text", owner_message(case, reminder=reminder)]
        STATE.mkdir(parents=True, exist_ok=True)
        with open(STATE / OWNER_LOG, "a", encoding="utf-8") as log:
            _ = log.write(f"{now().isoformat()} {key}\n")
            log.flush()
            sent, outcome = send(argv, seconds=min(send_seconds(), max(0, deadline - monotonic())))
            _ = log.write(outcome + "\n")
        if sent:
            with locked(deadline=deadline):
                current = read_case(case_path(case["id"]))
                if current is not None:
                    if reminder:
                        current["reminded"] = True
                    else:
                        current["owner_told"] = True
                    save(current)
    except OSError as error:
        return str(error)
    return "" if sent else outcome or "send.py failed"


def retire_alert(case: Case) -> tuple[bool, str]:
    """Return the status and output of the retirement attempt."""
    if not case["alert_key"]:
        return True, ""
    command = push_texts.user_retire_argv(case["alert_source"], case["alert_key"])
    return send(command[2:])


def open_case(report: Report, at: datetime | None = None, *, defer_owner: bool = False,
              deadline: float = math.inf) -> str:
    """Open a case for the alert, or join the open one for the same session and commands.

    It never raises: the alert already went out, so an error is only printed."""
    try:
        commands = list(dict.fromkeys(line for command in report.commands if (line := one_line(command))))
        with locked(deadline=deadline):
            for case in cases():
                if (case["status"] != "fixed" and case["session"] == report.session
                        and set(case["commands"]) == set(commands)):
                    case["alerts"] += 1
                    save(case)
                    return f"rca: joined {case['id']}"
            opened = at or now()
            case = Case(id=new_id(opened), session=report.session, machine=report.machine,
                        production=production_of(report), commands=commands, kind=report.kind,
                        transcript=transcript_of(report), opened=opened.isoformat(), status="open", owner="",
                        cause="", fix="", commit="", reminded=False, owner_told=False, alerts=1,
                        stopped=report.stopped or opened.isoformat(), waits=one_line(report.waits),
                        alert_source=report.source, alert_key=report.key)
            save(case)
        failure = "" if defer_owner else tell_owner(case, deadline=deadline)
        if failure:
            print(f"rca: {case['id']}: the owner message did not start, a reminder follows: {failure}", file=sys.stderr)
        return f"rca: opened {case['id']}"
    except Exception as error:  # noqa: BLE001  the caller's alert went out; a case is extra
        message = f"rca: no case opened: {type(error).__name__}: {error}"
        print(message, file=sys.stderr)
        return message


def loaded(case_id: str) -> Case:
    case = read_case(case_path(case_id))
    if case is None:
        raise ValueError(f"no case {case_id} on this machine; `rca.py list` shows the open ones")
    return case


def start(case_id: str, owner: str) -> None:
    with locked():
        case = loaded(case_id)
        if case["status"] == "fixed":
            raise ValueError(f"case {case_id} is fixed")
        case["status"], case["owner"] = "started", owner
        save(case)


def fixed(case_id: str, cause: str, fix: str, commit: str) -> tuple[bool, str]:
    """Close the case and return the result of retiring its alert."""
    with locked():
        case = loaded(case_id)
        case["status"] = "fixed"
        case["commit"] = commit.strip()
        case["cause"] = one_line(cause).rstrip(".")
        case["fix"] = one_line(fix).rstrip(".")
        save(case)
    return retire_alert(case)


def remind(at: datetime | None = None, *, deadline: float = math.inf) -> None:
    """Retry first notices and due reminders concurrently within one caller-owned deadline."""
    try:
        due = [(case, case.get("owner_told", True)) for case in cases()
               if case["status"] != "fixed" and (not case.get("owner_told", True)
                   or case["status"] == "open" and not case["reminded"]
                   and (at or now()) - datetime.fromisoformat(case["opened"]) >= REMIND_AFTER)]
        if not due:
            return
        with ThreadPoolExecutor(max_workers=len(due)) as pool:
            sends = [pool.submit(tell_owner, case, reminder=reminder, deadline=deadline) for case, reminder in due]
            for (case, reminder), future in zip(due, sends):
                failure = future.result()
                if failure:
                    print(f"rca: {case['id']}: owner delivery retained for retry: {failure}", file=sys.stderr)
                else:
                    print(f"rca: {'reminded' if reminder else 'told owner of'} {case['id']}")
    except Exception as error:  # noqa: BLE001  a pass of the caller must not end here
        print(f"rca: no reminders sent: {type(error).__name__}: {error}", file=sys.stderr)


def listing() -> str:
    lines: list[str] = []
    for case in cases():
        if case["status"] == "fixed":
            continue
        opened = datetime.fromisoformat(case["opened"]).astimezone(user_zone()).strftime("%m-%d %H:%M %Z")
        owner = f" owner {case['owner']}" if case["owner"] else ""
        lines.append(f"{case['id']}  {case['status']}{owner}  {case['session']} on {case['machine']}  {opened}"
                     + f"  alerts {case['alerts']}  {'; '.join(case['commands']) or 'none recorded'}")
    return "\n".join(lines) or "no open cases"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rca.py", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="action", required=True)
    opening = commands.add_parser("open", help="open a case for a permission alert, or join its open case")
    _ = opening.add_argument("--session", required=True)
    _ = opening.add_argument("--machine", required=True)
    _ = opening.add_argument("--kind", required=True, choices=("prompt", "auto_mode"))
    _ = opening.add_argument("--command", action="append", default=[], help="a command that needed permission")
    _ = opening.add_argument("--transcript", default="", help="default: the transcript of the session --session names")
    _ = opening.add_argument("--production", default="", help="default: the session's production mark")
    starting = commands.add_parser("start", help="record the unit that fixes the case")
    _ = starting.add_argument("id")
    _ = starting.add_argument("--owner", required=True)
    fixing = commands.add_parser("fixed", help="close the case and retire its alert")
    _ = fixing.add_argument("id")
    _ = fixing.add_argument("--cause", required=True)
    _ = fixing.add_argument("--fix", required=True)
    _ = fixing.add_argument("--commit", required=True)
    _ = commands.add_parser("list", help="the open and started cases")
    _ = commands.add_parser("remind", help="remind the owner of each case still open after 30 minutes")
    args = parser.parse_args(argv)
    action = cast(str, args.action)
    if action == "open":
        print(open_case(Report(cast(str, args.session), cast(str, args.machine), cast(Kind, args.kind),
                               tuple(cast(list[str], args.command)), cast(str, args.transcript),
                               cast(str, args.production))))
        return 0
    if action == "list":
        print(listing())
        return 0
    if action == "remind":
        remind()
        return 0
    try:
        if action == "start":
            start(cast(str, args.id), cast(str, args.owner))
            return 0
        retired, detail = fixed(cast(str, args.id), cast(str, args.cause), cast(str, args.fix), cast(str, args.commit))
    except ValueError as error:
        print(f"rca: {error}", file=sys.stderr)
        return 2
    if detail:
        print(detail, file=sys.stdout if retired else sys.stderr)
    if not retired:
        print("rca: The alert can still repeat. Run fixed again to retry retirement.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

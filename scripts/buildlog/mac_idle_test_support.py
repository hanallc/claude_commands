"""Private idle fixtures and notification transport guards for integration tests."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from collections.abc import Callable, Generator, Sequence
from typing import Literal, ParamSpec, TypeVar, cast, override
from unittest import mock
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts.lint.memory_test_support import write_config_fixture


def write_idle_config(directory: Path, overrides: str = "") -> Path:
    """Write a complete private configuration with the shared value limits."""
    path = directory / "mac_test.conf"
    _ = write_config_fixture(path, overrides)
    return path


def write_idle_alert(directory: Path, now: float, *, written_age: float = 0) -> Path:
    """One saved blocked-Mac fixture for both queue and target view tests."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "alert.json"
    _ = path.write_text(json.dumps({
        "version": 1, "written": now - written_age, "state": "idle", "since": now - 600, "changed": now - 600,
        "waiting": 7, "cause": "blocked", "holder": "research", "detail": "P15 F11 L0 run",
        "until": 0, "messaged": True, "pushed": False,
    }))
    return path


LiveClaim = Literal["none", "idle", "working"]


def write_live_mac_state(directory: Path, *, block: bool = False, claim: LiveClaim = "none",
                         unreachable_until: float = 0) -> Path:
    """Write the local Mac state that the alert line checks before it shows a saved condition.

    Set MAC_TEST_STATE_DIR to the returned folder. A call removes the state that it does not write.
    """
    from scripts.lib import process_start
    from scripts.mac_test import mac_test

    claims = directory / "claims"
    claims.mkdir(parents=True, exist_ok=True)
    for path in (directory / "block.json", claims / "live.json", directory / "unreachable_until"):
        path.unlink(missing_ok=True)
    if block:
        since = datetime(2026, 1, 1, tzinfo=timezone.utc)
        mac_test.write_block(directory / "block.json", mac_test.ActiveMacBlock(
            "research", "P15 F11 L0 run", since, since + timedelta(hours=4),
            mac_test.RecordedSession("research", "research"), mac_test.NO_SHOWRUNNER,
            "off_before", mac_test.FREE_MESSAGE_NOT_NEEDED))
    if claim != "none":
        start = process_start.process_start(os.getpid())
        if not isinstance(start, process_start.Started):
            raise AssertionError("the test process has no start identity")
        row: dict[str, object] = {"claim_id": "claim-live", "episode": "episode-live", "attempt": "attempt-live",
            "submitting_session": "research", "holder_session": "research", "pid": os.getpid(),
            "proc_start": start.start, "what": "nextest hana", "worktree": str(directory / "worktree"),
            "since": "2026-01-01T00:00:00+00:00"}
        if claim == "idle":
            row["work_idle_since"] = "2026-01-01T00:00:00+00:00"
        _ = (claims / "live.json").write_text(json.dumps(row))
    if unreachable_until:
        _ = (directory / "unreachable_until").write_text(str(unreachable_until))
    return directory


class NotificationTransportBlocked(AssertionError):
    """A test attempted to reach a real notification or address resolver."""


P = ParamSpec("P")
R = TypeVar("R")


def reject_notification_command(command: object) -> None:
    words = ([command] if isinstance(command, str) else
             cast(Sequence[object], command) if isinstance(command, (tuple, list)) else ())
    for word in words:
        text = str(word)
        if "message/send.py" in text or "production/showrunners.py" in text:
            raise NotificationTransportBlocked(f"Real test notification transport forbidden: {text}")


_guarded_tests: set[unittest.TestCase] = set()


def notification_spawn_guard(event: str, args: tuple[object, ...]) -> None:
    """The spawn boundary remains protected when tests restore subprocess functions."""
    if _guarded_tests and event == "subprocess.Popen":
        reject_notification_command(args[1])


sys.addaudithook(notification_spawn_guard)


def guarded_transport(transport: Callable[P, R]) -> Callable[P, R]:
    def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
        command = args[0] if args else kwargs.get("args")
        reject_notification_command(command)
        return transport(*args, **kwargs)
    return guarded


class NotificationGuardedTestCase(unittest.TestCase):
    @override
    def setUp(self) -> None:
        super().setUp()
        _guarded_tests.add(self)
        self.addCleanup(_guarded_tests.remove, self)
        _ = self.enterContext(mock.patch.object(subprocess, "run", side_effect=guarded_transport(subprocess.run)))
        _ = self.enterContext(mock.patch.object(subprocess, "Popen", side_effect=guarded_transport(subprocess.Popen)))


from scripts.message import send, sessions
from scripts.lib import process_start
from scripts.production import session_staff, staff, unit_lookup


def no_checkpoint() -> None:
    pass


@dataclass
class PrivateSenderAcceptance:
    """Real sender state with only discovery and transport replaced by private fixtures."""
    state: Path
    accepted: Path
    owner_start: str = "fixture-owner"

    def end_owner(self) -> None:
        """Replace the sender lifetime after a simulated process stop."""
        self.owner_start += "-replacement"

    def count(self) -> int:
        return len(self.accepted.read_text().splitlines()) if self.accepted.exists() else 0

    def options(self, channel: str, *, address: str = "uds:/fixture/builds.sock") -> send.Options:
        arguments = ["--to", "user" if channel == "push" else address, "--from", "fixture",
                     "--summary", "Waiting work", "--text", "Work waits for the Mac.",
                     "--key", "outage", "--repeat-minutes", "1440",
                     "--recipient-key", "user" if channel == "push" else address]
        if channel == "push":
            arguments += ["--text-id", "nate.adhoc", "--value", "sender=fixture",
                          "--value", "title=Waiting work", "--value", "description=Work waits for the Mac.", "--no-action"]
        return send.parse(arguments)._replace(shown_sender=send.ShownSender("fixture"))


@contextmanager
def private_sender_acceptance(state: Path, *, before_accept: Callable[[], None] = no_checkpoint,
                              after_accept: Callable[[], None] = no_checkpoint) -> Generator[PrivateSenderAcceptance]:
    """No real phone, socket, subprocess or recipient registry is reachable through these fakes."""
    state.mkdir(parents=True, exist_ok=True)
    fixture = PrivateSenderAcceptance(state, state / "acceptances.jsonl")
    owner_pid = os.getpid()
    real_process_start = process_start.process_start
    registry = sessions.RegistryRead(tuple(
        sessions.RegistryObservation(sessions.SessionRecord(
            pid=pid, sessionId=f"fixture-{name}", name=name, procStart=f"fixture-start-{pid}",
            messagingSocketPath=f"/fixture/{name}.sock", updatedAt=1, tmux="", formerNames=[]), {})
        for pid, name in enumerate(("builds", "restarted"), start=101)), ())

    def running_records(reading: sessions.RegistryRead) -> list[sessions.SessionRecord]:
        return [entry.record for entry in reading.entries]

    def lookup_conversation(conversation: str, *, registry: sessions.RegistryRead,
                            probe_address: bool) -> unit_lookup.ConversationLookup:
        if probe_address:
            raise AssertionError("the fixture does not probe sockets")
        row = sessions.addressed(f"session:{conversation}", running_records(registry))
        if row is None:
            return unit_lookup.ConversationLookup(unit_lookup.ProvedAbsentUnit())
        owner = unit_lookup.ProvedLiveUnit(
            unit_lookup.LiveClaude(row["name"], conversation, row["messagingSocketPath"], row["pid"]),
            session_staff.ProvedProcessGeneration(row["pid"], row.get("procStart", "")),
        )
        return unit_lookup.ConversationLookup(owner)

    def sender_process_start(pid: int) -> process_start.ProcessStart:
        if pid == owner_pid:
            return process_start.Started(fixture.owner_start)
        return real_process_start(pid)

    def transport(command: list[str], _stdin: str, _timeout: float) -> tuple[int, str, str]:
        before_accept()
        with fixture.accepted.open("a") as accepted:
            _ = accepted.write(json.dumps(command) + "\n")
        after_accept()
        return 0, "accepted by private transport", ""

    def discover(address: str, *arguments: object, **keywords: object) -> send.DeliveryRoute:
        del arguments, keywords
        return send.ClaudeRecipient(address)

    def relay(message: send.Message, timeout: float) -> send.Result:
        code, output, errors = send.run([message.to], "", timeout)
        return send.Result("sent" if code == 0 else "failed", output or errors)

    with mock.patch.object(staff, "snapshot", return_value=staff.StaffSnapshot(())), \
         mock.patch.object(process_start, "process_start", side_effect=sender_process_start), \
         mock.patch.object(sessions, "read_registry", return_value=registry), \
         mock.patch.object(sessions, "running_records", side_effect=running_records), \
         mock.patch.object(sessions, "running_sessions", return_value=[]), \
         mock.patch.object(unit_lookup, "lookup_conversation", side_effect=lookup_conversation), \
         mock.patch.object(send, "STATE", state), mock.patch.object(send, "wrong_template", return_value=None), mock.patch.object(send, "run", side_effect=transport), \
         mock.patch.object(send, "resolve_recipient", side_effect=discover), \
         mock.patch.object(send, "relay", side_effect=relay):
        yield fixture

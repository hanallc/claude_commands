"""One watch for every build machine: one notice when a machine runs no work for too long.

A machine that is not off on purpose sends one notice when, for longer than its notice time, it is unusable or
steps wait for it and it runs none. Nate gets a phone push only when a natedev, Mac or mac-linux machine does not
answer: only he can repair that. Every other notice is a session message to bob and the builds showrunner.
`buildlog sample` runs `watch_once` each minute on natedev.
"""

from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict, cast

from scripts.buildlog import mac_idle, mac_idle_alert, store
from scripts.lib import conf

if TYPE_CHECKING:
    from scripts.build_targets.execution_transport import ExecutionTransport
    from scripts.build_targets.target_config import Target
    from scripts.mac_test.claim_state import ActiveMacBlock, PendingMacBlock

SCRIPTS = Path(__file__).resolve().parents[1]
SENDER = "Machine watch"
UNREACHABLE_TEXT = "machine_watch.unreachable"
SESSION_RECEIVERS = ("bob", "builds")
"""Bob and the builds showrunner repair a machine that takes no work. The receivers are code, not configuration."""


@dataclass(frozen=True)
class WatchPaths:
    state: Path
    mac: mac_idle.MacIdlePaths


def state_dir() -> Path:
    return Path(os.environ.get("MACHINE_WATCH_STATE_DIR", str(store.root().parent / "machine-watch"))).expanduser()


def watch_paths() -> WatchPaths:
    return WatchPaths(state_dir(), mac_idle.watch_paths())


@dataclass(frozen=True)
class Unusable:
    """Builds cannot use the machine: it does not answer, or a fixed exclusion refuses every step."""

    reason: str


@dataclass(frozen=True)
class StepsWait:
    """Steps wait for the machine, and it runs none."""

    count: int


@dataclass(frozen=True)
class UnreachableMacAlert:
    """The Mac idle watch saved an unreachable reading while steps wait. Its push keeps its own text and key."""

    alert: mac_idle_alert.SavedAlert


@dataclass(frozen=True)
class FirstSeen:
    """The reading has no start time of its own, so the outage starts when the watch first sees it."""


FIRST_SEEN = FirstSeen()


@dataclass(frozen=True)
class Outage:
    cause: Unusable | StepsWait | UnreachableMacAlert
    since: float | FirstSeen


@dataclass(frozen=True)
class NoOutage:
    """The machine runs work, or nothing waits for it."""


NO_OUTAGE = NoOutage()


@dataclass(frozen=True)
class NoChange:
    """The reading cannot end or start an outage, so the saved state stays."""


NO_CHANGE = NoChange()


Reading = Outage | NoOutage | NoChange


class SavedNotice(TypedDict):
    since: float
    sent: bool


class SavedBlockNotice(TypedDict):
    since: float
    sent: list[str]


def read_json(path: Path) -> dict[str, object] | None:
    """An unreadable file reads as missing. One more notice can then go for the same outage."""
    try:
        value = cast(object, json.loads(path.read_text()))
    except (OSError, ValueError):
        return None
    return cast(dict[str, object], value) if isinstance(value, dict) else None


def read_notice(path: Path) -> SavedNotice | None:
    row = read_json(path)
    if row is None or not mac_idle_alert.nonnegative_time(row.get("since")) or type(row.get("sent")) is not bool:
        return None
    return cast(SavedNotice, cast(object, row))


def read_block_notice(path: Path) -> SavedBlockNotice | None:
    row = read_json(path)
    sent = row.get("sent") if row is not None else None
    if (row is None or not mac_idle_alert.nonnegative_time(row.get("since")) or not isinstance(sent, list)
            or not all(isinstance(name, str) for name in cast(list[object], sent))):
        return None
    return cast(SavedBlockNotice, cast(object, row))


def notice_seconds(machine: str, settings: conf.ConfigRead) -> float:
    """Read the machine key, else the key for all machines.

    The conf host level selects a key by the host that reads the file, not by the watched machine, so it does not
    fit here.
    """
    every_machine = settings.value("excluded_notice_seconds", conf.positive_whole_seconds)
    return settings.value(f"excluded_notice_seconds.{machine}", conf.positive_whole_seconds, default=every_machine)


def machines() -> tuple[Target, ...]:
    """Every registered target, the pinned targets included."""
    from scripts.build_targets import target_config

    configuration = conf.read("build_targets")
    found = list(target_config.read_targets(purpose=target_config.RegistryRead.PLACEMENT, configuration=configuration))
    for name in target_config.pinned_target_names(configuration):
        if all(target.name != name for target in found):
            found.extend(target_config.read_targets(selected=name, purpose=target_config.RegistryRead.PLACEMENT,
                                                    configuration=configuration))
    return tuple(found)


def transport_for(target: Target) -> ExecutionTransport | None:
    from scripts.aws_target import hana_placement
    from scripts.build_targets.target_config import SshTarget

    return hana_placement.read_execution_transport(target) if isinstance(target, SshTarget) else None


def nate_repairs(transport: ExecutionTransport | None) -> bool:
    """Nate repairs a local or plain SSH machine that does not answer. Bob repairs a Hana machine."""
    from scripts.build_targets.execution_transport import HanaControlTransport, HanaGpuNextestTransport

    return not isinstance(transport, HanaControlTransport | HanaGpuNextestTransport)


def refused_credential(since: float) -> Unusable | None:
    """Read the newest cloud gate launch row in the window. A credential refusal makes the GPU route unusable."""
    from scripts.aws_target import aws_page, ledger
    from scripts.build_targets.target_config import ssh_target_state_directory

    for row in ledger.recent_gate_launches(ssh_target_state_directory("aws"), since):
        evidence = aws_page.classify_launch_row(row, frozenset())
        if evidence is aws_page.LaunchRowEvidence.NO_ATTEMPT:
            continue
        if isinstance(evidence, aws_page.OtherLaunchRefusal) and evidence in aws_page.LAUNCH_REFUSAL_REASONS:
            return Unusable(aws_page.LAUNCH_REFUSAL_REASONS[evidence])
        return None
    return None


def unusable(target: Target, transport: ExecutionTransport | None, now: float, notice: float) -> Unusable | None:
    """Only a fixed cause counts. A busy, starting or unprovisioned Hana box is a temporary state."""
    from scripts.aws_target import hana_box, hana_placement
    from scripts.build_targets import targets
    from scripts.build_targets.execution_transport import HanaControlTransport, HanaGpuNextestTransport
    from scripts.build_targets.target_config import SshTarget

    if not isinstance(target, SshTarget):
        return None
    if isinstance(transport, HanaControlTransport):
        controller = hana_placement.controller_for(transport, read_only=True)
        snapshot = controller.inspect()
        if isinstance(snapshot, hana_box.HanaLifecycleEvidence) and not controller.image_is_current(snapshot.deployment):
            return Unusable(hana_box.HANA_IMAGE_STALE)
        return None
    if isinstance(transport, HanaGpuNextestTransport):
        setup = hana_placement.gpu_setup(transport)
        if isinstance(setup, hana_placement.HanaStepRefused):
            return Unusable(setup.reason)
        refused = refused_credential(now - notice)
        if refused is not None:
            return refused
    state = targets.ssh_target_state(target)
    return Unusable(f"Builds could not connect to it ({state.reason})") if isinstance(state, targets.Unreachable) else None


def waiting_steps(target: Target) -> int:
    """Count the steps that wait for the target. A target that runs a step has no waiting work."""
    from scripts.direct import step_record
    from scripts.direct.step_selection import Pinned
    from scripts.build_targets.target_config import LocalTarget

    states = step_record.StepState
    local = (states.ON_NATEDEV, states.LOCAL_NONCOMPILING)
    waiting = (states.WAITING_HOLD, states.WAITING_MAC, states.WAITING_SSH)
    records = step_record.live_records()
    for record in records:
        if (record.state in local and isinstance(target, LocalTarget)
                or record.state in (states.ON_SSH, states.ON_MAC) and record.selected_target == target.name):
            return 0
    return sum(record.state in waiting
               and (target.name in record.eligible_targets or record.selection == Pinned(target.name))
               for record in records)


def fresh_alert(paths: WatchPaths, now: float) -> mac_idle_alert.SavedAlert | None:
    saved = mac_idle_alert.read_alert(paths.mac.state / "alert.json")
    if not isinstance(saved, dict) or now - saved["written"] > mac_idle_alert.read_idle_config().stale_seconds:
        return None
    return saved


def mac_reading(paths: WatchPaths, now: float) -> Reading:
    """Read the Mac idle watch result of this pass. It knows Mac claims, CI jobs and disk exclusions."""
    saved = fresh_alert(paths, now)
    if saved is None or saved["state"] == "unreadable":
        return NO_CHANGE
    if saved["state"] == "unreachable":
        # The Mac reads unreachable only after a failed probe. While no step waits, its clear check ends the push.
        return Outage(UnreachableMacAlert(saved), saved["since"]) if saved["waiting"] else NO_CHANGE
    if saved["state"] == "idle" and saved["cause"] in ("free", "claim_idle") and saved["waiting"]:
        return Outage(StepsWait(saved["waiting"]), FIRST_SEEN)
    return NO_OUTAGE


def read_machine(target: Target, transport: ExecutionTransport | None, paths: WatchPaths, now: float,
                 notice: float) -> Reading:
    from scripts.build_targets.target_config import SshTarget, uses_legacy_mac_layout

    if isinstance(target, SshTarget) and uses_legacy_mac_layout(target.platform):
        return mac_reading(paths, now)
    broken = unusable(target, transport, now, notice)
    if broken is not None:
        return Outage(broken, FIRST_SEEN)
    count = waiting_steps(target)
    return Outage(StepsWait(count), FIRST_SEEN) if count else NO_OUTAGE


def cause_text(cause: Unusable | StepsWait) -> str:
    if isinstance(cause, Unusable):
        text = mac_idle_alert.display_field(cause.reason)
        return text if text.endswith(".") else text + "."
    return f"{mac_idle_alert.waiting_steps_text(cause.count)} for it, and nobody turned it off."


def send_session(to: str, text: str, key: str) -> bool:
    """Send one session message. A key that the sender already has gives no second delivery."""
    from scripts.message import send_budget, send_result
    from scripts.message.session_send import session_send_argv

    command = session_send_argv(to, sender=SENDER)
    command.extend(["--key", key])
    try:
        result = subprocess.run(command, input=text, text=True, capture_output=True,
                                timeout=send_budget.seconds(), check=False)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Machine watch: message to {to} failed: {error}", file=sys.stderr)
        return False
    outcome = send_result.send_outcome(result.returncode, result.stdout)
    if isinstance(outcome, send_result.ConfirmedSendOutcome) and outcome.header.outcome in (
            "sent", "skipped", "queued", "kept"):
        return True
    print(f"Machine watch: message to {to} not confirmed: {result.stdout.strip() or result.returncode}",
          file=sys.stderr)
    return False


def push_to_nate(machine: str, since: float, now: float) -> bool:
    """A machine that nate repairs is a plain SSH or local machine, so its only unusable cause is no connection."""
    from scripts.notify import push_texts
    from scripts.lib.process_failure import failure_detail

    rendered = push_texts.render(UNREACHABLE_TEXT, machine=machine, minutes=str(int((now - since) // 60)))
    start = str(int(since))
    command = push_texts.user_send_argv(rendered, sender=SENDER, key=f"machine-watch-{machine}-{start}")
    command.extend(["--clears-when",
                    f"{SCRIPTS}/lib/py {SCRIPTS}/buildlog/machine_watch.py cleared --machine {machine} --since {start}",
                    "--recipient-key", "user"])
    try:
        sent = subprocess.run(command, capture_output=True, text=True,
                              timeout=mac_idle_alert.read_idle_config().send_timeout_s, check=False)
        if sent.returncode == 0:
            return True
        reason = failure_detail(sent)
    except (OSError, subprocess.SubprocessError) as error:
        reason = str(error)
    print(f"Machine watch: phone push for {machine} failed: {mac_idle_alert.display_field(reason)}", file=sys.stderr)
    return False


def send_notice(machine: str, transport: ExecutionTransport | None, cause: Unusable | StepsWait | UnreachableMacAlert,
                since: float, now: float) -> bool:
    """A push goes only for an action that only nate can take. A machine that takes no work is ours to repair."""
    if isinstance(cause, UnreachableMacAlert):
        return mac_idle.push_outage(cause.alert, now)
    if isinstance(cause, Unusable) and nate_repairs(transport):
        return push_to_nate(machine, since, now)
    text = (f"Machine watch: {machine} has run no work for {int((now - since) // 60)} min. {cause_text(cause)}\n"
            + f"Check {machine}. This message goes once for this outage.")
    # Send to each receiver, then confirm all. A retry sends again, and the key stops a second delivery.
    sent = [send_session(to, text, f"machine-watch-{machine}-{int(since)}") for to in SESSION_RECEIVERS]
    return all(sent)


def nate_ordered(block: PendingMacBlock | ActiveMacBlock, machine: str) -> bool:
    """A block that nate ordered names him in its reason, or offload=off turns the Mac off as well."""
    from scripts.mac_test import mac_test

    return (re.search(r"\bnate\b", block.reason, re.IGNORECASE) is not None
            or isinstance(mac_test.machine_off_on_purpose(machine, mac_test.AskingSession(block.holder)),
                          mac_test.OffOnPurpose))


def holder_showrunner(block: PendingMacBlock | ActiveMacBlock) -> str | None:
    from scripts.mac_test import mac_test

    if isinstance(block.showrunner, mac_test.NamedShowrunner):
        return block.showrunner.name
    if isinstance(block.delivery, mac_test.RecordedSession):
        label = mac_idle.unit_labels_for([block.delivery.name], os.environ).get(block.delivery.name)
        if label is not None and label.showrunner != "—":
            return label.showrunner
    return None


def watch_idle_block(machine: str, paths: WatchPaths, now: float, notice: float) -> None:
    """A unit block that holds the Mac with no Mac work past the notice time sends one message to each receiver.

    The receivers are bob and the holder's showrunner. A block that nate ordered sends none.
    """
    from scripts.mac_test import mac_test

    path = paths.state / f"{machine}-block.json"
    saved = fresh_alert(paths, now)
    if saved is None or (saved["state"], saved["cause"]) != ("idle", "blocked"):
        path.unlink(missing_ok=True)
        return
    try:
        block = mac_test.read_block(paths.mac.mac_state / "block.json")
    except mac_test.StateReadError:
        return
    if isinstance(block, mac_test.NoMacBlock) or nate_ordered(block, machine):
        path.unlink(missing_ok=True)
        return
    since = max(saved["since"], block.since.timestamp())
    if now - since < notice:
        return
    previous = read_block_notice(path)
    notice_row: SavedBlockNotice = {"since": since, "sent": []}
    if previous is not None and int(previous["since"]) == int(since):
        notice_row["sent"] = previous["sent"]
    showrunner = holder_showrunner(block)
    holder = mac_idle_alert.display_field(block.holder)
    text = (f"Machine watch: {holder} has blocked the Mac for {int((now - since) // 60)} min, and the Mac ran no "
            + f"work in that time. Block reason: {mac_idle_alert.display_field(block.reason)}.\n"
            + f"Ask {holder} to use the Mac or to remove the block with /build:mac unblock.")
    for recipient in ("bob", showrunner) if showrunner is not None else ("bob",):
        if recipient in notice_row["sent"]:
            continue
        if send_session(recipient, text, f"machine-watch-{machine}-block-{int(since)}"):
            notice_row["sent"].append(recipient)
            mac_idle.write_record(path, notice_row)


def watch_machine(target: Target, paths: WatchPaths, now: float, settings: conf.ConfigRead) -> None:
    """Apply the one rule to one machine."""
    from scripts.build_targets.target_config import SshTarget, uses_legacy_mac_layout
    from scripts.mac_test import mac_test

    notice = notice_seconds(target.name, settings)
    if isinstance(target, SshTarget) and uses_legacy_mac_layout(target.platform):
        watch_idle_block(target.name, paths, now, notice)
    path = paths.state / f"{target.name}.json"
    if isinstance(mac_test.machine_off_on_purpose(target.name), mac_test.OffOnPurpose):
        # A machine that the user turned off ends a sent notice, so a new outage sends again when it is in use.
        path.unlink(missing_ok=True)
        return
    transport = transport_for(target)
    reading = read_machine(target, transport, paths, now, notice)
    if isinstance(reading, NoChange):
        return
    if isinstance(reading, NoOutage):
        path.unlink(missing_ok=True)
        return
    # One outage sends one notice. Only the end of the outage, or a machine that is off on purpose, clears "sent".
    # A lost or unreadable state file can send one more notice for the same outage.
    saved = read_notice(path)
    if isinstance(reading.since, float):
        since = reading.since
    else:
        since = saved["since"] if saved is not None else now
    sent = saved is not None and int(saved["since"]) == int(since) and saved["sent"]
    if not sent and now - since >= notice:
        sent = send_notice(target.name, transport, reading.cause, since, now)
    mac_idle.write_record(path, SavedNotice(since=since, sent=sent))


def watch_once(paths: WatchPaths, now: float) -> None:
    """Run the Mac idle watch, then the one rule for each machine."""
    try:
        mac_idle.watch_once(paths.mac, now)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Machine watch: Mac idle check failed: {error}", file=sys.stderr)
    paths.state.mkdir(parents=True, exist_ok=True)
    with (paths.state / "watch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        settings = conf.read("build_targets")
        for target in machines():
            try:
                watch_machine(target, paths, now, settings)
            except (OSError, ValueError, subprocess.SubprocessError) as error:
                print(f"Machine watch: {target.name} check failed: {error}", file=sys.stderr)


def cleared(machine: str, since: float) -> bool:
    """The clear check of a phone push: the machine is off on purpose, or the saved outage ended."""
    from scripts.mac_test import mac_test

    if isinstance(mac_test.machine_off_on_purpose(machine), mac_test.OffOnPurpose):
        return True
    path = watch_paths().state / f"{machine}.json"
    if not path.exists():
        return True
    saved = read_notice(path)
    return saved is not None and int(saved["since"]) != int(since)


def main(args: list[str]) -> int:
    if len(args) == 5 and args[0] == "cleared" and args[1] == "--machine" and args[3] == "--since":
        try:
            since = float(args[4])
        except ValueError:
            return 2
        if not math.isfinite(since):
            return 2
        return int(not cleared(args[2], since))
    if args == ["watch"]:
        watch_once(watch_paths(), time.time())
        return 0
    print("usage: machine_watch.py cleared --machine NAME --since SECONDS | watch", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

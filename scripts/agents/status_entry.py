"""Show the account inventory without loading the lifecycle commands."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping
from concurrent.futures import Future
from threading import Thread
from typing import Literal, Never, TypedDict, override

from scripts.agents import remote
from scripts.agents.inventory import CheckoutState, Host, InvalidInventory, Inventory, inventory, parse_inventory
from scripts.herdr import herdr
from scripts.herdr.herdr_types import HerdrFailure
from scripts.production import staff
from scripts.whoami.account import Account, UnreadableAccount, named_claude_account, own_claude_account


class UnreachableMachine(TypedDict):
    machine: str
    unreachable: Literal[True]

class UnavailableMachine(TypedDict):
    machine: str
    status: Literal["unavailable"]
    rc: int
    reason: str

MachineStatusReport = Inventory | UnreachableMachine | UnavailableMachine


def _selected_account(requested: str | None) -> Account:
    return own_claude_account() if requested is None else named_claude_account(requested)

def _host_text(host: Host) -> str:
    match host["kind"]:
        case "unit":
            return f"unit {host['unit']} (herdr {host['session_name']})"
        case "legacy tmux" | "legacy tmux unit":
            return f"{host['kind']} {host['session_name']} (manual recovery)"
        case "herdr":
            return f"herdr {host['herdr_session']}"
        case "ghostty" | "zed":
            desktop = host["desktop"]
            return (
                f"{host['kind']} on {desktop['name']}"
                if desktop["kind"] == "named"
                else host["kind"]
            )
        case kind:
            return kind

def _checkout_text(checkout: CheckoutState) -> str:
    if checkout["kind"] == "not a checkout":
        return "not a checkout"
    head = checkout["head"]
    name = head["name"] if head["kind"] == "branch" else head["commit"][:12]
    upstream = checkout["upstream"]
    tracking = (
        f"ahead {upstream['ahead']}"
        if upstream["kind"] == "tracking"
        else "no upstream"
    )
    return f"{name} {tracking}, {len(checkout['dirty'])} dirty"

def _inventory_lines(report: Inventory, kept_workers: Mapping[str, list[str]]) -> list[str]:
    lines = [f"{report['machine']}:"]
    for session in report["sessions"]:
        summary = f"  {session['kind']} {session['name']} · {_host_text(session['host'])}"
        summary += f" · {session['status']} · {_checkout_text(session['checkout'])}"
        if (workers := kept_workers.get(session["session_id"])) is not None:
            summary += " · paused (workers kept" + (f": {', '.join(workers)})" if workers else ")")
        lines.append(summary)
        lines.extend(f"    timer {timer}" for timer in session["timers"])
    lines.extend(
        f"  unknown account · {item['pid']} {item['name']} · {item['reason']}"
        for item in report["unattributed"]
    )
    if not report["sessions"] and not report["unattributed"]:
        lines.append(f"  no sessions on {report['label']}")
    return lines

def _unavailable(machine: str, rc: int, reason: str) -> UnavailableMachine:
    return {
        "machine": machine,
        "status": "unavailable",
        "rc": rc,
        "reason": reason.strip() or "remote command failed",
    }

def _local_inventory(login: str) -> tuple[Inventory, staff.StaffSnapshot]:
    membership = staff.snapshot()
    with staff.supplied_snapshot(membership), herdr.observation_snapshot():
        return inventory(login), membership


def _status(selected: Account, here: bool) -> tuple[list[MachineStatusReport], staff.StaffSnapshot]:
    if here:
        reading: staff.PeerStatusAuthority | staff.ReadStatusAuthority = staff.ReadStatusAuthority()
        stdin = sys.stdin
        if stdin is not None and os.environ.get(remote.STATUS_STAFF_STDIN) == "1" and not staff.is_owner():
            try:
                reading = staff.decode_peer_status_authority(stdin.read())
            except (OSError, UnicodeError):
                pass
        with staff.status_authority(reading):
            local, membership = _local_inventory(selected.login)
            return [local], membership

    arguments = ["status", selected.login, "--json", "--here"]
    view = staff.peer_status_authority()
    payload = view.encode() if isinstance(view, staff.PeerStatusAuthority) else ""
    peer: Future[tuple[int, str]] = Future()
    cancellation = remote.RemoteCallCancellation()

    def read_peer() -> None:
        try:
            result = remote.run_remote(arguments, stdin=payload, cancellation=cancellation)
        except BaseException as error:
            peer.set_exception(error)
        else:
            peer.set_result(result)

    with staff.status_authority(view):
        # A local failure must let the command exit before the peer replies.
        Thread(target=read_peer, daemon=True).start()
        try:
            local, membership = _local_inventory(selected.login)
        except BaseException:
            cancellation.cancel()
            raise
        status, output = peer.result()

        machine = remote.other_machine()
        if status == 255:
            return [local, {"machine": machine, "unreachable": True}], membership
        if status != 0:
            return [local, _unavailable(machine, status, output)], membership
        try:
            return [local, parse_inventory(output)], membership
        except InvalidInventory as error:
            return [local, _unavailable(machine, status, str(error))], membership


def _report_json(report: MachineStatusReport, kept_workers: Mapping[str, list[str]]) -> Mapping[str, object]:
    if "sessions" not in report:
        return report
    sessions = [
        {**session, "kept_workers": workers}
        if (workers := kept_workers.get(session["session_id"])) is not None else session
        for session in report["sessions"]
    ]
    return {**report, "sessions": sessions}


class StatusArguments(argparse.Namespace):
    account: str | None = None
    as_json: bool = False
    here: bool = False


def add_arguments(command: argparse.ArgumentParser) -> None:
    _ = command.add_argument("account", nargs="?")
    _ = command.add_argument("--json", action="store_true", dest="as_json")
    _ = command.add_argument("--here", action="store_true")


class _StatusUsageRequested(Exception):
    """The parser needs the full agents usage text."""


class _StatusArgumentParser(argparse.ArgumentParser):
    @override
    def error(self, message: str) -> Never:
        raise _StatusUsageRequested

    @override
    def print_help(self, file: object = None) -> Never:
        raise _StatusUsageRequested


def _parse_status(parser: argparse.ArgumentParser, arguments: list[str]) -> StatusArguments:
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    add_arguments(commands.add_parser("status"))
    return parser.parse_args(arguments, namespace=StatusArguments())


def main(arguments: list[str], *, usage: Callable[[], str]) -> int:
    try:
        options = _parse_status(_StatusArgumentParser(prog="agents"), arguments)
    except _StatusUsageRequested:
        parser = argparse.ArgumentParser(prog="agents", usage=usage().removeprefix("usage: ").rstrip())
        options = _parse_status(parser, arguments)
    try:
        selected = _selected_account(options.account)
        reports, membership = _status(selected, options.here)
        kept_workers = {str(row.current_session): sorted(child.name for child in membership.find(kind="worker", parent=row.id))
                        for row in membership.rows
                        if row.kind in ("unit", "showrunner") and isinstance(row.paused, staff.Paused) and row.paused.keep_workers}
        if options.as_json:
            output = [_report_json(report, kept_workers) for report in reports]
            print(json.dumps(output[0] if options.here else output, sort_keys=True))
        else:
            print(f"{selected.label} · {selected.login}")
            for report in reports:
                if "sessions" in report:
                    print("\n".join(_inventory_lines(report, kept_workers)))
                elif "unreachable" in report:
                    print(f"{report['machine']}: unreachable")
                else:
                    print(f"{report['machine']}: unavailable (rc {report['rc']})")
        return 0
    except staff.StaffRefused as error:
        print(f"shutdown: StaffRefused: {error}", file=sys.stderr)
        return 2
    except (OSError, ValueError, staff.StaffNotFound, UnreadableAccount, HerdrFailure) as error:
        print(f"shutdown: {error}", file=sys.stderr)
        return 2

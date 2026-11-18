"""Give a producer role to the conversation that runs the command."""
from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from pathlib import Path
from typing import Literal, cast

from scripts.accounts import accounts
from scripts.agents import member_account, pane_record, remote, restart, shutdown_record
from scripts.agents.stop_results import NotRunning, StopResult, Stopped
from scripts.herdr import herdr, pane_launch
from scripts.lib import models_registry
from scripts.production import staff


@dataclass(frozen=True)
class LiveProducerPane:
    launch: pane_launch.CurrentLaunch
    terminal: str


def producer_pane_kinds(info: dict[str, object]) -> list[Literal["claude", "codex"]]:
    """Read agent families from a pane's foreground processes."""
    kinds: list[Literal["claude", "codex"]] = []
    for value in herdr.json_items(info.get("foreground_processes"), "producer foreground processes"):
        process = herdr.json_object(value, "producer foreground process")
        argv = [herdr.json_text(argument, "producer argument")
                for argument in herdr.json_items(process.get("argv"), "producer arguments")]
        if herdr.is_claude_process(argv):
            kinds.append("claude")
        elif herdr.is_codex_process(argv):
            kinds.append("codex")
    return kinds


def producer_pane(row: staff.StaffRow, record: staff.StaffSnapshot, client: herdr.Herdr,
                  folder: Path) -> LiveProducerPane | herdr.ExistingPane | herdr.NewTab:
    """Read the producer tab and prove its shell or live conversation."""
    workspace = herdr.row_workspace(row, record, row.name)
    panes = client.panes()
    named = [pane for pane in panes if pane.get("label") == row.name]
    if named:
        spaces = {space.get("workspace_id") for space in client.listed("workspace") if space.get("label") == workspace}
        named = [pane for pane in named if pane["workspace_id"] in spaces]
    if not named:
        return herdr.NewTab(workspace, row.name)
    if len(named) != 1:
        raise ValueError(f"{row.name}: several producer panes match")
    pane = named[0]
    tab, terminal = pane.get("tab_id"), pane.get("terminal_id")
    if not terminal or not tab or sum(other.get("tab_id") == tab for other in panes) != 1:
        raise ValueError(f"{row.name}: the producer pane has no unique terminal and tab")
    identity = pane["pane_id"]
    if isinstance(client.pane_activity(identity), herdr.PaneAtShell):
        return herdr.ExistingPane(identity, terminal)
    info = herdr.json_object(client.result("pane", "process-info", "--pane", identity).get("process_info"),
                             "producer pane process info")
    kinds = producer_pane_kinds(info)
    if len(kinds) != 1:
        raise ValueError(f"{row.name}: the producer pane has no unique native conversation")
    launch = client.producer_admission_launch(identity, kinds[0])
    owner = record.by_member_reference(launch["session_id"])
    if owner is not None and owner.id != row.id:
        raise ValueError(f"{row.name}: the producer pane runs {owner.name}")
    if Path(launch["cwd"]).resolve() != folder.resolve():
        raise ValueError(f"{row.name}: the producer conversation is outside {folder}")
    return LiveProducerPane(launch, terminal)


def require_producer_shell(row: staff.StaffRow, record: staff.StaffSnapshot, client: herdr.Herdr,
                           destination: herdr.ExistingPane, folder: Path) -> None:
    saved = pane_record.read(client.session).get(destination.pane)
    if saved is not None:
        reference = pane_launch.launch_reference(saved, record)
        if isinstance(reference, pane_launch.StaffLaunchReference) and reference.id != row.id:
            raise ValueError(f"{row.name}: the producer shell belongs to another member")
    info = herdr.json_object(client.result("pane", "process-info", "--pane", destination.pane).get("process_info"),
                             "producer shell process info")
    for value in herdr.json_items(info.get("foreground_processes"), "producer foreground processes"):
        shell = herdr.json_object(value, "producer shell")
        cwd = shell.get("cwd")
        if shell.get("pid") == info.get("shell_pid") and isinstance(cwd, str) and Path(cwd).resolve() == folder.resolve():
            return
    raise ValueError(f"{row.name}: the producer shell is outside {folder} or its folder is unreadable")


def stop_previous(row: staff.StaffRow, client: herdr.Herdr, *, keep: tuple[str, ...] = ()) -> None:
    old_panes = [pane for _row, pane in restart.row_panes([row], client) if pane not in keep]
    if not restart.stop_row(row, client):
        raise ValueError(f"{row.name}: the old conversation {row.current_session} did not stop")
    print(f"{row.name}: the old conversation stopped; read it with: "
          + f"~/.claude/scripts/lib/py ~/.claude/scripts/agents/catch_up.py {row.current_session}")
    for pane in old_panes:
        if not client.close_retained_pane(pane):
            raise ValueError(f"{row.name}: the old pane {pane} did not close")


def stop_unbound(pane: LiveProducerPane, client: herdr.Herdr) -> None:
    identity = pane.launch["session_id"]
    stop = cast(Callable[[str, str], StopResult], import_module("scripts.agents.stop_resume").stop_native_conversation)
    result = stop(identity, pane_launch.saved_kind(pane.launch))
    if not isinstance(result, (Stopped, NotRunning)):
        raise ValueError(f"{identity}: the unbound producer conversation did not stop")
    if not client.close_retained_pane(pane.launch["pane_id"], terminal=pane.terminal):
        raise ValueError(f"{identity}: the unbound producer pane did not close")


def bind(row: staff.StaffRow, client: herdr.Herdr, entry: pane_launch.CurrentLaunch,
         home: staff.SessionHome) -> staff.StaffRow:
    """Give the row to the proved live conversation and publish its pane binding."""
    target = staff.native_id(entry["session_id"])
    kind = pane_launch.saved_kind(entry)
    folder = Path(entry["cwd"])
    location = staff.PresentWorktree(remote.local_machine(), folder)
    if row.current_session != target or row.home != home or row.worktree != location:
        row = staff.transfer_producer(row.id, target, home, location)
    entry["name"] = row.name
    with pane_launch.producer_launch_request(pane_launch.ProducerLaunchRequest(row.id, target, kind, folder, home.path)):
        pane_record.write(client.session, entry)
        row = pane_launch.staff_admitted(row)
    binding = pane_record.read(client.session).get(entry["pane_id"])
    if binding is None or binding.get("staff_id") != row.id or not binding.get("producer_binding", False):
        raise ValueError(f"{row.name}: the pane publication is unproved; repeat the command")
    _ = client.retitle_row(str(row.id))
    client.remember_row_tab(str(row.id), entry["pane_id"])
    return row


def _producer(record: staff.StaffSnapshot, name: str) -> staff.StaffRow:
    owners = [row for row in record.rows if row.kind == "producer" and row.name == name]
    if len(owners) != 1:
        raise ValueError(f"{name}: {'several producer rows match' if owners else 'no producer row'}")
    return owners[0]


def _take(name: str, caller: str, pane: str, client: herdr.Herdr, model: str, effort: str) -> staff.StaffRow:
    """Run the transfer steps under the row lock. Each step is safe to run again."""
    record = staff.initial_snapshot()
    row = _producer(record, name)
    # The row id can be the caller's own id: the first conversation of a producer takes its role back.
    member = next((other for other in record.rows
                   if other.id != row.id and caller in (other.id, other.current_session)), None)
    if member is not None:
        raise ValueError(f"this conversation is the {member.kind} {member.name}; a staff member cannot take a producer role")
    machine, producer_folder = shutdown_record.row_folder(row, record)
    if machine != remote.local_machine():
        raise ValueError(f"{name} runs on {machine}; run the transfer there")
    adoption_launch = cast(Callable[[str, str, herdr.Herdr], pane_launch.CurrentLaunch],
                           import_module("scripts.agents.stop_resume").producer_adoption_launch)
    entry = adoption_launch(caller, pane, client)
    if not entry["pane_id"]:
        raise ValueError("this conversation has no herdr pane")
    native = staff.native_id(entry["session_id"])
    kind = pane_launch.saved_kind(entry)
    home = pane_launch.config_home(entry, record=staff.StaffSnapshot(()))
    if not isinstance(home, staff.SessionHome):
        raise ValueError("the account of this conversation is unavailable")
    account = member_account.resume_account(kind, row)
    if isinstance(account, accounts.NamedAccount) and account.dir.resolve() != home.path.resolve():
        raise ValueError(f"{name} runs on {account.label}; first run: agents move {native} {account.label}")
    folder = Path(entry["cwd"])
    if model:
        settings = models_registry.producer_settings(name, kind, model, effort)
    else:
        settings = models_registry.launch_settings("producers", name)
        if settings.kind != kind:
            raise ValueError(f"{name} uses {settings.kind}; give --model and --effort for {kind}")
    previous = producer_pane(row, record, client, producer_folder)
    if row.current_session != native:
        keep = (entry["pane_id"],)
        if isinstance(previous, LiveProducerPane) and previous.launch["session_id"] != row.current_session:
            keep += (previous.launch["pane_id"],)
        stop_previous(row, client, keep=keep)
    if isinstance(previous, LiveProducerPane) and previous.launch["session_id"] not in (row.current_session, native):
        stop_unbound(previous, client)
    if model:
        _ = models_registry.store_producer(row.name, kind, model, effort)
        # Use the new settings in this operation.
        _ = os.environ.pop(models_registry.OPERATION_ENVIRONMENT[0], None)
    row = bind(row, client, entry, home)
    print(f"{name}: session {native} holds the role; {kind}:{settings.model}:{settings.effort}, folder {folder}")
    return row


def transfer(name: str, *, model: str = "", effort: str = "") -> int:
    """Move the producer row to the caller, then restart the caller as the producer."""
    if bool(model) != bool(effort):
        raise ValueError("give --model and --effort together")
    caller, pane = staff.runtime_id(), os.environ.get("HERDR_PANE_ID", "")
    if not caller or not pane:
        raise ValueError("run the transfer in the conversation that takes the role, in its herdr pane")
    client = herdr.Herdr(herdr.shared_session())
    with staff.slot_operation("producer-first-" + name, name):
        row = _producer(staff.initial_snapshot(), name)
        with staff.row_operation(str(row.id)):
            row = _take(name, caller, pane, client, model, effort)
    # The restart takes the row lock in a process outside this conversation.
    return restart.restart(stop_all=True, only=(str(row.id),), why=f"agents transfer {name} ran")

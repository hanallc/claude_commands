"""Select the account for a member's resumed session."""
from __future__ import annotations

from typing import TYPE_CHECKING, Literal, cast
from collections.abc import Callable
from scripts.herdr.place import HerdrPlace
from importlib import import_module
from pathlib import Path
import subprocess
import sys

from scripts.accounts import accounts
from scripts.agents.stop_results import SessionWithoutStaffRow, SESSION_WITHOUT_STAFF_ROW
from scripts.whoami import agent_accounts
from scripts.whoami import account
from scripts.herdr import pane_launch, herdr_types

from scripts.production import staff

if TYPE_CHECKING:
    from scripts.production.staff import StaffRow


def producer_account(kind: Literal["claude", "codex"], *, to: str = "") -> accounts.AccountSelection:
    """Select the producer account before staff admission."""
    if to:
        return resume_account(kind, to=to)
    return accounts.role_account("workers" if kind == "codex" else "producer")


def resume_account(kind: Literal["claude", "codex"], row: StaffRow | SessionWithoutStaffRow = SESSION_WITHOUT_STAFF_ROW,
                   *, to: str | accounts.AccountSelection = "") -> accounts.AccountSelection:
    """Keep an account selection or select the requested label or role account."""
    if isinstance(to, (accounts.NamedAccount, accounts.CodexWorkersAccount)):
        return to
    if to:
        return (accounts.CodexWorkersAccount(to, agent_accounts.codex_account_home(to).home)
                if kind == "codex" else accounts.NamedAccount(to, accounts.account_dir(to)))
    if not isinstance(row, SessionWithoutStaffRow) and row.kind == "session" and isinstance(row.home, staff.SessionHome):
        return (accounts.CodexWorkersAccount(agent_accounts.codex_home_role_label(row.home.path), row.home.path)
                if kind == "codex" else accounts.NamedAccount(accounts.claude_config_label(row.home.path), row.home.path))
    if kind == "codex":
        return accounts.role_account("workers")
    if not isinstance(row, SessionWithoutStaffRow) and row.kind == "producer":
        return accounts.role_account("producer")
    if not isinstance(row, SessionWithoutStaffRow) and row.kind in ("unit", "showrunner"):
        return accounts.role_account(row.kind)
    return accounts.role_account("unit")


def implicit_claude_resume(row: StaffRow | SessionWithoutStaffRow, entry: pane_launch.NativePaneLaunch,
                           target: str | accounts.AccountSelection) -> pane_launch.ImplicitClaudeHome | pane_launch.SelectedClaudeAccount:
    """Reuse the implicit account when a standalone Claude resumes on its saved home."""
    if not target and isinstance(row, staff.StaffRow) and row.kind == "session" and pane_launch.saved_kind(entry) == "claude":
        implicit = pane_launch.implicit_claude_home(entry)
        if isinstance(implicit, pane_launch.ImplicitClaudeHome) and row.home == staff.SessionHome(implicit.config_dir):
            return implicit
    return pane_launch.SelectedClaudeAccount.USE_SELECTION


def repair_home(row: StaffRow, kind: str, pid: int) -> None:
    """Repair the member's home from its unchanged current process."""
    try:
        reader = cast(pane_launch.NativeProcessReader, cast(object, import_module("scripts.herdr.herdr")))
        observed = reader.native_process(pid, kind)
        if observed.current_identity != row.current_session:
            return
        if kind == "codex":
            codex = account.process_codex_home(pid)
            if isinstance(codex, account.ProcessEnvironmentUnread):
                return
            folder = codex.home if isinstance(codex, account.ProcessCodexHome) else Path.home() / ".codex"
        else:
            claude = account.process_config_dir(pid)
            if not isinstance(claude, account.ConfiguredClaudeFolder):
                return
            folder = claude.path
        place = cast(Callable[[int], object], import_module("scripts.herdr.herdr").herdr_place_of)(pid)
        if isinstance(place, HerdrPlace):
            cast(Callable[[str, str], None], import_module("scripts.herdr.herdr").forget_pane_evidence)(place.session, place.pane)
        current = reader.native_process(pid, kind)
        if current.current_identity != observed.current_identity or current.started != observed.started:
            return
        home = staff.SessionHome(folder.absolute())
        if row.home != home:
            _ = staff.set_home(row.id, home)
    except (OSError, ValueError, subprocess.SubprocessError, herdr_types.HerdrFailure,
            staff.StaffUnreadable, staff.StaffNotFound) as error:
        print(f"{row.name}: home repair is unavailable: {error}", file=sys.stderr)

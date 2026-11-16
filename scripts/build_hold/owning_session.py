"""Identify the session that owns a build, or the absence of that session."""

from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path


@dataclass(frozen=True)
class OwningSession:
    """The identifier names the session that owns the run."""
    session_id: str


@dataclass(frozen=True)
class NoOwningSession:
    """The run has no identified owning session."""


@dataclass(frozen=True)
class RunOwner:
    """The staff record identifies this session as the run owner."""
    session_id: str


@dataclass(frozen=True)
class NoRunNamed:
    """The caller has not named a run."""


@dataclass(frozen=True)
class RunNotInStaff:
    """The named run has no matching staff record."""
    run: Path


def run_resolution(environment: Mapping[str, str] = os.environ) -> RunOwner | NoRunNamed | RunNotInStaff:
    """Find the staff session that owns the named run."""
    run = (environment.get("UNIT_RUN_DIR") or environment.get("PLAN_DELEGATE_BOARD_DIR")
           or environment.get("UNIT_SESSION_DIR") or environment.get("PLAN_DELEGATE_SESSION_DIR"))
    if run:
        # Only a named run needs the staff table.
        from scripts.production import staff

        try:
            rows = staff.local_snapshot().rows
        except staff.StaffUnreadable:
            rows = ()
        for row in rows:
            if isinstance(row.run, staff.OpenRun) and str(row.run.path) == run and row.current_session:
                return RunOwner(row.current_session)
        return RunNotInStaff(Path(run))
    return NoRunNamed()


def owning_session(environment: Mapping[str, str] = os.environ) -> OwningSession | NoOwningSession:
    """Find the session that owns this run before using the shell's session id."""
    resolution = run_resolution(environment)
    if isinstance(resolution, RunOwner):
        return OwningSession(resolution.session_id)
    session_id = environment.get("CLAUDE_CODE_SESSION_ID", "")
    return OwningSession(session_id) if session_id else NoOwningSession()

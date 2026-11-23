"""One fake stop-and-resume boundary for account move callers."""
from __future__ import annotations

import json
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from types import MappingProxyType
from scripts.accounts import moves
from scripts.agents import stop_resume
from scripts.agents.stop_results import ClaudeStartState
from scripts.production import staff


_NO_SOURCES: Mapping[str, str] = MappingProxyType({})
_FIXTURE_REFRESH_EXPIRY = datetime(2100, 1, 1, tzinfo=timezone.utc)


def write_claude_move_credentials(directory: Path, *, expires_at: datetime = _FIXTURE_REFRESH_EXPIRY) -> None:
    """Write synthetic sign-in evidence in a fixture account directory."""
    _ = (directory / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {
        "refreshToken": "fixture", "refreshTokenExpiresAt": int(expires_at.timestamp() * 1000),
    }}))


@dataclass(frozen=True)
class MoveCall:
    session_id: str
    target: str
    why: str


@dataclass
class FakeSessionMoves:
    moved: Callable[[MoveCall], None] = lambda _call: None
    calls: list[MoveCall] = field(default_factory=list)
    failures: dict[str, str] = field(default_factory=dict)

    batches: list[list[str]] = field(default_factory=list)
    sources: list[dict[str, str]] = field(default_factory=list)

    def __call__(self, sessions: list[str], target: str, *, why: str, cause: moves.MoveCause = "by hand",
                 source_accounts: Mapping[str, str] = _NO_SOURCES, batch_id: str = "",
                 intent: moves.FollowGroupAccount | moves.ExplicitMoveTarget =
                 moves.ExplicitMoveTarget.REQUESTED_ACCOUNT,
                 staff_members: Collection[staff.StaffId] = ()) -> list[stop_resume.MoveResult]:
        del cause, batch_id, staff_members
        self.sources.append(dict(source_accounts))
        self.batches.append(list(sessions))
        return [stop_resume.ClaudeUp(session, "w1:p1", target, start_state=ClaudeStartState.ALREADY_RUNNING)
                if isinstance(intent, moves.FollowGroupAccount) and source_accounts.get(session) == target
                else self.move(session, target, why=why) for session in sessions]

    def move(self, session: str, target: str, *, why: str) -> stop_resume.ClaudeUp | stop_resume.StopFailed:
        call = MoveCall(session, target, why)
        self.calls.append(call)
        if session in self.failures:
            return stop_resume.StopFailed(session, self.failures[session])
        self.moved(call)
        return stop_resume.ClaudeUp(session, "w1:p1", target)

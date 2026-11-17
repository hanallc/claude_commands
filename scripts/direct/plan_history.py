"""Read durable plan phases, run history and progress-based ETA projections."""

from __future__ import annotations

from scripts.direct import pass_record as pass_recorder

import fcntl
import json
import re
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import NamedTuple, NotRequired, TypedDict, cast

from scripts.production import staff
from scripts.lib import unit_runs
from scripts.production import session_staff  # noqa: E402
from scripts.agents import remote  # noqa: E402
from scripts.direct.recorder_state import STATE_FILENAME as STATE_FILENAME  # noqa: E402


# How far the percentage beside an ETA is assumed to be off when no calibration
# history has an opinion. Ten points is the width the recorded phase estimates
# have shown; a run with enough matching samples replaces it with its own
# measured error, so this only ever governs the first runs of a fresh history.
DEFAULT_PERCENT_SPREAD = 10.0
# The widest the best and worst case may stray from the reported rate: at worst
# the work is half as far along as it says, at best twice.
RATE_FACTOR_LIMIT = 2.0
# Every phase heading in a worker plan, in all three forms it takes over a
# plan's life. `· status: todo` and `· status: done` are the live-zone forms;
# a shrunk or as-built phase drops the status marker and usually carries a
# commit annotation instead. Counting on the status marker alone silently
# ignores every already-archived phase, which is how a 67%-complete plan once
# reported 36%. Match the `Phase <id>` heading itself and classify after.
PHASE_ID = r"\d+[a-z]?"
PHASE_HEADING_PATTERN = re.compile(
    r"^(?P<hashes>#{2,4})[ \t]+Phase[ \t]+(?P<id>" + PHASE_ID + r")\b(?P<rest>[^\n]*)$",
    re.MULTILINE,
)
PHASE_STATUS_PATTERN = re.compile(r"·[ \t]*status:[ \t]*(?P<status>todo|done)\b")
# `### Phase 12 Review` sits at the same heading level as a phase but is a
# section inside one. A real phase heading always names a title after an
# em-dash separator.
PHASE_TITLE_PATTERN = re.compile(r"^[ \t]*[—–-][ \t]*\S")


class PlanPhase(TypedDict):
    """One real phase heading, classified as the plan records it now."""

    id: str
    title: str
    done: bool
    waits_for: NotRequired[str]
    depends_on: NotRequired[tuple[str, ...]]


class EtaBand(NamedTuple):
    """Remaining seconds and the earliest and latest plausible arrivals."""

    remaining: int
    earliest: int
    latest: int


class EtaProjectionUnavailable(NamedTuple):
    """The reported progress cannot support an ETA projection."""


@dataclass
class HistorySnapshot:
    """Share staff and run history reads within one report."""

    record: staff.StaffSnapshot
    events: dict[Path, list[dict[str, object]]] = field(default_factory=dict)
    plans: dict[Path, tuple[list[PlanPhase], list[str]]] = field(default_factory=dict)

    @cached_property
    def starts(self) -> list[tuple[Path, dict[str, object]]]:
        return _run_starts()


class NoHistorySnapshot(NamedTuple):
    """Read history directly outside a report."""


_history_snapshot: ContextVar[HistorySnapshot | NoHistorySnapshot] = ContextVar(
    "history_snapshot", default=NoHistorySnapshot())


@contextmanager
def history_snapshot(record: staff.StaffSnapshot) -> Generator[None]:
    """Retain history reads until the report ends."""
    token = _history_snapshot.set(HistorySnapshot(record))
    try:
        yield
    finally:
        _history_snapshot.reset(token)


def staff_record() -> staff.StaffSnapshot:
    """Use the report's staff snapshot, or read staff for another caller."""
    snapshot = _history_snapshot.get()
    return snapshot.record if isinstance(snapshot, HistorySnapshot) else staff.snapshot()


def _read_run_events(path: Path) -> list[dict[str, object]]:
    """Read run events under a shared lock and skip invalid JSON rows."""
    try:
        with path.open(encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
            lines = handle.read().splitlines()
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except (OSError, UnicodeError):
        return []
    events: list[dict[str, object]] = []
    for line in lines:
        try:
            event = cast(object, json.loads(line))
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(cast(dict[str, object], event))
    return events


def read_run_events(path: Path) -> list[dict[str, object]]:
    """Read each run's events once within a report, or directly outside it."""
    snapshot = _history_snapshot.get()
    if isinstance(snapshot, NoHistorySnapshot):
        return _read_run_events(path)
    if path not in snapshot.events:
        snapshot.events[path] = _read_run_events(path)
    return snapshot.events[path]


def history_root() -> Path:
    return unit_runs.history_root()


def run_events_path(session_dir: Path) -> Path:
    """The events file of the run that uses this session folder."""
    return history_root() / "runs" / f"{session_dir.name}.jsonl"


def resolve_plan_path(working_dir: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (working_dir / path).resolve()


def plan_member(plan_path: Path, *, resolver: session_staff.RowResolver | None = None) -> session_staff.MemberFound | session_staff.NoSuchMember:
    """Resolve the plan folder through the current staff worktree."""
    resolver = resolver if resolver is not None else session_staff.RowResolver(staff.snapshot())
    return resolver.member_by_folder(remote.local_machine(), plan_path.expanduser().resolve().parent)


def plan_phase_details(plan_path: Path) -> tuple[list[PlanPhase], list[str]]:
    """Read phase headings and their recorded waits from one plan read."""
    snapshot = _history_snapshot.get()
    if isinstance(snapshot, HistorySnapshot) and plan_path in snapshot.plans:
        return snapshot.plans[plan_path]
    text = plan_path.read_text(encoding="utf-8")
    phases: list[PlanPhase] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    headings = [match for match in PHASE_HEADING_PATTERN.finditer(text)
                if PHASE_STATUS_PATTERN.search(match.group("rest")) or PHASE_TITLE_PATTERN.match(match.group("rest"))]
    for index, match in enumerate(headings):
        rest = match.group("rest")
        status_match = PHASE_STATUS_PATTERN.search(rest)
        # `### Phase 12 Review` and its like are sections, not phases.
        if status_match is None and not PHASE_TITLE_PATTERN.match(rest):
            continue
        identifier = match.group("id")
        if identifier in seen:
            duplicates.append(identifier)
            continue
        seen.add(identifier)
        title_text = rest if status_match is None else rest[: status_match.start()]
        title_text = re.sub(r"^[ \t]*[—–-][ \t]*", "", title_text, count=1)
        title = re.sub(
            r"[ \t]+\((?:commit[ \t]+)?(?:`[0-9a-fA-F]{7,40}`|[0-9a-fA-F]{7,40})\)[ \t]*$",
            "",
            title_text,
        ).strip()
        # No status marker means the phase was shrunk into its as-built record,
        # which only happens after the phase completed.
        done = status_match is None or status_match.group("status") == "done"
        phase = PlanPhase(id=identifier, title=title, done=done)
        section = text[match.end():headings[index + 1].start() if index + 1 < len(headings) else len(text)]
        waits = re.search(r"(?im)^[ \t]*(?:[-*][ \t]+)?\*\*Waits for:\*\*[ \t]*(.+)$", section)
        depends = re.search(r"(?im)^[ \t]*(?:[-*][ \t]+)?\*\*Depends on:\*\*[ \t]*(.+)$", section)
        if waits:
            phase["waits_for"] = waits.group(1).strip()
        if depends:
            phase["depends_on"] = tuple(re.findall(r"\b(\d+[a-z]?)\b", depends.group(1), re.IGNORECASE))
        phases.append(phase)
    if isinstance(snapshot, HistorySnapshot):
        snapshot.plans[plan_path] = (phases, duplicates)
    return phases, duplicates


def plan_phases(plan_path: Path) -> list[PlanPhase]:
    """Return the plan's unique phase headings in document order."""
    phases, _ = plan_phase_details(plan_path)
    return phases


def run_started_event(path: Path) -> dict[str, object] | None:
    try:
        with path.open(encoding="utf-8") as handle:
            first_line = handle.readline()
    except OSError:
        return None
    try:
        parsed = cast(object, json.loads(first_line))
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    event = cast(dict[str, object], parsed)
    if event.get("event_type") != "run_started":
        return None
    return event


def _run_starts() -> list[tuple[Path, dict[str, object]]]:
    try:
        paths = list((history_root() / "runs").glob("*.jsonl"))
    except OSError:
        return []
    return [(path, event) for path in paths if (event := run_started_event(path)) is not None]


def run_starts() -> list[tuple[Path, dict[str, object]]]:
    """Read run headers once within a report."""
    snapshot = _history_snapshot.get()
    return snapshot.starts if isinstance(snapshot, HistorySnapshot) else _run_starts()


def plan_runs(plan_path: Path, *,
              record: staff.StaffSnapshot | None = None) -> list[Path]:
    """Return this plan's durable run files, oldest first."""
    target = plan_path.expanduser().resolve()
    starts = run_starts()
    if record is None:
        record = (staff_record() if any(event.get(unit_runs.OWNER_FIELD) or event.get("main_agent") or event.get("owner_session") for _, event in starts)
                  else staff.StaffSnapshot(()))
    matches: list[tuple[float, str, Path]] = []
    for path, event in starts:
        owner = unit_runs.run_owner(event, record)
        fields = dict(event)
        if isinstance(owner, staff.StaffRow):
            pass_recorder.staff_paths(fields, owner)
        working_dir = fields.get("working_dir")
        plan_doc = fields.get("plan_doc")
        if not isinstance(working_dir, str) or not isinstance(plan_doc, str):
            continue
        if not working_dir or not plan_doc:
            continue
        if resolve_plan_path(Path(working_dir), plan_doc) != target:
            continue
        started_at = 0.0
        for value in (event.get("run_started_at"), event.get("timestamp_epoch")):
            if isinstance(value, int | float) and not isinstance(value, bool):
                started_at = float(value)
                break
        matches.append((started_at, path.name, path))
    return [path for _, _, path in sorted(matches)]


def eta_seconds(percent: int, elapsed: int) -> int | None:
    """Project the time left by extending the rate the clock has run so far.

    Percent over elapsed is the only rate both scopes share. For the project the
    percent comes from the plan's phase counts, so this is exactly "mean time per
    phase so far, times the phases left"; for the phase it inherits whatever
    accuracy the reporter's estimate has and claims nothing more. Both are read
    off the number the same line displays, capped value included, so the estimate
    never contradicts the percentage sitting beside it.
    """
    if percent <= 0 or percent >= 100 or elapsed <= 0:
        return None
    return int(elapsed * (100 - percent) / percent)


def eta_band_seconds(
    percent: int, elapsed: int, spread: float
) -> EtaBand | EtaProjectionUnavailable:
    """Return the projected remaining seconds and its optimistic/pessimistic band."""
    eta = eta_seconds(percent, elapsed)
    if eta is None:
        return EtaProjectionUnavailable()
    optimistic = min(99.0, percent * RATE_FACTOR_LIMIT, percent + max(0.0, spread))
    pessimistic = max(1.0, percent / RATE_FACTOR_LIMIT, percent - max(0.0, spread))
    low = int(elapsed * (100.0 - optimistic) / optimistic)
    high = int(elapsed * (100.0 - pessimistic) / pessimistic)
    return EtaBand(remaining=eta, earliest=low, latest=high)


def percent_spread(calibration: dict[str, object] | None) -> float:
    """How far off the percentage has actually run, when history can say.

    A calibration that cleared its sample floor has measured this reporter's
    error around this percentage against phases that finished, which beats any
    constant. Below the floor the measurement is noise wearing a number, so the
    default stands.
    """
    if calibration is None or calibration.get("apply_suggestion") is not True:
        return DEFAULT_PERCENT_SPREAD
    measured = calibration.get("median_raw_absolute_error_percentage_points")
    if not isinstance(measured, int | float):
        return DEFAULT_PERCENT_SPREAD
    return max(1.0, float(measured))


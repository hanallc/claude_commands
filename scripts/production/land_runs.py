"""Records of the checkpoint lands that run now, and the result of each land.

`merge_checkpoint.py` writes one record while it holds a production's merge lock, and removes it at exit.
A record whose process ended is not a running land; the next read removes it.
It also adds one result row for each checkpoint it lands or stops.
The units board reads the records to tell a running checkpoint from a queued one, and the results to say how a land ended.
"""
from __future__ import annotations

import json
import os
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from collections.abc import Collection, Generator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NotRequired, TypedDict, cast
from uuid import uuid4

from scripts.lib import file_lock
from scripts.lib.atomic_file import write_text_atomic
from scripts.lib.process_start import Started, Unreadable, process_start


# The results file keeps only this many newest rows.
KEPT_RESULTS = 300
DEFAULT_STATE_DIR = "~/.local/state/gate"


class GateStateMoveRequired(Exception):
    """The default gate state still requires the deploy move."""


class LandRecord(TypedDict):
    """A running land; owners[i] is the "unit phase" of commits[i]. An older record has no owners."""

    pid: int
    start: str
    commits: list[str]
    owners: NotRequired[list[str]]
    test_reference: NotRequired[str]
    command_pid: NotRequired[int]
    command_start: NotRequired[str]
    claim_id: NotRequired[str]


@dataclass(frozen=True)
class LandingTestReference:
    token: str


@dataclass(frozen=True)
class NoLandingTestReference:
    pass


_TEST_REFERENCE: ContextVar[LandingTestReference | NoLandingTestReference] = ContextVar(
    "landing_test_reference", default=NoLandingTestReference())
TEST_REFERENCE_ENV = "BUILDLOG_LANDING_TEST_REFERENCE"


def test_reference() -> LandingTestReference | NoLandingTestReference:
    reference = _TEST_REFERENCE.get()
    if isinstance(reference, LandingTestReference):
        return reference
    token = os.environ.get(TEST_REFERENCE_ENV, "")
    return LandingTestReference(token) if token else NoLandingTestReference()


def test_environment() -> dict[str, str]:
    reference = test_reference()
    if isinstance(reference, LandingTestReference):
        return {**os.environ, TEST_REFERENCE_ENV: reference.token}
    return dict(os.environ)


class LandResult(TypedDict):
    commit: str
    production: str
    unit: str
    phase: str
    landed: bool
    step: str
    state: str
    detail: str
    time: str


def default_state_dir() -> Path:
    """Return the default gate state folder after the deploy move."""
    directory = Path(DEFAULT_STATE_DIR).expanduser()
    # Chunk 6 removes this guard and the old folder path.
    old = Path("~/.local/state/merge-gate").expanduser()
    if os.path.lexists(old) and not os.path.lexists(directory):
        raise GateStateMoveRequired(
            "Gate state move required: set /build:hold drain natedev, then run "
            + "bash ~/.claude/scripts/production/gate_rename_deploy/deploy_gate_rename.sh")
    return directory


def state_dir() -> Path:
    """Return the selected gate state folder."""
    selected = os.environ.get("GATE_STATE_DIR")
    return default_state_dir() if selected is None else Path(selected).expanduser()


def lands_dir() -> Path:
    return state_dir() / "lands"


def _claims_dir() -> Path:
    return state_dir() / "land_claims"


def runners_dir() -> Path:
    return state_dir() / "land_runners"


def protection_paths() -> tuple[Path, ...]:
    return runners_dir(), _claims_dir(), lands_dir()


def _write_land(path: Path, record: LandRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_text_atomic(path, json.dumps(record) + "\n")


def record_runner(pid: int) -> None:
    owner = process_start(pid)
    if not isinstance(owner, Started):
        raise ValueError("the land runner has no live process identity")
    for _ in _active_records(runners_dir(), prune=True):
        pass
    path = runners_dir() / f"{pid}.json"
    _write_land(path, {"pid": pid, "start": owner.start, "commits": []})
    (_claims_dir() / path.name).unlink(missing_ok=True)


def close_claim(pid: int) -> None:
    """Remove the claim when the runner skips a command."""
    for path, record in _active_records(_claims_dir()):
        if record["pid"] == pid:
            path.unlink(missing_ok=True)


def runner_is_live() -> bool:
    return any(True for _ in _active_records(runners_dir()))


def claim(pid: int, commits: Sequence[str]) -> None:
    """Record a dequeued land while the runner starts its merge command."""
    owner = process_start(pid)
    if not isinstance(owner, Started):
        raise ValueError("the land runner has no live process identity")
    _write_land(_claims_dir() / f"{pid}.json", {"pid": pid, "start": owner.start, "commits": list(commits),
                                              "claim_id": uuid4().hex})


def _active_records(directory: Path, *, prune: bool = False) -> Generator[tuple[Path, LandRecord]]:
    try:
        paths = sorted(directory.glob("*.json"))
    except OSError:
        return
    for path in paths:
        try:
            record = cast(LandRecord, json.loads(path.read_text()))
            if not isinstance(cast(object, record["pid"]), int) or not isinstance(cast(object, record["start"]), str):
                continue
            owner = process_start(record["pid"])
            if owner == Started(record["start"]):
                command_pid = record.get("command_pid")
                if command_pid is None or process_start(command_pid) == Started(record.get("command_start", "")):
                    yield path, record
            elif prune and not isinstance(owner, Unreadable):
                path.unlink(missing_ok=True)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue


def _accept_claim(commits: Sequence[str]) -> None:
    for path, claim_record in _active_records(_claims_dir()):
        if any(holds(claim_record["commits"], commit) for commit in commits):
            path.unlink(missing_ok=True)


def _parents() -> set[int]:
    parents: set[int] = set()
    pid = os.getppid()
    proc = Path(os.environ.get("VERIFY_PROC_ROOT", "/proc"))
    while pid > 1 and pid not in parents:
        parents.add(pid)
        try:
            pid = int((proc / str(pid) / "stat").read_bytes().rpartition(b")")[2].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return parents


@contextmanager
def claim_command() -> Generator[None]:
    """Bind the runner's claim before the merge command validates its input."""
    pid = os.getpid()
    identity = process_start(pid)
    parents = _parents()
    for path, record in _active_records(_claims_dir()):
        if record["pid"] not in parents or "command_pid" in record or not isinstance(identity, Started):
            continue
        claim_id = record.get("claim_id", uuid4().hex)
        _write_land(path, {**record, "command_pid": pid, "command_start": identity.start, "claim_id": claim_id})
        try:
            yield
        finally:
            for current, saved in _active_records(_claims_dir()):
                if current == path and saved.get("claim_id") == claim_id:
                    path.unlink(missing_ok=True)
        return
    yield


def running_test_references() -> frozenset[LandingTestReference | NoLandingTestReference]:
    """Read live claims before registrations so the handoff keeps protection."""
    references: set[LandingTestReference | NoLandingTestReference] = {
        NoLandingTestReference() for _ in _active_records(_claims_dir())}
    for _, record in _active_records(lands_dir()):
        token = record.get("test_reference", "")
        if token:
            references.add(LandingTestReference(token))
    return frozenset(references)


def results_path() -> Path:
    return state_dir() / "land_results.jsonl"


@contextmanager
def landing(commits: Sequence[str], owners: Sequence[str] = ()) -> Generator[None]:
    """Record the commits of this land, and the "unit phase" that owns each, while the caller runs it."""
    identity = process_start(os.getpid())
    if not isinstance(identity, Started):
        yield
        return
    path = lands_dir() / f"{os.getpid()}.json"
    reference = LandingTestReference(uuid4().hex)
    record: LandRecord = {"pid": os.getpid(), "start": identity.start, "commits": list(commits), "owners": list(owners),
                          "test_reference": reference.token}
    _write_land(path, record)
    scope = _TEST_REFERENCE.set(reference)
    try:
        _accept_claim(commits)
        yield
    finally:
        _TEST_REFERENCE.reset(scope)
        path.unlink(missing_ok=True)


def running_lands() -> dict[str, str]:
    """The commits of every land whose process still runs, each with its "unit phase" or "".

    It removes the record of a land that ended.
    """
    commits: dict[str, str] = {}
    for _, record in _active_records(lands_dir(), prune=True):
        try:
            owners = record.get("owners", [])
            commits.update((commit, owners[index] if index < len(owners) else "")
                           for index, commit in enumerate(record["commits"]))
        except (KeyError, TypeError):
            continue
    return commits


def holds(running: Collection[str], commit: str) -> bool:
    """True when a running land names this commit; a land can name a short hash."""
    return any(commit.startswith(listed) or listed.startswith(commit) for listed in running if listed and commit)


def record_result(*, commit: str, production: str, unit: str, phase: str,
                  landed: bool, step: str = "", state: str = "", detail: str = "") -> None:
    """Add the result of one checkpoint land, and keep only the newest rows."""
    row: LandResult = {"commit": commit, "production": production, "unit": unit, "phase": phase, "landed": landed,
                       "step": step, "state": state, "detail": detail,
                       "time": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    path = results_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock.exclusive(path.with_name(path.name + ".lock")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        lines = [*lines, json.dumps(row)][-KEPT_RESULTS:]
        write_text_atomic(path, "\n".join(lines) + "\n")


def last_results() -> dict[str, LandResult]:
    """The newest result of each commit; a bad row is not a result."""
    results: dict[str, LandResult] = {}
    try:
        lines = results_path().read_text(encoding="utf-8").splitlines()
    except OSError:
        return results
    for line in lines:
        try:
            row = cast(LandResult, json.loads(line))
            commit = cast(object, row["commit"])
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(commit, str) and commit:
            results[commit] = row
    return results


def result_for(results: dict[str, LandResult], commit: str) -> LandResult | None:
    """The newest result whose commit names this commit; a result can name a short hash."""
    found = [row for listed, row in results.items() if holds(frozenset({listed}), commit)]
    return max(found, key=lambda row: row["time"]) if found else None

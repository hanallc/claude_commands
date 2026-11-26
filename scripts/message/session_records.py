"""Read native Claude session records without staff lookup."""
from __future__ import annotations

import json
import math
import os
import stat
import time
import tomllib
from collections.abc import Callable, Generator, Iterable, Iterator, Set
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, cast
from uuid import UUID

from scripts.lib.transcripts import config_dirs as config_dirs, transcript_paths, uncompressed_path
from scripts.lib.session_paths import sessions_directory
from scripts.message.process_identity import process_start as process_start
from scripts.message.relay_identity import RelayIdentity, live_relay_identity, relay_senders_directory

HEADLESS_ENTRYPOINT_PREFIX = "sdk-"
# A session at its prompt: idle, or idle while a background command runs.
INPUT_STATUSES = frozenset({"idle", "shell"})


class SessionRecord(TypedDict):
    pid: int
    sessionId: str
    name: str
    messagingSocketPath: str
    updatedAt: int
    # Older records and callers may omit it; read_session normalizes absence to empty.
    procStart: NotRequired[str]
    # "<tmux session name at start>:@<window>.%<pane>"; empty outside tmux. Only the pane id stays true.
    tmux: str
    # Every name the session had before the one it has now.
    formerNames: list[str]
    # How Claude was started: "cli" for an interactive session, "sdk-cli" for a `claude -p` run.
    # Older records and callers may omit it; read_session normalizes absence to empty.
    entrypoint: NotRequired[str]
    # The conversation's folder, for the exact staff-worktree join. Older records may omit it.
    cwd: NotRequired[str]
    # Older records omit status. The reader converts absence to an empty string.
    status: NotRequired[str]


class UnreadableSessionRecord(Enum):
    FOUND = "unreadable session record"


RegistryEntry = SessionRecord | UnreadableSessionRecord


class RegistryDetails(TypedDict, total=False):
    cwd: str
    kind: str
    status: str


@dataclass(frozen=True)
class RegistryObservation:
    record: SessionRecord
    details: RegistryDetails


@dataclass(frozen=True)
class RegistryRead:
    entries: tuple[RegistryObservation, ...]
    unreadable: tuple[Path, ...]


@dataclass(frozen=True)
class ReadCurrentRegistry:
    """Read the registry for this call."""


CURRENT_REGISTRY = ReadCurrentRegistry()
_liveness_registry = ContextVar[RegistryRead | ReadCurrentRegistry]("liveness_registry", default=CURRENT_REGISTRY)


@contextmanager
def use_registry_for_liveness(registry: RegistryRead) -> Generator[None]:
    """Use this registry for liveness checks that do not supply their own read."""
    token = _liveness_registry.set(registry)
    try:
        yield
    finally:
        _liveness_registry.reset(token)


@dataclass(frozen=True)
class SessionRetryPolicy:
    registry_reads: int
    registry_delay_seconds: float


def retry_policy() -> SessionRetryPolicy:
    """Read tuning at use; a missing or invalid setting never invents a retry budget."""
    path = Path(__file__).resolve().parents[2] / "config/session_retries.toml"
    with path.open("rb") as source:
        values = cast(dict[str, object], tomllib.load(source))
    count = values.get("registry_reads")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise ValueError(f"{path}: registry_reads must be a positive integer")
    delay = values.get("registry_delay_seconds")
    if not isinstance(delay, (int, float)) or isinstance(delay, bool) or not 0 <= delay < float("inf"):
        raise ValueError(f"{path}: registry_delay_seconds must be finite and nonnegative")
    return SessionRetryPolicy(count, float(delay))


@dataclass(frozen=True)
class RegistryRecordGone:
    """The file vanished, or its filename's process is proven gone."""


@dataclass(frozen=True)
class ConfiguredRegistryDirectory:
    """Use the registry directory selected by the current environment."""


CONFIGURED_REGISTRY_DIRECTORY = ConfiguredRegistryDirectory()


def registry_unreadable_reason(paths: tuple[Path, ...]) -> str:
    return "session registry unreadable: " + ", ".join(map(str, paths))


class RegistryReadExpired(TimeoutError):
    """Keep the evidence from a registry scan that reaches its deadline."""

    def __init__(self, registry: RegistryRead) -> None:
        self.registry: RegistryRead = registry
        super().__init__(registry_unreadable_reason(registry.unreadable) if registry.unreadable
                         else "session registry read deadline passed")


def _check_registry_deadline(deadline: float, delay: float = 0, *, unreadable: Path | None = None) -> None:
    if deadline != math.inf and time.monotonic() + delay >= deadline:
        raise RegistryReadExpired(RegistryRead((), () if unreadable is None else (unreadable,)))


def read_registry(*, directory: Path | ConfiguredRegistryDirectory = CONFIGURED_REGISTRY_DIRECTORY,
                  dependencies: Callable[[Path], None] | None = None,
                  for_session: str | None = None, deadline: float = math.inf) -> RegistryRead:
    """Report unreadable records unless their identity excludes the selected session."""
    if isinstance(directory, ConfiguredRegistryDirectory):
        directory = sessions_dir()
    _check_registry_deadline(deadline)
    try:
        paths = [path for path in directory.iterdir() if path.suffix == ".json"]
    except OSError:
        _check_registry_deadline(deadline, unreadable=directory)
        return RegistryRead((), (directory,))
    _check_registry_deadline(deadline)
    entries: list[RegistryObservation] = []
    unreadable: list[Path] = []
    policy = retry_policy()
    try:
        for path in paths:
            observation = _read_observation(path, policy, dependencies=dependencies, for_session=for_session, deadline=deadline)
            if isinstance(observation, RegistryObservation):
                entries.append(observation)
            elif isinstance(observation, UnreadableSessionRecord):
                unreadable.append(path)
        _check_registry_deadline(deadline)
    except RegistryReadExpired as error:
        unreadable.extend(error.registry.unreadable)
        raise RegistryReadExpired(RegistryRead(tuple(entries), tuple(unreadable))) from error
    return RegistryRead(tuple(entries), tuple(unreadable))


def parse_session(parsed: object) -> RegistryEntry:
    """Validate one decoded registry write without reading its file again."""
    if not isinstance(parsed, dict):
        return UnreadableSessionRecord.FOUND
    data = cast(dict[str, object], parsed)
    pid = data.get("pid")
    session_id = data.get("sessionId")
    name = data.get("name")
    socket_path = data.get("messagingSocketPath")
    updated_at = data.get("updatedAt")
    proc_start = data.get("procStart")
    tmux = data.get("tmux")
    former = data.get("formerNames")
    entrypoint = data.get("entrypoint")
    cwd = data.get("cwd")
    status = data.get("status")
    if (
        not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not isinstance(session_id, str)
        or not isinstance(name, str)
        or not isinstance(socket_path, str)
        or not isinstance(updated_at, int)
        or isinstance(updated_at, bool)
    ):
        return UnreadableSessionRecord.FOUND
    return SessionRecord(
        pid=pid,
        sessionId=session_id,
        name=name,
        messagingSocketPath=socket_path,
        updatedAt=updated_at,
        procStart=proc_start if isinstance(proc_start, str) else "",
        tmux=tmux if isinstance(tmux, str) else "",
        formerNames=[name for name in cast(list[object], former) if isinstance(name, str)]
        if isinstance(former, list) else [],
        entrypoint=entrypoint if isinstance(entrypoint, str) else "",
        cwd=cwd if isinstance(cwd, str) else "",
        status=status if isinstance(status, str) else "",
    )


def _other_session(path: Path, raw: object, session_id: str) -> bool:
    """Prove a different conversation from the current process generation's record."""
    if not isinstance(raw, dict):
        return False
    record = cast(dict[str, object], raw)
    identity, pid, started = record.get("sessionId"), record.get("pid"), record.get("procStart")
    if (not isinstance(identity, str) or identity == session_id or not isinstance(pid, int)
            or isinstance(pid, bool) or pid <= 0 or path.name != f"{pid}.json"
            or not isinstance(started, str) or not started):
        return False
    return str(UUID(identity)) == identity and process_start(pid) == started


def _read_observation(path: Path, policy: SessionRetryPolicy, *,
                      dependencies: Callable[[Path], None] | None = None, for_session: str | None = None,
                      deadline: float = math.inf,
                      ) -> RegistryObservation | UnreadableSessionRecord | RegistryRecordGone | None:
    """Reread a concurrent write; only a live unreadable record remains uncertain."""
    for attempt in range(policy.registry_reads):
        _check_registry_deadline(deadline, unreadable=path if attempt else None)
        try:
            if dependencies is not None:
                dependencies(path)
            raw = cast(object, json.loads(path.read_text()))
            record = parse_session(raw)
            if not isinstance(record, UnreadableSessionRecord):
                return RegistryObservation(record, cast(RegistryDetails, raw))
            if for_session is not None and attempt + 1 == policy.registry_reads and _other_session(path, raw, for_session):
                return None
        except FileNotFoundError:
            return RegistryRecordGone()
        except (OSError, ValueError, TypeError):
            pass
        if attempt + 1 < policy.registry_reads:
            _check_registry_deadline(deadline, policy.registry_delay_seconds, unreadable=path)
            time.sleep(policy.registry_delay_seconds)
    if path.stem.isdigit():
        try:
            os.kill(int(path.stem), 0)
        except (ProcessLookupError, OverflowError):
            return RegistryRecordGone()
        except OSError:
            pass  # A denied process probe does not prove absence.
    return UnreadableSessionRecord.FOUND


def read_session(path: Path) -> RegistryEntry:
    observation = _read_observation(path, retry_policy())
    return observation.record if isinstance(observation, RegistryObservation) else UnreadableSessionRecord.FOUND


def headless_process(pid: int, entrypoint: str = "") -> bool:
    """Exclude SDK and relay processes even before a session record exists."""
    return (entrypoint.startswith(HEADLESS_ENTRYPOINT_PREFIX)
            or isinstance(live_relay_identity(pid, relay_senders_directory()), RelayIdentity))


def headless(record: SessionRecord) -> bool:
    """A `claude -p` run, or a process identified by the relay launcher's PID record.

    It is no session: no roster lists it, and no name lookup finds it.
    """
    return headless_process(record["pid"], record.get("entrypoint", ""))


def running_session(record: SessionRecord) -> bool:
    """The pid runs with its recorded start, or an older record has its socket. A headless run never counts."""
    if headless(record):
        return False
    try:
        os.kill(record["pid"], 0)
    except PermissionError:
        pass
    except (ProcessLookupError, OverflowError):
        return False
    recorded_start = record.get("procStart", "")
    if recorded_start:
        return recorded_start == process_start(record["pid"])
    return messaging_socket_exists(record)


def messaging_socket_exists(record: SessionRecord) -> bool:
    try:
        return stat.S_ISSOCK(Path(record["messagingSocketPath"]).stat().st_mode)
    except (OSError, ValueError, TypeError):
        return False


def sessions_dir() -> Path:
    return sessions_directory()


@dataclass(frozen=True)
class UnverifiedSessionStart:
    """The session has no verified process start."""


@dataclass(frozen=True)
class LiveSessionProcess:
    """The registry or saved process identity positively identifies a live session."""

    pid: int
    # Keep equality by PID for callers that only use the live PID.
    started: str | UnverifiedSessionStart = field(default=UnverifiedSessionStart(), compare=False)


@dataclass(frozen=True)
class SessionNotLive:
    """The available registry or process identity establishes no live session."""


@dataclass(frozen=True)
class SessionLivenessUnknown:
    """Unreadable evidence prevents establishing whether the session is live."""

    reason: str
    unreadable_registry: tuple[Path, ...] = field(default=(), compare=False, repr=False)


def remote_control(record: SessionRecord) -> Literal["active", "absent"]:
    """Read only this confirmed session's Remote Control registry field."""
    try:
        value = cast(object, json.loads((sessions_dir() / f"{record['pid']}.json").read_text()))
        if isinstance(value, dict):
            bridge = cast(dict[str, object], value).get("bridgeSessionId")
            if isinstance(bridge, str) and bridge:
                return "active"
    except (OSError, ValueError, TypeError):
        pass
    return "absent"


def process_liveness(pid: int, recorded_start: str) -> LiveSessionProcess | SessionNotLive | SessionLivenessUnknown:
    """Check the saved process identity without treating unreadable evidence as absence."""
    if not recorded_start:
        return SessionNotLive()
    current = process_start(pid)
    if current is not None and current != recorded_start:
        return SessionNotLive()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return SessionNotLive()
    except PermissionError:
        if current == recorded_start:
            return LiveSessionProcess(pid, recorded_start)
    except (OSError, OverflowError) as error:
        return SessionLivenessUnknown(f"process identity unreadable: {error}")
    if current is None:
        return SessionLivenessUnknown(f"process identity unreadable for pid {pid}")
    return LiveSessionProcess(pid, recorded_start)


@dataclass(frozen=True)
class SessionProcesses:
    """Every live registry record's pid for a session, from a complete registry read."""

    pids: tuple[int, ...]


def _session_observations(session_id: str, registry: RegistryRead) -> Iterator[LiveSessionProcess | SessionLivenessUnknown]:
    for observation in registry.entries:
        registered = observation.record
        if registered["sessionId"] != session_id:
            continue
        if running_session(registered):
            yield LiveSessionProcess(registered["pid"], registered.get("procStart") or UnverifiedSessionStart())
        else:
            observed = process_liveness(registered["pid"], registered.get("procStart", ""))
            if not isinstance(observed, SessionNotLive):
                yield observed
    if registry.unreadable:
        yield SessionLivenessUnknown(registry_unreadable_reason(registry.unreadable), registry.unreadable)


def session_liveness(session_id: str, registry: RegistryRead | None = None, *, deadline: float = math.inf,
                     ) -> LiveSessionProcess | SessionNotLive | SessionLivenessUnknown:
    """Find a running session and its PID within the registry read deadline.

    Report unknown liveness if the registry or process identity is unreadable.
    Pass one registry read to check multiple sessions."""
    reading = _liveness_registry.get() if registry is None else registry
    if isinstance(reading, ReadCurrentRegistry):
        try:
            reading = read_registry() if deadline == math.inf else read_registry(deadline=deadline)
        except RegistryReadExpired as error:
            observed = next(_session_observations(session_id, error.registry), None)
            if observed is None:
                raise
            return observed
    return next(_session_observations(session_id, reading), SessionNotLive())


def session_processes(session_id: str) -> SessionProcesses | SessionLivenessUnknown:
    """Count all registered processes; incomplete evidence cannot prove a unique process."""
    pids: list[int] = []
    for observed in _session_observations(session_id, read_registry()):
        if isinstance(observed, SessionLivenessUnknown):
            return observed
        pids.append(observed.pid)
    return SessionProcesses(tuple(sorted(pids)))


def live_session(record: SessionRecord) -> bool:
    return running_session(record) and messaging_socket_exists(record)


def _matching_records(records: Iterable[SessionRecord], matches: Callable[[SessionRecord], bool]) -> list[SessionRecord]:
    return sorted((record for record in records if matches(record)),
                  key=lambda record: record["updatedAt"], reverse=True)


def running_records(registry: RegistryRead) -> list[SessionRecord]:
    """Every running record of one registry read, newest first, including processes without sockets."""
    return _matching_records((item.record for item in registry.entries), running_session)


@dataclass(frozen=True)
class RunningSessionEnumeration:
    """Keep the observed running sessions and the paths that prevent a complete read."""

    records: tuple[SessionRecord, ...]
    unreadable: tuple[Path, ...]


@dataclass(frozen=True)
class RunningSessionDiscoveryFailed:
    """An error prevented enumeration of running sessions."""

    reason: str


def enumerate_running_sessions(*, registry: RegistryRead | ReadCurrentRegistry = CURRENT_REGISTRY
                               ) -> RunningSessionEnumeration | RunningSessionDiscoveryFailed:
    try:
        if isinstance(registry, ReadCurrentRegistry):
            registry = read_registry()
        return RunningSessionEnumeration(tuple(running_records(registry)), registry.unreadable)
    except (OSError, ValueError) as error:
        return RunningSessionDiscoveryFailed(f"session registry discovery failed: {error}")


def running_sessions(*, registry: RegistryRead | ReadCurrentRegistry = CURRENT_REGISTRY) -> list[SessionRecord]:
    """Return observed running records, newest first."""
    return running_records(read_registry() if isinstance(registry, ReadCurrentRegistry) else registry)


def live_sessions() -> list[SessionRecord]:
    """Return observed running records with a messaging socket, newest first."""
    return _matching_records((item.record for item in read_registry().entries), live_session)


def _claude_project_roots() -> Iterator[Path]:
    return (directory / "projects" for directory in config_dirs())


def transcript_locations(session_id: str) -> list[Path]:
    """Return distinct transcript paths across the Claude accounts."""
    return sorted({path.resolve() for projects in _claude_project_roots()
                   for path in transcript_paths(projects, f"*/{session_id}.jsonl")})


def transcript_session_ids(wanted: Set[str]) -> set[str]:
    """Find Claude transcripts for the requested sessions with one scan per root."""
    if len(wanted) <= 1:
        return {identity for identity in wanted if transcript_locations(identity)}
    found: set[str] = set()
    scanned: set[Path] = set()
    for root in _claude_project_roots():
        try:
            projects = root.resolve()
        except (OSError, RuntimeError):
            # Python 3.12 raises RuntimeError for a symlink loop.
            continue
        if projects in scanned:
            continue
        scanned.add(projects)
        try:
            for path in projects.glob("*/*.jsonl*"):
                logical = uncompressed_path(path)
                if logical.suffix == ".jsonl" and logical.stem in wanted:
                    found.add(logical.stem)
        except OSError:
            continue
    return found


def latest_transcript(session_id: str, projects: Path) -> Path | None:
    """The session's transcript under the Claude projects folder: the newest one, when a resume left several."""
    transcripts = sorted(transcript_paths(projects, f"*/{session_id}.jsonl"), key=lambda path: path.stat().st_mtime)
    return transcripts[-1] if transcripts else None

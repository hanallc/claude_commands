"""Compare running systems with main and cache the last rebuild result."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import os
import re
import sys
import syslog
import time
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Protocol, cast
from urllib.parse import quote

from scripts.lib import atomic_file, conf_reads
from scripts.rebuild import need

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config/statusline.conf"
MACHINES = ("natedev", "mac")
CHANGE_LIMIT = 5
SHORT_REASON_LIMIT = 60
CHANGES_NOT_READ = "Changes not read yet"
REASON_NOT_READ = "reason not read"


def rebuild_names(machines: Iterable[str], reasons: Mapping[str, str] | None = None) -> str:
    """Name each waiting machine and show its short reason when supplied."""
    waiting = set(machines)
    labels: list[str] = []
    for machine in MACHINES:
        if machine not in waiting:
            continue
        label = f"rebuild {machine}"
        reason = (reasons.get(machine) or REASON_NOT_READ) if reasons is not None else ""
        labels.append(f"{label}: {reason}" if reason else label)
    return ", ".join(labels)


def short_reason(reason: str, more: int = 0) -> str:
    """Fit a reason and its remaining count in the status line."""
    words = " ".join("".join(char for char in reason if char.isprintable() or char.isspace()).split())
    suffix = f" +{more}" if more > 0 else ""
    limit = SHORT_REASON_LIMIT - len(suffix)
    if len(words) > limit:
        words = words[:limit - 1].rstrip() + "…"
    return words + suffix


@dataclass(frozen=True)
class RebuildSettings:
    """The configured source, machines, refresh interval, and evaluation time limit."""

    repo: Path
    ref: str
    natedev: str
    mac: str
    ssh: str
    interval: int
    eval_timeout: int
    eval_retry_seconds: int

    @classmethod
    def read(cls, path: Path) -> RebuildSettings:
        values = config_values(path)
        settings = cls(Path(values["rebuild.repo"]), values["rebuild.ref"],
                       values["rebuild.natedev"], values["rebuild.mac"],
                       values["rebuild.ssh"], int(values["rebuild.interval"]), int(values["rebuild.eval_timeout"]),
                       int(values["rebuild.eval_retry_seconds"]))
        if (not settings.repo.is_absolute() or settings.interval <= 0
                or settings.eval_timeout <= 0 or settings.eval_retry_seconds <= 0):
            raise ValueError("Invalid rebuild settings")
        if not all((settings.ref, settings.natedev, settings.mac, settings.ssh)):
            raise ValueError("Empty rebuild settings")
        return settings


class CommandRunner(Protocol):
    """Read a command's output within its time limit."""

    def __call__(self, command: list[str], *, timeout: int = 30) -> str: ...


command_output = need.command_output
system_path = need.system_path


@dataclass(frozen=True)
class NoExpectedSystems:
    """No evaluation result exists for reuse."""


@dataclass(frozen=True)
class ExpectedSystems:
    """The expected system paths from one commit and evaluation source."""

    commit: str
    source: tuple[str, ...]
    natedev: str
    mac: str

    def path_for(self, machine: str) -> str:
        return self.mac if machine == "mac" else self.natedev

    def details(self) -> str:
        return json.dumps({"commit": self.commit, "source": self.source,
                           "natedev": self.natedev, "mac": self.mac})


@dataclass(frozen=True)
class IncompleteExpectedSystems:
    """The configured revision when one or more cached evaluations are invalid."""

    commit: str
    source: tuple[str, ...]

    def details(self) -> str:
        return json.dumps({"commit": self.commit, "source": self.source})


@dataclass(frozen=True)
class NoEvaluationFailure:
    """No failed evaluation requires a retry delay."""


@dataclass(frozen=True)
class EvaluationFailure:
    """The failed evaluation and the machines that succeeded for the same source."""

    commit: str
    source: tuple[str, ...]
    failed_at: float
    evaluated_machines: tuple[str, ...] = ()

    def details(self) -> str:
        return json.dumps({"commit": self.commit, "source": self.source, "failed_at": self.failed_at,
                           "evaluated_machines": self.evaluated_machines})


class RunningProbeOutcome(Enum):
    """Say whether the probe ran and returned a running system path."""

    SUCCEEDED = True
    FAILED = False
    DID_NOT_RUN = None

    @classmethod
    def decode(cls, value: object) -> RunningProbeOutcome:
        if value is True:
            return cls.SUCCEEDED
        if value is False:
            return cls.FAILED
        return cls.DID_NOT_RUN


@dataclass(frozen=True)
class RunningSystemProbes:
    """Keep one probe outcome for each machine."""

    natedev: RunningProbeOutcome
    mac: RunningProbeOutcome

    def __init__(self, natedev: RunningProbeOutcome | bool = RunningProbeOutcome.DID_NOT_RUN,
                 mac: RunningProbeOutcome | bool = RunningProbeOutcome.DID_NOT_RUN) -> None:
        object.__setattr__(self, "natedev", RunningProbeOutcome.decode(natedev) if isinstance(natedev, bool) else natedev)
        object.__setattr__(self, "mac", RunningProbeOutcome.decode(mac) if isinstance(mac, bool) else mac)

    def outcome(self, machine: str) -> RunningProbeOutcome:
        return {"natedev": self.natedev, "mac": self.mac}.get(machine, RunningProbeOutcome.DID_NOT_RUN)

    def succeeded(self, machine: str) -> bool:
        return self.outcome(machine) is RunningProbeOutcome.SUCCEEDED

    def details(self) -> dict[str, bool | None]:
        return {machine: self.outcome(machine).value for machine in MACHINES}

    @classmethod
    def from_attempted_probes(cls, observations: Mapping[str, ConfirmedRunningSystem]) -> RunningSystemProbes:
        return cls("natedev" in observations, "mac" in observations)

    @classmethod
    def decode(cls, value: object) -> RunningSystemProbes:
        if not isinstance(value, dict):
            return cls()
        fields = cast(dict[str, object], value)
        return cls(RunningProbeOutcome.decode(fields.get("natedev")), RunningProbeOutcome.decode(fields.get("mac")))


def _timestamp(value: object) -> float | None:
    if type(value) in (int, float):
        number = cast(int | float, value)
        if 0 <= number < float("inf"):
            return number
    return None


@dataclass(frozen=True)
class EvaluatedSystemPath:
    """The last expected path and the revision that produced it."""

    path: str
    evaluated_at: float
    revision: str

    def details(self) -> dict[str, str | float]:
        return {"path": self.path, "evaluated_at": self.evaluated_at, "revision": self.revision}

    @classmethod
    def decode(cls, value: object) -> EvaluatedSystemPath | None:
        if not isinstance(value, dict):
            return None
        fields = cast(dict[str, object], value)
        path, revision = fields.get("path"), fields.get("revision")
        at = _timestamp(fields.get("evaluated_at"))
        if (not isinstance(path, str) or not system_path(path) or at is None
                or not isinstance(revision, str) or not REVISION.fullmatch(revision)):
            return None
        return cls(path, at, revision)


@dataclass(frozen=True)
class ConfirmedRunningSystem:
    """The running system path from the last successful probe."""

    path: str
    observed_at: float

    @classmethod
    def decode(cls, value: object) -> ConfirmedRunningSystem | None:
        if not isinstance(value, dict):
            return None
        fields = cast(dict[str, object], value)
        path, at = fields.get("path"), _timestamp(fields.get("observed_at"))
        if not isinstance(path, str) or not system_path(path) or at is None:
            return None
        return cls(path, at)


@dataclass(frozen=True)
class RebuildComparison:
    """Keep the status text and the running systems that answered."""

    row: str
    probes: RunningSystemProbes
    observations: dict[str, ConfirmedRunningSystem]

    @property
    def running_paths(self) -> dict[str, str]:
        return {machine: observed.path for machine, observed in self.observations.items()}


def _report_comparison_failure(message: str) -> None:
    print(message, file=sys.stderr)


def _probe_running_systems(settings: RebuildSettings, run: Callable[[list[str]], str],
                           clock: Callable[[], float],
                           report_failure: Callable[[str], None]) -> dict[str, ConfirmedRunningSystem]:
    observations: dict[str, ConfirmedRunningSystem] = {}
    for machine, command in (
        ("natedev", ["realpath", "/run/current-system"]),
        ("mac", ["ssh", "-o", "BatchMode=yes", settings.ssh, "realpath /run/current-system"]),
    ):
        try:
            running = run(command)
        except (OSError, ValueError):
            running = ""
        if system_path(running):
            observations[machine] = ConfirmedRunningSystem(running, clock())
        else:
            report_failure(f"rebuild comparison: {machine}: The running system probe failed.")
    return observations


def compare(settings: RebuildSettings, expected: ExpectedSystems | NoExpectedSystems,
            run: Callable[[list[str]], str] = command_output, *,
            clock: Callable[[], float] = time.time) -> RebuildComparison:
    """Compare running systems and retain failed probes as unavailable."""
    observations = _probe_running_systems(settings, run, clock, _report_comparison_failure)
    probes = RunningSystemProbes.from_attempted_probes(observations)
    paths = {machine: EvaluatedSystemPath(path, 0, expected.commit) for machine in MACHINES
             if isinstance(expected, ExpectedSystems) and system_path(path := expected.path_for(machine))}
    answer = RebuildAnswer((), False, expected, probes=probes, evaluated_system_paths=paths,
                           running_paths={machine: observed.path for machine, observed in observations.items()})
    return RebuildComparison(_project_comparison(answer).row(), probes, observations)


def row(settings: RebuildSettings, expected: ExpectedSystems | NoExpectedSystems,
        run: Callable[[list[str]], str] = command_output) -> str:
    """Return the existing status text for the current system comparison."""
    return compare(settings, expected, run).row


type OpenRebuildNeeds = tuple[need.MachineNeeds, ...]


@dataclass(frozen=True)
class RebuildNeedsNotRead:
    """The open rebuild records have not been read."""

    def __iter__(self) -> Iterator[need.MachineNeeds]:
        return iter(())


@dataclass(frozen=True)
class RebuildNeedsUnavailable:
    """The folder read failed before it could return requests."""

    reason: str

    def __iter__(self) -> Iterator[need.MachineNeeds]:
        return iter(())


@dataclass(frozen=True)
class PartialRebuildNeeds:
    """The readable requests from a folder with one or more damaged records."""

    reason: str
    summaries: OpenRebuildNeeds

    def __iter__(self) -> Iterator[need.MachineNeeds]:
        return iter(self.summaries)


type RebuildNeedsRead = OpenRebuildNeeds | RebuildNeedsNotRead | RebuildNeedsUnavailable | PartialRebuildNeeds


@dataclass(frozen=True)
class RebuildAnswer:
    """The known rebuild machines and whether the cache needs a refresh.

    `compared` holds the machines whose running system differs from main;
    `needs` holds the open rebuild records or the reason they are unavailable.
    `probes` records the outcome of each machine's latest probe.
    `confirmed_systems` and `evaluated_system_paths` retain each machine’s last successful reads.
    `comparison_cache_stale` records expiry or a newer configuration.
    """

    machines: tuple[str, ...]
    refresh_due: bool
    expected: ExpectedSystems | IncompleteExpectedSystems | NoExpectedSystems = NoExpectedSystems()
    failure: EvaluationFailure | NoEvaluationFailure = NoEvaluationFailure()
    compared: tuple[str, ...] = ()
    needs: RebuildNeedsRead = RebuildNeedsNotRead()
    probes: RunningSystemProbes = RunningSystemProbes()
    running_paths: dict[str, str] = field(default_factory=dict)
    comparison_deadline: int = 0
    configured_subject: str = ""
    confirmed_systems: dict[str, ConfirmedRunningSystem] = field(default_factory=dict)
    evaluated_system_paths: dict[str, EvaluatedSystemPath] = field(default_factory=dict)
    unreadable_comparisons: frozenset[str] = frozenset()
    saved_running_paths: dict[str, str] = field(default_factory=dict)
    unavailable_observed_needs: frozenset[str] = frozenset()
    comparison_cache_stale: bool = False

    def row(self) -> str:
        return rebuild_names(self.machines)

    def configured_revision(self) -> str:
        if not isinstance(self.expected, NoExpectedSystems):
            return self.expected.commit
        return self.failure.commit if isinstance(self.failure, EvaluationFailure) else ""

    def current_expected_path(self, machine: str) -> str:
        revision = self.configured_revision()
        path = self.evaluated_system_paths.get(machine)
        return path.path if revision and path is not None and path.revision == revision else ""


class ComparisonEvidence(Enum):
    CURRENT = "current"
    SAVED = "saved"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class MachineRebuildNeed:
    needed: bool
    evidence: ComparisonEvidence


def machine_rebuild_need(answer: RebuildAnswer, machine: str, *, observed_need: bool = False) -> MachineRebuildNeed:
    """Clear a need only when a successful probe matches the current expected path."""
    if machine in answer.unreadable_comparisons:
        return MachineRebuildNeed(False, ComparisonEvidence.UNKNOWN)
    expected = answer.current_expected_path(machine)
    probed_path = answer.running_paths.get(machine, "") if answer.probes.succeeded(machine) else ""
    observation_unavailable = machine in answer.unavailable_observed_needs
    if expected and probed_path == expected:
        evidence = ComparisonEvidence.UNKNOWN if observation_unavailable else ComparisonEvidence.CURRENT
        return MachineRebuildNeed(False, evidence)

    confirmed = answer.confirmed_systems.get(machine)
    running = probed_path or (confirmed.path if confirmed is not None else answer.saved_running_paths.get(machine, ""))
    kept = observed_need or machine in answer.compared
    if not expected or not running:
        return MachineRebuildNeed(kept, ComparisonEvidence.UNKNOWN)
    evidence = ComparisonEvidence.CURRENT if probed_path else ComparisonEvidence.SAVED
    return MachineRebuildNeed(kept or running != expected,
                              ComparisonEvidence.UNKNOWN if observation_unavailable else evidence)


def config_values(path: Path) -> Mapping[str, str]:
    values: dict[str, str] = {}
    contents = conf_reads.read_text(path)
    for line in contents.splitlines():
        key, sep, value = line.partition("#")[0].partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return conf_reads.trace_values(path, contents, values)


def cache_path(config: Path) -> Path:
    configured = config_values(config)["rebuild.cache"]
    try:
        path = Path(configured).expanduser()
    except RuntimeError as error:
        raise ValueError(f"Cannot expand the rebuild cache path {configured!r}.") from error
    if not path.is_absolute():
        raise ValueError(f"The rebuild cache path must be absolute: {configured!r}")
    return path


@dataclass(frozen=True)
class Change:
    """One /etc/nixos commit that a machine's next rebuild applies.

    `session` is the local session that wrote it, from the commit's Claude-Session
    line; it is empty when that line is absent or names no known transcript.
    """

    commit: str
    at: str
    reason: str
    session: str


@dataclass(frozen=True)
class ComparedChanges:
    """The cached changes for one running system and configured revision."""

    running_path: str
    active_revision: str
    configured_revision: str
    commits: tuple[Change, ...]
    paths: tuple[str, ...] = ()
    paths_between_revisions: bool = False
    legacy_commits: dict[str, str] = field(default_factory=dict)


def commits_with_subjects(changes: ComparedChanges | None) -> tuple[Change, ...]:
    """Keep cached commits with displayable subjects."""
    return tuple(change for change in changes.commits if change.reason.strip()) if changes else ()


def more_changes(total: int) -> str:
    """Label the changes beyond the page limit."""
    return f"+{total - CHANGE_LIMIT} more" if total > CHANGE_LIMIT else ""


def comparison_reason(changes: ComparedChanges | None, *, configured_subject: str = "", compact: bool = False) -> str:
    """Name pending subjects or changed paths."""
    subjects = tuple(change.reason for change in commits_with_subjects(changes))
    entries = subjects or (changes.paths if changes else ())
    if not entries:
        if changes is not None and changes.paths_between_revisions:
            if changes.active_revision.endswith("-dirty"):
                return "The running build has local changes."
            return "The two revisions have the same files."
        reason = f"{CHANGES_NOT_READ} · {configured_subject}" if configured_subject else CHANGES_NOT_READ
        return short_reason(reason) if compact else reason
    prefix = ""
    if not subjects:
        compared_paths = changes is not None and changes.paths_between_revisions
        prefix = ("Files changed between revisions: " if compared_paths
                  else "Files changed in the configured commit: ")
    if compact:
        return short_reason(prefix + entries[0], len(entries) - 1)
    shown = entries[:CHANGE_LIMIT]
    more = more_changes(len(entries))
    return prefix + " · ".join((*shown, more) if more else shown)


@dataclass(frozen=True)
class RebuildChangesDisplay:
    """The commits and remaining count for a rebuild page."""

    commits: tuple[Change, ...] = ()
    more: str = ""
    fallback: str = ""

    def text(self) -> str:
        if not self.commits:
            return self.fallback
        subjects = tuple(commit.reason for commit in self.commits)
        return " · ".join((*subjects, self.more) if self.more else subjects)


def changes_for_display(answer: RebuildAnswer, machine: str, changes: ComparedChanges | None,
                        records: tuple[need.NeedRecord, ...]) -> RebuildChangesDisplay:
    """Show cached subjects for commits without a filed reason."""
    legacy_commits = changes.legacy_commits if changes else {}
    filed = {legacy_commits.get(record["nixos_commit"], record["nixos_commit"])
             for record in records if record["reason"].strip()}
    subjects = commits_with_subjects(changes)
    commits = tuple(change for change in subjects if change.commit not in filed)
    if commits:
        return RebuildChangesDisplay(commits[:CHANGE_LIMIT], more_changes(len(commits)))
    if not subjects and (not records or (machine in answer.compared and any(
            not record["reason"].strip() for record in records))):
        subject = answer.configured_subject if answer.configured_revision() not in filed else ""
        return RebuildChangesDisplay(fallback=comparison_reason(changes, configured_subject=subject))
    return RebuildChangesDisplay()


type CachedChanges = ComparedChanges | tuple[Change, ...]


BRIDGE = re.compile(r"https://claude\.ai/code/(session_[0-9A-Za-z]+)")
REVISION = need.REVISION


def changes_path(cache: Path) -> Path:
    return cache.with_name(cache.name + "-changes.json")


def sessions_path(cache: Path) -> Path:
    return cache.with_name(cache.name + "-sessions.json")


def transcript_session(bridge: str, run: CommandRunner) -> str:
    """Find the local session whose transcript records the Remote Control session `bridge`."""
    from scripts.message import session_records

    roots = sorted({path.resolve() for path in (folder / "projects" for folder in session_records.config_dirs())
                    if path.is_dir()})
    if not roots:
        return ""
    marker = f'"type":"remote_session_change","url":"https://claude.ai/code/{bridge}"'
    found = run(["rg", "--no-ignore", "--files-with-matches", "--fixed-strings", "--glob", "*.jsonl", marker,
                 *map(str, roots)], timeout=120)
    # A subagent transcript sits one folder deeper; only a session transcript names the session.
    sessions = sorted(Path(line).stem for line in found.splitlines() if Path(line).parent.parent in roots)
    return sessions[0] if sessions else ""


def machine_changes(settings: RebuildSettings, system: need.ActiveSystem, known: dict[str, str],
                    run: CommandRunner, git_output: Callable[[list[str]], str | None], *,
                    configured: str, compared: bool = False) -> ComparedChanges:
    """List pending commits or changed paths."""
    commit = system.revision.removesuffix("-dirty")
    git = ["git", "-C", str(settings.repo)]
    listing = None
    if commit == configured and configured:
        return ComparedChanges(system.path, system.revision, configured, (), paths_between_revisions=True)
    if commit:
        listing = git_output([*git, "log",
                              "--format=%H%x1f%aI%x1f%s%x1f%(trailers:key=Claude-Session,valueonly,separator=%x20)",
                              f"{commit}..{configured or settings.ref}"])
        if listing == "" and configured:
            names = git_output([*git, "diff", "--no-ext-diff", "--name-only", "-z", commit, configured, "--"])
            if names is not None:
                paths = tuple(name for name in names.split("\0") if name)
                return ComparedChanges(system.path, system.revision, configured, (), paths, paths_between_revisions=True)
            listing = None
    if listing is None and configured and (compared or commit):
        names = git_output([*git, "show", "--format=", "--diff-merges=first-parent", "--name-only", "-z", configured])
        paths = tuple(name for name in (names or "").split("\0") if name)
        return ComparedChanges(system.path, system.revision, configured, (), paths)
    changes: list[Change] = []
    for line in (listing or "").splitlines():
        fields = line.split("\x1f")
        if len(fields) != 4:
            continue
        bridge = BRIDGE.search(fields[3])
        if bridge is not None and bridge[1] not in known:
            known[bridge[1]] = transcript_session(bridge[1], run)
        changes.append(Change(fields[0], fields[1], fields[2], known[bridge[1]] if bridge is not None else ""))
    return ComparedChanges(system.path, system.revision, configured, tuple(changes))


def write_changes(config: Path, run: CommandRunner | None = None) -> None:
    """Record pending changes and the session that wrote each commit."""
    output = command_output if run is None else run

    def git_output(command: list[str]) -> str | None:
        # Existing output runners use empty text for failure.
        return need.command_result(command) if run is None else run(command) or None

    try:
        settings = RebuildSettings.read(config)
        cache = cache_path(config)
        log = config_values(config)["rebuild.generations"]
    except (OSError, ValueError, KeyError):
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    with changes_path(cache).with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        try:
            known = cast(dict[str, str], json.loads(sessions_path(cache).read_text(encoding="utf-8")))
        except (OSError, ValueError):
            known = {}
        # A machine that does not answer keeps its last list.
        previous = _read_changes(cache)
        answer = read(config)
        configured = answer.configured_revision()
        prefixes = {commit for summary in answer.needs for record in summary.records
                    if need.SHORT_COMMIT.fullmatch(commit := record["nixos_commit"])}
        legacy_commits = {prefix: commit for prefix in sorted(prefixes)
                          if (commit := need.resolve_legacy_commit(prefix, settings.repo, output))}
        machines: dict[str, object] = {}
        for machine in MACHINES:
            system = need.active_system(output(need.active_probe(machine, log=log, ssh=settings.ssh)))
            if system is None:
                changes = previous.get(machine)
            else:
                try:
                    need.clear_running_needs(machine, system, settings.repo, output, legacy_commits=legacy_commits)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    print(f"rebuild needs: {error}", file=sys.stderr)
                changes = machine_changes(settings, system, known, output, git_output, configured=configured,
                                          compared=machine in answer.compared
                                          and system.path == answer.running_paths.get(machine))
            if isinstance(changes, ComparedChanges):
                machines[machine] = asdict(replace(changes, legacy_commits=legacy_commits))
            elif changes is not None:
                machines[machine] = [asdict(change) for change in changes]
        atomic_file.write_text_atomic(sessions_path(cache), json.dumps(known))
        with _lock_observations(cache):
            atomic_file.write_text_atomic(changes_path(cache), json.dumps(machines))


def _decode_commits(value: object) -> tuple[Change, ...]:
    if not isinstance(value, list):
        raise ValueError("Expected a list of commits.")
    commits: list[Change] = []
    for item in cast(list[object], value):
        if not isinstance(item, dict) or not all(isinstance(text, str) for text in cast(dict[str, object], item).values()):
            raise ValueError("Expected text in each commit.")
        commits.append(Change(**cast(dict[str, str], item)))
    return tuple(commits)


def _decode_recorded_changes(value: object) -> CachedChanges:
    if not isinstance(value, dict):
        return _decode_commits(value)
    fields = cast(dict[str, object], value)
    if "active_revision" not in fields:
        # Older caches did not verify the active generation.
        return _decode_commits(fields["commits"])
    running_path, running, configured = fields["running_path"], fields["active_revision"], fields["configured_revision"]
    if (not isinstance(running_path, str) or not system_path(running_path)
            or not isinstance(running, str) or (running and not REVISION.fullmatch(running.removesuffix("-dirty")))
            or not isinstance(configured, str) or (configured and not REVISION.fullmatch(configured))):
        raise ValueError("Invalid running path or revision.")
    paths = fields.get("paths", [])
    if not isinstance(paths, list) or not all(isinstance(name, str) and name for name in cast(list[object], paths)):
        raise ValueError("Invalid list of changed paths.")
    compared_paths = fields.get("paths_between_revisions", False)
    aliases = fields.get("legacy_commits", {})
    if not isinstance(compared_paths, bool) or not isinstance(aliases, dict):
        raise ValueError("Invalid comparison details.")
    legacy_commits = {prefix: commit for prefix, commit in cast(dict[str, object], aliases).items()
                      if need.SHORT_COMMIT.fullmatch(prefix) and isinstance(commit, str)
                      and need.FULL_NEED_COMMIT.fullmatch(commit) and commit.startswith(prefix)}
    return ComparedChanges(running_path, running, configured, _decode_commits(fields["commits"]),
                           tuple(cast(list[str], paths)), compared_paths, legacy_commits)


def _read_changes(cache: Path, watch: need.SourceWatch | None = None) -> dict[str, CachedChanges]:
    path = changes_path(cache)
    if watch is not None:
        watch.watch(path)
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(value, dict):
            return {}
        result: dict[str, CachedChanges] = {}
        for machine, changes in cast(dict[str, object], value).items():
            try:
                result[machine] = _decode_recorded_changes(changes)
            except (ValueError, KeyError, TypeError, RecursionError):
                if watch is not None:
                    watch.mark_incomplete()
        return result
    except FileNotFoundError:
        if watch is not None and not watch.verified_absence(path):
            watch.mark_incomplete()
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        if watch is not None:
            watch.mark_incomplete()
    return {}


def read_recorded_changes(config: Path, *, watch: need.SourceWatch | None = None) -> dict[str, CachedChanges]:
    """Read both the current and legacy forms of the changes cache."""
    try:
        return _read_changes(cache_path(config), watch)
    except (OSError, ValueError, KeyError):
        if watch is not None:
            watch.mark_incomplete()
        return {}


def read_changes(config: Path = DEFAULT_CONFIG) -> dict[str, tuple[Change, ...]]:
    """Read the recorded commit list for each machine."""
    return {machine: changes.commits if isinstance(changes, ComparedChanges) else changes
            for machine, changes in read_recorded_changes(config).items()}


def matched_changes(answer: RebuildAnswer, machine: str, cached: CachedChanges | None) -> ComparedChanges | None:
    """Return changes only if the cache names both systems compared."""
    configured = answer.configured_revision()
    if (isinstance(cached, ComparedChanges) and configured and cached.configured_revision == configured
            and answer.probes.succeeded(machine) and cached.running_path == answer.running_paths.get(machine)):
        return cached
    return None


def _with_saved_running_paths(answer: RebuildAnswer,
                              read_changes: Callable[[], Mapping[str, CachedChanges]]) -> RebuildAnswer:
    """Use a recorded path when the comparison cache has no running path for a machine."""
    missing = tuple(machine for machine in MACHINES if machine not in answer.unreadable_comparisons
                    and machine not in answer.confirmed_systems and machine not in answer.running_paths
                    and machine not in answer.saved_running_paths and machine not in answer.compared
                    and not answer.probes.succeeded(machine) and answer.current_expected_path(machine))
    if not missing:
        return answer
    recorded = read_changes()
    paths = {machine: record.running_path for machine in missing
             if isinstance(record := recorded.get(machine), ComparedChanges)}
    return replace(answer, saved_running_paths=answer.saved_running_paths | paths)


def read_compared_changes(config: Path, answer: RebuildAnswer, *,
                          watch: need.SourceWatch | None = None) -> dict[str, ComparedChanges]:
    """Read change lists that match the two systems compared."""
    if not answer.configured_revision():
        return {}
    return {machine: changes for machine, cached in read_recorded_changes(config, watch=watch).items()
            if (changes := matched_changes(answer, machine, cached)) is not None}


def read(config: Path = DEFAULT_CONFIG, *, clock: Callable[[], float] = time.time,
         watch: need.SourceWatch | None = None) -> RebuildAnswer:
    """Combine the cached comparison with open rebuild needs."""
    answer = _read_cache(config, clock, watch=watch)
    errors: list[str] = []
    try:
        needs = tuple(need.read_needs(errors) if watch is None else need.read_needs(errors, watch=watch))
        machines = set(answer.machines) | {summary.newest["machine"] for summary in needs}
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        return replace(answer, needs=RebuildNeedsUnavailable(str(error)))
    return replace(answer, machines=tuple(machine for machine in MACHINES if machine in machines),
                   needs=PartialRebuildNeeds("\n".join(errors), needs) if errors else needs)


_cache_path_failures: dict[Path, str] = {}
_cache_path_failure_lock = Lock()


def _report_cache_path_failure(config: Path, source: Path, error: Exception) -> None:
    message = f"rebuild cache: {source}: {error}"
    with _cache_path_failure_lock:
        if _cache_path_failures.get(config) != message:
            _cache_path_failures[config] = message
            _report_comparison_failure(message)


def observed_need_path(cache: Path, machine: str) -> Path:
    return cache.with_name(f"{cache.name}-observed-needs-{machine}.json")


@contextmanager
def _observation_lock(path: Path, report_close_failure: Callable[[Exception], None]) -> Generator[None]:
    """Protect updates to one machine's observed need."""
    lock = path.with_suffix(".lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield
    finally:
        try:
            lock.close()
        except (OSError, ValueError) as error:
            report_close_failure(error)


def _report_observation_failure(cache: Path, machine: str, error: Exception,
                                report_failure: Callable[[str], None], watch: need.SourceWatch | None) -> None:
    report_failure(f"rebuild observed needs: {cache}: {machine}: {error}")
    if watch is not None:
        watch.mark_incomplete()


@contextmanager
def _lock_observations(cache: Path, *, report_failure: Callable[[str], None] = _report_comparison_failure,
                       watch: need.SourceWatch | None = None) -> Generator[set[str]]:
    """Lock each machine in order and record failures through release."""
    unavailable: set[str] = set()

    def report_lock_failure(machine: str, error: Exception) -> None:
        unavailable.add(machine)
        _report_observation_failure(cache, machine, error, report_failure, watch)

    with ExitStack() as resources:
        for machine in MACHINES:
            try:
                resources.enter_context(_observation_lock(
                    observed_need_path(cache, machine), partial(report_lock_failure, machine)))
            except (OSError, ValueError) as error:
                report_lock_failure(machine, error)
        yield unavailable


def _read_observed_need(path: Path, watch: need.SourceWatch | None) -> bool:
    if watch is not None:
        watch.watch(path)
    try:
        data = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        if watch is not None and not watch.verified_absence(path):
            raise
        return False
    if data is not True:
        raise ValueError("Invalid observed rebuild need.")
    return True


def _project_comparison(answer: RebuildAnswer) -> RebuildAnswer:
    compared = tuple(machine for machine in MACHINES if machine_rebuild_need(answer, machine).needed)
    return replace(answer, machines=compared, compared=compared)


@dataclass(frozen=True)
class ObservedMachineNeed:
    """One machine's projected need and the availability of its saved observation."""

    needed: bool
    unavailable: bool


def _observe_machine_need(cache: Path, machine: str, answer: RebuildAnswer, *,
                          locked: bool, report_failure: Callable[[str], None],
                          watch: need.SourceWatch | None = None) -> ObservedMachineNeed:
    comparison = machine_rebuild_need(answer, machine)
    try:
        path = observed_need_path(cache, machine)
        observed = _read_observed_need(path, watch)
        comparison = machine_rebuild_need(answer, machine, observed_need=observed)
        if locked and machine not in answer.unreadable_comparisons:
            if observed and not comparison.needed:
                path.unlink()
            elif not observed and comparison.needed and machine not in answer.compared:
                atomic_file.atomic_sample(path, "true")
    except (OSError, ValueError, RecursionError) as error:
        _report_observation_failure(cache, machine, error, report_failure, watch)
        return ObservedMachineNeed(comparison.needed, True)
    return ObservedMachineNeed(comparison.needed, not locked)


def _read_cache(config: Path, clock: Callable[[], float], *, watch: need.SourceWatch | None = None) -> RebuildAnswer:
    try:
        if watch is not None:
            watch.watch(config)
        cache = cache_path(config)
    except (OSError, ValueError, KeyError) as error:
        _report_cache_path_failure(config, config, error)
        return RebuildAnswer((), False)
    with _lock_observations(cache, watch=watch) as unavailable:
        answer = _read_cache_file(config, cache, clock, watch)
        if answer.configured_revision():
            answer = _with_saved_running_paths(answer, lambda: _read_changes(cache, watch))
            compared: list[str] = []
            for machine in MACHINES:
                result = _observe_machine_need(cache, machine, answer, locked=machine not in unavailable,
                                               report_failure=_report_comparison_failure, watch=watch)
                if result.needed:
                    compared.append(machine)
                if result.unavailable:
                    unavailable.add(machine)
            answer = replace(answer, machines=tuple(compared), compared=tuple(compared))
    return replace(answer, unavailable_observed_needs=frozenset(unavailable))


@dataclass(frozen=True)
class DecodedMachineComparison:
    """The validated fields from one machine's cache record."""

    needs_rebuild: bool
    probe: bool | None
    confirmed: ConfirmedRunningSystem | None
    expected: EvaluatedSystemPath | None


class RebuildNeedMarker(Enum):
    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


def _read_need_markers(line: str, cache: Path) -> dict[str, RebuildNeedMarker]:
    markers: dict[str, RebuildNeedMarker] = {}
    if not line:
        return markers
    for marker in line.split(", "):
        words = marker.split(maxsplit=2)
        if len(words) < 2 or words[1] not in MACHINES:
            _report_comparison_failure(f"rebuild cache: {cache}: Unknown machine in need marker {marker!r}.")
            continue
        machine = words[1]
        valid = machine not in markers and marker == f"rebuild {machine}"
        markers[machine] = RebuildNeedMarker.VALID if valid else RebuildNeedMarker.INVALID
    return markers


def _decode_machine_comparison(machine: str, marker: RebuildNeedMarker, *,
                               outcomes: Mapping[str, object], observed: Mapping[str, object],
                               evaluations: Mapping[str, object],
                               requires_expected_path: bool) -> DecodedMachineComparison | None:
    outcome = outcomes.get(machine)
    reading = ConfirmedRunningSystem.decode(observed.get(machine))
    path = EvaluatedSystemPath.decode(evaluations.get(machine))
    if (marker is RebuildNeedMarker.INVALID
            or machine not in outcomes or (outcome is not None and not isinstance(outcome, bool))
            or ((outcome is True or machine in observed) and reading is None)
            or ((machine in evaluations or requires_expected_path) and path is None)):
        return None
    return DecodedMachineComparison(marker is RebuildNeedMarker.VALID, outcome, reading, path)


def _read_cache_file(config: Path, cache: Path, clock: Callable[[], float],
                     watch: need.SourceWatch | None) -> RebuildAnswer:
    """Read the comparison cache without evaluating systems."""
    try:
        if watch is not None:
            watch.watch(cache)
        with cache.open(encoding="utf-8") as stream:
            lines = stream.read().splitlines()
            stamp = os.fstat(stream.fileno())
        config_stamp = config.stat()
    except (OSError, ValueError) as error:
        _report_cache_path_failure(config, cache, error)
        return RebuildAnswer((), True)
    with _cache_path_failure_lock:
        _ = _cache_path_failures.pop(config, None)
    try:
        if len(lines) < 4 or not lines[1].isascii() or not lines[1].isdecimal():
            return RebuildAnswer((), True)
        deadline = int(lines[1])
        cache_stale = deadline <= clock() or config_stamp.st_mtime_ns > stamp.st_mtime_ns
        markers = _read_need_markers(lines[0], cache)
        value = cast(object, json.loads(lines[3]))
        if not isinstance(value, dict):
            return RebuildAnswer((), True)
        details = cast(dict[str, object], value)
        probe_fields = details.get("running_system_probes")
        confirmed = details.get("confirmed_running_systems")
        evaluated = details.get("evaluated_system_paths")
        if not (isinstance(probe_fields, dict) and isinstance(confirmed, dict) and isinstance(evaluated, dict)):
            return RebuildAnswer((), True)
        outcomes = cast(dict[str, object], probe_fields)
        observed = cast(dict[str, object], confirmed)
        evaluations = cast(dict[str, object], evaluated)
        commit, source = details.get("commit"), details.get("source")
        failed_at = _timestamp(details.get("failed_at"))
        current_revision = commit if isinstance(commit, str) and REVISION.fullmatch(commit) else ""
        requires_expected_path = bool(current_revision) and failed_at is None

        valid_outcomes: dict[str, object] = {}
        compared: list[str] = []
        running_paths: dict[str, str] = {}
        confirmed_systems: dict[str, ConfirmedRunningSystem] = {}
        evaluated_system_paths: dict[str, EvaluatedSystemPath] = {}
        for machine in MACHINES:
            record = _decode_machine_comparison(
                machine, markers.get(machine, RebuildNeedMarker.ABSENT), outcomes=outcomes,
                observed=observed, evaluations=evaluations, requires_expected_path=requires_expected_path)
            if record is None:
                continue
            valid_outcomes[machine] = record.probe
            if record.needs_rebuild:
                compared.append(machine)
            if record.confirmed is not None:
                confirmed_systems[machine] = record.confirmed
                if record.probe is True:
                    running_paths[machine] = record.confirmed.path
            if record.expected is not None:
                evaluated_system_paths[machine] = record.expected

        expected: ExpectedSystems | IncompleteExpectedSystems | NoExpectedSystems = NoExpectedSystems()
        failure: EvaluationFailure | NoEvaluationFailure = NoEvaluationFailure()
        configured_subject = ""
        if (current_revision
                and isinstance(source, list) and all(isinstance(item, str) for item in cast(list[object], source))):
            evaluation_source = tuple(cast(list[str], source))
            current = {machine: path.path for machine, path in evaluated_system_paths.items()
                       if path.revision == current_revision}
            subject = details.get("configured_subject")
            if isinstance(subject, str):
                configured_subject = subject
            if failed_at is not None:
                failure = EvaluationFailure(current_revision, evaluation_source, failed_at, tuple(current))
            elif all(machine in current for machine in MACHINES):
                expected = ExpectedSystems(current_revision, evaluation_source, current["natedev"], current["mac"])
            else:
                expected = IncompleteExpectedSystems(current_revision, evaluation_source)
        unreadable = frozenset(MACHINES) - valid_outcomes.keys()
        refresh_due = cache_stale or bool(unreadable) or isinstance(expected, IncompleteExpectedSystems)
        return RebuildAnswer(tuple(compared), refresh_due,
                             expected, failure, tuple(compared),
                             probes=RunningSystemProbes.decode(valid_outcomes), running_paths=running_paths,
                             comparison_deadline=deadline,
                             configured_subject=configured_subject, confirmed_systems=confirmed_systems,
                             evaluated_system_paths=evaluated_system_paths, unreadable_comparisons=unreadable,
                             comparison_cache_stale=cache_stale)
    except (ValueError, RecursionError):
        return RebuildAnswer((), True)


def _watched_source(path: Path) -> Path:
    """The path itself, or for one that does not exist yet its nearest folder that does."""
    while not path.exists() and path.parent != path:
        path = path.parent
    return path


def write_status_cache(cache: Path, line: str, due: str, sources: list[Path], began: int, details: str = "") -> None:
    """Replace the cache with the answer, deadline, source paths, and optional result details."""
    cache.parent.mkdir(parents=True, exist_ok=True)
    scratch = cache.with_name(f".{cache.name}.{os.getpid()}")
    named = dict.fromkeys(str(_watched_source(path)) for path in sources)
    _ = scratch.write_text("\n".join([line, due, *named, *([details] if details else [])]) + "\n", encoding="utf-8")
    os.utime(scratch, ns=(began, began))
    _ = scratch.replace(cache)


def _refresh_expected_systems(settings: RebuildSettings, answer: RebuildAnswer, commit: str,
                              paths: dict[str, EvaluatedSystemPath], run: CommandRunner,
                              clock: Callable[[], float],
                              report_failure: Callable[[str], None]) -> ExpectedSystems | EvaluationFailure:
    """Evaluate each missing path and save the successful paths with their own times."""
    source = (str(settings.repo), settings.ref, settings.natedev, settings.mac)
    previous = answer.expected
    if isinstance(previous, ExpectedSystems) and previous.commit == commit and previous.source == source:
        return previous
    current: dict[str, EvaluatedSystemPath] = {}
    failure = answer.failure
    previous_source = failure.source if isinstance(failure, EvaluationFailure) else (
        previous.source if not isinstance(previous, NoExpectedSystems) else source
    )
    if previous_source != source:
        paths.clear()
    if isinstance(failure, EvaluationFailure) and failure.commit == commit and failure.source == source:
        if clock() < failure.failed_at + settings.eval_retry_seconds:
            return failure
        current = {machine: path for machine, path in paths.items() if path.revision == commit}
    elif isinstance(previous, IncompleteExpectedSystems) and previous.commit == commit and previous.source == source:
        current = {machine: path for machine, path in paths.items() if path.revision == commit}

    flake = f"git+file://{quote(str(settings.repo))}?rev={commit}"
    attributes = {
        "natedev": f"nixosConfigurations.{json.dumps(settings.natedev)}.config.system.build.toplevel.outPath",
        "mac": f"darwinConfigurations.{json.dumps(settings.mac)}.system.outPath",
    }
    for machine, attribute in attributes.items():
        if machine in current:
            continue
        expression = f"(builtins.getFlake {json.dumps(flake)}).{attribute}"
        try:
            output = run(["nix", "eval", "--json", "--no-write-lock-file", "--expr", expression],
                         timeout=settings.eval_timeout)
            path = cast(object, json.loads(output))
        except (OSError, ValueError, RecursionError):
            path = ""
        if isinstance(path, str) and system_path(path):
            current[machine] = EvaluatedSystemPath(path, clock(), commit)
        else:
            report_failure(f"rebuild comparison: {machine}: The expected system evaluation failed.")
    paths.update(current)
    if "natedev" in current and "mac" in current:
        return ExpectedSystems(commit, source, current["natedev"].path, current["mac"].path)
    return EvaluationFailure(commit, source, clock(), tuple(current))


def write(config: Path, run: CommandRunner = command_output, *, clock: Callable[[], float] = time.time,
          report_failure: Callable[[str], None] = _report_comparison_failure) -> None:
    """Refresh the configured cache without concurrent evaluations."""
    try:
        cache = cache_path(config)
    except (OSError, ValueError, KeyError):
        return
    cache.parent.mkdir(parents=True, exist_ok=True)
    with cache.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        answer = _read_cache_file(config, cache, clock, None)
        if not answer.refresh_due:
            return
        began = time.time_ns()
        due = ""
        evaluation = answer.failure if isinstance(answer.failure, EvaluationFailure) else answer.expected
        subject = answer.configured_subject
        probes = RunningSystemProbes()
        observed: dict[str, ConfirmedRunningSystem] = {}
        unreadable: frozenset[str] = answer.unreadable_comparisons
        confirmed_systems = answer.confirmed_systems.copy()
        evaluated_system_paths = answer.evaluated_system_paths.copy()
        try:
            settings = RebuildSettings.read(config)
            resolved = run(["git", "-C", str(settings.repo), "show", "-s", "--format=%H%x00%s",
                            "--end-of-options", f"{settings.ref}^{{commit}}"])
            commit, _, resolved_subject = resolved.partition("\0")
            if REVISION.fullmatch(commit):
                evaluation = _refresh_expected_systems(
                    settings, answer, commit, evaluated_system_paths, run, clock, report_failure)
                if evaluation is not answer.failure:
                    subject = resolved_subject
                observed = _probe_running_systems(settings, run, clock, report_failure)
                confirmed_systems.update(observed)
                probes = RunningSystemProbes.from_attempted_probes(observed)
                unreadable = frozenset()
            due = str(int(clock()) + settings.interval)
        except (OSError, ValueError, KeyError):
            pass
        details = evaluation.details() if not isinstance(evaluation, NoExpectedSystems) else ""
        fields = cast(dict[str, object], json.loads(details)) if details else {}
        fields["configured_subject"] = subject
        fields["running_system_probes"] = probes.details()
        fields["confirmed_running_systems"] = {machine: asdict(system) for machine, system in confirmed_systems.items()}
        fields["evaluated_system_paths"] = {machine: system.details() for machine, system in evaluated_system_paths.items()}
        with _lock_observations(cache, report_failure=report_failure) as unavailable:
            prior = _project_comparison(_with_saved_running_paths(answer, lambda: _read_changes(cache)))
            next_answer = replace(
                answer, compared=prior.compared, probes=probes, unreadable_comparisons=unreadable,
                expected=NoExpectedSystems() if isinstance(evaluation, EvaluationFailure) else evaluation,
                failure=evaluation if isinstance(evaluation, EvaluationFailure) else NoEvaluationFailure(),
                confirmed_systems=confirmed_systems, evaluated_system_paths=evaluated_system_paths,
                running_paths={machine: system.path for machine, system in observed.items()},
            )
            updated = _project_comparison(_with_saved_running_paths(next_answer, lambda: _read_changes(cache)))
            try:
                write_status_cache(cache, updated.row(), due or "0", [config], began, json.dumps(fields))
            except (OSError, ValueError, RecursionError) as error:
                report_failure(f"rebuild cache: {cache}: Could not publish the comparison: {error}")
                return

            if updated.configured_revision():
                for machine in MACHINES:
                    _ = _observe_machine_need(cache, machine, updated, locked=machine not in unavailable,
                                              report_failure=report_failure)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("mode", choices=("read", "refresh"))
    _ = parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    config = cast(Path, getattr(args, "config"))
    mode = cast(str, getattr(args, "mode"))
    if mode == "read":
        for machine in read(config).machines:
            print(machine)
    else:
        def report_failure(message: str) -> None:
            syslog.syslog(syslog.LOG_WARNING, message)

        syslog.openlog("rebuild-refresh")
        write(config, command_output, report_failure=report_failure)
        write_changes(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

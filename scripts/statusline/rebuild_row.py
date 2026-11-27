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
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
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


@dataclass(frozen=True)
class RunningSystemProbes:
    """Record which machines supplied a valid running system path."""

    natedev: bool = False
    mac: bool = False

    def succeeded(self, machine: str) -> bool:
        return {"natedev": self.natedev, "mac": self.mac}.get(machine, False)

    @classmethod
    def decode(cls, value: object) -> RunningSystemProbes:
        if not isinstance(value, dict):
            return cls()
        fields = cast(dict[str, object], value)
        return cls(fields.get("natedev") is True, fields.get("mac") is True)


@dataclass(frozen=True)
class ExpectedEvaluationTimeNotRecorded:
    """An older cache supplied an expected path without its evaluation time."""


def _timestamp(value: object) -> float | None:
    if type(value) in (int, float):
        number = cast(int | float, value)
        if 0 <= number < float("inf"):
            return number
    return None


@dataclass(frozen=True)
class EvaluatedSystemPath:
    """The last successful evaluation of a machine's expected system path."""

    path: str
    evaluated_at: float | ExpectedEvaluationTimeNotRecorded

    def details(self) -> dict[str, str | float]:
        result: dict[str, str | float] = {"path": self.path}
        if isinstance(self.evaluated_at, (int, float)):
            result["evaluated_at"] = self.evaluated_at
        return result

    @classmethod
    def decode(cls, value: object) -> EvaluatedSystemPath | None:
        if not isinstance(value, dict):
            return None
        fields = cast(dict[str, object], value)
        path = fields.get("path")
        if not isinstance(path, str) or not system_path(path):
            return None
        if "evaluated_at" not in fields:
            return cls(path, ExpectedEvaluationTimeNotRecorded())
        at = _timestamp(fields["evaluated_at"])
        return cls(path, at) if at is not None else None


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


def _probe_running_systems(settings: RebuildSettings, run: Callable[[list[str]], str],
                           clock: Callable[[], float]) -> dict[str, ConfirmedRunningSystem]:
    observations: dict[str, ConfirmedRunningSystem] = {}
    for machine, command in (
        ("natedev", ["realpath", "/run/current-system"]),
        ("mac", ["ssh", "-o", "BatchMode=yes", settings.ssh, "realpath /run/current-system"]),
    ):
        running = run(command)
        if system_path(running):
            observations[machine] = ConfirmedRunningSystem(running, clock())
    return observations


def compare(settings: RebuildSettings, expected: ExpectedSystems | NoExpectedSystems,
            run: Callable[[list[str]], str] = command_output, *,
            clock: Callable[[], float] = time.time) -> RebuildComparison:
    """Compare running systems and retain failed probes as unavailable."""
    observations = _probe_running_systems(settings, run, clock)
    items = [machine for machine, observed in observations.items()
             if isinstance(expected, ExpectedSystems) and system_path(expected.path_for(machine))
             and observed.path != expected.path_for(machine)]
    return RebuildComparison(rebuild_names(items),
                             RunningSystemProbes("natedev" in observations, "mac" in observations),
                             observations)


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
    `probes` records which machines answered the latest probe.
    `confirmed_systems` and `evaluated_system_paths` retain each machine’s last successful reads.
    """

    machines: tuple[str, ...]
    refresh_due: bool
    expected: ExpectedSystems | NoExpectedSystems = NoExpectedSystems()
    failure: EvaluationFailure | NoEvaluationFailure = NoEvaluationFailure()
    compared: tuple[str, ...] = ()
    needs: RebuildNeedsRead = RebuildNeedsNotRead()
    probes: RunningSystemProbes = RunningSystemProbes()
    running_paths: dict[str, str] = field(default_factory=dict)
    comparison_deadline: int = 0
    configured_subject: str = ""
    confirmed_systems: dict[str, ConfirmedRunningSystem] = field(default_factory=dict)
    evaluated_system_paths: dict[str, EvaluatedSystemPath] = field(default_factory=dict)

    def row(self) -> str:
        return rebuild_names(self.machines)

    def configured_revision(self) -> str:
        if isinstance(self.expected, ExpectedSystems):
            return self.expected.commit
        return self.failure.commit if isinstance(self.failure, EvaluationFailure) else ""


def config_values(path: Path) -> Mapping[str, str]:
    values: dict[str, str] = {}
    contents = conf_reads.read_text(path)
    for line in contents.splitlines():
        key, sep, value = line.partition("#")[0].partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return conf_reads.trace_values(path, contents, values)


def cache_path(config: Path) -> Path:
    path = Path(config_values(config)["rebuild.cache"]).expanduser()
    if not path.is_absolute():
        raise ValueError("The rebuild cache path must be absolute")
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
            if isinstance(changes, dict):
                fields = cast(dict[str, object], changes)
                if "active_revision" not in fields:
                    # Older caches did not verify the active generation.
                    result[machine] = _decode_commits(fields["commits"])
                    continue
                running_path, running, configured = fields["running_path"], fields["active_revision"], fields["configured_revision"]
                if (not isinstance(running_path, str) or not system_path(running_path)
                        or not isinstance(running, str) or (running and not REVISION.fullmatch(running.removesuffix("-dirty")))
                        or not isinstance(configured, str) or (configured and not REVISION.fullmatch(configured))):
                    continue
                paths = fields.get("paths", [])
                if not isinstance(paths, list) or not all(
                        isinstance(name, str) and name for name in cast(list[object], paths)):
                    continue
                compared_paths = fields.get("paths_between_revisions", False)
                aliases = fields.get("legacy_commits", {})
                if not isinstance(compared_paths, bool) or not isinstance(aliases, dict):
                    continue
                legacy_commits = {prefix: commit for prefix, commit in cast(dict[str, object], aliases).items()
                                  if need.SHORT_COMMIT.fullmatch(prefix) and isinstance(commit, str)
                                  and need.FULL_NEED_COMMIT.fullmatch(commit) and commit.startswith(prefix)}
                result[machine] = ComparedChanges(running_path, running, configured, _decode_commits(fields["commits"]),
                                                  tuple(cast(list[str], paths)), compared_paths, legacy_commits)
            else:
                result[machine] = _decode_commits(changes)
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
    answer = _read_cache(config, clock, watch=None if watch is None else watch.watch)
    errors: list[str] = []
    try:
        needs = tuple(need.read_needs(errors) if watch is None else need.read_needs(errors, watch=watch))
        machines = set(answer.machines) | {summary.newest["machine"] for summary in needs}
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        return replace(answer, needs=RebuildNeedsUnavailable(str(error)))
    return replace(answer, machines=tuple(machine for machine in MACHINES if machine in machines),
                   needs=PartialRebuildNeeds("\n".join(errors), needs) if errors else needs)


def _read_cache(config: Path, clock: Callable[[], float], *,
                watch: Callable[[Path], None] | None = None) -> RebuildAnswer:
    """Read the configured comparison cache without evaluating systems."""
    try:
        if watch is not None:
            watch(config)
        cache = cache_path(config)
    except (OSError, ValueError, KeyError):
        return RebuildAnswer((), False)
    try:
        if watch is not None:
            watch(cache)
        with cache.open(encoding="utf-8") as stream:
            lines = stream.read().splitlines()
            stamp = os.fstat(stream.fileno())
        if len(lines) < 2 or not lines[1].isascii() or not lines[1].isdecimal():
            return RebuildAnswer((), True)
        deadline = int(lines[1])
        refresh_due = deadline <= clock() or config.stat().st_mtime_ns > stamp.st_mtime_ns
        items = lines[0].split(", ") if lines[0] else []
        if any(item not in ("rebuild natedev", "rebuild mac") for item in items):
            return RebuildAnswer((), True)
        expected: ExpectedSystems | NoExpectedSystems = NoExpectedSystems()
        failure: EvaluationFailure | NoEvaluationFailure = NoEvaluationFailure()
        probes = RunningSystemProbes()
        running_paths: dict[str, str] = {}
        configured_subject = ""
        confirmed_systems: dict[str, ConfirmedRunningSystem] = {}
        evaluated_system_paths: dict[str, EvaluatedSystemPath] = {}
        legacy_expected: dict[str, str] = {}
        if len(lines) >= 4:
            try:
                value = cast(object, json.loads(lines[3]))
                if not isinstance(value, dict):
                    raise ValueError("The comparison details must be an object.")
                details = cast(dict[str, object], value)
                probes = RunningSystemProbes.decode(details.get("running_system_probes"))
                paths = details.get("running_system_paths")
                if isinstance(paths, dict):
                    running_paths = {machine: path for machine, path in cast(dict[str, object], paths).items()
                                     if isinstance(path, str) and system_path(path)}
                confirmed = details.get("confirmed_running_systems")
                if isinstance(confirmed, dict):
                    observations = cast(dict[str, object], confirmed)
                    for machine in MACHINES:
                        reading = observations.get(machine)
                        if (observed := ConfirmedRunningSystem.decode(reading)) is not None:
                            confirmed_systems[machine] = observed
                            fields = cast(dict[str, object], reading)
                            path = fields.get("expected_path")
                            if isinstance(path, str) and system_path(path):
                                legacy_expected[machine] = path
                evaluated = details.get("evaluated_system_paths")
                if isinstance(evaluated, dict):
                    paths = cast(dict[str, object], evaluated)
                    evaluated_system_paths = {machine: path for machine in MACHINES
                                              if (path := EvaluatedSystemPath.decode(paths.get(machine))) is not None}
                commit, source = details.get("commit"), details.get("source")
                if (isinstance(commit, str) and REVISION.fullmatch(commit)
                        and isinstance(source, list) and all(isinstance(value, str) for value in cast(list[object], source))):
                    subject = details.get("configured_subject")
                    if isinstance(subject, str):
                        configured_subject = subject
                    failed_at = details.get("failed_at")
                    if isinstance(failed_at, (float, int)) and 0 <= failed_at < float("inf"):
                        evaluated_machines = details.get("evaluated_machines")
                        succeeded = (tuple(machine for machine in MACHINES
                                           if machine in cast(list[object], evaluated_machines))
                                     if isinstance(evaluated_machines, list) else ())
                        failure = EvaluationFailure(commit, tuple(cast(list[str], source)), failed_at, succeeded)
                    else:
                        local, mac = details["natedev"], details["mac"]
                        if isinstance(local, str) and system_path(local) and isinstance(mac, str) and system_path(mac):
                            expected = ExpectedSystems(commit, tuple(cast(list[str], source)), local, mac)
            except (ValueError, KeyError, TypeError):
                pass
        for machine in MACHINES:
            if machine not in confirmed_systems and probes.succeeded(machine) and (running := running_paths.get(machine)):
                confirmed_systems[machine] = ConfirmedRunningSystem(running, stamp.st_mtime)
            if machine not in evaluated_system_paths:
                path = (expected.path_for(machine) if isinstance(expected, ExpectedSystems)
                        else legacy_expected.get(machine, ""))
                if path:
                    evaluated_system_paths[machine] = EvaluatedSystemPath(path, ExpectedEvaluationTimeNotRecorded())
        compared = tuple(item.removeprefix("rebuild ") for item in items)
        if isinstance(expected, NoExpectedSystems) and isinstance(failure, NoEvaluationFailure):
            configured_subject = ""
        return RebuildAnswer(compared, refresh_due, expected, failure, compared,
                             probes=probes, running_paths=running_paths, comparison_deadline=deadline,
                             configured_subject=configured_subject, confirmed_systems=confirmed_systems,
                             evaluated_system_paths=evaluated_system_paths)
    except (OSError, ValueError):
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
                              clock: Callable[[], float]) -> ExpectedSystems | EvaluationFailure:
    """Evaluate each missing path and save the successful paths with their own times."""
    source = (str(settings.repo), settings.ref, settings.natedev, settings.mac)
    previous = answer.expected
    if isinstance(previous, ExpectedSystems) and previous.commit == commit and previous.source == source:
        return previous
    current: dict[str, EvaluatedSystemPath] = {}
    failure = answer.failure
    if isinstance(failure, EvaluationFailure) and failure.commit == commit and failure.source == source:
        if clock() < failure.failed_at + settings.eval_retry_seconds:
            return failure
        current = {machine: paths[machine] for machine in failure.evaluated_machines if machine in paths}

    flake = f"git+file://{quote(str(settings.repo))}?rev={commit}"
    attributes = {
        "natedev": f"nixosConfigurations.{json.dumps(settings.natedev)}.config.system.build.toplevel.outPath",
        "mac": f"darwinConfigurations.{json.dumps(settings.mac)}.system.outPath",
    }
    for machine, attribute in attributes.items():
        if machine in current:
            continue
        expression = f"(builtins.getFlake {json.dumps(flake)}).{attribute}"
        output = run(["nix", "eval", "--json", "--no-write-lock-file", "--expr", expression],
                     timeout=settings.eval_timeout)
        try:
            path = cast(object, json.loads(output))
        except ValueError:
            continue
        if isinstance(path, str) and system_path(path):
            current[machine] = EvaluatedSystemPath(path, clock())
    paths.update(current)
    if "natedev" in current and "mac" in current:
        return ExpectedSystems(commit, source, current["natedev"].path, current["mac"].path)
    return EvaluationFailure(commit, source, clock(), tuple(current))


def write(config: Path, run: CommandRunner = command_output, *, clock: Callable[[], float] = time.time) -> None:
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
        answer = _read_cache(config, clock)
        if not answer.refresh_due:
            return
        began = time.time_ns()
        line, due, details = "", "", ""
        subject = ""
        probes = RunningSystemProbes()
        running_paths: dict[str, str] = {}
        confirmed_systems = answer.confirmed_systems.copy()
        evaluated_system_paths = answer.evaluated_system_paths.copy()
        if isinstance(answer.failure, EvaluationFailure):
            line, details = answer.row(), answer.failure.details()
            subject = answer.configured_subject
        try:
            settings = RebuildSettings.read(config)
            resolved = run(["git", "-C", str(settings.repo), "show", "-s", "--format=%H%x00%s",
                            "--end-of-options", f"{settings.ref}^{{commit}}"])
            commit, _, resolved_subject = resolved.partition("\0")
            if REVISION.fullmatch(commit):
                evaluation = _refresh_expected_systems(settings, answer, commit, evaluated_system_paths, run, clock)
                if evaluation is not answer.failure:
                    subject = resolved_subject
                    details = evaluation.details()
                    line = ""
            observed = _probe_running_systems(settings, run, clock)
            confirmed_systems.update(observed)
            probes = RunningSystemProbes("natedev" in observed, "mac" in observed)
            running_paths = {machine: system.path for machine, system in observed.items()}
            compared = {item.removeprefix("rebuild ") for item in line.split(", ")}
            for machine, evaluated in evaluated_system_paths.items():
                if (running := confirmed_systems.get(machine)) is not None:
                    if running.path == evaluated.path:
                        compared.discard(machine)
                    else:
                        compared.add(machine)
            line = rebuild_names(compared)
            due = str(int(clock()) + settings.interval)
        except (OSError, ValueError, KeyError):
            pass
        if confirmed_systems or evaluated_system_paths:
            due = due or "0"
        if details or confirmed_systems or evaluated_system_paths:
            fields = cast(dict[str, object], json.loads(details)) if details else {}
            fields["configured_subject"] = subject
            fields["running_system_probes"] = {"natedev": probes.natedev, "mac": probes.mac}
            fields["running_system_paths"] = running_paths
            fields["confirmed_running_systems"] = {machine: asdict(system) for machine, system in confirmed_systems.items()}
            fields["evaluated_system_paths"] = {machine: system.details() for machine, system in evaluated_system_paths.items()}
            details = json.dumps(fields)
        write_status_cache(cache, line, due, [config], began, details)


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
        write(config)
        write_changes(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

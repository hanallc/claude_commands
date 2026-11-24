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
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import quote

from scripts.lib import atomic_file, conf_reads
from scripts.rebuild import need

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config/statusline.conf"
MACHINES = ("natedev", "mac")


def rebuild_names(machines: Iterable[str]) -> str:
    """Name each waiting machine once in status line order."""
    waiting = set(machines)
    return ", ".join(f"rebuild {machine}" for machine in MACHINES if machine in waiting)


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

    def details(self) -> str:
        return json.dumps({"commit": self.commit, "source": self.source,
                           "natedev": self.natedev, "mac": self.mac})


@dataclass(frozen=True)
class NoEvaluationFailure:
    """No failed evaluation requires a retry delay."""


@dataclass(frozen=True)
class EvaluationFailure:
    """The commit, source, and completion time of a failed evaluation."""

    commit: str
    source: tuple[str, ...]
    failed_at: float

    def details(self) -> str:
        return json.dumps({"commit": self.commit, "source": self.source, "failed_at": self.failed_at})


def expected_systems(settings: RebuildSettings, previous: ExpectedSystems | NoExpectedSystems,
                     run: CommandRunner, commit: str) -> ExpectedSystems | NoExpectedSystems:
    """Reuse a complete evaluation for the resolved commit or evaluate it."""
    source = (str(settings.repo), settings.ref, settings.natedev, settings.mac)
    if (isinstance(previous, ExpectedSystems) and previous.commit == commit and previous.source == source
            and system_path(previous.natedev) and system_path(previous.mac)):
        return previous
    flake = f"git+file://{quote(str(settings.repo))}?rev={commit}"
    expression = (
        f"let flake = builtins.getFlake {json.dumps(flake)}; "
        + "pathOrEmpty = value: let result = builtins.tryEval value; "
        + "in if result.success then result.value else \"\"; in { "
        + f"natedev = pathOrEmpty flake.nixosConfigurations.{json.dumps(settings.natedev)}.config.system.build.toplevel.outPath; "
        + f"mac = pathOrEmpty flake.darwinConfigurations.{json.dumps(settings.mac)}.system.outPath; }}"
    )
    output = run(["nix", "eval", "--json", "--no-write-lock-file", "--expr", expression], timeout=settings.eval_timeout)
    try:
        paths = cast(dict[str, object], json.loads(output))
        local, mac = paths["natedev"], paths["mac"]
        return ExpectedSystems(commit, source,
                               local if isinstance(local, str) and system_path(local) else "",
                               mac if isinstance(mac, str) and system_path(mac) else "")
    except (ValueError, KeyError, TypeError):
        pass
    return NoExpectedSystems()


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
class RebuildComparison:
    """Keep the status text and the running systems that answered."""

    row: str
    probes: RunningSystemProbes
    running_paths: dict[str, str]


def compare(settings: RebuildSettings, expected: ExpectedSystems | NoExpectedSystems,
            run: Callable[[list[str]], str] = command_output) -> RebuildComparison:
    """Compare running systems and retain failed probes as unavailable."""
    items: list[str] = []
    running_paths: dict[str, str] = {}
    for machine, path, command in (
        ("natedev", expected.natedev if isinstance(expected, ExpectedSystems) else "",
         ["realpath", "/run/current-system"]),
        ("mac", expected.mac if isinstance(expected, ExpectedSystems) else "",
         ["ssh", "-o", "BatchMode=yes", settings.ssh, "realpath /run/current-system"]),
    ):
        running = run(command)
        if system_path(running):
            running_paths[machine] = running
            if system_path(path) and running != path:
                items.append(machine)
    return RebuildComparison(rebuild_names(items),
                             RunningSystemProbes("natedev" in running_paths, "mac" in running_paths),
                             running_paths)


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
    """The read failed wholly or returned some valid records."""

    reason: str
    summaries: OpenRebuildNeeds | None = None

    def __iter__(self) -> Iterator[need.MachineNeeds]:
        return iter(self.summaries or ())


@dataclass(frozen=True)
class RebuildAnswer:
    """The known rebuild machines and whether the cache needs a refresh.

    `compared` holds the machines whose running system differs from main;
    `needs` holds the open rebuild records or the reason they are unavailable.
    `probes` records whether each machine supplied a running system path.
    """

    machines: tuple[str, ...]
    refresh_due: bool
    expected: ExpectedSystems | NoExpectedSystems = NoExpectedSystems()
    failure: EvaluationFailure | NoEvaluationFailure = NoEvaluationFailure()
    compared: tuple[str, ...] = ()
    needs: OpenRebuildNeeds | RebuildNeedsNotRead | RebuildNeedsUnavailable = RebuildNeedsNotRead()
    probes: RunningSystemProbes = RunningSystemProbes()
    running_paths: dict[str, str] = field(default_factory=dict)

    def row(self) -> str:
        return rebuild_names(self.machines)

    def configured_revision(self) -> str:
        if isinstance(self.expected, ExpectedSystems):
            return self.expected.commit
        return self.failure.commit if isinstance(self.failure, EvaluationFailure) else ""


def config_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in conf_reads.read_text(path).splitlines():
        key, sep, value = line.partition("#")[0].partition("=")
        if sep:
            values[key.strip()] = value.strip()
    return values


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
    """The commits from the running system revision to the configured revision."""

    running_path: str
    active_revision: str
    configured_revision: str
    commits: tuple[Change, ...]


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
                    run: CommandRunner, *, configured: str) -> CachedChanges:
    """List commits after a verified running revision."""
    commit = system.revision.removesuffix("-dirty")
    if not commit:
        return ()
    listing = "" if commit == configured else run(["git", "-C", str(settings.repo), "log",
        "--format=%H%x1f%aI%x1f%s%x1f%(trailers:key=Claude-Session,valueonly,separator=%x20)",
        f"{commit}..{configured or settings.ref}"])
    changes: list[Change] = []
    for line in listing.splitlines():
        fields = line.split("\x1f")
        if len(fields) != 4:
            continue
        bridge = BRIDGE.search(fields[3])
        if bridge is not None and bridge[1] not in known:
            known[bridge[1]] = transcript_session(bridge[1], run)
        changes.append(Change(fields[0], fields[1], fields[2], known[bridge[1]] if bridge is not None else ""))
    return ComparedChanges(system.path, system.revision, configured, tuple(changes))


def write_changes(config: Path, run: CommandRunner = command_output) -> None:
    """Record each machine's pending /etc/nixos changes and the session that wrote each one."""
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
        answer = _read_cache(config, time.time)
        configured = answer.configured_revision()
        machines: dict[str, object] = {}
        for machine in MACHINES:
            system = need.active_system(run(need.active_probe(machine, log=log, ssh=settings.ssh)))
            if system is None:
                old = previous.get(machine)
                changes = old.commits if isinstance(old, ComparedChanges) else old
            else:
                try:
                    need.clear_running_needs(machine, system, settings.repo, run)
                except (OSError, ValueError, KeyError, TypeError) as error:
                    print(f"rebuild needs: {error}", file=sys.stderr)
                changes = machine_changes(settings, system, known, run, configured=configured)
            if isinstance(changes, ComparedChanges):
                machines[machine] = asdict(changes)
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


def _read_changes(cache: Path) -> dict[str, CachedChanges]:
    try:
        value = cast(object, json.loads(changes_path(cache).read_text(encoding="utf-8")))
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
                path, running, configured = fields["running_path"], fields["active_revision"], fields["configured_revision"]
                if (not isinstance(path, str) or not system_path(path)
                        or not isinstance(running, str) or not REVISION.fullmatch(running.removesuffix("-dirty"))
                        or not isinstance(configured, str) or (configured and not REVISION.fullmatch(configured))):
                    continue
                result[machine] = ComparedChanges(path, running, configured, _decode_commits(fields["commits"]))
            else:
                result[machine] = _decode_commits(changes)
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def _recorded_changes(config: Path) -> dict[str, CachedChanges]:
    try:
        return _read_changes(cache_path(config))
    except (OSError, ValueError, KeyError):
        return {}


def read_changes(config: Path = DEFAULT_CONFIG) -> dict[str, tuple[Change, ...]]:
    """Read the recorded commit list for each machine."""
    return {machine: changes.commits if isinstance(changes, ComparedChanges) else changes
            for machine, changes in _recorded_changes(config).items()}


def read_compared_changes(config: Path, answer: RebuildAnswer) -> dict[str, ComparedChanges]:
    """Read commits that match the running path and configured revision in the comparison."""
    configured = answer.configured_revision()
    if not configured:
        return {}
    return {machine: changes for machine, changes in _recorded_changes(config).items()
            if isinstance(changes, ComparedChanges) and changes.configured_revision == configured
            and answer.probes.succeeded(machine) and changes.running_path == answer.running_paths.get(machine)}


def read(config: Path = DEFAULT_CONFIG, *, clock: Callable[[], float] = time.time) -> RebuildAnswer:
    """Combine the cached comparison with open rebuild needs."""
    answer = _read_cache(config, clock)
    try:
        # Glob can hide an inaccessible folder.
        try:
            with os.scandir(need.needs_folder()):
                pass
        except FileNotFoundError:
            pass
        errors: list[str] = []
        needs = tuple(need.read_needs(errors))
        machines = set(answer.machines) | {summary.newest["machine"] for summary in needs}
    except (OSError, ValueError, KeyError, TypeError) as error:
        return replace(answer, needs=RebuildNeedsUnavailable(str(error)))
    return replace(answer, machines=tuple(machine for machine in MACHINES if machine in machines),
                   needs=RebuildNeedsUnavailable("\n".join(errors), needs) if errors else needs)


def _read_cache(config: Path, clock: Callable[[], float]) -> RebuildAnswer:
    """Read the configured comparison cache without evaluating systems."""
    try:
        cache = cache_path(config)
    except (OSError, ValueError, KeyError):
        return RebuildAnswer((), False)
    try:
        lines = cache.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2 or not lines[1].isdigit():
            return RebuildAnswer((), True)
        refresh_due = int(lines[1]) <= clock() or config.stat().st_mtime_ns > cache.stat().st_mtime_ns
        items = lines[0].split(", ") if lines[0] else []
        if any(item not in ("rebuild natedev", "rebuild mac") for item in items):
            return RebuildAnswer((), True)
        expected: ExpectedSystems | NoExpectedSystems = NoExpectedSystems()
        failure: EvaluationFailure | NoEvaluationFailure = NoEvaluationFailure()
        probes = RunningSystemProbes()
        running_paths: dict[str, str] = {}
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
                commit, source = details["commit"], details["source"]
                if (isinstance(commit, str) and REVISION.fullmatch(commit)
                        and isinstance(source, list) and all(isinstance(value, str) for value in cast(list[object], source))):
                    failed_at = details.get("failed_at")
                    if isinstance(failed_at, (float, int)) and 0 <= failed_at < float("inf"):
                        failure = EvaluationFailure(commit, tuple(cast(list[str], source)), failed_at)
                    else:
                        local, mac = details["natedev"], details["mac"]
                        if isinstance(local, str) and system_path(local) and isinstance(mac, str) and system_path(mac):
                            expected = ExpectedSystems(commit, tuple(cast(list[str], source)), local, mac)
            except (ValueError, KeyError, TypeError):
                pass
        compared = tuple(item.removeprefix("rebuild ") for item in items)
        return RebuildAnswer(compared, refresh_due, expected, failure, compared,
                             probes=probes, running_paths=running_paths)
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
        probes = answer.probes
        running_paths = answer.running_paths
        if isinstance(answer.failure, EvaluationFailure):
            line, details = answer.row(), answer.failure.details()
        try:
            settings = RebuildSettings.read(config)
            commit = run(["git", "-C", str(settings.repo), "rev-parse", "--verify", f"{settings.ref}^{{commit}}"])
            source = (str(settings.repo), settings.ref, settings.natedev, settings.mac)
            failure = answer.failure
            if (isinstance(failure, EvaluationFailure) and failure.commit == commit and failure.source == source
                    and clock() < failure.failed_at + settings.eval_retry_seconds):
                line, details = answer.row(), failure.details()
            elif REVISION.fullmatch(commit):
                expected = expected_systems(settings, answer.expected, run, commit)
                evaluated_at = clock()
                comparison = compare(settings, expected, run)
                line, probes = comparison.row, comparison.probes
                running_paths = comparison.running_paths
                if isinstance(expected, ExpectedSystems) and system_path(expected.natedev) and system_path(expected.mac):
                    details = expected.details()
                else:
                    details = EvaluationFailure(commit, source, evaluated_at).details()
            due = str(int(clock()) + settings.interval)
        except (OSError, ValueError, KeyError):
            pass
        if details:
            fields = cast(dict[str, object], json.loads(details))
            fields["running_system_probes"] = {"natedev": probes.natedev, "mac": probes.mac}
            fields["running_system_paths"] = running_paths
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

"""Compare running systems with main and cache the last rebuild result."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

import argparse
import fcntl
import json
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Protocol, cast
from urllib.parse import quote

from scripts.lib import atomic_file, conf_reads
from scripts.rebuild import need

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config/statusline.conf"


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


def command_output(command: list[str], *, timeout: int = 30) -> str:
    """Return command output, or an empty string when the command fails."""
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def system_path(value: str) -> bool:
    """Accept a single absolute Nix store path."""
    return value.startswith("/nix/store/") and len(value.split("/")) == 4 and not any(c.isspace() for c in value)


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


def row(settings: RebuildSettings, expected: ExpectedSystems | NoExpectedSystems,
        run: Callable[[list[str]], str] = command_output) -> str:
    """Compare running systems with the last evaluated system paths."""
    items: list[str] = []
    for machine, path, command in (
        ("natedev", expected.natedev if isinstance(expected, ExpectedSystems) else "",
         ["realpath", "/run/current-system"]),
        ("mac", expected.mac if isinstance(expected, ExpectedSystems) else "",
         ["ssh", "-o", "BatchMode=yes", settings.ssh, "realpath /run/current-system"]),
    ):
        running = run(command)
        if system_path(path) and system_path(running) and running != path:
            items.append(f"rebuild {machine}")
    return ", ".join(items)


@dataclass(frozen=True)
class RebuildAnswer:
    """The known rebuild machines and whether the cache needs a refresh.

    `compared` holds the machines whose running system differs from main;
    `needs` holds the open rebuild needs that `read` joined into `machines`.
    """

    machines: tuple[str, ...]
    refresh_due: bool
    expected: ExpectedSystems | NoExpectedSystems = NoExpectedSystems()
    failure: EvaluationFailure | NoEvaluationFailure = NoEvaluationFailure()
    compared: tuple[str, ...] = ()
    needs: tuple[need.MachineNeeds, ...] = ()

    def row(self) -> str:
        return ", ".join(f"rebuild {machine}" for machine in self.machines)


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


BRIDGE = re.compile(r"https://claude\.ai/code/(session_[0-9A-Za-z]+)")
LOG_PATH = re.compile(r"~?[\w./-]+")
SWITCH_FIELDS = 5


def changes_path(cache: Path) -> Path:
    return cache.with_name(cache.name + "-changes.json")


def sessions_path(cache: Path) -> Path:
    return cache.with_name(cache.name + "-sessions.json")


def switched_commit(log: str, running: str) -> str:
    """Return the commit of the last logged switch to the running system, else of the last switch."""
    switches = [fields for fields in (line.split() for line in log.splitlines())
                if len(fields) == SWITCH_FIELDS and fields[1] == "switch"]
    last = next((fields for fields in reversed(switches) if fields[4] == running), switches[-1] if switches else None)
    return last[3].removesuffix("-dirty") if last is not None else ""


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


def machine_changes(settings: RebuildSettings, command: list[str], known: dict[str, str],
                    run: CommandRunner) -> tuple[Change, ...] | None:
    """List the commits on the configured ref after the machine's last switch, or None when unknown."""
    lines = run(command).splitlines()
    commit = switched_commit("\n".join(lines[1:]), lines[0]) if lines else ""
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
        return None
    listing = run(["git", "-C", str(settings.repo), "log",
                   "--format=%H%x1f%aI%x1f%s%x1f%(trailers:key=Claude-Session,valueonly,separator=%x20)",
                   f"{commit}..{settings.ref}"])
    changes: list[Change] = []
    for line in listing.splitlines():
        fields = line.split("\x1f")
        if len(fields) != 4:
            continue
        bridge = BRIDGE.search(fields[3])
        if bridge is not None and bridge[1] not in known:
            known[bridge[1]] = transcript_session(bridge[1], run)
        changes.append(Change(fields[0], fields[1], fields[2], known[bridge[1]] if bridge is not None else ""))
    return tuple(changes)


def write_changes(config: Path, run: CommandRunner = command_output) -> None:
    """Record each machine's pending /etc/nixos changes and the session that wrote each one."""
    try:
        settings = RebuildSettings.read(config)
        cache = cache_path(config)
        log = config_values(config)["rebuild.generations"]
    except (OSError, ValueError, KeyError):
        return
    if not LOG_PATH.fullmatch(log):
        return
    script = f"realpath /run/current-system && tail -n 200 {log}"
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
        previous = read_changes(config)
        machines: dict[str, list[dict[str, str]]] = {}
        for machine, command in (("natedev", ["sh", "-c", script]),
                                 ("mac", ["ssh", "-o", "BatchMode=yes", settings.ssh, script])):
            changes = machine_changes(settings, command, known, run)
            if changes is None:
                changes = previous.get(machine)
            if changes is not None:
                machines[machine] = [asdict(change) for change in changes]
        atomic_file.write_text_atomic(sessions_path(cache), json.dumps(known))
        atomic_file.write_text_atomic(changes_path(cache), json.dumps(machines))


def read_changes(config: Path = DEFAULT_CONFIG) -> dict[str, tuple[Change, ...]]:
    """Read the recorded changes per machine; a machine without a record has no list."""
    try:
        recorded = cast(dict[str, list[dict[str, str]]],
                        json.loads(changes_path(cache_path(config)).read_text(encoding="utf-8")))
        return {machine: tuple(Change(**change) for change in changes) for machine, changes in recorded.items()}
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def read(config: Path = DEFAULT_CONFIG, *, clock: Callable[[], float] = time.time) -> RebuildAnswer:
    """Combine the cached comparison with open rebuild needs."""
    answer = _read_cache(config, clock)
    try:
        needs = tuple(need.read_needs())
        machines = set(answer.machines) | {summary.newest["machine"] for summary in needs}
    except (OSError, ValueError, KeyError, TypeError):
        return answer
    return replace(answer, machines=tuple(machine for machine in ("natedev", "mac") if machine in machines), needs=needs)


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
        if len(lines) >= 4:
            try:
                details = cast(dict[str, object], json.loads(lines[3]))
                commit, source = details["commit"], details["source"]
                if (isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit)
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
        return RebuildAnswer(compared, refresh_due, expected, failure, compared)
    except (OSError, ValueError):
        return RebuildAnswer((), True)


def write(config: Path, run: CommandRunner = command_output, *, clock: Callable[[], float] = time.time) -> None:
    """Refresh the configured cache without concurrent evaluations."""
    from scripts.statusline import counts_row

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
            elif re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit):
                expected = expected_systems(settings, answer.expected, run, commit)
                evaluated_at = clock()
                line = row(settings, expected, run)
                if isinstance(expected, ExpectedSystems) and system_path(expected.natedev) and system_path(expected.mac):
                    details = expected.details()
                else:
                    details = EvaluationFailure(commit, source, evaluated_at).details()
            due = str(int(clock()) + settings.interval)
        except (OSError, ValueError, KeyError):
            pass
        counts_row.write_cache(cache, line, due, [config], began, details)


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

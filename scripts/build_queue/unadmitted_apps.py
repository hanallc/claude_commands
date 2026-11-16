"""Find Hana app trees without a live memory admission."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
from typing import cast

from scripts.build_queue.scope_activity import environment_values
from scripts.lib.atomic_file import write_text_atomic
from scripts.lint import memory_admit as gate
from scripts.message import unit_labels


@dataclass(frozen=True)
class AppOwner:
    """The app has an owner and a message destination."""
    name: str
    recipient: str


@dataclass(frozen=True)
class UnknownAppOwner:
    """The app has no identified owner or message destination."""
    name: str = "owner unknown"


@dataclass(frozen=True)
class UnadmittedApp:
    pid: int
    start_time: str
    binary: str
    owner: AppOwner | UnknownAppOwner
    ram_bytes: int
    swap_bytes: int
    started_at: float

    @property
    def key(self) -> str:
        return f"{self.pid}:{self.start_time}"


@dataclass(frozen=True)
class AppScan:
    """One process scan supplies app rows and identities for alert cleanup."""
    apps: tuple[UnadmittedApp, ...]
    live_keys: frozenset[str]


@dataclass(frozen=True)
class NoAppAlerts:
    """This reading must not send app alerts."""


NO_APP_ALERTS = NoAppAlerts()


def admitted_scopes(directory: Path, cgroup_root: Path) -> tuple[str, ...]:
    """Read active app scopes from the durable admission state."""
    try:
        value = cast(object, json.loads((directory / "state.json").read_text()))
    except FileNotFoundError:
        return ()
    if not isinstance(value, dict):
        raise ValueError("admission state must be an object")
    reservations = cast(dict[str, object], value).get("reservations")
    if not isinstance(reservations, dict):
        raise ValueError("admission reservations must be an object")
    scopes: list[str] = []
    for reservation in cast(dict[str, object], reservations).values():
        if not isinstance(reservation, dict):
            raise ValueError("admission reservation must be an object")
        row = cast(dict[str, object], reservation)
        if row.get("step") != gate.APP_KIND:
            continue
        sidecar = row.get("sidecar")
        if not isinstance(sidecar, str):
            raise ValueError("app admission sidecar must be a path")
        try:
            scope = Path(Path(sidecar).read_text().strip())
            if scope.name.startswith("app-") and scope.suffix == ".scope":
                scopes.append("/" + scope.relative_to(cgroup_root).as_posix())
        except (OSError, ValueError):
            continue
    return tuple(scopes)


def scan(proc: Path, admission_directory: Path, cgroup_root: Path) -> AppScan:
    """Match the resolved binary path, as app_profile does, and group descendants once."""
    processes = gate.process_table(proc)
    live_keys = frozenset(f"{pid}:{identity.start}" for pid, identity in processes.items())
    binaries: dict[int, str] = {}
    children: dict[int, list[int]] = {}
    for pid, identity in processes.items():
        children.setdefault(identity.parent, []).append(pid)
        try:
            executable = proc / str(pid) / "exe"
            target = Path(str(executable.readlink()).removesuffix(" (deleted)"))
            # Linux exposes the executable's resolved file name through this link.
            if target.name != "hana":
                continue
            binary = (target if target.is_absolute() else executable.parent / target).resolve()
            if binary.name == "hana":
                binaries[pid] = str(binary)
        except OSError:
            continue
    if not binaries:
        return AppScan((), live_keys)
    # The admission grant precedes app startup, so read admissions after the process scan.
    admitted = admitted_scopes(admission_directory, cgroup_root)
    identities = gate.QueueIdentityReader(processes, proc)
    try:
        boot = float(next(line.split()[1] for line in (proc / "stat").read_text().splitlines()
                          if line.startswith("btime ")))
    except (OSError, ValueError, IndexError, StopIteration):
        boot = 0.0
    ticks = int(os.sysconf("SC_CLK_TCK"))
    apps: list[UnadmittedApp] = []
    owner_sessions: dict[int, str] = {}
    for pid, binary in binaries.items():
        ancestor, seen = processes[pid].parent, {pid}
        while ancestor in processes and ancestor not in seen and ancestor not in binaries:
            seen.add(ancestor)
            ancestor = processes[ancestor].parent
        if ancestor in binaries:
            continue
        try:
            cgroup = next(line[3:] for line in (proc / str(pid) / "cgroup").read_text().splitlines()
                          if line.startswith("0::"))
        except (OSError, StopIteration):
            cgroup = ""
        if any(cgroup == scope or cgroup.startswith(scope + "/") for scope in admitted):
            continue
        caller = identities.identity(pid, {"start_time": processes[pid].start})
        explicit = environment_values(proc / str(pid) / "environ", ("HANA_TEST_OWNER",)).get("HANA_TEST_OWNER", "").strip()
        owner: AppOwner | UnknownAppOwner = UnknownAppOwner()
        if explicit:
            owner = AppOwner(explicit, explicit)
        elif caller["session_id"]:
            session = caller["session_id"]
            owner_sessions[pid] = session
            backend = "codex" if any(env.get("CODEX_THREAD_ID") == session for env in identities.environments.values()) else "session"
            owner = AppOwner(caller["session_name"] or session, f"{backend}:{session}")
        elif caller["session_name"]:
            owner = AppOwner(caller["session_name"], caller["session_name"])
        members, pending = set[int](), [pid]
        while pending:
            member = pending.pop()
            if member not in members:
                members.add(member)
                pending.extend(children.get(member, ()))
        memory = gate.process_memory(members, proc)
        apps.append(UnadmittedApp(pid, processes[pid].start, binary, owner,
                                  memory.bytes, memory.swap_bytes,
                                  boot + int(processes[pid].start) / ticks if boot else 0.0))
    labels = unit_labels.unit_labels_for(list(dict.fromkeys(owner_sessions.values())), os.environ) if owner_sessions else {}
    for index, app in enumerate(apps):
        label = labels.get(owner_sessions.get(app.pid, ""))
        if label is not None and label.session and isinstance(app.owner, AppOwner):
            apps[index] = replace(app, owner=AppOwner(label.session, app.owner.recipient))
    return AppScan(tuple(sorted(apps, key=lambda app: (app.started_at, app.pid))), live_keys)


class AppAlertLedger:
    """Persist alert claims before sending, until their processes exit."""

    def __init__(self, path: Path) -> None:
        self.path: Path = path
        try:
            value = cast(object, json.loads(path.read_text()))
        except FileNotFoundError:
            value = []
        if not isinstance(value, list) or not all(isinstance(key, str) for key in cast(list[object], value)):
            raise ValueError(f"{path}: expected app alert keys")
        self.claimed: set[str] = set(cast(list[str], value))

    def update(self, reading: AppScan, send: Callable[[UnadmittedApp], bool]) -> None:
        previous = self.claimed.copy()
        self.claimed.intersection_update(reading.live_keys)
        if previous != self.claimed:
            self.save()
        for app in reading.apps:
            if isinstance(app.owner, AppOwner) and app.key not in self.claimed:
                self.claimed.add(app.key)
                self.save()
                _ = send(app)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_text_atomic(self.path, json.dumps(sorted(self.claimed)) + "\n")

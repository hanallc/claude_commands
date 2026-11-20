#!/usr/bin/env python3
"""Publish queued program ownership and execute the program."""
from __future__ import annotations
if not __package__:
    try:
        with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
    except FileNotFoundError:
        pass

from collections.abc import Sequence
from dataclasses import dataclass
import os
from pathlib import Path
import select
import signal
import sys

from scripts.lib import atomic_file
from scripts.lib.process_start import Started, process_start
from scripts.build_queue.admission_wire import Owner, encode_record
from scripts.build_queue.snapshot_codec import SnapshotParseError


class ProgramStartRefused(Exception):
    """The program cannot publish its ownership or enter its scope."""


@dataclass(frozen=True)
class ProgramStart:
    """Link the program and its queue holder before execution."""

    holder: Owner
    program: Owner
    scope: str
    ready: bool = True
    sidecar: str = ""

    def __post_init__(self) -> None:
        for owner in (self.holder, self.program):
            if not 0 < owner.pid < 2 ** 31 or not owner.start_time:
                raise SnapshotParseError("invalid program process identity")
        for path in (self.scope, self.sidecar):
            if "\0" in path:
                raise SnapshotParseError("invalid program path")
            try:
                _ = os.fsencode(path)
            except UnicodeError as error:
                raise SnapshotParseError("invalid program path") from error


def wait_for_start(descriptor: int, stop_seconds: float) -> None:
    with os.fdopen(descriptor, "rb") as permission:
        if not select.select([permission], [], [], stop_seconds)[0] or permission.read(1) != b"1":
            raise ProgramStartRefused("the queue place holder did not authorize startup")


def program_start(record: Path, holder: Owner, argv: Sequence[str], *,
                  sidecar: Path | None = None, scope: str = "") -> None:
    """Publish ownership before execution and reject a dead holder."""
    if process_start(holder.pid) != Started(holder.start_time):
        raise ProgramStartRefused("the queue place holder exited before program startup")
    start = process_start(os.getpid())
    if not isinstance(start, Started):
        raise ProgramStartRefused("the program has no live process identity")
    owner = Owner(os.getpid(), start.start)
    sidecar_name = str(sidecar) if sidecar is not None else ""
    if sidecar is not None and scope:
        atomic_file.write_text_atomic(record, encode_record(ProgramStart(holder, owner, scope, ready=False, sidecar=sidecar_name)))
        atomic_file.atomic_sample(sidecar, scope + "\n")
    atomic_file.write_text_atomic(record, encode_record(ProgramStart(holder, owner, scope, sidecar=sidecar_name)))
    if process_start(holder.pid) != Started(holder.start_time):
        raise ProgramStartRefused("the queue place holder exited during program publication")
    _ = signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGTERM, signal.SIGINT, signal.SIGHUP})
    os.execvp(argv[0], list(argv))


def program_scope() -> Path | None:
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        if line.startswith("0::") and "/builds.slice/" in line:
            return Path("/sys/fs/cgroup") / line[3:].lstrip("/")
    return None



def scope_start(sidecar: Path, holder_pid: int, holder_start: str, argv: Sequence[str], *, record: Path) -> None:
    """Publish the scope before the program starts."""
    if process_start(holder_pid) != Started(holder_start):
        raise ProgramStartRefused("the queue place holder exited before scope startup")
    scope = program_scope()
    if scope is None:
        raise ProgramStartRefused("the program did not enter its memory scope")
    program_start(record, Owner(holder_pid, holder_start), argv, sidecar=sidecar, scope=str(scope))


def main(argv: list[str] | None = None) -> int:
    """Start the program after publishing its ownership or memory scope."""
    argv = sys.argv[1:] if argv is None else argv
    if argv[1:2] == ["--authorized"]:
        if len(argv) < 9 or argv[0] not in ("--scope-start", "--program-start"):
            raise ProgramStartRefused("invalid authorized startup arguments")
        sidecar, holder, record = Path(argv[2]), Owner(int(argv[3]), argv[4]), Path(argv[5])
        wait_for_start(int(argv[6]), float(argv[7]))
        if argv[0] == "--scope-start":
            scope_start(sidecar, holder.pid, holder.start_time, argv[8:], record=record)
        else:
            program_start(record, holder, argv[8:], sidecar=sidecar)
        return 0
    if len(argv) >= 6 and argv[0] == "--scope-start":
        scope_start(Path(argv[1]), int(argv[2]), argv[3], argv[5:], record=Path(argv[4]))
        return 0
    if len(argv) >= 5 and argv[0] == "--program-start":
        program_start(Path(argv[1]), Owner(int(argv[2]), argv[3]), argv[4:])
        return 0
    print("program start: expected --program-start or --scope-start arguments", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

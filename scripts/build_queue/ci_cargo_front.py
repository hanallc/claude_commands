#!/usr/bin/env -S ${HOME}/.claude/scripts/lib/py
"""Hold admission while a CI cargo call and its descendants run."""
from __future__ import annotations
try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as _package_root: exec(_package_root.read(), {"__file__": _package_root.name})
except FileNotFoundError:
    pass

from collections.abc import Sequence
from contextlib import ExitStack
import ctypes
from dataclasses import dataclass
from functools import partial
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time


from scripts.build_queue import admission_client as client
from scripts.build_queue import admission_protocol as wire
from scripts.build_queue import admission_records as records
from scripts.build_queue import progress_profile as progress
from scripts.build_queue import queue_place
from scripts.build_queue import step_estimator
from scripts.buildlog import parse
from scripts.lib import cancellation, conf, process_stat
from scripts.lib.descendants import adopt_descendants


PR_SET_PDEATHSIG = 1
LIBC = ctypes.CDLL(None, use_errno=True)
PROC_ROOT = process_stat.PROC_ROOT
JOB_WAIT_LIMIT_KEY = "ci_job_wait_limit_minutes"


@dataclass(frozen=True)
class JobTemporaryFolder:
    """Put counter files in the job's default temporary folder."""


@dataclass(frozen=True)
class JobWaitLimit:
    """A queue wait ends when the job is this many minutes old. start is on the CLOCK_BOOTTIME clock."""
    start: float
    minutes: int

    def age_seconds(self) -> float:
        return time.clock_gettime(time.CLOCK_BOOTTIME) - self.start

    def wait_until(self) -> float:
        """Return the CLOCK.monotonic() time when the job reaches the limit."""
        return client.CLOCK.monotonic() + self.minutes * 60 - self.age_seconds()

    def error_line(self, left: queue_place.QueueWaitLimitReached, *, started_late: bool) -> str:
        step = "started past the limit" if started_late else f"waited {left.waited_seconds // 60} min"
        return (f"::error::build queue: the job ran {int(self.age_seconds()) // 60} min, past "
                + f"{JOB_WAIT_LIMIT_KEY}={self.minutes}; this step {step} and left the queue: {left.facts}")


def building_call(arguments: Sequence[str]) -> bool:
    index = parse.cargo_subcommand_index(arguments)
    if index is None:
        return False
    value = arguments[index]
    for argument in arguments[index + 1:]:
        if argument == "--":
            break
        if argument in ("-h", "--help") or (value != "install" and argument in ("-V", "--version")):
            return False
    return value in parse.CARGO_BUILD_VERBS


def cargo_path(explicit: str, front: Path) -> tuple[str, str]:
    """Keep job tools on PATH and remove folders that would call the front again."""
    front = front.resolve()
    entries: list[str] = []
    command = ""
    for entry in os.get_exec_path():
        folder = Path(entry or ".")
        if folder.resolve() == front.parent:
            continue
        candidate = folder / "cargo"
        executable = candidate.is_file() and os.access(candidate, os.X_OK)
        if executable and candidate.resolve() == front:
            continue
        entries.append(entry)
        if not command and executable:
            command = str(candidate.absolute())
    if explicit:
        command = str(Path(explicit).absolute())
        directory = str(Path(command).parent)
        entries = [directory, *(entry for entry in entries if entry != directory)]
    if command:
        return command, os.pathsep.join(entries)
    raise FileNotFoundError("cargo: no cargo executable after the admission front")


def capture_progress(stream: int, sidecar: Path) -> None:
    parser = progress.CounterParser()
    while data := os.read(stream, 65536):
        _ = os.write(sys.stderr.fileno(), data)
        counter = parser.consume(data.decode(errors="replace"), complete=False)
        if isinstance(counter, progress.ProgressCounter):
            progress.publish_counter(sidecar, counter)
            progress.progress_path(sidecar, "json").chmod(0o644)
        else:
            progress.progress_path(sidecar, "json").unlink(missing_ok=True)


def cancel(number: int, frame: object) -> None:
    del frame
    raise SystemExit(128 + number)


def end_with_front(front_pid: int) -> None:
    if LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
        os._exit(1)
    if os.getppid() != front_pid:
        os._exit(1)


def job_start(pid: int, proc_root: Path) -> float | None:
    """Return the CLOCK_BOOTTIME seconds when the GitHub job of pid started, or None without a readable Runner.Worker ancestor.

    A self-hosted runner starts one Runner.Worker process for each job, so its start is the job start.
    """
    seen: set[int] = set()
    while pid > 0 and pid not in seen:
        seen.add(pid)
        try:
            name, fields = process_stat.name_and_fields(pid, proc_root)
            if name == "Runner.Worker":
                return int(fields[19]) / os.sysconf("SC_CLK_TCK")
            pid = int(fields[1])
        except (OSError, IndexError, ValueError):
            return None
    return None


def remove_abandoned_folders(shared_folder: Path) -> None:
    """Remove this user's shared admission folders when their owners are not live."""
    uid = os.getuid()
    prefix = f"build-admission-ci-{uid}-"
    with os.scandir(shared_folder) as entries:
        for entry in entries:
            if not entry.name.startswith(prefix):
                continue
            try:
                if not entry.is_dir(follow_symlinks=False) or entry.stat(follow_symlinks=False).st_uid != uid:
                    continue
                owner = entry.name[len(prefix):].split("-", 2)
                if (len(owner) == 3 and owner[0].isdecimal() and owner[1].isdecimal()
                        and records.process_start_time(int(owner[0])) == owner[1]):
                    continue
                shutil.rmtree(entry.path)
            except FileNotFoundError:
                pass


def run_cargo(command: str, arguments: Sequence[str], child_path: str, shared_folder: Path | JobTemporaryFolder) -> int:
    pid = os.getpid()
    start = records.process_start_time(pid)
    if start is None:
        raise RuntimeError("cargo admission owner has no process identity")
    limit: JobWaitLimit | None = None
    if (job := job_start(pid, PROC_ROOT)) is not None:
        try:
            limit = JobWaitLimit(job, conf.read_or_repository("build_queue").value(JOB_WAIT_LIMIT_KEY, conf.positive_whole))
        except conf.Unusable as error:
            print(f"::error::build queue: {error}", file=sys.stderr)
            return 1
    elif os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning::build queue: no readable Runner.Worker ancestor, so this step has no {JOB_WAIT_LIMIT_KEY} deadline",
              file=sys.stderr)
    argv = [command, *arguments]
    repository, worktree = step_estimator.git_identity(argv, os.getcwd())
    width = records.test_threads(argv)
    test_width: records.TestWidth = records.RequestedTestWidth(width) if width else records.UnspecifiedTestWidth()
    prefix = f"build-admission-ci-{os.getuid()}-"
    if isinstance(shared_folder, Path):
        remove_abandoned_folders(shared_folder)
        prefix += f"{pid}-{start}-"
    with tempfile.TemporaryDirectory(prefix=prefix,
                                     dir=shared_folder if isinstance(shared_folder, Path) else None) as folder:
        directory = Path(folder)
        directory.chmod(0o755)
        sidecar = directory / "cargo.cgroup"
        _ = sidecar.write_text("")
        sidecar.chmod(0o644)
        request = wire.ServiceEstimateRequest(client.request_id(), wire.Owner(pid, start), tuple(argv), os.getcwd(),
            client.CLOCK.now(), str(sidecar), repository, worktree,
            os.environ.get("RUNNER_NAME", ""), os.environ.get("GITHUB_JOB", ""), test_width,
            route=records.direct_cargo_route())
        wait_until = limit.wait_until() if limit is not None else math.inf
        started_late = wait_until <= client.CLOCK.monotonic()
        with ExitStack() as reservation:
            try:
                held = reservation.enter_context(queue_place.held_reservation(request, wait_until=wait_until))
            except queue_place.QueueWaitLimitReached as error:
                assert limit is not None
                # The runner reads workflow commands from stderr too; stdout can be the caller's pipe.
                print(limit.error_line(error, started_late=started_late), file=sys.stderr, flush=True)
                return 1
            except queue_place.QueuePlaceRefused as error:
                print(error, file=sys.stderr)
                return 1
            grant = held.grant
            if not isinstance(grant, wire.RunnerAdmitted):
                print("the service estimate grant has no runner timing", file=sys.stderr)
                return 1
            place = queue_place.QueuePlace(sidecar, sidecar, configuration=grant.execution,
                purpose=queue_place.ProgramPurpose.RUNNER, arguments=tuple(argv))
            environment = dict(os.environ, PATH=child_path, CARGO_TERM_PROGRESS_WHEN="always", CARGO_TERM_PROGRESS_WIDTH="80")
            status = 1
            cleanup = ExitStack()
            try:
                _ = adopt_descendants()
                # The main thread forks because the death signal follows the thread that forks.
                process = place.popen(argv, env=environment, stderr=subprocess.PIPE,
                                      preexec_fn=partial(end_with_front, pid))
                stream = process.stderr
                if stream is None:
                    raise RuntimeError("cargo progress pipe is unavailable")
                reader = threading.Thread(target=capture_progress, args=(stream.fileno(), sidecar), daemon=True)
                reader.start()
                _ = cleanup.callback(stream.close)
                _ = cleanup.callback(reader.join)
                status = process.wait()
            except SystemExit as error:
                status = error.code if isinstance(error.code, int) else 1
                raise
            finally:
                with cancellation.defer_cleanup_signals():
                    try:
                        place.finish(failed=status != 0)
                    finally:
                        try:
                            cleanup.close()
                        finally:
                            try:
                                while os.waitpid(-1, os.WNOHANG)[0]:
                                    pass
                            except ChildProcessError:
                                pass
                            finally:
                                held.status = status
        return status if status >= 0 else 128 - status


def main(argv: Sequence[str] | None = None) -> int:
    call = parse.cargo_front_call(sys.argv[1:] if argv is None else argv)
    arguments = list(call.arguments)
    explicit = call.cargo
    front = Path(call.front) if call.front else Path(__file__)
    # The admission service must read the counter files, and `nix develop` makes TMPDIR private.
    shared_folder: Path | JobTemporaryFolder = Path(call.shared_folder) if call.shared_folder else JobTemporaryFolder()
    command, child_path = cargo_path(explicit, front)
    if not building_call(arguments):
        os.execve(command, [command, *arguments], dict(os.environ, PATH=child_path))
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        _ = signal.signal(number, cancel)
    return run_cargo(command, arguments, child_path, shared_folder)


if __name__ == "__main__":
    raise SystemExit(main())

"""Take a Mac queue placement, run through SSH, and return the placement.

It sets no signal handler. A signal ends it with its default action; its
children share its process group; the queue ends the placement when this
process ends; and the Mac work ends when the SSH input closes.
"""
from __future__ import annotations

try:
    with open(__file__[:__file__.rfind("/") + 1] + "../lib/package_root.py") as package_root:
        exec(package_root.read(), {"__file__": package_root.name})
except FileNotFoundError:
    pass

import argparse
from collections.abc import Callable
import contextlib
import fcntl
import json
import math
from dataclasses import dataclass
from enum import Enum
import os
import select
import signal
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import threading
import time
from typing import TextIO, TypedDict, cast, final
import uuid

from scripts.lib import conf, process_group
from scripts.lib.atomic_file import atomic_sample, write_text_atomic


def admission_command() -> list[str]:
    return [os.path.expanduser(word) for word in shlex.split(conf.read("mac_gate").text("admission_client_command"))]


@dataclass(frozen=True)
class MacPlacement:
    host: str
    episode: str
    attempt: str


def parse_placement(output: str) -> MacPlacement:
    for line in reversed(output.splitlines()):
        fields = line.split("\t")
        if len(fields) == 6:
            return MacPlacement(fields[2], fields[4], fields[5])
    raise ValueError("the queue returned no placement")


def placement_arguments(episode: str, since: float, pid: int, worktree: Path, package: str,
                        kind: str, source_key: str, session: str, assignment_file: Path,
                        argv: list[str]) -> list[str]:
    return ["place-step", "--once", "--wait-events", "--episode", episode, "--since", str(since),
        "--pid", str(pid), "--repo-root", str(worktree), "--package", package, "--kind", kind,
        "--source-key", source_key, "--session-id", session, "--target", "mac",
        "--assignment-file", str(assignment_file), "--token", "none", "--", *argv]


def remote_wrapper(work: str, on_close: str, grace_polls: int, *, owned_close: bool = False) -> str:
    """Run the work in the SSH session's process group, and stop the work when the SSH input closes.

    zsh refuses job control without a terminal, so the work shares the session's group. When the
    input closes, the watcher ends every other process of the group, then runs `on_close` (work
    such as a GUI app that started outside the group).
    """
    if owned_close:
        # The gate hook stops the guest. Leave the hook alive until it finishes.
        return ("work=" + shlex.quote(work) + "\non_close=" + shlex.quote(on_close) + r"""
exec 3<&0
{
    read -r _ <&3
    exec /bin/zsh -c "$on_close"
} </dev/null >/dev/null 2>&1 &!
/bin/zsh -c "$work" </dev/null
printf '\nmac_exit=%s\n' $?
""")
    return ("work=" + shlex.quote(work) + "\non_close=" + shlex.quote(on_close)
            + f"\ngrace_polls={grace_polls}" + r"""
exec 3<&0
group=$(ps -o pgid= -p $$)
group=${group//[[:space:]]/}
printf 'mac_group=%s\n' "$group"
{
    read -r _ <&3
    zmodload zsh/system zsh/zselect
    self=$sysparams[pid]
    members() { reply=(${(f)"$(pgrep -g $group)"}); reply=(${reply:#$self}) }
    members; (( ! $#reply )) || kill -TERM $reply
    for _ in {1..$grace_polls}; do members; (( $#reply )) || break; zselect -t 10; done
    members; (( ! $#reply )) || kill -KILL $reply
    [[ -z $on_close ]] || /bin/zsh -c "$on_close"
} </dev/null >/dev/null 2>&1 &
watcher=$!
/bin/zsh -c "$work" </dev/null
code=$?
kill -TERM "$watcher" 2>/dev/null
printf '\nmac_exit=%s\n' $code
""")


def remote_command(placement: MacPlacement, cwd: str, argv: list[str], on_close: str,
                   *, owned_close: bool = False) -> list[str]:
    work = ("cd " + shlex.quote(cwd) + " && " if cwd else "")
    work += "MAC_PLACEMENT=" + shlex.quote(placement.episode + "/" + placement.attempt) + " " + shlex.join(argv)
    # Ten polls a second while the group ends.
    grace_polls = max(1, math.ceil(conf.read("mac_gate").number("terminate_grace_seconds") * 10))
    wrapper = remote_wrapper(work, on_close, grace_polls, owned_close=owned_close)
    return ["ssh", "-o", "BatchMode=yes", placement.host, "/bin/zsh -c " + shlex.quote(wrapper)]


class QueueFailureStep(Enum):
    QUEUE_RUN = "queue-run"
    RUN_DIRECTORY_OPEN = "run-directory-open"
    ADMISSION = "admission"
    PREPARATION = "preparation"
    TRANSPORT = "transport"
    GUEST = "guest"
    REMOTE_STOP = "remote-stop"
    CLEANUP = "cleanup"
    SSH_CLOSE = "ssh-closure"
    LEASE_CLOSE = "lease-closure"
    RUN_RECORD_REMOVE = "run-record-removal"
    RECEIPT_REMOVE = "receipt-removal"
    RUN_DIRECTORY_REMOVE = "run-directory-removal"
    RUN_DIRECTORY_CLOSE = "run-directory-closure"
    RECOVERY_SCAN = "recovery-scan"
    RECOVERY_LOCK = "recovery-lock"
    RECOVERY_CLEANUP = "recovery-cleanup"
    PLACEMENT_RELEASE = "placement-release"
    COMMAND_CLOSE = "command-closure"
    COMMAND_TERMINATE = "command-termination"
    COMMAND_GRACE = "command-grace-wait"
    COMMAND_KILL = "command-kill"
    COMMAND_REAP = "command-reap"
    PREPARATION_INTERRUPT = "preparation-interrupt"
    CALLER_WATCH_START = "caller-watch-start"
    CALLER_WATCH_MONITOR = "caller-watch-monitor"
    CALLER_WATCH_SHUTDOWN = "caller-watch-shutdown"


@dataclass(frozen=True)
class QueueStepFailure:
    step: QueueFailureStep
    status: int

    def line(self) -> str:
        return f"{self.step.value} {self.status}\n"


def read_queue_failures(path: Path) -> tuple[QueueStepFailure, ...]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, ValueError):
        return ()
    failures: list[QueueStepFailure] = []
    for line in lines:
        with contextlib.suppress(ValueError):
            step, status = line.split()
            failures.append(QueueStepFailure(QueueFailureStep(step), int(status)))
    return tuple(failures)


def queue_diagnostic(message: str) -> None:
    with contextlib.suppress(OSError):
        print(f"mac queue: {message}", file=sys.stderr)


@dataclass(frozen=True, eq=False)
class QueueFinalizer:
    step: QueueFailureStep
    action: Callable[[], int | None]
    failure_exit_status: int


@final
class QueueSteps:
    def __init__(self, failure_file: Path | None) -> None:
        self.failure_file = failure_file
        self.failures: list[QueueStepFailure] = []
        self.deferred_exit: BaseException | None = None
        self.record_lock = threading.Lock()
        self.finalizers: list[QueueFinalizer] = []
        self.watcher_wakeup = threading.Event()

    def checkpoint(self) -> int:
        return len(self.finalizers)

    def defer(self, step: QueueFailureStep, action: Callable[[], int | None],
              *, failure_exit_status: int = 0) -> QueueFinalizer:
        finalizer = QueueFinalizer(step, action, failure_exit_status)
        self.finalizers.append(finalizer)
        return finalizer

    def finish_first(self, finalizer: QueueFinalizer) -> None:
        self.finalizers.remove(finalizer)
        self.finalizers.append(finalizer)

    def finalize(self, checkpoint: int) -> int:
        status = 0
        while len(self.finalizers) > checkpoint:
            finalizer = self.finalizers.pop()
            result = self.execute(finalizer.step, finalizer.action)
            if result:
                status = status or finalizer.failure_exit_status or result
        return status

    def execute(self, step: QueueFailureStep, action: Callable[[], int | None], *, finalize: bool = False) -> int:
        """Record the action before running any finalizer it registered."""
        checkpoint = self.checkpoint()
        try:
            status = self._execute_action(step, action)
        finally:
            closed = self.finalize(checkpoint) if finalize else 0
        return status or closed

    def _execute_action(self, step: QueueFailureStep, action: Callable[[], int | None]) -> int:
        detail = ""
        deferred_exit = None
        try:
            status = action() or 0
        except BaseException as error:
            status = 2
            detail = f": {error}"
            if not isinstance(error, Exception):
                status = 1
                detail = f": {type(error).__name__}"
                # The failed watcher sends SIGINT to wake the queue thread.
                if isinstance(error, KeyboardInterrupt) and self.watcher_wakeup.is_set():
                    self.watcher_wakeup.clear()
                else:
                    deferred_exit = error
        if status:
            with self.record_lock:
                if self.deferred_exit is None:
                    self.deferred_exit = deferred_exit
                self._record(QueueStepFailure(step, status))
            if step is QueueFailureStep.CLEANUP:
                label = "lifecycle cleanup"
            elif step is QueueFailureStep.RECOVERY_CLEANUP:
                label = "cleanup of an ended run"
            else:
                label = step.value.replace("-", " ")
            queue_diagnostic(f"{label} failed{detail or f' ({status})'}")
        return status

    def _record(self, failure: QueueStepFailure) -> None:
        self.failures.append(failure)
        if self.failure_file is None:
            return
        try:
            atomic_sample(self.failure_file, "".join(item.line() for item in self.failures))
        except BaseException as error:
            if self.deferred_exit is None and not isinstance(error, Exception):
                self.deferred_exit = error
            detail = str(error) or type(error).__name__
            queue_diagnostic(f"cannot record {failure.step.value} failure (status {failure.status}): {detail}")


class RemoteState(Enum):
    NOT_STARTED = "not started"
    LIVE = "live"
    EXITED = "exited"


@dataclass
class RemoteRun:
    state: RemoteState = RemoteState.NOT_STARTED
    group: int = 0


def stop_remote(placement: MacPlacement, remote: RemoteRun, steps: QueueSteps) -> bool:
    if remote.group == 0:
        return False
    grace = conf.read("mac_gate").number("terminate_grace_seconds")
    # The installed helper checks the group id before it sends; the ps check below proves the stop.
    script = f"""group={remote.group}
signal_group() {{ "$HOME/.claude/scripts/lib/py" "$HOME/.claude/scripts/lib/process_group.py" "$group" "$1" 2>/dev/null }}
signal_group TERM
sleep {grace}
signal_group KILL
groups=$(ps -axo pgid=) || exit 1
if printf '%s\\n' "$groups" | grep -Eq "^[[:space:]]*$group[[:space:]]*$"; then exit 1; fi
printf 'mac_stopped=%s\\n' "$group"
"""
    code, output = capture_command(["ssh", "-o", "BatchMode=yes", placement.host,
                                    "/bin/zsh -c " + shlex.quote(script)], steps, stderr=subprocess.DEVNULL)
    return code == 0 and output.strip() == f"mac_stopped={remote.group}"


def own_command[Output: (str, bytes)](process: subprocess.Popen[Output],
                                      steps: QueueSteps, *,
                                      close_step: QueueFailureStep = QueueFailureStep.COMMAND_CLOSE
                                      ) -> subprocess.Popen[Output]:
    kill_sent = False

    def terminate() -> None:
        if process.poll() is None:
            process.terminate()

    def wait_for_termination() -> None:
        if process.poll() is None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                _ = process.wait(timeout=conf.read("mac_gate").number("terminate_grace_seconds"))

    def kill() -> None:
        nonlocal kill_sent
        if process.poll() is None:
            process.kill()
            kill_sent = True

    def reap() -> None:
        if kill_sent or process.poll() is not None:
            _ = process.wait()

    for stream in (process.stdin, process.stderr, process.stdout):
        if stream is not None:
            _ = steps.defer(close_step, stream.close)
    _ = steps.defer(QueueFailureStep.COMMAND_REAP, reap)
    _ = steps.defer(QueueFailureStep.COMMAND_KILL, kill)
    _ = steps.defer(QueueFailureStep.COMMAND_GRACE, wait_for_termination)
    _ = steps.defer(QueueFailureStep.COMMAND_TERMINATE, terminate)
    return process


def capture_command(command: list[str], steps: QueueSteps, *, stderr: int | None = None) -> tuple[int, str]:
    process = own_command(subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr, text=True), steps)
    assert process.stdout is not None
    output = cast(TextIO, process.stdout).read()
    return process.wait(), output


def stream_remote(command: list[str], remote: RemoteRun, steps: QueueSteps) -> int:
    process = own_command(subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace"),
        steps, close_step=QueueFailureStep.SSH_CLOSE)
    remote.state = RemoteState.LIVE
    assert process.stdout is not None
    last = ""
    for line in cast(TextIO, process.stdout):
        if line.startswith("mac_group=") and line.removeprefix("mac_group=").strip().isdigit():
            remote.group = int(line.removeprefix("mac_group="))
            continue
        print(line, end="", flush=True)
        last = line.rstrip("\n")
    if last.startswith("mac_exit=") and last.removeprefix("mac_exit=").isdigit():
        status = int(last.removeprefix("mac_exit="))
        remote.state = RemoteState.EXITED
        return status
    queue_diagnostic("the Mac returned no exit status")
    return process.wait() or 255


def interrupt_preparation(process: subprocess.Popen[str], steps: QueueSteps) -> None:
    if isinstance(steps.deferred_exit, KeyboardInterrupt) and process.poll() is None:
        # Let preparation stop its commands and remove its temporary refs.
        process.send_signal(signal.SIGINT)
        with contextlib.suppress(subprocess.TimeoutExpired):
            _ = process.wait(timeout=conf.read("mac_gate").number("terminate_grace_seconds"))


def run_callback(script: str, phase: str, argv_file: Path, arguments: list[str],
                 environment: dict[str, str], steps: QueueSteps,
                 *, interrupt_prepare: bool = False) -> int:
    process = own_command(subprocess.Popen([script, phase, str(argv_file), *arguments],
                                           env=environment, text=True), steps)
    if interrupt_prepare:
        _ = steps.defer(QueueFailureStep.PREPARATION_INTERRUPT, lambda: interrupt_preparation(process, steps))
    return process.wait()


MAC_BUSY_STATUS = 78
RUNS = ".local/state/mac-queue/runs"
RECEIPTS = ".local/state/mac-queue/receipts"


class SavedRun(TypedDict):
    lifecycle_script: str
    arguments: list[str]
    host: str


def own_descriptor(steps: QueueSteps, step: QueueFailureStep, descriptor: int) -> QueueFinalizer:
    return steps.defer(step, lambda: os.close(descriptor))


def remove_receipt(episode: str) -> None:
    (Path.home() / RECEIPTS / (episode + ".json")).unlink(missing_ok=True)


def run_directory(episode: str, steps: QueueSteps) -> Path:
    """Lock the state folder until the run ends."""
    runs = Path.home() / RUNS
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    starting = runs / (episode + ".new")
    starting.mkdir(mode=0o700)
    directory = starting

    def remove_if_complete() -> None:
        if directory == starting or not (directory / "run.json").exists():
            shutil.rmtree(directory)

    removal = steps.defer(QueueFailureStep.RUN_DIRECTORY_REMOVE, remove_if_complete)
    descriptor = os.open(starting / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    _ = own_descriptor(steps, QueueFailureStep.RUN_DIRECTORY_CLOSE, descriptor)
    steps.finish_first(removal)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    directory = starting.rename(runs / episode)
    return directory


def recover_run(directory: Path, steps: QueueSteps, environment: dict[str, str]) -> int:
    checkpoint = steps.checkpoint()
    locked = False
    receipt_removed = False

    def try_lock() -> None:
        nonlocal locked
        try:
            descriptor = os.open(directory / "lock", os.O_RDWR)
        except FileNotFoundError:
            return
        _ = own_descriptor(steps, QueueFailureStep.RUN_DIRECTORY_CLOSE, descriptor)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        locked = directory.exists()

    def cleanup() -> int:
        record = directory / "run.json"
        if not record.exists():
            return 0
        saved = cast(SavedRun, json.loads(record.read_text()))
        return run_callback(saved["lifecycle_script"], "cleanup", directory / "argv.json",
                            saved["arguments"], {**environment, "MAC_QUEUE_HOST": saved["host"]}, steps)

    def remove_recovery_receipt() -> None:
        nonlocal receipt_removed
        remove_receipt(directory.name)
        receipt_removed = True

    def remove_recovery_directory() -> None:
        if receipt_removed:
            shutil.rmtree(directory)

    status = steps.execute(QueueFailureStep.RECOVERY_LOCK, try_lock)
    if locked and not status:
        status = steps.execute(QueueFailureStep.RECOVERY_CLEANUP, cleanup, finalize=True)
        if status:
            queue_diagnostic(f"kept {directory}")
        else:
            _ = steps.defer(QueueFailureStep.RUN_DIRECTORY_REMOVE, remove_recovery_directory)
            _ = steps.defer(QueueFailureStep.RECEIPT_REMOVE, remove_recovery_receipt)
    closed = steps.finalize(checkpoint)
    return status or closed


def clean_ended_runs(steps: QueueSteps, environment: dict[str, str]) -> int:
    """Clean ended runs before starting new Mac work."""
    directories: list[Path] = []

    def scan() -> None:
        runs = Path.home() / RUNS
        directories.extend(sorted(directory for directory in runs.iterdir() if not directory.name.endswith(".new")))

    status = steps.execute(QueueFailureStep.RECOVERY_SCAN, scan)
    for directory in directories:
        if steps.deferred_exit is not None:
            break
        recovery_status = recover_run(directory, steps, environment)
        status = status or recovery_status
    return status


class Arguments(argparse.Namespace):
    what: str = ""
    kind: str = ""
    package: str = ""
    worktree: Path = Path()
    cwd: str = ""
    lifecycle_script: str = ""
    failure_file: Path | None = None
    caller_fd: int = -1
    no_wait: bool = False
    owned_close: bool = False
    argv: list[str] = []


def run(args: Arguments, steps: QueueSteps) -> int:
    episode, since, pid = uuid.uuid4().hex, time.time(), os.getpid()
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    original_command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv.copy()
    command = original_command
    print(f"Mac queue: {args.what}", file=sys.stderr)
    remote = RemoteRun()
    prepared = False
    placement = MacPlacement("", episode, "")
    assignment_file = Path()
    callback_environment = dict(os.environ)
    guest_status = 0
    on_close = ""
    cleaned = False
    safe = True
    directory = Path()
    run_checkpoint = steps.checkpoint()

    def open_run_directory() -> None:
        nonlocal directory, assignment_file
        assignment_file = Path.home() / RECEIPTS / (episode + ".json")
        directory = run_directory(episode, steps)

    status = steps.execute(QueueFailureStep.RUN_DIRECTORY_OPEN, open_run_directory)
    if status:
        return status
    argv_file = directory / "argv.json"
    run_record = directory / "run.json"
    source_key = ""
    code = 0

    def initialize_admission() -> None:
        nonlocal source_key
        assignment_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _ = assignment_file.write_text(json.dumps({"episode": episode, "pid": pid}))
        assignment_file.chmod(0o600)
        code, source = capture_command(["git", "-C", str(args.worktree), "rev-parse", "HEAD"],
                                       steps, stderr=subprocess.DEVNULL)
        source_key = source.strip() if code == 0 else ""

    def request_placement() -> int:
        nonlocal code, placement
        code, output = capture_command(admission_command() + placement_arguments(episode, since, pid,
            args.worktree, args.package, args.kind, source_key, session, assignment_file, command), steps)
        if code:
            print(output, end="", flush=True)
            return 0 if code == MAC_BUSY_STATUS and not args.no_wait else code
        placement = parse_placement(output)
        callback_environment["MAC_QUEUE_HOST"] = placement.host
        return 0

    def admit() -> int:
        status = steps.execute(QueueFailureStep.ADMISSION, initialize_admission, finalize=True)
        while not status:
            status = steps.execute(QueueFailureStep.ADMISSION, request_placement, finalize=True)
            if status or code != MAC_BUSY_STATUS:
                break
            status = steps.execute(QueueFailureStep.ADMISSION,
                                   lambda: time.sleep(conf.read("build_admission").number("waiting_pass_seconds")))
        return status

    def prepare() -> int:
        nonlocal prepared, command, on_close
        if not args.lifecycle_script:
            return 0
        saved: SavedRun = {"lifecycle_script": args.lifecycle_script, "arguments": original_command,
                           "host": placement.host}
        write_text_atomic(run_record, json.dumps(saved))
        prepared = True
        code = run_callback(args.lifecycle_script, "prepare", argv_file, original_command,
                            callback_environment, steps, interrupt_prepare=args.owned_close)
        if code:
            return code
        command = cast(list[str], json.loads(argv_file.read_text()))
        close_path = argv_file.with_suffix(".json.close")
        on_close = close_path.read_text() if close_path.exists() else ""
        return 0

    def acquire_lease() -> None:
        lease_path = argv_file.with_suffix(".json.lease")
        if prepared and lease_path.exists():
            descriptor = os.open(lease_path.read_text().strip(), os.O_WRONLY | os.O_CREAT, 0o600)
            _ = own_descriptor(steps, QueueFailureStep.LEASE_CLOSE, descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX)

    def transport() -> int:
        nonlocal guest_status, safe
        if args.owned_close and not on_close:
            raise ValueError("the run needs a close hook")
        safe = False
        code = stream_remote(remote_command(placement, args.cwd, command, on_close,
                                            owned_close=args.owned_close), remote, steps)
        if remote.state is not RemoteState.EXITED:
            return code
        guest_status = code
        return 0

    def cleanup() -> int:
        return run_callback(args.lifecycle_script, "cleanup", argv_file, original_command, callback_environment, steps)

    def remove_run_record() -> None:
        if cleaned:
            run_record.unlink()

    def release() -> int:
        if not safe:
            queue_diagnostic("the Mac run may still be live; placement release is withheld")
            queue_diagnostic("the placement was not returned; the queue ends it when this run ends")
            return 1
        reason = (f"the Mac run ended with status {status}" if remote.state is not RemoteState.NOT_STARTED
                  else "the Mac run did not start")
        process = own_command(subprocess.Popen(admission_command() + ["cancel-placement", "--episode", episode,
            "--pid", str(pid), "--reason", reason], stdout=subprocess.DEVNULL), steps)
        result = process.wait()
        if result:
            queue_diagnostic("the placement was not returned; the queue ends it when this run ends")
        return result

    # Remove the receipt after returning the placement, with the run lock held.
    _ = steps.defer(QueueFailureStep.RECEIPT_REMOVE, lambda: remove_receipt(episode))
    _ = steps.defer(QueueFailureStep.PLACEMENT_RELEASE, release, failure_exit_status=1)
    _ = steps.defer(QueueFailureStep.RUN_RECORD_REMOVE, remove_run_record)
    lease_checkpoint = steps.checkpoint()
    ssh_checkpoint = lease_checkpoint

    def work() -> None:
        nonlocal status, ssh_checkpoint
        status = admit()
        if args.lifecycle_script and status in (10, 11, 12):
            status = 9
        if not status:
            status = clean_ended_runs(steps, callback_environment)
        if not status:
            status = steps.execute(QueueFailureStep.PREPARATION, prepare, finalize=True)
        if not status:
            status = steps.execute(QueueFailureStep.PREPARATION, acquire_lease)
        ssh_checkpoint = steps.checkpoint()
        if not status:
            status = steps.execute(QueueFailureStep.TRANSPORT, transport)
        if not status:
            status = steps.execute(QueueFailureStep.GUEST, lambda: guest_status)

    # Record an interruption between steps before running the pending finalizers.
    work_status = steps.execute(QueueFailureStep.QUEUE_RUN, work)
    status = status or work_status
    safe = remote.state is not RemoteState.LIVE
    if not args.owned_close:
        close_status = steps.finalize(ssh_checkpoint)
        status = status or close_status
        if not safe:
            stop_status = steps.execute(QueueFailureStep.REMOTE_STOP,
                                       lambda: int(not stop_remote(placement, remote, steps)), finalize=True)
            safe = stop_status == 0
            status = status or stop_status
    if prepared and (safe or args.owned_close):
        cleanup_status = steps.execute(QueueFailureStep.CLEANUP, cleanup, finalize=True)
        status = status or cleanup_status
        cleaned = cleanup_status == 0
        if cleaned:
            safe = True
        elif args.owned_close and remote.state is not RemoteState.NOT_STARTED:
            safe = False
    # Owned cleanup uses the close hook while SSH still holds its input open.
    lease_status = steps.finalize(lease_checkpoint)
    status = status or lease_status
    run_status = steps.finalize(run_checkpoint)
    return status or run_status


CALLER_EXITED = 3


def watch_caller(caller: int, queue: int) -> int:
    """Report readiness and wait for a caller or queue exit."""
    watch = select.poll()
    try:
        watch.register(queue, select.POLLIN)
        watch.register(caller, select.POLLIN)
        if watch.poll(0):
            return 2
        print("ready", flush=True)
        events = dict(watch.poll())
        if queue in events:
            return 0
        if events.get(caller, 0) & (select.POLLIN | select.POLLHUP):
            return CALLER_EXITED
    except OSError as error:
        print(f"mac queue: caller watch failed: {error}", file=sys.stderr)
    return 2


def caller_watch(caller: int, failed: threading.Event, steps: QueueSteps) -> None:
    queue_thread = threading.get_ident()
    closing = threading.Event()
    _ = own_descriptor(steps, QueueFailureStep.CALLER_WATCH_SHUTDOWN, caller)
    if os.getpgrp() != os.getpid():
        raise ValueError("the Mac queue must own its process group")
    reader, writer = os.pipe()
    _ = own_descriptor(steps, QueueFailureStep.CALLER_WATCH_SHUTDOWN, reader)
    queue_input = own_descriptor(steps, QueueFailureStep.CALLER_WATCH_SHUTDOWN, writer)
    watcher = own_command(subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "watch-caller", str(caller), str(reader)],
        pass_fds=(caller, reader), start_new_session=True,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE), steps)
    assert watcher.stdout is not None
    output = watcher.stdout.fileno()
    readiness = select.poll()
    readiness.register(output, select.POLLIN)
    grace = conf.read("mac_gate").number("terminate_grace_seconds")
    if not readiness.poll(math.ceil(grace * 1000)) or os.read(output, 6) != b"ready\n":
        raise ValueError("the Mac queue caller watch did not start")

    def check_watcher() -> int:
        status = watcher.wait()
        return 0 if closing.is_set() or status == CALLER_EXITED else status or 2

    def monitor() -> None:
        status = steps.execute(QueueFailureStep.CALLER_WATCH_MONITOR, check_watcher)
        if closing.is_set():
            return
        if status:
            queue_diagnostic(f"caller watch ended unexpectedly ({status})")
            failed.set()
            steps.watcher_wakeup.set()
        # Wake the queue thread if it is blocked reading SSH output.
        wake_status = steps.execute(QueueFailureStep.CALLER_WATCH_MONITOR,
                                    lambda: signal.pthread_kill(queue_thread, signal.SIGINT))
        if wake_status:
            steps.watcher_wakeup.clear()

    def wait_for_watcher() -> None:
        try:
            status = watcher.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            status = 2
        if failed.is_set() or status not in (0, CALLER_EXITED):
            failed.set()
            raise ValueError(f"the Mac queue caller watch ended with status {status}")

    _ = steps.defer(QueueFailureStep.CALLER_WATCH_SHUTDOWN, wait_for_watcher)
    steps.finish_first(queue_input)
    _ = steps.defer(QueueFailureStep.CALLER_WATCH_SHUTDOWN, closing.set)
    threading.Thread(target=monitor, daemon=True).start()


def main(argv: list[str] | None = None) -> int:
    words = sys.argv[1:] if argv is None else argv
    if len(words) == 3 and words[0] == "watch-caller":
        return watch_caller(int(words[1]), int(words[2]))
    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--what", required=True)
    _ = parser.add_argument("--kind", choices=("shot", "install", "probe", "run"), required=True)
    _ = parser.add_argument("--package", required=True)
    _ = parser.add_argument("--worktree", type=Path, default=Path.cwd())
    _ = parser.add_argument("--cwd", default="")
    _ = parser.add_argument("--lifecycle-script", default="")
    _ = parser.add_argument("--failure-file", type=Path, help=argparse.SUPPRESS)
    _ = parser.add_argument("--caller-fd", type=int, help=argparse.SUPPRESS)
    _ = parser.add_argument("--owned-close", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--no-wait", action="store_true", help="exit with status 78 when the Mac is busy")
    _ = parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv, namespace=Arguments())
    if not args.argv or args.argv == ["--"]:
        parser.error("an argv is required")
    steps = QueueSteps(args.failure_file)
    failed = threading.Event()
    status = 0

    def stop_failed_group() -> None:
        if failed.is_set():
            process_group.terminate_owned_process_group()

    def serve() -> None:
        nonlocal status
        # Terminate the group after the other finalizers run.
        _ = steps.defer(QueueFailureStep.CALLER_WATCH_SHUTDOWN, stop_failed_group)
        if args.caller_fd >= 0:
            status = steps.execute(QueueFailureStep.CALLER_WATCH_START,
                                   lambda: caller_watch(args.caller_fd, failed, steps))
        if not status:
            status = run(args, steps)

    shutdown = steps.execute(QueueFailureStep.QUEUE_RUN, serve, finalize=True)
    if steps.deferred_exit is not None:
        raise steps.deferred_exit
    return status or shutdown


if __name__ == "__main__":
    raise SystemExit(main())

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

import codecs
from collections.abc import Callable, Generator
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
import socket
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING, IO, NotRequired, TypedDict, cast
import uuid

from scripts.agents.worker_lifecycle import operation_deadline
from scripts.lib import conf, process_group
from scripts.lib.operation_deadline import remaining
from scripts.lib.atomic_file import create_text_atomic, sync_folder, write_text_atomic
from scripts.mac_test.claim_state import (
    CREATION_LEASE_FD,
    QueueProcessUnsettled,
    StateReadError,
    assigned_gate_paths,
    capture_cleanup_command,
    close_process_pipes,
    owned_groups_alive,
    queue_runs_directory,
    terminate_and_wait,
    wait_for_exit,
)

if TYPE_CHECKING:
    from scripts.build_queue import admission_wire as wire, execution_identity, placement
    from scripts.build_queue.placement import ClaimHolderIdentity, PlacementAssignment
    from scripts.mac_test import claim_state, offload
    from scripts.mac_test.claim_state import AssignedGatePaths


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


class RemoteState(Enum):
    NOT_STARTED = "not started"
    LIVE = "live"
    EXITED = "exited"


@dataclass
class RemoteRun:
    state: RemoteState = RemoteState.NOT_STARTED
    group: int = 0
    transport_pid: int = 0


def stop_remote(placement: MacPlacement, remote: RemoteRun) -> bool:
    from scripts.mac_test.mac_gate import checkout_cleanup_seconds

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
    result = capture_cleanup_command(["ssh", "-o", "BatchMode=yes", placement.host,
                                      "/bin/zsh -c " + shlex.quote(script)], checkout_cleanup_seconds())
    return result.returncode == 0 and result.stdout.strip() == f"mac_stopped={remote.group}"


class QueueCancelled(Exception):
    """The caller ended before the queue finished."""


@dataclass(frozen=True)
class QueueCaller:
    """Descriptors that become readable when the queue must stop work."""

    descriptors: tuple[int, ...] = ()

    def ready(self, descriptor: int = -1, timeout: float = 0) -> None:
        watch = select.poll()
        for caller in self.descriptors:
            watch.register(caller, select.POLLIN)
        if descriptor >= 0:
            watch.register(descriptor, select.POLLIN)
        events = dict(watch.poll(None if timeout < 0 else math.ceil(timeout * 1000)))
        if any(caller in events for caller in self.descriptors):
            raise QueueCancelled("the Mac queue was cancelled")

    def wait(self, process: subprocess.Popen[str]) -> int:
        if self.descriptors:
            descriptor = os.pidfd_open(process.pid)
            try:
                self.ready(descriptor, timeout=-1)
            finally:
                os.close(descriptor)
        return process.wait()

    def lines(self, output: IO[str]) -> Generator[str]:
        if not self.descriptors:
            yield from output
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        descriptor = output.fileno()
        pending: list[str] = []
        while True:
            self.ready(descriptor, timeout=-1)
            chunk = os.read(descriptor, 65536)
            *lines, tail = decoder.decode(chunk, final=not chunk).split("\n")
            for line in lines:
                pending.append(line)
                yield "".join(pending) + "\n"
                pending.clear()
            if tail:
                pending.append(tail)
            if not chunk:
                if pending:
                    yield "".join(pending)
                return


NO_QUEUE_CALLER = QueueCaller()


def capture_command(command: list[str], caller: QueueCaller = NO_QUEUE_CALLER) -> tuple[int, str]:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        output = "".join(caller.lines(process.stdout))
        return caller.wait(process), output
    finally:
        close_process_pipes(process)
        terminate_and_wait(process)


def print_remote_line(line: str) -> None:
    print(line, end="", flush=True)


def stream_remote(command: list[str], remote: RemoteRun, ssh: contextlib.ExitStack,
                  emit: Callable[[str], None] = print_remote_line, *,
                  caller: QueueCaller = NO_QUEUE_CALLER) -> int:
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding="utf-8", errors="replace")
    _ = ssh.callback(close_process_pipes, process)
    _ = ssh.callback(terminate_and_wait, process)
    remote.state = RemoteState.LIVE
    remote.transport_pid = process.pid
    assert process.stdout is not None
    last = ""
    for line in caller.lines(process.stdout):
        if line.startswith("mac_group=") and line.removeprefix("mac_group=").strip().isdigit():
            remote.group = int(line.removeprefix("mac_group="))
            continue
        emit(line)
        last = line.rstrip("\n")
    ssh_status = caller.wait(process)
    if last.startswith("mac_exit=") and last.removeprefix("mac_exit=").isdigit():
        remote.state = RemoteState.EXITED
        return int(last.removeprefix("mac_exit="))
    print("mac queue: the Mac returned no exit status", file=sys.stderr)
    return ssh_status or 255


def stop_preparation(process: subprocess.Popen[str], until: float) -> None:
    """Stop the active commands so prepare can remove its temporary files."""
    from scripts.lib.process_start import Started, proc_table_start
    from scripts.lint.memory_admit import process_table, process_tree

    proc = Path("/proc")
    tree = process_table(proc)
    owner = tree.get(process.pid)
    if owner is None:
        return
    for pid in process_tree({process.pid: owner.start}, tree) - {process.pid}:
        with contextlib.suppress(ProcessLookupError):
            descriptor = os.pidfd_open(pid)
            try:
                if proc_table_start(pid, proc) == Started(tree[pid].start):
                    # Git can inherit the gate's mask for TERM, HUP and INT.
                    signal.pidfd_send_signal(descriptor, signal.SIGKILL)
            finally:
                os.close(descriptor)
    with contextlib.suppress(subprocess.TimeoutExpired, TimeoutError):
        _ = wait_for_exit(process, until)


@contextlib.contextmanager
def creation_lease(path: Path, mode: int) -> Generator[int]:
    """Hold a lock while a preparation command can create files."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, mode | fcntl.LOCK_NB)
        yield descriptor
    finally:
        os.close(descriptor)


def run_callback(script: str, phase: str, argv_file: Path, arguments: list[str],
                 environment: dict[str, str], *, interrupt_prepare: bool = False,
                 caller: QueueCaller = NO_QUEUE_CALLER, timeout: float | None = None) -> int:
    command = [script, phase, str(argv_file), *arguments]
    grace = conf.read("mac_gate").number("terminate_grace_seconds")
    descriptors: tuple[int, ...] = ()
    lease = creation_lease_path(argv_file) if interrupt_prepare else argv_file.with_suffix(".json.cleaning")
    try:
        with contextlib.ExitStack() as cleanup:
            if interrupt_prepare or timeout is not None:
                mode = fcntl.LOCK_SH if interrupt_prepare else fcntl.LOCK_EX
                descriptor = cleanup.enter_context(creation_lease(lease, mode))
                environment = {**environment, CREATION_LEASE_FD: str(descriptor)}
                descriptors = (descriptor,)
            budget = timeout if timeout is not None else float("inf")
            with operation_deadline(timeout=budget) as deadline:
                process = subprocess.Popen(command, env=environment, text=True, pass_fds=descriptors, process_group=0)
                try:
                    if timeout is None:
                        status = caller.wait(process)
                    else:
                        status = wait_for_exit(process, deadline - min(grace, remaining()) / 2)
                except (KeyboardInterrupt, QueueCancelled):
                    if interrupt_prepare and process.poll() is None:
                        stop_preparation(process, time.monotonic() + grace)
                    raise
                finally:
                    terminate_and_wait(process, deadline=deadline, grace=grace, owns_group=True)
    finally:
        if descriptors:
            # Reopen the lease after this process closes its copy.
            try:
                with creation_lease(lease, fcntl.LOCK_EX):
                    pass
            except BlockingIOError as error:
                raise QueueProcessUnsettled("A callback child still holds the Mac gate lease") from error
    return status


def creation_lease_path(argv_file: Path) -> Path:
    return argv_file.with_suffix(".json.creating")


MAC_BUSY_STATUS = 78
RECEIPTS = ".local/state/mac-queue/receipts"


class SavedAssignedClaim(TypedDict):
    assignment: str


class SavedGateOwner(TypedDict):
    pid: int
    start: str
    boot: str


class GateSettlement(Enum):
    RELEASE_PENDING = "release-pending"
    RELEASE_CONFIRMED = "release-confirmed"


class SavedRun(TypedDict):
    lifecycle_script: str
    arguments: list[str]
    host: str
    claim: NotRequired[SavedAssignedClaim]
    lifecycle_started: NotRequired[bool]
    completion_report: NotRequired[str]
    gate_owner: NotRequired[SavedGateOwner]
    manual_request: NotRequired[bool]
    settlement: GateSettlement


@dataclass(frozen=True)
class NoMacCompletionReport:
    """The journal has no validated completion for the assignment."""


@dataclass(frozen=True)
class AllQueueRuns:
    """Recover manual runs and assigned runs."""


@dataclass(frozen=True)
class AssignedRunsOnTarget:
    """Recover assigned runs for one claim watcher."""

    target: str


ALL_QUEUE_RUNS = AllQueueRuns()


@dataclass(frozen=True)
class QueueRunOutsideSelection:
    """This watcher leaves the run to its owner."""


def encode_run(saved: SavedRun) -> str:
    return json.dumps({**saved, "settlement": saved["settlement"].value})


@dataclass(frozen=True)
class RecoveryRunJournal:
    """A writer must keep these locks until the write process exits."""

    path: Path
    locks: tuple[int, ...]


def save_run(path: Path | RecoveryRunJournal, saved: SavedRun) -> None:
    if isinstance(path, RecoveryRunJournal):
        from scripts.mac_test.mac_gate import checkout_cleanup_seconds

        result = capture_cleanup_command(
            [sys.executable, str(Path(__file__).resolve()), "write-run", path.path.stem],
            checkout_cleanup_seconds(), pass_fds=path.locks, input_text=encode_run(saved))
        if result.returncode:
            raise ValueError(result.stderr.strip() or "The Mac recovery journal write failed")
    else:
        write_text_atomic(path, encode_run(saved), new_file_mode=0o600)


def read_run_journal(path: Path) -> SavedRun:
    from scripts.aws_target import ledger

    row = ledger.mapping(cast(object, json.loads(path.read_text())))
    if "lifecycle_started" in row and type(row["lifecycle_started"]) is not bool:
        raise ValueError("the Mac journal has an invalid preparation state")
    if "settlement" in row:
        if "release_confirmed" in row:
            raise ValueError("the Mac journal has two settlement records")
        settlement = GateSettlement(row["settlement"])
    else:
        confirmed = row.pop("release_confirmed", False)
        if type(confirmed) is not bool:
            raise ValueError("the Mac journal has an invalid release confirmation")
        settlement = GateSettlement.RELEASE_CONFIRMED if confirmed else GateSettlement.RELEASE_PENDING
    return cast(SavedRun, cast(object, {**row, "settlement": settlement}))


def run_journal(directory: Path) -> SavedRun:
    return read_run_journal(directory / "run.json")


@dataclass(frozen=True)
class NoRegisteredMacRun:
    """The attempt has no assigned gate journal."""


NO_REGISTERED_MAC_RUN = NoRegisteredMacRun()


@dataclass(frozen=True)
class RegisteredMacRun:
    assignment: PlacementAssignment
    paths: AssignedGatePaths
    saved: SavedRun
    owner: SavedGateOwner

    @property
    def holder(self) -> ClaimHolderIdentity:
        from scripts.build_queue import placement

        return placement.ClaimHolderIdentity(self.assignment.owner.host, self.owner["pid"], self.owner["start"])


@dataclass(frozen=True)
class NoEndedManualMacRun:
    """There is no ended manual run at this journal."""


@dataclass(frozen=True)
class ManualMacRun:
    paths: AssignedGatePaths
    saved: SavedRun
    owner: SavedGateOwner


def registered_mac_run_exists(assignment: PlacementAssignment) -> bool:
    try:
        paths = assigned_gate_paths(assignment)
    except ValueError:
        return False
    return paths.journal.exists()


def read_registered_mac_run(assignment: PlacementAssignment) -> RegisteredMacRun | NoRegisteredMacRun:
    try:
        paths = assigned_gate_paths(assignment)
    except ValueError:
        return NoRegisteredMacRun()
    try:
        saved = read_run_journal(paths.journal)
    except FileNotFoundError:
        return NoRegisteredMacRun()
    if ("claim" not in saved or "gate_owner" not in saved or read_assignment(saved["claim"]["assignment"]) != assignment
            or saved["host"] != assignment.destination.host
            or len(saved["arguments"]) != 2 or Path(saved["arguments"][0]) != paths.request):
        raise ValueError("the Mac journal does not match the registered attempt")
    owner = saved["gate_owner"]
    if type(owner["pid"]) is not int or owner["pid"] <= 1 or not owner["start"] or not owner["boot"]:
        raise ValueError("the Mac journal has no valid runner identity")
    return RegisteredMacRun(assignment, paths, saved, owner)



def saved_completion(saved: SavedRun, assignment: PlacementAssignment) -> wire.ExecutionReport | NoMacCompletionReport:
    from scripts.build_queue import admission_wire as wire, placement

    if "completion_report" not in saved:
        return NoMacCompletionReport()
    report = wire.parse_request(saved["completion_report"])
    if (not isinstance(report, wire.ExecutionReport) or report.assignment != assignment
            or report.claim != placement.assigned_claim_reference(assignment)
            or not isinstance(report.outcome, (placement.AttemptCompleted, placement.AttemptCancelled,
                                               placement.PrelaunchRefused))
            or not isinstance(report.path, placement.DispatchPathSettled)):
        raise ValueError("the Mac journal has an invalid completion report")
    return report


def journaled_mac_completion(assignment: PlacementAssignment) -> wire.ExecutionReport | NoMacCompletionReport:
    """Read the completion record without waiting for the runner lock."""
    try:
        paths = assigned_gate_paths(assignment)
    except ValueError:
        return NoMacCompletionReport()
    registered = read_registered_mac_run(assignment)
    if isinstance(registered, RegisteredMacRun):
        return saved_completion(registered.saved, assignment)
    try:
        saved = run_journal(paths.directory)
    except FileNotFoundError:
        return NoMacCompletionReport()
    if "claim" not in saved or read_assignment(saved["claim"]["assignment"]) != assignment:
        return NoMacCompletionReport()
    return saved_completion(saved, assignment)


@contextlib.contextmanager
def run_directory(episode: str) -> Generator[Path]:
    """Hold this run's state folder. Its lock ends with the process, so a later run finds a run that a signal ended."""
    runs = queue_runs_directory()
    runs.mkdir(parents=True, exist_ok=True, mode=0o700)
    starting = runs / (episode + ".new")
    starting.mkdir(mode=0o700)
    descriptor = os.open(starting / "lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        directory = runs / episode
        _ = starting.rename(directory)
        try:
            yield directory
        finally:
            if not (directory / "run.json").exists():
                shutil.rmtree(directory, ignore_errors=True)
    finally:
        os.close(descriptor)


def clean_ended_runs(environment: dict[str, str], selection: AllQueueRuns | AssignedRunsOnTarget = ALL_QUEUE_RUNS) -> None:
    """Finish the cleanup of each run that ended before its own, such as by a signal."""
    from scripts.build_queue.admission_wire import AdmissionUnavailable

    runs = queue_runs_directory()
    if isinstance(selection, AllQueueRuns):
        for run in read_ended_manual_mac_runs():
            try:
                recover_manual_mac_run(run, environment)
            except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
                print(f"mac queue: manual gate cleanup remains unresolved: {error}", file=sys.stderr)
    for directory in sorted(runs.iterdir()) if runs.is_dir() else []:
        if not directory.is_dir() or directory.name.endswith(".new"):
            continue
        try:
            descriptor = os.open(directory / "lock", os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            record = directory / "run.json"
            try:
                if record.exists():
                    saved = run_journal(directory)
                    status = clean_ended_run(directory, saved, environment, selection)
                    if isinstance(status, QueueRunOutsideSelection):
                        continue
                    if status:
                        print(f"mac queue: cleanup of an ended run failed ({status}); kept {directory}", file=sys.stderr)
                        continue
                elif isinstance(selection, AssignedRunsOnTarget):
                    continue
            except (AdmissionUnavailable, OSError, ValueError, KeyError, TypeError) as error:
                print(f"mac queue: cleanup of an ended run failed: {error}; kept {directory}", file=sys.stderr)
                continue
            (Path.home() / RECEIPTS / (directory.name + ".json")).unlink(missing_ok=True)
            shutil.rmtree(directory, ignore_errors=True)
        finally:
            os.close(descriptor)


def recover_ended_assigned_runs(target: str) -> None:
    clean_ended_runs(dict(os.environ), AssignedRunsOnTarget(target))


def clean_ended_run(directory: Path, saved: SavedRun, environment: dict[str, str],
                    selection: AllQueueRuns | AssignedRunsOnTarget) -> int | QueueRunOutsideSelection:
    if "claim" not in saved:
        if isinstance(selection, AssignedRunsOnTarget):
            return QueueRunOutsideSelection()
        return cleanup_lifecycle(directory, saved, environment, ended=True)
    assignment = read_assignment(saved["claim"]["assignment"])
    if isinstance(selection, AssignedRunsOnTarget) and assignment.destination.target != selection.target:
        return QueueRunOutsideSelection()
    _ = settle_assigned_run(directory, saved, environment, assignment, read_recoverable_claim(assignment),
                            transport=contextlib.ExitStack(), ended=True)
    return 0


def cleanup_lifecycle(directory: Path, saved: SavedRun, environment: dict[str, str], *, ended: bool = False) -> int:
    if not saved.get("lifecycle_started", True):
        return 0
    from scripts.mac_test.mac_gate import checkout_cleanup_seconds

    timeout = checkout_cleanup_seconds() + conf.read("mac_gate").number("terminate_grace_seconds")
    argv_file = directory / "argv.json"
    lease = creation_lease_path(argv_file)
    with contextlib.ExitStack() as leases:
        if lease.exists():
            try:
                _ = leases.enter_context(creation_lease(lease, fcntl.LOCK_EX))
            except BlockingIOError as error:
                raise ValueError("the Mac preparation command is still active") from error
        script, phase = (ended_run_callback(saved["lifecycle_script"]) if ended
                         else (Path(saved["lifecycle_script"]), "cleanup"))
        return run_callback(str(script), phase, argv_file,
                            saved["arguments"], {**environment, "MAC_QUEUE_HOST": saved["host"]}, timeout=timeout)


def prepare_lifecycle(directory: Path, saved: SavedRun, journal: Path, environment: dict[str, str], *,
                      owned_close: bool, caller: QueueCaller = NO_QUEUE_CALLER) -> int:
    caller.ready()
    saved["lifecycle_started"] = True
    save_run(journal, saved)
    return run_callback(saved["lifecycle_script"], "prepare", directory / "argv.json",
                        saved["arguments"], environment, interrupt_prepare=owned_close, caller=caller)


def lifecycle_arguments(argv_file: Path, lease: contextlib.ExitStack) -> tuple[list[str], str]:
    command = cast(list[str], json.loads(argv_file.read_text()))
    close_path = argv_file.with_suffix(".json.close")
    lease_path = argv_file.with_suffix(".json.lease")
    if lease_path.exists():
        descriptor = os.open(lease_path.read_text().strip(), os.O_WRONLY | os.O_CREAT, 0o600)
        _ = lease.callback(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    return command, close_path.read_text() if close_path.exists() else ""


def read_assignment(text: str) -> PlacementAssignment:
    from scripts.build_queue import admission_wire as wire

    reply = wire.parse_reply(text)
    if not isinstance(reply, wire.Assignment) or reply.assignment.destination.provider != "mac":
        raise ValueError("the runner requires a Mac assignment")
    return reply.assignment


def assigned_journal_owns_claim(target: str, identity: claim_state.ClaimIdentity, *, registered_only: bool = False) -> bool:
    """Keep journaled claims out of the watcher's idle release path."""
    try:
        paths = assigned_gate_paths(identity.attempt)
    except ValueError:
        return False
    try:
        try:
            saved = read_run_journal(paths.journal)
        except FileNotFoundError:
            if registered_only:
                return False
            saved = run_journal(paths.directory)
        if "claim" not in saved:
            return False
        assignment = read_assignment(saved["claim"]["assignment"])
    except FileNotFoundError:
        return False
    except ValueError as error:
        raise StateReadError(f"cannot read the Mac run journal {paths.journal}: {error}") from error

    from scripts.build_queue import placement

    reference = placement.assigned_claim_reference(assignment)
    return ((reference.target, reference.claim_id, reference.episode, reference.attempt)
            == (target, identity.claim_id, identity.episode, identity.attempt))


def read_recoverable_claim(assignment: PlacementAssignment) -> offload.AssignedMacClaim | offload.MacClaimSettled:
    from scripts.lib import process_start
    from scripts.mac_test import offload

    claim = offload.read_assigned_mac_claim(assignment)
    if isinstance(claim, offload.MacClaimUnconfirmed):
        raise ValueError(claim.reason)
    if isinstance(claim, offload.AssignedMacClaim):
        record = claim.record
        start = process_start.process_start(record["pid"])
        if isinstance(start, process_start.Unreadable) or start == process_start.Started(record["proc_start"]):
            raise ValueError("the Mac claim holder can still be live")
    return claim


def release_assigned_claim(
    assignment: PlacementAssignment, claim: offload.AssignedMacClaim | offload.MacClaimSettled,
    *, wait_for_provider_lock: bool,
) -> None:
    from scripts.buildlog import store
    from scripts.build_queue import placement
    from scripts.mac_test import claim_state, mac_test, offload

    if isinstance(claim, offload.AssignedMacClaim):
        reference = placement.assigned_claim_reference(assignment)
        identity = claim_state.ClaimIdentity(reference.claim_id, reference.episode, reference.attempt)
        record = claim.record
        holder = placement.ClaimHolderIdentity(store.host_name(), record["pid"], record["proc_start"])
        released = mac_test.release_exact_claims(claim_state.state_paths(reference.target), holder, identity, "",
                                                wait_for_provider_lock=wait_for_provider_lock)
        if isinstance(released, mac_test.ClaimReleaseRefused):
            raise ValueError(released.reason)
    if not isinstance(offload.read_assigned_mac_claim(assignment), offload.MacClaimSettled):
        raise ValueError("the exact Mac claim has not settled")


def save_assigned_outcome(saved: SavedRun, path: Path | RecoveryRunJournal, assignment: PlacementAssignment,
                          outcome: placement.AttemptCompleted | placement.AttemptCancelled | placement.PrelaunchRefused,
                          evidence: execution_identity.ExecutionEvidence) -> wire.ExecutionReport:
    from scripts.build_queue import admission_wire as wire, placement

    report = wire.ExecutionReport(uuid.uuid4().hex, assignment, placement.assigned_claim_reference(assignment),
                                  evidence, outcome, placement.DispatchPathSettled())
    saved["completion_report"] = wire.encode_request(report)
    save_run(path, saved)
    return report


def registered_mac_runner_live(run: RegisteredMacRun) -> bool:
    return gate_runner_live(run.owner)


def gate_runner_live(owner: SavedGateOwner) -> bool:
    from scripts.lib import process_start
    from scripts.lib.boot_identity import boot_identity
    if owner["boot"] != boot_identity():
        return False
    start = process_start.process_start(owner["pid"])
    return isinstance(start, process_start.Unreadable) or start == process_start.Started(owner["start"])


def queue_creators_ended(owner: SavedGateOwner, *, allowed: tuple[int, ...] = ()) -> bool:
    """Check for creation children that can still act in the runner session."""
    from scripts.lib.boot_identity import boot_identity

    if boot_identity() != owner["boot"]:
        return True
    budget = conf.read("mac_gate").number("terminate_grace_seconds")
    members = capture_cleanup_command(["ps", "-eo", "pid=,sid=,stat="], budget)
    members.check_returncode()
    return not any(int(session) == owner["pid"] and not state.startswith("Z") and int(pid) not in allowed
                   for pid, session, state in (line.split() for line in members.stdout.splitlines()))


def settle_assigned_run(directory: Path, saved: SavedRun, environment: dict[str, str],
                        assignment: PlacementAssignment, claim: offload.AssignedMacClaim | offload.MacClaimSettled,
                        *, transport: contextlib.ExitStack,
                        registered: RegisteredMacRun | NoRegisteredMacRun = NO_REGISTERED_MAC_RUN,
                        transport_pid: int = 0, recovery_locks: tuple[int, ...] = (),
                        ended: bool = False) -> wire.ExecutionReport | NoMacCompletionReport:
    """End checkout work and confirm exact release before reporting settlement."""
    from scripts.build_queue import admission_wire as wire

    durable = isinstance(registered, RegisteredMacRun)
    path = registered.paths.journal if isinstance(registered, RegisteredMacRun) else directory / "run.json"
    journal = RecoveryRunJournal(path, recovery_locks) if recovery_locks else path
    try:
        if isinstance(registered, RegisteredMacRun) and not queue_creators_ended(
                registered.owner, allowed=(os.getpid(), transport_pid)):
            raise ValueError("Mac creation processes remain active")
        if cleanup_lifecycle(directory, saved, environment, ended=ended):
            raise ValueError("the Mac checkout cleanup remains unresolved")
        if saved.get("lifecycle_started", True):
            saved["lifecycle_started"] = False
            save_run(journal, saved)
    finally:
        transport.close()
    release_assigned_claim(assignment, claim, wait_for_provider_lock=not durable)
    report = saved_completion(saved, assignment)
    if durable:
        saved["settlement"] = GateSettlement.RELEASE_CONFIRMED
        save_run(journal, saved)
    else:
        if isinstance(report, wire.ExecutionReport):
            _ = report_assigned_settlement(report)
        path.unlink()
    return report


def report_assigned_settlement(report: wire.ExecutionReport) -> wire.DispatchStatus:
    from scripts.build_queue import admission_client, placement

    reply = admission_client.report_execution(report)
    outcome = report.outcome
    reason = ("completed" if isinstance(outcome, placement.AttemptCompleted)
              else "cancelled" if isinstance(outcome, placement.AttemptCancelled)
              else outcome.reason if isinstance(outcome, placement.PrelaunchRefused) else "")
    if reply.assignment != report.assignment or reply.state != placement.ClosedAssignment(reason):
        raise ValueError(reply.reason or "the service did not confirm Mac gate settlement")
    if isinstance(outcome, placement.PrelaunchRefused) and reply.retry != placement.DoNotRetry(reason):
        raise ValueError("the service did not confirm Mac gate refusal")
    return reply


def finish_registered_mac_run(run: RegisteredMacRun, report: wire.ExecutionReport) -> None:
    from scripts.build_queue import admission_wire as wire, placement
    from scripts.mac_test import mac_gate

    reply = report_assigned_settlement(report)
    folder = run.paths.request
    if isinstance(report.outcome, placement.PrelaunchRefused):
        _ = (folder / mac_gate.ASSIGNED_REFUSAL).write_text(wire.encode_reply(reply))
    elif isinstance(report.outcome, placement.AttemptCompleted):
        _ = (folder / mac_gate.ASSIGNED_COMPLETION).write_text(run.assignment.attempt)
    remove_gate_plan(run.paths)


def remove_gate_plan(paths: AssignedGatePaths) -> None:
    """Remove the plan after confirmed settlement."""
    for folder in (paths.request_root, paths.directory, paths.directory.with_suffix(".new")):
        with contextlib.suppress(FileNotFoundError):
            shutil.rmtree(folder)
    if paths.request_root.parent.exists():
        sync_folder(paths.request_root.parent)
    (Path.home() / RECEIPTS / (paths.directory.name + ".json")).unlink(missing_ok=True)
    sync_folder(paths.journal.parent)
    paths.journal.unlink()
    sync_folder(paths.journal.parent)
    paths.preparation_lock.unlink(missing_ok=True)
    paths.lock.unlink(missing_ok=True)


def remove_recovered_gate_plan(paths: AssignedGatePaths, locks: tuple[int, ...]) -> None:
    """Give a removal process the locks until it exits."""
    from scripts.mac_test.mac_gate import checkout_cleanup_seconds

    result = capture_cleanup_command(
        [sys.executable, str(Path(__file__).resolve()), "remove-run", paths.journal.stem],
        checkout_cleanup_seconds(), pass_fds=locks)
    if result.returncode:
        raise ValueError(result.stderr.strip() or f"removal ended with status {result.returncode}")


@contextlib.contextmanager
def gate_run_locks(paths: AssignedGatePaths) -> Generator[tuple[int, ...]]:
    descriptors: list[int] = []
    with contextlib.ExitStack() as locks:
        for path in (paths.lock, paths.preparation_lock):
            descriptor = locks.enter_context(creation_lease(path, fcntl.LOCK_EX))
            descriptors.append(descriptor)
        yield tuple(descriptors)


def read_ended_manual_mac_runs() -> Generator[ManualMacRun]:
    """Find manual plans that need recovery without a placement."""
    for path in queue_runs_directory().glob("*.json"):
        try:
            paths = assigned_gate_paths(path.stem)
            run = read_ended_manual_mac_run(paths)
            if isinstance(run, ManualMacRun):
                yield run
        except FileNotFoundError:
            continue
        except (OSError, ValueError, KeyError, TypeError) as error:
            print(f"mac queue: cannot read the manual gate plan: {error}", file=sys.stderr)


def read_ended_manual_mac_run(paths: AssignedGatePaths) -> ManualMacRun | NoEndedManualMacRun:
    saved = read_run_journal(paths.journal)
    if (saved.get("manual_request") is not True or "gate_owner" not in saved or "claim" in saved
            or len(saved["arguments"]) != 2 or Path(saved["arguments"][0]) != paths.request
            or gate_runner_live(saved["gate_owner"])):
        return NoEndedManualMacRun()
    return ManualMacRun(paths, saved, saved["gate_owner"])


def recover_manual_mac_run(run: ManualMacRun, environment: dict[str, str]) -> None:
    paths = run.paths
    try:
        with gate_run_locks(paths) as locks:
            latest = read_ended_manual_mac_run(paths)
            if not isinstance(latest, ManualMacRun) or not queue_creators_ended(latest.owner):
                return
            if not cleanup_lifecycle(paths.directory, latest.saved, environment, ended=True):
                remove_recovered_gate_plan(paths, locks)
    except (BlockingIOError, FileNotFoundError):
        return


def recover_registered_mac_run(run: RegisteredMacRun, *, service_settled: bool) -> None:
    """Resume cleanup for an ended runner while holding its run and preparation locks."""
    from scripts.build_queue import execution_identity, placement
    from scripts.mac_test import claim_state, offload

    paths = run.paths
    try:
        with gate_run_locks(paths) as locks:
            latest = read_registered_mac_run(run.assignment)
            if isinstance(latest, NoRegisteredMacRun):
                return
            run = latest
            if registered_mac_runner_live(run):
                return
            if run.saved["settlement"] is GateSettlement.RELEASE_CONFIRMED:
                if service_settled:
                    remove_recovered_gate_plan(run.paths, locks)
                return
            if "completion_report" not in run.saved:
                evidence = (execution_identity.ExecutionEvidenceUnavailable("the Mac runner ended", time.time())
                            if run.saved.get("lifecycle_started", True) else execution_identity.ExecutionNotStarted())
                _ = save_assigned_outcome(run.saved, RecoveryRunJournal(paths.journal, locks), run.assignment,
                                          placement.AttemptCancelled("the Mac queue ended"), evidence)
            assignment = run.assignment
            claim = read_recoverable_claim(assignment)
            if isinstance(claim, offload.AssignedMacClaim) and not claim.held_by(assignment, run.holder):
                raise ValueError("the registered Mac claim holder changed")
            environment = {**os.environ, "MAC_QUEUE_SESSION": claim_state.session_id(assignment.session)}
            _ = settle_assigned_run(paths.directory, run.saved, environment, assignment, claim,
                                    transport=contextlib.ExitStack(), registered=run, recovery_locks=locks, ended=True)
    except BlockingIOError:
        # A live owner or a preparation command still holds a lock.
        return


def refuse_assigned_launch(assignment: PlacementAssignment, folder: Path, reason: str) -> int:
    from scripts.build_queue import admission_wire as wire, execution_identity, placement
    from scripts.mac_test.mac_gate import ASSIGNED_REFUSAL

    status = report_assigned_settlement(wire.ExecutionReport(
        uuid.uuid4().hex, assignment, placement.assigned_claim_reference(assignment),
        execution_identity.ExecutionNotStarted(), placement.PrelaunchRefused(reason), placement.DispatchPathSettled()))
    _ = (folder / ASSIGNED_REFUSAL).write_text(wire.encode_reply(status))
    return 2


@contextlib.contextmanager
def run_attempt_directory(assignment: PlacementAssignment,
                          registered: RegisteredMacRun | NoRegisteredMacRun) -> Generator[Path]:
    if isinstance(registered, RegisteredMacRun):
        registered.paths.directory.mkdir(mode=0o700)
        yield registered.paths.directory
    else:
        with run_directory(assignment.attempt) as directory:
            yield directory


def run_assigned(args: Arguments, encoded: str) -> int:
    """Run the saved attempt and leave unresolved claims for recovery."""
    from scripts.aws_target import cloud_gate
    from scripts.build_queue import admission_client, admission_wire as wire, execution_identity, placement
    from scripts.buildlog import store
    from scripts.lib import process_start
    from scripts.mac_test import claim_state, eligibility, mac_gate, offload

    assignment = read_assignment(encoded)
    destination = assignment.destination
    if not assignment.attempt.isascii() or not assignment.attempt.isalnum():
        raise ValueError("the Mac attempt identifier is invalid")
    if not args.owned_close or not args.lifecycle_script:
        raise ValueError("an assigned Mac gate requires lifecycle cleanup")
    arguments = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    folder = Path(arguments[0])
    place = MacPlacement(destination.host, assignment.episode, assignment.attempt)
    environment = {**os.environ, "MAC_QUEUE_HOST": destination.host,
                   "MAC_QUEUE_SESSION": claim_state.session_id(assignment.session)}
    registered = args.registered_run
    saved: SavedRun = (registered.saved if isinstance(registered, RegisteredMacRun) else {
        "lifecycle_script": args.lifecycle_script, "arguments": arguments, "host": place.host,
        "claim": {"assignment": encoded}, "lifecycle_started": False, "settlement": GateSettlement.RELEASE_PENDING})
    if (isinstance(registered, NoRegisteredMacRun)
            and isinstance(eligibility.read_backoff(time.time(), destination.target), eligibility.ActiveBackoff)):
        return refuse_assigned_launch(assignment, folder, "backoff")
    clean_ended_runs(environment)
    start = process_start.process_start(os.getpid())
    if not isinstance(start, process_start.Started):
        raise ValueError("the Mac claim holder has no process identity")
    holder = placement.ClaimHolderIdentity(store.host_name(), os.getpid(), start.start)
    reference = placement.assigned_claim_reference(assignment)
    with run_attempt_directory(assignment, registered) as directory:
        path = registered.paths.journal if isinstance(registered, RegisteredMacRun) else directory / "run.json"
        if isinstance(registered, NoRegisteredMacRun):
            save_run(path, saved)
        controller = offload.ProcessController(claim_in_runner_group=True)
        authorize = wire.ConsumePermission(uuid.uuid4().hex, assignment, reference, holder)
        refusal = ""
        status = 2
        remote = RemoteRun()
        result_lines = mac_gate.MacGateResultLines()
        authorized = False

        def emit(line: str) -> None:
            result_lines.record(line)
            print_remote_line(line)

        with contextlib.ExitStack() as lease, contextlib.ExitStack() as ssh:
            try:
                args.caller.ready()
                if (isinstance(registered, RegisteredMacRun)
                        and isinstance(eligibility.read_backoff(time.time(), destination.target), eligibility.ActiveBackoff)):
                    claimed = offload.MacClaimNotConfirmed("backoff", "", "backoff")
                else:
                    claimed = offload.claim_for_assigned_mac_run(controller, assignment, args.worktree)
                args.caller.ready()
                identity = controller.claim_identity
                if isinstance(claimed, offload.MacClaimNotConfirmed):
                    refusal = claimed.reason
                elif not isinstance(claimed, offload.FreshMacClaim) or not isinstance(identity, claim_state.ClaimIdentity):
                    raise ValueError("a previous Mac claim still requires cleanup")
                elif isinstance(eligibility.read_backoff(time.time(), destination.target), eligibility.ActiveBackoff):
                    refusal = "backoff"
                elif prepare_lifecycle(directory, saved, path, environment, owned_close=True, caller=args.caller):
                    refusal = "Mac gate preparation failed"
                else:
                    command, on_close = lifecycle_arguments(directory / "argv.json", lease)
                    if not on_close:
                        raise ValueError("the run needs a close hook")
                    pending = offload.pending_start_path(reference.target, identity)
                    pending.parent.mkdir(parents=True, exist_ok=True)
                    write_text_atomic(pending, offload.pending_start_key(identity))
                    fence = offload.prepare_start_fence_command(identity, destination.target,
                                                                scope_key=controller.claim_scope_key)
                    if isinstance(registered, RegisteredMacRun):
                        fence = mac_gate.remote_preparation_command(folder, fence, mac_gate.RemotePreparationAction.PREPARE)
                    fence_process = subprocess.Popen(["ssh", "-n", "-o", "BatchMode=yes", place.host, fence], text=True)
                    try:
                        prepared = args.caller.wait(fence_process)
                    finally:
                        mac_gate.terminate_and_wait(fence_process)
                    if prepared:
                        refusal = "the Mac start fence could not be prepared"
                    else:
                        args.caller.ready()
                        permission = admission_client.consume_permission(authorize)
                        args.caller.ready()
                        authorized = permission == wire.FirstAuthorization(assignment, reference)
                        if authorized:
                            fenced = offload.fenced_start_command(identity, destination.target, shlex.join(command),
                                                                  scope_key=controller.claim_scope_key)
                            status = stream_remote(remote_command(place, args.cwd, ["/bin/zsh", "-c", fenced],
                                                                  on_close, owned_close=True), remote, ssh, emit, caller=args.caller)
                            if remote.state == RemoteState.EXITED and status == 0:
                                result = result_lines.decode(cloud_gate.read(folder / "request.json"))
                                _ = save_assigned_outcome(saved, path, assignment,
                                    placement.AttemptCompleted(int(not result.run.passed)),
                                    execution_identity.ExecutionEvidenceUnavailable(
                                        "the Mac gate returned a validated result", time.time()))
                        elif (isinstance(permission, wire.DispatchStatus) and permission.assignment == assignment
                              and isinstance(permission.permission, placement.PermissionAvailable)):
                            refusal = permission.reason or "the Mac launch was refused"
            finally:
                if isinstance(controller.pending, offload.PendingClaimOperation):
                    raise QueueProcessUnsettled("The assigned Mac claim remains unresolved")
                claim = offload.read_assigned_mac_claim(assignment)
                if (isinstance(claim, offload.MacClaimUnconfirmed)
                        or isinstance(claim, offload.AssignedMacClaim) and not claim.held_by(assignment, holder)):
                    raise ValueError("the exact Mac claim holder is unconfirmed")
                if isinstance(registered, RegisteredMacRun) and "completion_report" not in saved:
                    outcome = (placement.PrelaunchRefused(refusal) if refusal and not authorized
                               else placement.AttemptCancelled("the Mac gate did not finish"))
                    evidence = (execution_identity.ExecutionEvidenceUnavailable("the Mac result is unavailable", time.time())
                                if authorized else execution_identity.ExecutionNotStarted())
                    _ = save_assigned_outcome(saved, path, assignment, outcome, evidence)
                report = settle_assigned_run(directory, saved, environment, assignment, claim, transport=ssh,
                                             registered=registered, transport_pid=remote.transport_pid)
                if isinstance(registered, RegisteredMacRun) and isinstance(report, wire.ExecutionReport):
                    finish_registered_mac_run(registered, report)
        if isinstance(registered, NoRegisteredMacRun):
            if refusal and not authorized:
                return refuse_assigned_launch(assignment, folder, refusal)
            if isinstance(report, wire.ExecutionReport):
                _ = (folder / mac_gate.ASSIGNED_COMPLETION).write_text(assignment.attempt)
        return status


def ended_run_callback(saved_script: str) -> tuple[Path, str]:
    """Select an installed callback for a known script name."""
    from scripts.production.mac_gate_process import MAC_GATE_SCRIPT

    installed = Path.home() / ".claude"
    name = Path(saved_script).name
    if name == MAC_GATE_SCRIPT.name:
        return installed / MAC_GATE_SCRIPT, "cleanup-ended"
    if name == "mac_run.sh":
        return installed / "scripts/production/mac_run.sh", "cleanup"
    return Path(saved_script), "cleanup"


class Arguments:
    what: str = ""
    kind: str = ""
    package: str = ""
    worktree: Path = Path()
    cwd: str = ""
    lifecycle_script: str = ""
    caller_fd: int = -1
    cancel_fd: int = -1
    no_wait: bool = False
    owned_close: bool = False
    owns_gate_request: bool = False
    gate_preparation_fd: int = -1
    cancel_seconds: float = 0.0
    assignment_file: str = ""
    gate_assignment: str = ""
    registered_run: RegisteredMacRun | NoRegisteredMacRun = NO_REGISTERED_MAC_RUN
    manual_run: ManualMacRun | NoRegisteredMacRun = NO_REGISTERED_MAC_RUN
    argv: list[str] = []
    caller: QueueCaller = NO_QUEUE_CALLER


def run(args: Arguments) -> int:
    if args.assignment_file:
        from scripts.build_queue.admission_wire import AdmissionUnavailable

        try:
            args.caller.ready()
            return run_assigned(args, Path(args.assignment_file).read_text())
        except (AdmissionUnavailable, OSError, ValueError, KeyError, TypeError, QueueCancelled, subprocess.SubprocessError) as error:
            print(f"mac queue: {error}", file=sys.stderr)
            return 2
    from scripts.mac_test.mac_gate import checkout_cleanup_seconds

    manual = args.manual_run
    episode = manual.paths.directory.name if isinstance(manual, ManualMacRun) else uuid.uuid4().hex
    since, pid = time.time(), os.getpid()
    session = os.environ.get("CODEX_THREAD_ID") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    source = subprocess.run(["git", "-C", str(args.worktree), "rev-parse", "HEAD"],
                            capture_output=True, text=True)
    source_key = source.stdout.strip() if source.returncode == 0 else ""
    command = args.argv[1:] if args.argv[:1] == ["--"] else args.argv
    print(f"Mac queue: {args.what}", file=sys.stderr)
    status = 1
    remote = RemoteRun()
    prepared = False
    placement = MacPlacement("", episode, "")
    saved: SavedRun = (manual.saved if isinstance(manual, ManualMacRun) else {
        "lifecycle_script": args.lifecycle_script, "arguments": command.copy(), "host": "",
        "settlement": GateSettlement.RELEASE_PENDING,
    })
    receipts = Path.home() / RECEIPTS
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    assignment_file = receipts / (episode + ".json")
    _ = assignment_file.write_text(json.dumps({"episode": episode, "pid": pid}))
    assignment_file.chmod(0o600)
    if isinstance(manual, ManualMacRun):
        manual.paths.directory.mkdir(mode=0o700)
        run_state: contextlib.AbstractContextManager[Path] = contextlib.nullcontext(manual.paths.directory)
    else:
        run_state = run_directory(episode)
    with run_state as directory, contextlib.ExitStack() as lease, contextlib.ExitStack() as ssh:
        journal = manual.paths.journal if isinstance(manual, ManualMacRun) else directory / "run.json"
        argv_file = directory / "argv.json"
        callback_environment = dict(os.environ)
        try:
            while True:
                args.caller.ready()
                code, output = capture_command(admission_command() + placement_arguments(episode, since, pid,
                    args.worktree, args.package, args.kind, source_key, session, assignment_file, command), args.caller)
                if code != MAC_BUSY_STATUS or args.no_wait:
                    break
                print(output, end="", flush=True)
                args.caller.ready(timeout=conf.read("build_admission").number("waiting_pass_seconds"))
            if code:
                print(output, end="", flush=True)
                status = 9 if args.lifecycle_script and code in (10, 11, 12) else code
            else:
                placement = parse_placement(output)
                callback_environment["MAC_QUEUE_HOST"] = placement.host
                clean_ended_runs(callback_environment)
                on_close = ""
                if args.lifecycle_script:
                    saved["host"] = placement.host
                    prepared = True
                    status = prepare_lifecycle(directory, saved, journal, callback_environment,
                                               owned_close=args.owned_close, caller=args.caller)
                    if status == 0:
                        command, on_close = lifecycle_arguments(argv_file, lease)
                else:
                    status = 0
                if status == 0:
                    # A run that ends before the Mac answers reports a failure, not success.
                    status = 1
                    if args.owned_close and not on_close:
                        raise ValueError("the run needs a close hook")
                    status = stream_remote(remote_command(placement, args.cwd, command, on_close,
                                                          owned_close=args.owned_close), remote, ssh, caller=args.caller)
        except (OSError, ValueError, QueueCancelled) as error:
            print(f"mac queue: {error}", file=sys.stderr)
            status = 2
        finally:
            if not args.owned_close:
                ssh.close()
            safe = remote.state != RemoteState.LIVE
            if not safe and not args.owned_close:
                try:
                    safe = stop_remote(placement, remote)
                except (OSError, subprocess.TimeoutExpired, QueueProcessUnsettled):
                    safe = False
            if prepared and (safe or args.owned_close):
                try:
                    cleanup_status = cleanup_lifecycle(directory, saved, callback_environment)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    print(f"mac queue: lifecycle cleanup failed: {error}", file=sys.stderr)
                    cleanup_status = 2
                if cleanup_status:
                    print(f"mac queue: lifecycle cleanup failed ({cleanup_status})", file=sys.stderr)
                    status = status or cleanup_status
                    if args.owned_close:
                        safe = False
                else:
                    safe = True
                    if isinstance(manual, ManualMacRun):
                        saved["lifecycle_started"] = False
                        save_run(journal, saved)
                    else:
                        journal.unlink()
            # Owned cleanup uses the close hook while SSH still holds its input open.
            ssh.close()
            lease.close()
            # A Mac placement has no local grant, so the run returns it with a cancel that names the result.
            reason = (f"the Mac run ended with status {status}" if remote.state != RemoteState.NOT_STARTED
                      else "the Mac run did not start")
            if safe:
                try:
                    result = capture_cleanup_command(admission_command() + ["cancel-placement", "--episode", episode,
                        "--pid", str(pid), "--reason", reason], checkout_cleanup_seconds())
                    released = result.returncode == 0
                except (OSError, subprocess.TimeoutExpired, QueueProcessUnsettled):
                    released = False
            else:
                released = False
                print("mac queue: the Mac run may still be live; placement release is withheld", file=sys.stderr)
            if not isinstance(manual, ManualMacRun):
                assignment_file.unlink(missing_ok=True)
            if not released:
                print("mac queue: the placement was not returned; the queue ends it when this run ends", file=sys.stderr)
                status = status or 1
    return status


CALLER_EXITED = 3


def watch_caller(caller: int, queue: int, cancel: int = -1) -> int:
    """Report readiness and wait for a caller or queue exit."""
    watch = select.poll()
    try:
        watch.register(queue, select.POLLIN)
        watch.register(caller, select.POLLIN)
        if cancel >= 0:
            watch.register(cancel, select.POLLIN)
        print("ready", flush=True)
        events = dict(watch.poll())
        if queue in events:
            return 0
        if (events.get(caller, 0) | events.get(cancel, 0)) & (select.POLLIN | select.POLLHUP):
            return CALLER_EXITED
    except OSError as error:
        print(f"mac queue: caller watch failed: {error}", file=sys.stderr)
    return 2


@contextlib.contextmanager
def caller_watch(caller: int, cancel: int, cancel_seconds: float = 0.0) -> Generator[QueueCaller]:
    pid = os.getpid()
    if os.getpgrp() != pid:
        raise ValueError("the Mac queue must own its process group")
    reader, writer = os.pipe()
    failure = os.eventfd(0, os.EFD_CLOEXEC | os.EFD_NONBLOCK)
    closing = threading.Event()
    failed = threading.Event()
    descriptors = (caller, cancel) if cancel >= 0 else (caller,)
    lifetime = QueueCaller((*descriptors, failure))
    try:
        try:
            lifetime.ready()
            watched = (caller, reader, cancel) if cancel >= 0 else (caller, reader)
            watcher = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch-caller",
                                        *(str(descriptor) for descriptor in watched)],
                                       pass_fds=watched, start_new_session=True,
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
        finally:
            os.close(reader)
        deadline = 0.0
        try:
            assert watcher.stdout is not None
            output = watcher.stdout.fileno()
            readiness = select.poll()
            readiness.register(output, select.POLLIN)
            grace = conf.read("mac_gate").number("terminate_grace_seconds")
            if not readiness.poll(math.ceil(grace * 1000)) or os.read(output, 6) != b"ready\n":
                raise ValueError("the Mac queue caller watch did not start")

            def monitor(write_failure: int) -> None:
                try:
                    status = watcher.wait()
                    if closing.is_set():
                        return
                    if status != CALLER_EXITED:
                        print(f"mac queue: caller watch ended unexpectedly ({status})", file=sys.stderr, flush=True)
                        failed.set()
                        os.eventfd_write(write_failure, 1)
                    if cancel_seconds > 0 and not closing.wait(cancel_seconds):
                        for group in owned_groups_alive(pid, session=True) - {pid}:
                            with contextlib.suppress(ProcessLookupError):
                                process_group.send_process_group(group, signal.SIGKILL)
                        os.killpg(pid, signal.SIGKILL)
                finally:
                    os.close(write_failure)

            write_failure = os.dup(failure)
            supervisor = threading.Thread(target=monitor, args=(write_failure,), daemon=True)
            try:
                supervisor.start()
            except BaseException:
                os.close(write_failure)
                raise
            try:
                lifetime.ready()
                yield lifetime
            finally:
                closing.set()
                os.close(writer)
                writer = -1
                with operation_deadline(timeout=grace) as deadline:
                    try:
                        status = wait_for_exit(watcher, deadline - grace / 2)
                    except (subprocess.TimeoutExpired, TimeoutError):
                        status = 2
                    terminate_and_wait(watcher, deadline=deadline, grace=grace)
                    supervisor.join(remaining())
                    if status not in (0, CALLER_EXITED):
                        failed.set()
                        raise ValueError(f"the Mac queue caller watch ended with status {status}")
        finally:
            close_process_pipes(watcher)
            if deadline == 0:
                terminate_and_wait(watcher)

    finally:
        os.close(failure)
        for descriptor in descriptors:
            os.close(descriptor)
        if writer >= 0:
            os.close(writer)
        if failed.is_set():
            process_group.terminate_owned_process_group()


def gate_plan(args: Arguments, paths: AssignedGatePaths) -> tuple[SavedRun, SavedGateOwner]:
    from scripts.lib import process_start
    from scripts.lib.boot_identity import boot_identity

    start = process_start.process_start(os.getpid())
    if not isinstance(start, process_start.Started) or Path(args.argv[-2]) != paths.request:
        raise ValueError("the Mac gate request does not match its runner or attempt")
    owner: SavedGateOwner = {"pid": os.getpid(), "start": start.start, "boot": boot_identity()}
    return {
        "lifecycle_script": args.lifecycle_script, "arguments": args.argv[1:], "host": "",
        "gate_owner": owner, "lifecycle_started": False, "settlement": GateSettlement.RELEASE_PENDING,
    }, owner


@contextlib.contextmanager
def own_gate_plan(paths: AssignedGatePaths, saved: SavedRun) -> Generator[None]:
    paths.journal.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not create_text_atomic(paths.journal, encode_run(saved)):
        raise ValueError("the Mac gate attempt already has a journal")
    descriptor = os.open(paths.lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


@contextlib.contextmanager
def register_mac_run(args: Arguments, assignment: PlacementAssignment) -> Generator[RegisteredMacRun]:
    from scripts.build_queue import admission_client, admission_wire as wire, placement

    paths = assigned_gate_paths(assignment)
    saved, owner = gate_plan(args, paths)
    saved["host"] = assignment.destination.host
    saved["claim"] = {"assignment": args.gate_assignment}
    run = RegisteredMacRun(assignment, paths, saved, owner)
    with own_gate_plan(paths, saved):
        result = admission_client.placement_status(assignment.episode, assignment.attempt).status
        if (not isinstance(result, wire.KnownPlacementStatus) or result.assignment != assignment
                or result.state != placement.CurrentAssignment()):
            raise ValueError("the admission service did not confirm ownership of the Mac gate attempt")
        yield run


@contextlib.contextmanager
def own_manual_gate_plan(args: Arguments, folder: Path) -> Generator[ManualMacRun]:
    from scripts.mac_test.mac_gate import attempt_name

    paths = assigned_gate_paths(attempt_name(folder).removeprefix("mac-gate-"))
    saved, owner = gate_plan(args, paths)
    saved["manual_request"] = True
    with own_gate_plan(paths, saved):
        yield ManualMacRun(paths, saved, owner)


def settle_unprepared_request(run: RegisteredMacRun) -> None:
    """Settle an aborted handoff if the gate has stopped writing its request."""
    from scripts.build_queue import admission_wire as wire, execution_identity, placement
    from scripts.mac_test.mac_gate import gate_preparation_lock

    try:
        with contextlib.ExitStack() as locks:
            locks.enter_context(gate_preparation_lock(run.paths.preparation_lock))
            _ = save_assigned_outcome(run.saved, run.paths.journal, run.assignment,
                                      placement.AttemptCancelled("the Mac gate did not prepare its request"),
                                      execution_identity.ExecutionNotStarted())
            report = settle_assigned_run(run.paths.directory, run.saved, dict(os.environ), run.assignment,
                                         read_recoverable_claim(run.assignment),
                                         transport=contextlib.ExitStack(), registered=run)
            if isinstance(report, wire.ExecutionReport):
                finish_registered_mac_run(run, report)
    except BlockingIOError:
        # The service can finish this handoff after the gate stops writing.
        return


@contextlib.contextmanager
def own_gate_request(args: Arguments) -> Generator[None]:
    """Take ownership before the gate creates its request."""
    from scripts.mac_test.mac_gate import gate_preparation_lock, gate_requests_directory

    if len(args.argv) != 3 or args.argv[0] != "--":
        raise ValueError("the Mac gate requires a request folder and suite selection")
    folder = Path(args.argv[-2])
    root = folder.parent
    if (not args.owned_close or not args.lifecycle_script or args.caller_fd < 0
            or args.gate_preparation_fd < 0 or not math.isfinite(args.cancel_seconds) or args.cancel_seconds <= 0
            or not root.name.startswith("mac-gate-") or not folder.name.startswith("mac-gate-")
            or root.parent.resolve() != gate_requests_directory().resolve() or root.is_symlink()):
        raise ValueError("the queue cannot own this gate request folder")
    if root.exists():
        raise ValueError("the Mac gate request folder already exists")
    with contextlib.ExitStack() as owner:
        preparation = owner.enter_context(socket.socket(fileno=args.gate_preparation_fd))
        if args.gate_assignment:
            assignment = read_assignment(args.gate_assignment)
            if Path(args.assignment_file) != folder / "assignment.json":
                raise ValueError("the Mac gate assignment file is outside its request")
            registered = owner.enter_context(register_mac_run(args, assignment))
            args.registered_run = registered
            run = registered
        elif args.assignment_file:
            raise ValueError("an assigned Mac gate needs admission service ownership")
        else:
            args.manual_run = owner.enter_context(own_manual_gate_plan(args, folder))
            run = args.manual_run
        prepared = False
        run.paths.preparation_lock.touch(mode=0o600, exist_ok=False)
        try:
            root.mkdir(mode=0o700, parents=True)
            preparation.sendall(b"1")
            args.caller.ready(preparation.fileno(), timeout=-1)
            if preparation.recv(1) != b"1":
                raise QueueCancelled("the Mac gate did not finish preparing the request")
            if isinstance(run, RegisteredMacRun) and Path(args.assignment_file).read_text() != args.gate_assignment:
                raise ValueError("the Mac gate changed its assignment")
            prepared = True
            yield
        finally:
            if isinstance(run, ManualMacRun):
                if run.saved.get("lifecycle_started") is False:
                    with contextlib.suppress(BlockingIOError), gate_preparation_lock(run.paths.preparation_lock):
                        remove_gate_plan(run.paths)
            elif not prepared:
                settle_unprepared_request(run)


def main(argv: list[str] | None = None) -> int:
    words = sys.argv[1:] if argv is None else argv
    if len(words) == 2 and words[0] in ("remove-run", "write-run"):
        try:
            paths = assigned_gate_paths(words[1])
            if words[0] == "write-run":
                write_text_atomic(paths.journal, sys.stdin.read(), new_file_mode=0o600)
            else:
                remove_gate_plan(paths)
        except (OSError, ValueError) as error:
            print(f"mac queue: recovery remains unresolved: {error}", file=sys.stderr)
            return 1
        return 0
    if len(words) in (3, 4) and words[0] == "watch-caller":
        cancel = int(words[3]) if len(words) == 4 else -1
        return watch_caller(int(words[1]), int(words[2]), cancel)
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    _ = parser.add_argument("--what", required=True)
    _ = parser.add_argument("--kind", choices=("shot", "install", "probe", "run"), required=True)
    _ = parser.add_argument("--package", required=True)
    _ = parser.add_argument("--worktree", type=Path, default=Path.cwd())
    _ = parser.add_argument("--cwd", default="")
    _ = parser.add_argument("--lifecycle-script", default="")
    _ = parser.add_argument("--assignment-file", default="", help=argparse.SUPPRESS)
    _ = parser.add_argument("--gate-assignment", default="", help=argparse.SUPPRESS)
    _ = parser.add_argument("--caller-fd", type=int, help=argparse.SUPPRESS)
    _ = parser.add_argument("--cancel-fd", type=int, help=argparse.SUPPRESS)
    _ = parser.add_argument("--owned-close", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--owns-gate-request", action="store_true", help=argparse.SUPPRESS)
    _ = parser.add_argument("--gate-preparation-fd", type=int, help=argparse.SUPPRESS)
    _ = parser.add_argument("--cancel-seconds", type=float, help=argparse.SUPPRESS)
    _ = parser.add_argument("--no-wait", action="store_true", help="exit with status 78 when the Mac is busy")
    _ = parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv, namespace=Arguments())
    if not args.argv or args.argv == ["--"]:
        parser.error("an argv is required")
    if args.caller_fd < 0 and not args.owns_gate_request:
        return run(args)
    from scripts.build_queue.admission_wire import AdmissionUnavailable

    try:
        with contextlib.ExitStack() as lifetime:
            args.caller = lifetime.enter_context(caller_watch(args.caller_fd, args.cancel_fd, args.cancel_seconds))
            if args.owns_gate_request:
                lifetime.enter_context(own_gate_request(args))
            return run(args)
    except (AdmissionUnavailable, OSError, ValueError, QueueCancelled, subprocess.SubprocessError) as error:
        print(f"mac queue: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
